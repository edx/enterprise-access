"""
Deterministic scoring of harness output, in the three tiers Decision 8 defines.

Pure computation over the traces the harness produced. No network, no model, no judgement
calls that a person has not already written down -- which is the point of putting the
threshold in configuration rather than in a reviewer's head.

Why three tiers rather than one number
--------------------------------------
A single aggregate recall figure is the wrong shape for a ship decision, for three reasons
that are properties of *this* evaluation rather than opinions:

1. **Resolution.** Eight scoreable personas quantise any aggregate at 12.5 percentage
   points, and several personas have exactly one expected course -- so that persona scores
   0% or 100% and nothing between. A bar of "recall >= 40%" would have increments finer
   than the instrument.
2. **Averaging hides the defect.** Measured technology recall is 0% against 40% for
   non-technology. An aggregate bar of 30% can be met with technology still at zero, so an
   aggregate metric is structurally unable to gate on the thing that is actually broken.
3. **Recall measures agreement with the author, not learner value.** Ground truth is the
   courses product thought of. High recall is good evidence; *low* recall is ambiguous --
   bad retrieval, or thin ground truth.

So: Tier 1 gates correctness (bugs, not quality, and no product input needed), Tier 2 is
the ship bar as per-persona pass/fail plus a count, and Tier 3 is tracked but never
gating. ``expect_no_coverage`` personas invert the Tier 2 rule rather than being excluded
from it -- for them, returning nothing *is* the correct answer.
"""
import logging
from collections import defaultdict

from enterprise_access.apps.pathways.pathway_assembly import LEVEL_ORDER, PATHWAY_SIZE

logger = logging.getLogger(__name__)

# Tier 2's two product-owned numbers. Named constants rather than literals because they
# are the parameters of a decision, and a decision that is hard to find is hard to revise.
MIN_EXPECTED_COURSES_IN_PATHWAY = 1
MIN_PASSING_PERSONAS = 6

SPLIT_TECHNOLOGY = 'technology'
SPLIT_NON_TECHNOLOGY = 'non_technology'


def cell_dicts(cells):
    """Accept either ``CellResult`` objects or plain dicts."""
    return [cell if isinstance(cell, dict) else cell.to_dict() for cell in cells]


def score_persona(persona, cells) -> dict:
    """
    Score one persona across all of its cells.

    A persona passes if **any** of its cells produced a passing pathway. That is
    deliberate: the cells are repeat runs and career modes of the same question, and the
    oracle arm existing at all is an admission that auto-mode career selection is a
    separate problem. Requiring every cell to pass would conflate the two again.
    """
    persona_cells = [c for c in cell_dicts(cells) if c['persona_id'] == persona.id]
    ran = [c for c in persona_cells if not c['skipped_reason'] and not c['error']]

    expected = set(persona.expected_course_keys)
    per_cell = [_score_cell(persona, cell, expected) for cell in ran]

    scoreable = persona.has_ground_truth and persona.is_expert_authored
    passed = any(entry['passed'] for entry in per_cell) if per_cell else False

    return {
        'persona_id': persona.id,
        'domain': persona.domain,
        'split': SPLIT_TECHNOLOGY if persona.is_technology else SPLIT_NON_TECHNOLOGY,
        'scoreable': scoreable,
        'expect_no_coverage': persona.expect_no_coverage,
        'expected_course_count': len(expected),
        'cells_planned': len(persona_cells),
        'cells_ran': len(ran),
        'passed': passed if scoreable else None,
        'best_recall': max((e['recall'] for e in per_cell), default=None),
        'cells': per_cell,
    }


def _score_cell(persona, cell, expected) -> dict:
    """Score one cell against the persona's ground truth."""
    returned = set(cell['course_keys'])
    matched = expected & returned
    recall = (len(matched) / len(expected)) if expected else None

    if persona.expect_no_coverage:
        # Scenario: expected absence is not counted as failure. Returning nothing is the
        # right answer, so the rule inverts rather than being skipped.
        passed = not cell['complete']
    else:
        passed = len(matched) >= MIN_EXPECTED_COURSES_IN_PATHWAY

    return {
        'career_mode': cell['career_mode'],
        'run_index': cell['run_index'],
        'complete': cell['complete'],
        'returned_count': len(returned),
        'matched_keys': sorted(matched),
        'recall': recall,
        'passed': passed,
        'violations': list(cell['violations']),
    }


def tier_one_gates(cells) -> dict:
    """
    Apply the Tier 1 correctness gates across every cell.

    Each is a bug if it fires. ``passed`` being False means the run should not be scored
    at all -- a quality number computed over structurally invalid pathways is noise.

    Most of the gates live in ``pathway_assembly.validate_pathway`` and arrive here as
    persisted ``violations``; this function adds only the ones that need the *run* rather
    than a single pathway to see.
    """
    rows = cell_dicts(cells)
    ran = [c for c in rows if not c['skipped_reason'] and not c['error']]

    assembly_violations = [
        {'persona_id': c['persona_id'], 'career_mode': c['career_mode'],
         'run_index': c['run_index'], 'violations': c['violations']}
        for c in ran if c['violations']
    ]

    wrong_length = [
        {'persona_id': c['persona_id'], 'returned': len(c['course_keys'])}
        for c in ran
        if c['complete'] and len(c['course_keys']) != PATHWAY_SIZE
    ]

    # A pathway reported incomplete must carry no courses at all: a partial set returned
    # to a client would render as a pathway that nobody claimed was one.
    partial_pathways = [
        {'persona_id': c['persona_id'], 'returned': len(c['course_keys'])}
        for c in ran
        if not c['complete'] and c['course_keys']
    ]

    failures = {
        'assembly_violations': assembly_violations,
        'wrong_length': wrong_length,
        'partial_pathways_returned': partial_pathways,
    }
    return {
        'passed': not any(failures.values()),
        'failures': {name: rows for name, rows in failures.items() if rows},
        'cells_checked': len(ran),
    }


def tier_two_bar(persona_scores, *, min_passing=MIN_PASSING_PERSONAS) -> dict:
    """
    The ship bar: how many scoreable personas passed, and whether a split scored zero.

    The no-zero-split rule is what an aggregate cannot express. A run where every
    technology persona fails is not shippable regardless of the total, because the failure
    is concentrated in a domain rather than spread thin.
    """
    scoreable = [s for s in persona_scores if s['scoreable']]
    passing = [s for s in scoreable if s['passed']]

    per_split = {}
    for split in (SPLIT_TECHNOLOGY, SPLIT_NON_TECHNOLOGY):
        split_rows = [s for s in scoreable if s['split'] == split]
        split_passing = [s for s in split_rows if s['passed']]
        per_split[split] = {
            'scoreable': len(split_rows),
            'passing': len(split_passing),
            # A split with no scoreable personas cannot fail this rule; it has nothing to
            # say either way, and treating silence as failure would block on ground truth.
            'zero': bool(split_rows) and not split_passing,
        }

    zero_splits = [name for name, row in per_split.items() if row['zero']]

    return {
        'scoreable': len(scoreable),
        'passing': len(passing),
        'min_passing': min_passing,
        'count_met': len(passing) >= min_passing,
        'no_zero_split': not zero_splits,
        'zero_splits': zero_splits,
        'passed': len(passing) >= min_passing and not zero_splits,
        'per_split': per_split,
        'passing_persona_ids': sorted(s['persona_id'] for s in passing),
        'failing_persona_ids': sorted(
            s['persona_id'] for s in scoreable if not s['passed']
        ),
    }


def tier_three_metrics(persona_scores, cells) -> dict:
    """
    Tracked-not-gated numbers.

    Level mix is here rather than in Tier 1 on purpose: a rung can be genuinely empty in
    the catalog (``Nursing`` has no advanced course, ``Welding`` has one course in total),
    so a hard level quota would fail personas for a content reason indistinguishable from
    a retrieval failure -- and ``level_type`` is 19-36% unreliable at the item level, so a
    gate on it would be measuring the metadata's noise.
    """
    rows = cell_dicts(cells)
    ran = [c for c in rows if not c['skipped_reason'] and not c['error']]
    complete = [c for c in ran if c['complete']]

    recalls_by_split = defaultdict(list)
    for score in persona_scores:
        if score['scoreable'] and score['best_recall'] is not None:
            recalls_by_split[score['split']].append(score['best_recall'])

    splits = {}
    for split in (SPLIT_TECHNOLOGY, SPLIT_NON_TECHNOLOGY):
        values = recalls_by_split.get(split) or []
        splits[split] = {
            'personas': len(values),
            'mean_recall': (sum(values) / len(values)) if values else None,
        }
    delta = None
    if splits[SPLIT_TECHNOLOGY]['mean_recall'] is not None and \
            splits[SPLIT_NON_TECHNOLOGY]['mean_recall'] is not None:
        delta = splits[SPLIT_TECHNOLOGY]['mean_recall'] - splits[SPLIT_NON_TECHNOLOGY]['mean_recall']

    # A pathway that shipped unexplained is a real but much milder defect than one that
    # failed to assemble, so it is tracked rather than gated.
    unexplained = [c for c in complete if not c.get('rationale_count')]

    return {
        'cells_ran': len(ran),
        'cells_complete': len(complete),
        'completion_rate': (len(complete) / len(ran)) if ran else None,
        'unexplained_pathway_rate': (
            len(unexplained) / len(complete)
        ) if complete else None,
        'zero_hit_rate': (
            len([c for c in ran if not c['course_keys']]) / len(ran)
        ) if ran else None,
        'unfilled_rung_rate': {
            level: (
                len([c for c in complete if level in c['unfilled_rungs']]) / len(complete)
            ) if complete else None
            for level in LEVEL_ORDER
        },
        'splits': splits,
        'technology_delta': delta,
        'career_mode_delta': _career_mode_delta(persona_scores),
        'cross_run_consistency': _cross_run_consistency(ran),
    }


def _career_mode_delta(persona_scores) -> dict:
    """
    How many personas pass in each career mode.

    The gap is the cost of automatic career selection. Charging that cost to course
    retrieval is the specific mistake the oracle arm exists to prevent.
    """
    counts = {}
    for mode in ('auto', 'oracle'):
        passing = 0
        for score in persona_scores:
            if not score['scoreable']:
                continue
            if any(c['passed'] for c in score['cells'] if c['career_mode'] == mode):
                passing += 1
        counts[mode] = passing
    counts['delta'] = counts['oracle'] - counts['auto']
    return counts


def _cross_run_consistency(ran_cells) -> dict:
    """
    Set overlap across repeat runs of the same persona and mode.

    Reported as mean Jaccard similarity over the pairs available. ``None`` when there is
    only one run, which is honest: a single run says nothing about stability, and
    reporting 1.0 would claim perfect consistency from no evidence.
    """
    grouped = defaultdict(list)
    for cell in ran_cells:
        grouped[(cell['persona_id'], cell['career_mode'])].append(set(cell['course_keys']))

    similarities = []
    for key_sets in grouped.values():
        for index, first in enumerate(key_sets):
            for second in key_sets[index + 1:]:
                union = first | second
                if union:
                    similarities.append(len(first & second) / len(union))
    return {
        'pairs_compared': len(similarities),
        'mean_jaccard': (sum(similarities) / len(similarities)) if similarities else None,
    }


def score_run(personas, cells, *, min_passing=MIN_PASSING_PERSONAS) -> dict:
    """
    Score a whole harness run into the three tiers.

    Returns ``tier_one``, ``tier_two``, ``tier_three`` and the per-persona detail.
    ``shippable`` requires *both* Tier 1 and Tier 2 -- Tier 3 never gates.
    """
    persona_scores = [score_persona(persona, cells) for persona in personas]
    tier_one = tier_one_gates(cells)
    tier_two = tier_two_bar(persona_scores, min_passing=min_passing)
    tier_three = tier_three_metrics(persona_scores, cells)

    return {
        'personas': persona_scores,
        'tier_one': tier_one,
        'tier_two': tier_two,
        'tier_three': tier_three,
        'shippable': tier_one['passed'] and tier_two['passed'],
    }


def regression_verdict(current, previous) -> dict:
    """
    Compare a run against its predecessor.

    The regression bar is engineering-owned and needs no product input: no Tier 2 or Tier 3
    metric may fall run-over-run without an explicit note. It is the bar with immediate
    value, because the ship bar will not be met for some time -- and without it there is
    nothing to steer by in between.

    Returns ``None`` when there is no previous run, rather than inventing a baseline.
    """
    if not previous:
        return None

    regressions = []
    improvements = []

    def compare(name, current_value, previous_value):
        if current_value is None or previous_value is None:
            return
        if current_value < previous_value:
            regressions.append({'metric': name, 'from': previous_value, 'to': current_value})
        elif current_value > previous_value:
            improvements.append({'metric': name, 'from': previous_value, 'to': current_value})

    compare('tier_two.passing', current['tier_two']['passing'], previous['tier_two']['passing'])
    compare(
        'tier_three.completion_rate',
        current['tier_three']['completion_rate'], previous['tier_three']['completion_rate'],
    )
    for split in (SPLIT_TECHNOLOGY, SPLIT_NON_TECHNOLOGY):
        compare(
            f'tier_three.{split}.mean_recall',
            current['tier_three']['splits'][split]['mean_recall'],
            previous['tier_three']['splits'][split]['mean_recall'],
        )

    return {
        'passed': not regressions,
        'regressions': regressions,
        'improvements': improvements,
    }
