"""
The judge's best pathway per shape tier: a shortlist for a human review of shapes.

Product asked (2026-09-28) to see the careers the catalog serves best in four shapes: two
introductory courses, two intermediate courses, a full ladder where the catalog has one, and
one other shape that serves the career well -- each the best the judge could find. The
collection builds the candidates (``collect_pathway_variants`` with the shape arms); this
module picks among them, from the exported runs:

1. Every complete, judged variant -- and the delivered pathway -- is placed in a tier by the
   levels its courses **actually** landed on, never by the shape it was asked for. A model
   pick that drifted, or a model-sized pathway that happens to climb every rung, lands where
   its courses put it.
2. Within a tier the judge's verdict ranks the candidates, then the share of courses it rated
   on topic, then how many it rated on topic. The last is what prefers a five-course ladder
   over a three-course one when both are good and fully on topic, which is "a full ladder if
   possible".

Why here and not in the pipeline
--------------------------------
``judging`` records and never decides, and that stays true: nothing here runs inside
``PathwayAssemblyWorkflow`` or changes what the endpoint delivers. Choosing by verdict happens
after the fact, on exported runs, which is the harness's job.

Choosing by the v2 rubric
-------------------------
``rubric='v2'`` ranks by the second judge rubric's verdicts instead (``judgement_v2`` on each
pathway, collected with ``--judge-rubric v2``). The order is the same, with one step added:
after verdict and on-topic ties, the pathway with fewer flagged courses wins -- a course the
judge called too specific, redundant, mis-levelled or aimed at another role. Every pick
records the rubric that chose it, and the two rubrics' picks are never mixed.

Reading a pick's verdict
------------------------
A pick is the best of several judged candidates, so its verdict is biased upward against any
single arm's -- that is what best-of-N means, and the same rubric chose it. Quote per-arm rates
from the collection summary, never the picks'. The picks are a shortlist for a reviewer, not a
measurement, and the reviewer is the check on them.
"""
import csv
from dataclasses import dataclass

from enterprise_access.apps.pathways.judging import JUDGE_RUBRICS, RUBRIC_V1, RUBRIC_V2, is_flagged
from enterprise_access.apps.pathways.pathway_assembly import LEVEL_ORDER
from enterprise_access.apps.pathways.pathway_variants import (
    DEFAULT_PATHWAY_LABEL,
    MIN_PATHWAY_SIZE,
    STRATEGY_MODEL_SIZED,
    STRATEGY_SHAPE_CUT,
    STRATEGY_SHAPE_PICK,
    STRATEGY_SHAPE_PICK_V2
)


@dataclass(frozen=True)
class ShapeTier:
    """One of the four shapes a career is reviewed in."""

    key: str
    label: str
    rule: str


TIER_INTRO = ShapeTier('intro', 'Introductory, 2 courses', 'exactly two courses, both Introductory')
TIER_INTERMEDIATE = ShapeTier('intermediate', 'Intermediate, 2 courses', 'exactly two courses, both Intermediate')
TIER_LADDER = ShapeTier('ladder', 'Full ladder', 'at least one course on every rung')
TIER_OTHER = ShapeTier(
    'other', 'Another shape',
    'courses on exactly two rungs, or on the Advanced rung alone',
)
SHAPE_TIERS = (TIER_INTRO, TIER_INTERMEDIATE, TIER_LADDER, TIER_OTHER)

# What a review collection asks for. Each shape feeds one tier: 2/0/0 and 0/2/0 are the first
# two, the three ladders compete for the third, and the rest -- on-ramps, bridges, the flat
# five-course mixes and the advanced-led sets -- compete for the fourth with whatever the
# model-sized arm chose. Single-level introductory or intermediate sets of other lengths are
# deliberately absent: a third introductory course is a longer first tier, not another shape.
REVIEW_SHAPES = (
    '2/0/0', '0/2/0',
    '2/2/1', '1/2/1', '1/1/1',
    '2/1/0', '1/2/0', '0/2/1', '3/2/0', '2/3/0', '0/1/2', '0/0/2',
)
REVIEW_STRATEGIES = (STRATEGY_MODEL_SIZED, STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK)

VERDICT_SCORE = {'good': 3, 'weak': 2, 'bad': 1}

# Last-resort tie-break, after verdict and on-topic counts. The delivered pathway first, as
# the incumbent; then a model's choice over a mechanical cut, since the model chose its
# courses as a set for this career and the cut took them one rung at a time.
STRATEGY_PREFERENCE = (
    DEFAULT_PATHWAY_LABEL, STRATEGY_SHAPE_PICK, STRATEGY_SHAPE_PICK_V2, STRATEGY_MODEL_SIZED, STRATEGY_SHAPE_CUT,
)

# Where each rubric's judgement sits on an exported pathway, and on the run for the
# delivered pathway.
JUDGEMENT_FIELDS = {RUBRIC_V1: 'judgement', RUBRIC_V2: 'judgement_v2'}


def _judgement_field(rubric: str) -> str:
    """
    The exported field holding ``rubric``'s judgement.

    Raises:
        ValueError: An unknown rubric.
    """
    if rubric not in JUDGE_RUBRICS:
        raise ValueError(f'Unknown judge rubric {rubric!r}; expected one of {list(JUDGE_RUBRICS)}.')
    return JUDGEMENT_FIELDS[rubric]


def mix_tuple(level_mix: dict) -> tuple:
    """``{'Introductory': 2, ...}`` -> ``(2, 0, 0)``."""
    return tuple(int((level_mix or {}).get(level, 0) or 0) for level in LEVEL_ORDER)


def tier_for(level_mix: dict, n_courses: int) -> str:
    """
    The tier a pathway belongs to by the levels its courses landed on, or ``''`` for none.

    A course without a recognised level cannot be placed, so a pathway holding one is left
    out rather than guessed at. So is a single-level introductory or intermediate set of any
    length but two (see ``REVIEW_SHAPES``).
    """
    counts = mix_tuple(level_mix)
    if n_courses < MIN_PATHWAY_SIZE or sum(counts) != n_courses:
        return ''
    if counts == (2, 0, 0):
        return TIER_INTRO.key
    if counts == (0, 2, 0):
        return TIER_INTERMEDIATE.key
    if all(counts):
        return TIER_LADDER.key
    rungs = sum(1 for count in counts if count)
    if rungs == 2 or counts[:2] == (0, 0):
        return TIER_OTHER.key
    return ''


def _judged_pathways(run: dict, rubric: str = RUBRIC_V1) -> list[dict]:
    """
    Every pathway on a run that could be picked: complete, gate-clean and judged.

    Judged means judged under ``rubric``; each returned pathway carries that judgement as
    ``judgement``, whichever field it was exported in.

    A variant that fell short of the size or shape it was built for is not offered as the
    shape it happens to have reached. Its realised shape is requested in its own right when
    it matters, and a pathway that missed its brief is a finding for the collection, not a
    candidate for review.
    """
    judgement_field = _judgement_field(rubric)
    pathways = []
    default = run.get('pathway')
    if default:
        pathways.append({
            'label': DEFAULT_PATHWAY_LABEL, 'strategy': DEFAULT_PATHWAY_LABEL, 'shape': '',
            **default, 'judgement': run.get(judgement_field),
        })
    pathways.extend(
        {**variant, 'judgement': variant.get(judgement_field)} for variant in run.get('variants') or []
    )

    eligible = []
    for pathway in pathways:
        judgement = pathway.get('judgement') or {}
        courses = pathway.get('courses') or []
        if not pathway.get('complete') or pathway.get('violations') or pathway.get('error'):
            continue
        if judgement.get('error') or judgement.get('verdict') not in VERDICT_SCORE:
            continue
        if len(courses) < MIN_PATHWAY_SIZE:
            continue
        eligible.append(pathway)
    return eligible


def _preference(strategy: str, shape: str) -> tuple:
    """The last-resort tie-break: ``STRATEGY_PREFERENCE``, then ``REVIEW_SHAPES`` order."""
    strategy_rank = (
        STRATEGY_PREFERENCE.index(strategy) if strategy in STRATEGY_PREFERENCE else len(STRATEGY_PREFERENCE)
    )
    shape_rank = REVIEW_SHAPES.index(shape) if shape in REVIEW_SHAPES else len(REVIEW_SHAPES)
    return strategy_rank, shape_rank


def candidates_by_tier(run: dict, rubric: str = RUBRIC_V1) -> dict:
    """
    Judged pathways grouped by tier, identical course lists merged, best first.

    Returns ``{tier_key: [candidate, ...]}``. A candidate carries every label that produced
    its course list, the ``rubric``'s judgement, ``precision`` (on-topic courses / courses)
    and ``n_flagged`` (courses the v2 judge flagged; always 0 under v1, which flags nothing).
    """
    merged: dict = {}
    for pathway in _judged_pathways(run, rubric):
        courses = pathway['courses']
        keys = tuple(course.get('key', '') for course in courses)
        judgement = pathway['judgement']
        if keys in merged:
            merged[keys]['labels'].append(pathway['label'])
            continue
        n_courses = len(courses)
        n_on_topic = int(judgement.get('n_on_topic') or 0)
        flags = judgement.get('flags') or {}
        merged[keys] = {
            'rubric': rubric,
            'labels': [pathway['label']],
            'strategy': pathway.get('strategy', ''),
            'shape': pathway.get('shape', ''),
            'level_mix': '/'.join(str(count) for count in mix_tuple(pathway.get('level_mix'))),
            'tier': tier_for(pathway.get('level_mix'), n_courses),
            'courses': courses,
            'verdict': judgement['verdict'],
            'reason': judgement.get('reason', ''),
            'on_topic': judgement.get('on_topic') or {},
            'n_on_topic': n_on_topic,
            'n_courses': n_courses,
            'precision': round(n_on_topic / n_courses, 4) if n_courses else 0.0,
            'flags': flags,
            'n_flagged': sum(1 for course in courses if is_flagged(flags.get(course.get('key', '')))),
        }

    by_tier: dict = {tier.key: [] for tier in SHAPE_TIERS}
    for candidate in merged.values():
        if candidate['tier']:
            by_tier[candidate['tier']].append(candidate)
    for tier_candidates in by_tier.values():
        # ``n_flagged`` is 0 for every v1 candidate, so it changes no v1 ordering.
        tier_candidates.sort(key=lambda c: (
            -VERDICT_SCORE[c['verdict']], -c['precision'], -c['n_on_topic'], c['n_flagged'],
            _preference(c['strategy'], c['shape']), c['labels'][0],
        ))
    return by_tier


def window_supply(candidates: list[dict]) -> dict:
    """Candidates per rung in the window, for saying why a tier came back empty."""
    supply = {level: 0 for level in LEVEL_ORDER}
    for candidate in candidates or []:
        if candidate.get('level_type') in supply:
            supply[candidate['level_type']] += 1
    return supply


def _why_none(tier: ShapeTier, supply: dict) -> str:
    """A plain reason a tier has no pick, from what the window held."""
    needs = {
        TIER_INTRO.key: ['Introductory'],
        TIER_INTERMEDIATE.key: ['Intermediate'],
        TIER_LADDER.key: list(LEVEL_ORDER),
    }.get(tier.key, [])
    short = [f'{supply.get(level, 0)} {level}' for level in needs if supply.get(level, 0) < 2]
    held = ', '.join(short) if short else ''
    reason = f'no complete, judged pathway that is {tier.rule}'
    return f'{reason}; the window held {held}' if held else reason


def select_shapes(run: dict, rubric: str = RUBRIC_V1) -> dict:
    """
    The judge's best pathway in each tier for one exported career run, under ``rubric``.

    Returns the career's identity, the rubric, the window's supply per rung, and one entry
    per tier in ``SHAPE_TIERS`` order with ``pick`` (or ``None`` and ``why_none``), how many
    candidates competed, and the runners-up, best first.

    Raises:
        ValueError: An unknown rubric.
    """
    by_tier = candidates_by_tier(run, rubric)
    supply = window_supply(run.get('candidates'))
    tiers = []
    for tier in SHAPE_TIERS:
        ranked = by_tier[tier.key]
        tiers.append({
            'tier': tier.key,
            'tier_label': tier.label,
            'rule': tier.rule,
            'pick': ranked[0] if ranked else None,
            'why_none': '' if ranked else _why_none(tier, supply),
            'n_candidates': len(ranked),
            'runners_up': [
                {key: candidate[key] for key in (
                    'labels', 'level_mix', 'verdict', 'n_on_topic', 'n_courses', 'precision', 'reason',
                    'n_flagged',
                )}
                for candidate in ranked[1:]
            ],
        })
    return {
        'career': run.get('career_name') or run.get('requested_name', ''),
        'rubric': rubric,
        'requested_name': run.get('requested_name', ''),
        'external_id': run.get('external_id', ''),
        'workflow_uuid': run.get('workflow_uuid', ''),
        'supply': supply,
        'tiers': tiers,
        'candidates': list(run.get('candidates') or []),
    }


def select_all(runs: list[dict], rubric: str = RUBRIC_V1) -> list[dict]:
    """``select_shapes`` under ``rubric`` for every run that produced a pathway workflow."""
    _judgement_field(rubric)
    return [select_shapes(run, rubric) for run in runs if run.get('workflow_uuid') and not run.get('error')]


# ``rubric`` and ``n_flagged`` are appended so earlier readers of the column order still work.
SELECTION_CSV_COLUMNS = (
    'career', 'tier', 'status', 'labels', 'level_mix', 'verdict', 'n_on_topic', 'n_courses',
    'n_candidates', 'judge_reason', 'course_keys', 'course_titles', 'why_none', 'rubric', 'n_flagged',
)


def write_selection_csv(selections: list[dict], path) -> int:
    """One row per career and tier; returns the row count."""
    rows = []
    for selection in selections:
        for tier in selection['tiers']:
            pick = tier['pick'] or {}
            courses = pick.get('courses') or []
            rows.append({
                'career': selection['career'],
                'tier': tier['tier'],
                'status': 'picked' if tier['pick'] else 'none',
                'labels': ' | '.join(pick.get('labels') or []),
                'level_mix': pick.get('level_mix', ''),
                'verdict': pick.get('verdict', ''),
                'n_on_topic': pick.get('n_on_topic', ''),
                'n_courses': pick.get('n_courses', ''),
                'n_candidates': tier['n_candidates'],
                'judge_reason': pick.get('reason', ''),
                'course_keys': ' | '.join(course.get('key', '') for course in courses),
                'course_titles': ' | '.join(course.get('title', '') for course in courses),
                'why_none': tier['why_none'],
                'rubric': selection.get('rubric', RUBRIC_V1),
                'n_flagged': pick.get('n_flagged', ''),
            })
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=SELECTION_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
