"""
Batch collection of pathway size variants: careers in, every strategy's pathways out.

The persona harness answers "what does a learner get, from intake onward?" for ten
personas. This answers a narrower question for as many careers as you like: for a given
career, what does each pathway strategy build at each size, and how does the judge score
it? It skips intake and career discovery -- each career is looked up by name in the jobs
index for its skills, then run straight through ``PathwayAssemblyWorkflow`` with the
requested experiments switched on.

Like the harness it produces traces, not conclusions, and every run stays inspectable as a
workflow record. Cost is bounded the same way: ``max_calls`` is checked before each career
against an upper bound of the model calls that career can issue, and ``dry_run`` issues
none.
"""
import csv
import json
import logging
from dataclasses import dataclass, field, fields

from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchClient
from enterprise_access.apps.pathways import api as pathways_api
from enterprise_access.apps.pathways.models import AssemblePathwayOutput, PathwayAssemblyWorkflow
from enterprise_access.apps.pathways.pathway_assembly import LEVEL_ORDER
from enterprise_access.apps.pathways.pathway_variants import (
    DEFAULT_PATHWAY_LABEL,
    estimated_model_calls,
    resolve_variant_request
)
from enterprise_access.apps.workflow.exceptions import UnitOfWorkException

logger = logging.getLogger(__name__)

# Skips that would recur identically on a re-run, so a resumed collection keeps them rather
# than asking again. Errors and budget skips are NOT here: those are worth retrying.
NO_CAREER = 'no career with this exact name in the jobs index'
NO_SKILLS = 'career carries no skills'
FINAL_SKIPS = (NO_CAREER, NO_SKILLS)

# Careers are looked up by FILTERING on the ``name`` facet, not by text search. A text
# search for "Data Analyst" ranks "Reference Data Analyst", "Data Analyst Consultant" and
# dozens more above the bare title, which is absent from the first twenty hits (measured
# 2026-09-25) -- the same trap career discovery hit with its ten-hit window. A facet filter
# matches the exact name, case-insensitively, whatever its rank. A handful of hits covers
# the rare title held by more than one record.
CAREER_NAME_FACET = 'name'
CAREER_LOOKUP_HITS = 5

# Column names read from a careers CSV, in order of preference. Covers a plain list and the
# exports of the "careers we serve best" workbook (its Careers and Families tabs).
CAREER_NAME_COLUMNS = ('career_name', 'career', 'name', 'label', 'career family')


def load_career_names(path) -> list[str]:
    """
    Read career names from a text file (one per line, ``#`` comments) or a CSV.

    A file whose first line names a recognised column is read as CSV; anything else is one
    name per line. Blank and duplicate names are dropped, first occurrence kept.
    """
    with open(path, newline='', encoding='utf-8') as handle:
        lines = handle.read().splitlines()
    if not lines:
        return []

    header = [column.strip().lower() for column in next(csv.reader([lines[0]]))]
    column = next((name for name in CAREER_NAME_COLUMNS if name in header), None)
    if column is not None:
        index = header.index(column)
        rows = csv.reader(lines[1:])
        names = [row[index].strip() for row in rows if len(row) > index]
    else:
        names = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith('#')]
    return list(dict.fromkeys(name for name in names if name))


def lookup_career(name: str) -> dict | None:
    """
    Find a career by exact name (case-insensitive) in the English jobs index.

    Returns a ``career_candidate_from_hit`` dict, preferring a match that carries skills,
    or ``None`` when no record has that exact name. Exact rather than best match: a nearest
    neighbour would quietly collect variants for a different career than the one asked
    about. The name is re-checked on the way out, so a facet quirk cannot widen the match.
    """
    quoted = '"' + name.strip().replace('"', '\\"') + '"'
    response = AlgoliaSearchClient().search_jobs_index(
        '',
        hitsPerPage=CAREER_LOOKUP_HITS,
        attributesToRetrieve=pathways_api.CAREER_ATTRIBUTES,
        filters=(
            f'{pathways_api.METADATA_LANGUAGE_FACET}:{pathways_api.SUPPORTED_METADATA_LANGUAGE} '
            f'AND {CAREER_NAME_FACET}:{quoted}'
        ),
    )
    wanted = name.strip().lower()
    matches = [
        candidate for candidate in (
            pathways_api.career_candidate_from_hit(hit)
            for hit in response.get('hits') or [] if isinstance(hit, dict)
        )
        if candidate and candidate['name'].strip().lower() == wanted
    ]
    with_skills = [candidate for candidate in matches if candidate['skills']]
    return (with_skills or matches or [None])[0]


@dataclass
class CareerRun:
    """One career through the pathway workflow, with every requested experiment."""

    requested_name: str
    career_name: str = ''
    external_id: str = ''
    skill_count: int = 0
    workflow_uuid: str = ''
    pathway: dict | None = None
    judgement: dict | None = None
    variants: list = field(default_factory=list)
    skipped_reason: str = ''
    error: str = ''

    def to_dict(self) -> dict:
        """Plain dict for JSON export."""
        return {
            'requested_name': self.requested_name,
            'career_name': self.career_name,
            'external_id': self.external_id,
            'skill_count': self.skill_count,
            'workflow_uuid': str(self.workflow_uuid or ''),
            'pathway': self.pathway,
            'judgement': self.judgement,
            'variants': list(self.variants),
            'skipped_reason': self.skipped_reason,
            'error': self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'CareerRun':
        """Rebuild a run from ``to_dict`` output, ignoring keys it does not know."""
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def is_final(self) -> bool:
        """
        Whether a resumed collection should keep this run rather than redo it.

        True for a career whose workflow ran, or whose skip would recur identically. False
        for errors and budget or dry-run skips, which a re-run might well get past.
        """
        if self.error:
            return False
        if self.skipped_reason:
            return self.skipped_reason in FINAL_SKIPS
        return self.pathway is not None

    def pathway_rows(self) -> list[dict]:
        """
        One flat row per pathway -- the delivered one, then each variant -- for CSV.

        The delivered pathway is included so every arm sits next to the baseline it is an
        alternative to.
        """
        rows = []
        if self.pathway is not None:
            rows.append(self._row(
                label=DEFAULT_PATHWAY_LABEL, strategy=DEFAULT_PATHWAY_LABEL, requested_size=5,
                pathway=self.pathway, judgement=self.judgement,
            ))
        for variant in self.variants:
            rows.append(self._row(
                label=variant.get('label', ''), strategy=variant.get('strategy', ''),
                requested_size=variant.get('requested_size'), pathway=variant,
                judgement=variant.get('judgement'),
            ))
        return rows

    def _row(self, *, label, strategy, requested_size, pathway, judgement) -> dict:
        courses = pathway.get('courses') or []
        mix = pathway.get('level_mix') or {}
        judgement = judgement or {}
        return {
            'career': self.career_name or self.requested_name,
            'external_id': self.external_id,
            'workflow_uuid': str(self.workflow_uuid or ''),
            'label': label,
            'strategy': strategy,
            'requested_size': '' if requested_size is None else requested_size,
            'size': len(courses),
            'complete': bool(pathway.get('complete')),
            'level_mix': '/'.join(str(mix.get(level, 0)) for level in LEVEL_ORDER),
            'verdict': judgement.get('verdict', ''),
            'n_on_topic': judgement.get('n_on_topic', '') if judgement else '',
            'judge_reason': judgement.get('reason', ''),
            'same_as': judgement.get('same_as', ''),
            'course_keys': ' | '.join(course.get('key', '') for course in courses),
            'course_titles': ' | '.join(course.get('title', '') for course in courses),
            'violations': '; '.join(pathway.get('violations') or []),
            'error': pathway.get('error', '') or judgement.get('error', ''),
        }


CSV_COLUMNS = (
    'career', 'external_id', 'workflow_uuid', 'label', 'strategy', 'requested_size', 'size',
    'complete', 'level_mix', 'verdict', 'n_on_topic', 'judge_reason', 'same_as',
    'course_keys', 'course_titles', 'violations', 'error',
)


class VariantCollector:
    """
    Runs careers through the pathway workflow with the requested experiments.

    Owns no scoring: it records what ran. ``summarise`` counts outcomes by label; any
    judgement about what they mean belongs to whoever reads the export.
    """

    def __init__(self, *, variant_sizes=(), variant_strategies=(), judge_enabled: bool = False,
                 rerank_enabled: bool = True, enrich_enabled: bool = False,
                 customer_uuid: str = '', allow_unscoped: bool = False,
                 max_calls: int | None = None, dry_run: bool = False, lookup=None):
        self.sizes, self.strategies = resolve_variant_request(variant_sizes, variant_strategies)
        self.judge_enabled = judge_enabled
        self.rerank_enabled = rerank_enabled
        self.enrich_enabled = enrich_enabled
        self.customer_uuid = customer_uuid
        self.allow_unscoped = allow_unscoped
        self.max_calls = max_calls
        self.dry_run = dry_run
        # Resolved here rather than as a default argument, so the module-level function
        # is read at construction time and can be substituted.
        self.lookup = lookup or lookup_career

    @property
    def calls_per_career(self) -> int:
        """
        Upper bound on the paid model calls one career can issue.

        The re-rank and each model arm and judgement; rationale enrichment when enabled. A
        career with no candidates issues none of them, which is why this is a bound.
        """
        calls = estimated_model_calls(
            sizes=self.sizes, strategies=self.strategies, judge_enabled=self.judge_enabled,
        )
        return calls + int(self.rerank_enabled) + int(self.enrich_enabled)

    def run(self, names, *, done=None, on_run=None) -> dict:
        """
        Collect every career in ``names``.

        Args:
            done: Runs from an earlier, interrupted collection, keyed by requested name.
                A career found here is carried over as-is -- not re-run, not re-charged.
            on_run: Called with each run this invocation completes, as soon as it
                completes, so a checkpoint survives a crash or a hang mid-batch.

        Returns ``runs``, ``calls_charged``, ``budget_exhausted`` and ``resumed``. The
        budget is checked before a career's workflow starts, against the upper bound of
        what it can cost, and charged only for careers that reach the workflow -- the lookup
        is a free search, so a career skipped there costs nothing. A career that fails is
        recorded and the batch continues.
        """
        done = done or {}
        runs, charged, exhausted, resumed = [], 0, False, 0
        for name in names:
            if name in done:
                runs.append(done[name])
                resumed += 1
                continue
            run = CareerRun(requested_name=name)
            runs.append(run)
            if self.dry_run:
                run.skipped_reason = 'dry run'
                continue
            if self.max_calls is not None and charged + self.calls_per_career > self.max_calls:
                exhausted = True
                run.skipped_reason = 'max calls reached'
                continue
            career = self.resolve(run)
            if career is not None:
                charged += self.calls_per_career
                self.execute(run, career)
            if on_run is not None:
                on_run(run)
        return {'runs': runs, 'calls_charged': charged, 'budget_exhausted': exhausted,
                'resumed': resumed}

    def resolve(self, run: CareerRun) -> dict | None:
        """Look the career up, or record on ``run`` why it cannot run and return ``None``."""
        try:
            career = self.lookup(run.requested_name)
        except Exception as exc:  # pylint: disable=broad-except
            # Any lookup failure costs this career, never the batch.
            run.error = f'career lookup failed ({type(exc).__name__}): {exc}'
            return None
        if career is None:
            run.skipped_reason = NO_CAREER
            return None
        if not career.get('skills'):
            # The pathway endpoint refuses a skill-less career for the same reason: a
            # pathway built from no skills is a keyword search.
            run.skipped_reason = NO_SKILLS
            return None
        return career

    def execute(self, run: CareerRun, career: dict) -> None:
        """Run the workflow for a resolved career and record what it produced."""
        run.career_name = career['name']
        run.external_id = career.get('external_id', '')
        run.skill_count = len(career['skills'])
        workflow = PathwayAssemblyWorkflow.objects.create(
            input_data=PathwayAssemblyWorkflow.generate_input_dict(
                career_name=career['name'],
                career_skills=career['skills'],
                customer_uuid=self.customer_uuid,
                allow_unscoped=self.allow_unscoped,
                rerank_enabled=self.rerank_enabled,
                enrich_enabled=self.enrich_enabled,
                variant_sizes=self.sizes,
                variant_strategies=self.strategies,
                judge_enabled=self.judge_enabled,
            ),
        )
        run.workflow_uuid = workflow.uuid
        try:
            workflow.execute()
        except UnitOfWorkException as exc:
            run.error = f'{type(exc).__name__}: {exc}'
            logger.warning('Variant collection failed for career %r: %s', run.career_name, exc)
            return

        # The assembly output rather than ``workflow.pathway()``: an incomplete default
        # pathway is still a data point here, where the endpoint would report none.
        run.pathway = (workflow.output_data or {}).get(AssemblePathwayOutput.KEY)
        run.judgement = workflow.default_judgement()
        run.variants = workflow.variants()


def append_checkpoint(path, run: CareerRun) -> None:
    """
    Append one run to a JSON-lines checkpoint and flush it to disk.

    One line per run, written the moment the run finishes: a collection killed or hung
    mid-batch loses at most the career in flight, never the ones already paid for.
    """
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(run.to_dict(), sort_keys=True) + '\n')
        handle.flush()


def load_checkpoint(path) -> dict:
    """
    The final runs recorded in a checkpoint, keyed by requested name.

    Later lines win. A line that does not parse -- the tail of a write the process died
    during -- is skipped with a warning rather than failing the resume.
    """
    done = {}
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return done
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            run = CareerRun.from_dict(json.loads(line))
        except (ValueError, TypeError):
            logger.warning('Skipping unreadable checkpoint line %d in %s.', number, path)
            continue
        if run.is_final:
            done[run.requested_name] = run
    return done


def summarise(runs) -> list[dict]:
    """
    Outcome counts per pathway label, delivered pathway first.

    ``judged`` counts real verdicts; a label that was never judged, or whose judgement
    failed, shows in ``unjudged`` rather than being folded into a verdict.
    """
    by_label: dict = {}
    for run in runs:
        for row in run.pathway_rows():
            stats = by_label.setdefault(row['label'], {
                'label': row['label'], 'pathways': 0, 'complete': 0, 'courses': 0,
                'good': 0, 'weak': 0, 'bad': 0, 'unjudged': 0,
            })
            stats['pathways'] += 1
            stats['complete'] += int(row['complete'])
            stats['courses'] += row['size']
            verdict = row['verdict']
            stats[verdict if verdict in ('good', 'weak', 'bad') else 'unjudged'] += 1

    def order(label):
        return (label != DEFAULT_PATHWAY_LABEL, label.split(':')[0], label)

    return [by_label[label] for label in sorted(by_label, key=order)]


def write_csv(runs, path) -> int:
    """Write one row per pathway to ``path``; returns the row count."""
    rows = [row for run in runs for row in run.pathway_rows()]
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
