"""
Score pathways against what a reviewer has already said about their courses.

A regression suite for pathway selection that needs no review round: any change can be scored in
seconds against the committed fixture ``fixtures/review_judgements/rounds_1_2.json``, which
``build_review_judgements`` derives from the Pathway Review Bench's votes. Offline throughout: it
reads files and issues no calls.

Why course judgements and not verdicts
--------------------------------------
Across two review rounds the reviewer rated 167 pathways, and re-rating the same pathway he agreed
with himself 68% of the time (28 of 41 in round 2). A single verdict is too noisy to separate two
selection rules that differ by a few courses. His judgements of *courses* are far more numerous and
far more stable, and they are the raw material a selection rule is trying to get right.

The three sets
--------------
Per career and level, from every round in the fixture together:

* **endorsed** -- courses he marked acceptable (a suggestion on a kept course, or an also-fine pick
  beside a replacement), the best pick he chose when replacing something (or the legacy bench's
  bare-key pick), and every course he kept in a pathway he rated *good*.
* **rejected** -- courses he dropped. A drop with no pick (``""``) or with "nothing here would work"
  (``"__none__"``) rejects the dropped course and endorses nothing.
* **contested** -- courses in both. Removed from the other two and never scored: a course he kept
  in one shape and dropped in another is a judgement about the pathway, not the course.

The measures
------------
Per group of pathways:

* **rejected rate** -- share of pathways holding at least one rejected course. The headline: it is
  the mistake he sees at once, and it needs no guess about what he would have said.
* **clean rate** -- share holding no rejected course and at least one endorsed one.
* **endorsed share** -- of the courses whose standing is known, the share endorsed. Courses he never
  ruled on are *unknown*, not wrong, and are left out rather than counted against it, which is why
  the rejected rate leads: a pathway of unseen courses scores no worse here than an endorsed one.

The numbers are identical to the reference script that defined them
(``learner_pathways/shape-review/score_against_acceptable.py`` in the docs repository), which this
module replaces inside the app.

The calibration split
---------------------
The fixture records each career's split by its rank in the reviewed list: the first nine are
``calibration`` and the rest ``held_out``. A model judge that is meant to stand in for the reviewer
is tuned against the calibration careers only; the held-out careers are then the test of whether it
agrees with him where nobody tuned it to. Selection rules are scored on both, but a rule tuned until
these numbers rise has been fitted to these careers -- it shows a change does what it claims on known
ground, and confirmation needs careers he has not seen.
"""
import collections
import fnmatch
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from enterprise_access.apps.pathway_eval.shape_review import tier_for
from enterprise_access.apps.pathways.pathway_assembly import LEVEL_ORDER
from enterprise_access.apps.pathways.pathway_variants import DEFAULT_PATHWAY_LABEL

FIXTURE_DIR = Path(__file__).resolve().parent / 'fixtures' / 'review_judgements'
DEFAULT_FIXTURE = FIXTURE_DIR / 'rounds_1_2.json'
FIXTURE_SCHEMA = 1

ENDORSED, REJECTED, CONTESTED, UNKNOWN = 'endorsed', 'rejected', 'contested', 'unknown'
SPLIT_CALIBRATION, SPLIT_HELD_OUT = 'calibration', 'held_out'
SPLITS = (SPLIT_CALIBRATION, SPLIT_HELD_OUT)
#: Careers ranked 1..9 in ``careers.txt`` are the calibration set; the rest are held out.
CALIBRATION_CAREERS = 9
DEFAULT_REVIEWER_LABEL = 'reviewer-1'

CAREERS_FILE = 'careers.txt'
BLIND_KEY_FILE = 'blind_key.json'

#: Round-1 bench items are ``S{rank:02d}-{suffix}``; the rank is the career's line in careers.txt.
ROUND1_SUFFIXES = ('1-intro', '2-inter', '3-ladder', '4-other')
_ROUND_PATTERN = re.compile(r'^R(\d+)-')

# What a dropped step's replacement can say. See ``review_feedback`` for the bench's history.
NOTHING_WOULD_WORK = '__none__'
VERDICT_GOOD = 'good'

# Why a course is in a set, recorded as provenance.
VIA_SUGGESTION = 'suggestion'
VIA_ALSO = 'also_fine'
VIA_BEST = 'best_pick'
VIA_KEPT_GOOD = 'kept_in_good'
VIA_DROPPED = 'dropped'


def round_of(item_id: str) -> int:
    """The review round a bench item belongs to: ``R2-..`` is round 2, ``S..`` round 1."""
    match = _ROUND_PATTERN.match(item_id or '')
    return int(match.group(1)) if match else 1


def sha256_bytes(data: bytes) -> str:
    """Hex digest."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------------------------
# Building the fixture
# ---------------------------------------------------------------------------------------------

def load_bench_votes(db_path, reviewer: str) -> list[dict]:
    """
    Every vote ``reviewer`` cast on the bench, with the courses of the item it was about.

    The database is opened read-only. What comes back carries no reviewer identity and no notes:
    the item id, the verdict, the dropped steps, the replacements and suggestions, and each
    course's step, level and key. Sorted by item id so an export is stable.
    """
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = con.execute(
            """select i.item_id, i.payload, v.verdict, v.dropped_steps, v.replacements, v.suggestions
               from pathway_review_pathwayreviewvote v
               join pathway_review_pathwayreviewitem i on i.id = v.item_id
               join core_user u on u.id = v.reviewer_id
               where u.username = ?""",
            (reviewer,),
        ).fetchall()
    finally:
        con.close()
    votes = []
    for item_id, payload, verdict, drops, replacements, suggestions in rows:
        payload = json.loads(payload or '{}')
        votes.append({
            'item_id': item_id,
            'verdict': verdict,
            'dropped_steps': json.loads(drops or '[]'),
            'replacements': json.loads(replacements or '{}'),
            'suggestions': json.loads(suggestions or '{}'),
            'courses': [
                {'step': course.get('step'), 'level': course.get('level'), 'key': course.get('key')}
                for course in payload.get('courses') or []
            ],
        })
    return sorted(votes, key=lambda vote: vote['item_id'])


def votes_export_bytes(votes) -> bytes:
    """The canonical serialisation of a votes export, which its sha256 is taken over."""
    return (json.dumps(votes, indent=1, sort_keys=True) + '\n').encode('utf-8')


def read_careers(path) -> list[str]:
    """The reviewed careers in rank order: ``careers.txt`` without blank lines or ``#`` comments."""
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith('#')]


def item_careers(careers_in_order, blind_key_items) -> dict:
    """
    Bench item id -> career, for both rounds.

    Round 2's items come from the blind key; round 1's are ``S{rank:02d}-{suffix}`` over the ranked
    careers. The blind key wins where both name an item.
    """
    careers = {item_id: info['career'] for item_id, info in (blind_key_items or {}).items() if info.get('career')}
    for rank, career in enumerate(careers_in_order, 1):
        for suffix in ROUND1_SUFFIXES:
            careers.setdefault(f'S{rank:02d}-{suffix}', career)
    return careers


def annotate_careers(votes, careers) -> list[dict]:
    """
    The votes export as committed: each vote with the career its item resolved to.

    The career is ``None`` when the item resolved to none. Carrying it means the export alone says
    which career every judgement belongs to.
    """
    return [{**vote, 'career': careers.get(vote['item_id'])} for vote in votes]


def _slot_levels(vote) -> dict:
    return {str(course.get('step')): course.get('level') for course in vote['courses']}


def judgements_with_provenance(votes, careers) -> tuple[dict, dict]:
    """
    ``(endorsed, rejected)`` before contested courses are taken out.

    Each maps ``(career, level) -> {key: {(item_id, via), ...}}``. A vote whose item has no
    career is skipped; see the module docstring for what puts a course in each set.
    """
    endorsed = collections.defaultdict(lambda: collections.defaultdict(set))
    rejected = collections.defaultdict(lambda: collections.defaultdict(set))
    for vote in votes:
        item_id = vote['item_id']
        career = careers.get(item_id)
        if not career:
            continue
        levels = _slot_levels(vote)
        dropped = {str(step) for step in vote.get('dropped_steps') or []}

        for step, keys in (vote.get('suggestions') or {}).items():
            for key in keys if isinstance(keys, list) else []:
                endorsed[(career, levels.get(str(step)))][key].add((item_id, VIA_SUGGESTION))
        for step, replacement in (vote.get('replacements') or {}).items():
            level = levels.get(str(step))
            if isinstance(replacement, dict):
                picks = [(replacement.get('best'), VIA_BEST)]
                picks += [(key, VIA_ALSO) for key in replacement.get('also') or []]
                for key, via in picks:
                    if key:
                        endorsed[(career, level)][key].add((item_id, via))
            elif isinstance(replacement, str) and replacement not in ('', NOTHING_WOULD_WORK):
                endorsed[(career, level)][replacement].add((item_id, VIA_BEST))   # the legacy bench
        for course in vote['courses']:
            slot = (career, course.get('level'))
            if str(course.get('step')) in dropped:
                rejected[slot][course.get('key')].add((item_id, VIA_DROPPED))
            elif vote.get('verdict') == VERDICT_GOOD:
                endorsed[slot][course.get('key')].add((item_id, VIA_KEPT_GOOD))
    return endorsed, rejected


def split_sets(endorsed, rejected) -> tuple[dict, dict, dict]:
    """``(endorsed, rejected, contested)`` as sets of keys per slot, contested taken out of both."""
    endorsed_sets = {slot: set(keys) for slot, keys in endorsed.items()}
    rejected_sets = {slot: set(keys) for slot, keys in rejected.items()}
    contested = {}
    for slot in set(endorsed_sets) | set(rejected_sets):
        both = endorsed_sets.get(slot, set()) & rejected_sets.get(slot, set())
        if both:
            contested[slot] = both
            endorsed_sets[slot] -= both
            rejected_sets[slot] -= both
    return endorsed_sets, rejected_sets, contested


def _entry(key, sources) -> dict:
    items = sorted({item_id for item_id, _ in sources})
    return {
        'key': key,
        'rounds': sorted({round_of(item_id) for item_id in items}),
        'items': items,
        'via': sorted({via for _, via in sources}),
    }


def build_fixture(*, votes, careers_in_order, blind_key_items, sources=None,
                  reviewer_label: str = DEFAULT_REVIEWER_LABEL,
                  calibration_careers: int = CALIBRATION_CAREERS) -> dict:
    """
    The committed fixture: per career and level, the endorsed, rejected and contested courses.

    Args:
        votes: The reviewer's votes (``load_bench_votes``), already free of reviewer identity.
            ``built_from.votes_export_sha256`` is taken over ``annotate_careers`` of them, which
            is what ``build_review_judgements`` writes beside the fixture.
        careers_in_order: The reviewed careers in rank order (``read_careers``).
        blind_key_items: The round-2 blind key's ``items``.
        sources: ``{file name: sha256}`` of the files the careers came from, for ``built_from``.
        reviewer_label: The anonymous name the judgements are recorded under.
        calibration_careers: How many of the top-ranked careers form the calibration split.
    """
    careers = item_careers(careers_in_order, blind_key_items)
    votes = annotate_careers(votes, careers)
    endorsed, rejected = judgements_with_provenance(votes, careers)
    endorsed_sets, rejected_sets, contested = split_sets(endorsed, rejected)

    # A suggestion on a step the item does not hold has no level, so it can never match a course.
    unplaced = sorted({(career, key) for found in (endorsed, rejected)
                       for (career, level), keys in found.items() if level is None for key in keys})
    by_career = {}
    for rank, career in enumerate(careers_in_order, 1):
        by_career[career] = {
            'rank': rank,
            'split': SPLIT_CALIBRATION if rank <= calibration_careers else SPLIT_HELD_OUT,
            'levels': {},
        }
    slots = sorted({slot for slot in set(endorsed) | set(rejected) if slot[1] is not None},
                   key=lambda slot: (slot[0], LEVEL_ORDER.index(slot[1]) if slot[1] in LEVEL_ORDER else 99, slot[1]))
    for career, level in slots:
        record = by_career.setdefault(career, {'rank': None, 'split': SPLIT_HELD_OUT, 'levels': {}})
        slot = (career, level)
        record['levels'][level] = {
            ENDORSED: [_entry(key, endorsed[slot][key]) for key in sorted(endorsed_sets.get(slot, ()))],
            REJECTED: [_entry(key, rejected[slot][key]) for key in sorted(rejected_sets.get(slot, ()))],
            CONTESTED: [
                {
                    'key': key,
                    'rounds': sorted({round_of(i) for i, _ in endorsed[slot][key] | rejected[slot][key]}),
                    'endorsed_in': sorted({i for i, _ in endorsed[slot][key]}),
                    'rejected_in': sorted({i for i, _ in rejected[slot][key]}),
                }
                for key in sorted(contested.get(slot, ()))
            ],
        }

    used = [vote for vote in votes if careers.get(vote['item_id'])]
    counts = {
        'votes': len(votes),
        'votes_with_a_career': len(used),
        'votes_by_round': dict(sorted(collections.Counter(str(round_of(v['item_id'])) for v in used).items())),
        'careers': len(by_career),
        'careers_with_judgements': sum(1 for record in by_career.values() if record['levels']),
        'slots': len(slots),
        ENDORSED: sum(len(keys) for slot, keys in endorsed_sets.items() if slot[1] is not None),
        REJECTED: sum(len(keys) for slot, keys in rejected_sets.items() if slot[1] is not None),
        CONTESTED: sum(len(keys) for slot, keys in contested.items() if slot[1] is not None),
        'unplaced_judgements': len(unplaced),
    }
    for split in SPLITS:
        members = [record for record in by_career.values() if record['split'] == split]
        counts[split] = {
            'careers': len(members),
            'slots': sum(len(record['levels']) for record in members),
            **{
                name: sum(len(level[name]) for record in members for level in record['levels'].values())
                for name in (ENDORSED, REJECTED, CONTESTED)
            },
        }
    return {
        'schema': FIXTURE_SCHEMA,
        'reviewer': reviewer_label,
        'split_rule': (
            f'careers ranked 1-{calibration_careers} in {CAREERS_FILE} are {SPLIT_CALIBRATION}; '
            f'the rest are {SPLIT_HELD_OUT}'
        ),
        'built_from': {
            'sources': dict(sorted((sources or {}).items())),
            'votes_export_sha256': sha256_bytes(votes_export_bytes(votes)),
            'counts': counts,
        },
        'careers': by_career,
    }


# ---------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewJudgements:
    """The three sets, keyed by ``(career, level)``, and each career's split."""

    endorsed: dict
    rejected: dict
    contested: dict
    splits: dict
    reviewer: str = DEFAULT_REVIEWER_LABEL

    @classmethod
    def from_fixture(cls, fixture: dict) -> 'ReviewJudgements':
        """Read a fixture written by ``build_fixture``."""
        if fixture.get('schema') != FIXTURE_SCHEMA:
            raise ValueError(f'Unknown review-judgements schema {fixture.get("schema")!r}.')
        sets = {ENDORSED: {}, REJECTED: {}, CONTESTED: {}}
        splits = {}
        for career, record in fixture['careers'].items():
            splits[career] = record['split']
            for level, slot in record['levels'].items():
                for name, found in sets.items():
                    keys = frozenset(entry['key'] for entry in slot.get(name) or ())
                    if keys:
                        found[(career, level)] = keys
        return cls(endorsed=sets[ENDORSED], rejected=sets[REJECTED], contested=sets[CONTESTED],
                   splits=splits, reviewer=fixture.get('reviewer', DEFAULT_REVIEWER_LABEL))

    @classmethod
    def load(cls, path=DEFAULT_FIXTURE) -> 'ReviewJudgements':
        """Load a fixture from disk; the committed one by default."""
        return cls.from_fixture(json.loads(Path(path).read_text(encoding='utf-8')))

    def standing(self, career: str, level: str, key: str) -> str:
        """``endorsed``, ``rejected``, ``contested`` or ``unknown`` for one course in one slot."""
        slot = (career, level)
        for name, found in ((ENDORSED, self.endorsed), (REJECTED, self.rejected), (CONTESTED, self.contested)):
            if key in found.get(slot, ()):
                return name
        return UNKNOWN

    def split_of(self, career: str) -> str:
        """The career's split, or ``''`` for a career the reviewer never saw."""
        return self.splits.get(career, '')


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def score_pathways(pathways, judgements: ReviewJudgements) -> dict:
    """
    The measures over a group of pathways, each a list of ``(career, level, key)``.

    An empty pathway is not counted. ``pathways``, ``holding_a_rejected_course``,
    ``rejected_rate``, ``clean_pathways``, ``clean_rate``, ``courses_with_a_known_standing`` and
    ``endorsed_share`` are exactly the reference script's; the course counts beside them say where
    the rest of the courses went.
    """
    n = holding_rejected = clean = 0
    standings = collections.Counter()
    for courses in pathways:
        if not courses:
            continue
        n += 1
        found = [judgements.standing(career, level, key) for career, level, key in courses]
        has_rejected, has_endorsed = REJECTED in found, ENDORSED in found
        holding_rejected += int(has_rejected)
        clean += int(not has_rejected and has_endorsed)
        standings.update(found)
    known = standings[ENDORSED] + standings[REJECTED]
    return {
        'pathways': n,
        'holding_a_rejected_course': holding_rejected,
        'rejected_rate': _rate(holding_rejected, n),
        'clean_pathways': clean,
        'clean_rate': _rate(clean, n),
        'courses_with_a_known_standing': known,
        'endorsed_share': _rate(standings[ENDORSED], known),
        'courses': sum(standings.values()),
        'endorsed_courses': standings[ENDORSED],
        'rejected_courses': standings[REJECTED],
        'contested_courses': standings[CONTESTED],
        'unknown_courses': standings[UNKNOWN],
    }


# ---------------------------------------------------------------------------------------------
# Reading pathways out of exported runs
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoredPathway:
    """One pathway to score: where it came from, and its courses as ``(career, level, key)``."""

    career: str
    label: str
    tier: str
    courses: tuple


def load_runs(path) -> list[dict]:
    """
    ``CareerRun`` dicts from a collection's JSON export, a collection checkpoint, or a replay.

    The export is one JSON document with ``runs``; the checkpoints are JSON lines. Every line is
    read -- a replay's runs have no delivered pathway, so ``load_checkpoint``'s finality test would
    drop them all -- and where a career appears more than once the later line wins, as a resumed
    collection's does. A line that does not parse is skipped.
    """
    text = Path(path).read_text(encoding='utf-8')
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict) and 'runs' in document:
        return list(document['runs'])
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        lines = [document]
    else:
        lines = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                lines.append(json.loads(line))
            except ValueError:
                continue
    latest = {}
    for run in lines:
        if isinstance(run, dict):
            latest[run.get('requested_name') or run.get('career_name') or len(latest)] = run
    return list(latest.values())


def _tier_of(courses) -> str:
    mix = collections.Counter(course.get('level_type') for course in courses)
    return tier_for({level: mix.get(level, 0) for level in LEVEL_ORDER}, len(courses))


def _matches(label: str, patterns) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(label, pattern) for pattern in patterns)


def pathways_from_runs(runs, *, labels=(), tiers=(), complete_only: bool = True) -> list[ScoredPathway]:
    """
    The pathways in a set of runs that match the chosen labels and shape tiers.

    Args:
        labels: Variant labels to keep, as shell-style patterns (``shape_pick_v2:*``). The
            delivered pathway is labelled ``default``. Empty keeps every label.
        tiers: Shape tiers to keep (``intro``, ``intermediate``, ``ladder``, ``other``), by the
            levels a pathway's courses landed on (``shape_review.tier_for``). Empty keeps all.
        complete_only: Keep only pathways that met their size or shape, as the reference does.
    """
    out = []
    for run in runs:
        career = run.get('career_name') or run.get('requested_name') or ''
        candidates = []
        if run.get('pathway'):
            candidates.append((DEFAULT_PATHWAY_LABEL, run['pathway']))
        candidates += [(variant.get('label', ''), variant) for variant in run.get('variants') or []]
        for label, pathway in candidates:
            if not _matches(label, labels) or (complete_only and not pathway.get('complete')):
                continue
            courses = pathway.get('courses') or []
            tier = _tier_of(courses)
            if tiers and tier not in tiers:
                continue
            out.append(ScoredPathway(
                career=career, label=label, tier=tier,
                courses=tuple((career, course.get('level_type'), course.get('key')) for course in courses),
            ))
    return out


def pathways_from_blind_queue(queue: dict, key_items: dict, arm: str) -> list[ScoredPathway]:
    """One arm's pathways out of a blind review queue (``queue-round2.json`` and its key)."""
    by_id = {ladder['id']: ladder for ladder in queue.get('ladders') or []}
    out = []
    for item_id, info in key_items.items():
        item = by_id.get(item_id)
        if info.get('arm') != arm or not item:
            continue
        career = info['career']
        out.append(ScoredPathway(
            career=career, label=arm, tier=info.get('tier', ''),
            courses=tuple((career, course['level'], course['key']) for course in item['courses']),
        ))
    return out


GROUP_BY_LABEL, GROUP_BY_TIER, GROUP_BY_NONE = 'label', 'tier', 'none'
GROUP_BY = (GROUP_BY_LABEL, GROUP_BY_TIER, GROUP_BY_NONE)
ALL = 'all'


def score_groups(pathways, judgements: ReviewJudgements, *, group_by: str = GROUP_BY_LABEL,
                 by_split: bool = False) -> list[dict]:
    """
    One row of measures per group, and per split when asked, in a stable order.

    Each row is ``score_pathways``'s dict plus ``group``, ``split`` (``all`` when not split) and
    ``careers_unseen``: how many of its pathways belong to a career the fixture has nothing on.
    """
    def group_of(pathway):
        if group_by == GROUP_BY_LABEL:
            return pathway.label
        if group_by == GROUP_BY_TIER:
            return pathway.tier or '(none)'
        return ALL

    grouped = collections.defaultdict(list)
    for pathway in pathways:
        group = group_of(pathway)
        grouped[(group, ALL)].append(pathway)
        if by_split:
            grouped[(group, judgements.split_of(pathway.career) or 'unseen')].append(pathway)
    split_order = {name: index for index, name in enumerate((ALL,) + SPLITS + ('unseen',))}
    rows = []
    for group, split in sorted(grouped, key=lambda pair: (pair[0], split_order.get(pair[1], 99))):
        members = grouped[(group, split)]
        rows.append({
            'group': group,
            'split': split,
            'careers_unseen': sum(1 for p in members if not judgements.split_of(p.career)),
            **score_pathways([list(p.courses) for p in members], judgements),
        })
    return rows
