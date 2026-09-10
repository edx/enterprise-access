"""
Tests for the deterministic scorers.

Three properties get the most attention, because each is a claim about *why* the report
has the shape Decision 8 gave it:

* Tier 1 failing must invalidate the quality numbers, not sit alongside them.
* A split scoring zero must fail Tier 2 even when the total clears the bar -- that is
  exactly what an aggregate metric cannot express.
* ``expect_no_coverage`` personas must invert the rule, not be excluded from it.
"""
import json
import tempfile
from io import StringIO
from pathlib import Path

import yaml
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from enterprise_access.apps.pathway_eval.personas import persona_from_dict
from enterprise_access.apps.pathway_eval.scoring import (
    SPLIT_NON_TECHNOLOGY,
    SPLIT_TECHNOLOGY,
    regression_verdict,
    score_persona,
    score_run,
    tier_one_gates,
    tier_three_metrics,
    tier_two_bar
)
from enterprise_access.apps.pathways.pathway_assembly import PATHWAY_SIZE

CAREER_ID = 'ETE78CD2CDFFFAC66B'


def persona_dict(persona_id, *, domain='technology', courses=('IBM+DA0101EN',),
                 expect_no_coverage=False, status='expert_authored'):
    return {
        'id': persona_id,
        'domain': domain,
        'tier': 'core',
        'inputs': {
            'selected_goals': 'change careers',
            'free_text': 'I want a new job',
            'known_context': 'analyst',
            'interested_industries': 'technology',
        },
        'expected': {
            'ground_truth_status': status,
            'expect_no_coverage': expect_no_coverage,
            'careers': [{'external_id': CAREER_ID}],
            'courses': [{'key': k} for k in courses],
        },
    }


def make_persona(persona_id, **kwargs):
    return persona_from_dict(persona_dict(persona_id, **kwargs))


# A complete pathway is exactly PATHWAY_SIZE courses, so a realistic cell has five keys.
# Padding here rather than in each test keeps the Tier 1 length gate meaningful: a fixture
# that returned one course marked complete would trip it in every test.
FILLER_KEYS = ('Filler+1', 'Filler+2', 'Filler+3', 'Filler+4', 'Filler+5')


def five_keys(*keys):
    """Pad ``keys`` out to a full pathway with filler that matches no ground truth."""
    padded = list(keys) + [k for k in FILLER_KEYS if k not in keys]
    return tuple(padded[:PATHWAY_SIZE])


def cell(persona_id, *, mode='auto', run=1, keys=None, complete=True,
         violations=(), unfilled=(), skipped='', error='', rationale_count=5):
    """Build one harness cell trace, padded to a valid pathway length by default."""
    if keys is None:
        keys = five_keys('IBM+DA0101EN')
    elif complete and len(keys) not in (0, PATHWAY_SIZE):
        keys = five_keys(*keys)
    return {
        'persona_id': persona_id,
        'career_mode': mode,
        'run_index': run,
        'career_workflow_uuid': 'c',
        'pathway_workflow_uuid': 'p',
        'career_name': 'Data Analyst',
        'career_external_id': CAREER_ID,
        'course_keys': list(keys),
        'complete': complete,
        'violations': list(violations),
        'unfilled_rungs': list(unfilled),
        'rationale_count': rationale_count,
        'skipped_reason': skipped,
        'error': error,
    }


class TestScorePersona(TestCase):
    """
    Tests for ``score_persona``.
    """

    def test_a_matched_expected_course_passes(self):
        persona = make_persona('p001')

        score = score_persona(persona, [cell('p001')])

        self.assertTrue(score['passed'])
        self.assertEqual(score['best_recall'], 1.0)

    def test_no_matched_course_fails(self):
        persona = make_persona('p001')

        score = score_persona(persona, [cell('p001', keys=five_keys('Other+1'))])

        self.assertFalse(score['passed'])
        self.assertEqual(score['best_recall'], 0.0)

    def test_one_of_several_expected_courses_is_enough_to_pass(self):
        """
        The bar is deliberately a floor. At 23% recall the thing to establish first is
        that the pipeline works at all for a domain.
        """
        persona = make_persona('p001', courses=('A+1', 'B+2', 'C+3'))

        score = score_persona(persona, [cell('p001', keys=five_keys('A+1'))])

        self.assertTrue(score['passed'])
        self.assertAlmostEqual(score['best_recall'], 1 / 3)

    def test_any_passing_cell_passes_the_persona(self):
        """
        Cells are repeat runs and career modes of the same question. Requiring all of
        them to pass would fold career-selection error back into the course score, which
        is what the oracle arm exists to separate.
        """
        persona = make_persona('p001')

        score = score_persona(persona, [
            cell('p001', mode='auto', keys=five_keys('Other+1')),
            cell('p001', mode='oracle', keys=five_keys('IBM+DA0101EN')),
        ])

        self.assertTrue(score['passed'])

    def test_expect_no_coverage_inverts_the_rule(self):
        """Scenario: Expected absence is not counted as failure."""
        persona = make_persona('p001', courses=(), expect_no_coverage=True)

        passing = score_persona(persona, [cell('p001', keys=(), complete=False)])
        failing = score_persona(persona, [cell('p001', keys=five_keys('A+1'), complete=True)])

        self.assertTrue(passing['passed'])
        self.assertFalse(failing['passed'])

    def test_a_placeholder_persona_is_not_scoreable(self):
        """Placeholder ground truth must not pollute a headline metric."""
        persona = make_persona('p001', status='placeholder')

        score = score_persona(persona, [cell('p001')])

        self.assertFalse(score['scoreable'])
        self.assertIsNone(score['passed'])

    def test_a_persona_with_no_ground_truth_is_not_scoreable(self):
        persona = make_persona('p001', courses=())

        self.assertFalse(score_persona(persona, [cell('p001')])['scoreable'])

    def test_skipped_and_errored_cells_are_not_scored(self):
        persona = make_persona('p001')

        score = score_persona(persona, [
            cell('p001', skipped='dry run'),
            cell('p001', error='boom'),
        ])

        self.assertEqual(score['cells_ran'], 0)
        self.assertEqual(score['cells_planned'], 2)
        self.assertFalse(score['passed'])

    def test_the_split_follows_the_domain(self):
        self.assertEqual(
            score_persona(make_persona('p1', domain='technology'), [])['split'],
            SPLIT_TECHNOLOGY,
        )
        self.assertEqual(
            score_persona(make_persona('p2', domain='healthcare'), [])['split'],
            SPLIT_NON_TECHNOLOGY,
        )


class TestTierOneGates(TestCase):
    """
    Tests for ``tier_one_gates``.
    """

    def test_clean_cells_pass(self):
        result = tier_one_gates([cell('p001'), cell('p002')])

        self.assertTrue(result['passed'])
        self.assertEqual(result['failures'], {})

    def test_persisted_assembly_violations_fail_the_gate(self):
        result = tier_one_gates([
            cell('p001', violations=['3 courses from P1 exceeds the cap of 2']),
        ])

        self.assertFalse(result['passed'])
        self.assertIn('assembly_violations', result['failures'])

    def test_a_complete_pathway_of_the_wrong_length_fails(self):
        short = cell('p001')
        short['course_keys'] = ['A+1', 'B+2']

        result = tier_one_gates([short])

        self.assertFalse(result['passed'])
        self.assertIn('wrong_length', result['failures'])

    def test_an_incomplete_pathway_returning_courses_fails(self):
        """
        A partial set handed to a client would render as a pathway nobody claimed was one.
        """
        result = tier_one_gates([cell('p001', keys=('A+1',), complete=False)])

        self.assertFalse(result['passed'])
        self.assertIn('partial_pathways_returned', result['failures'])

    def test_an_incomplete_pathway_with_no_courses_is_fine(self):
        result = tier_one_gates([cell('p001', keys=(), complete=False)])

        self.assertTrue(result['passed'])

    def test_skipped_cells_are_not_gated(self):
        result = tier_one_gates([cell('p001', skipped='dry run', violations=['x'])])

        self.assertTrue(result['passed'])
        self.assertEqual(result['cells_checked'], 0)


class TestTierTwoBar(TestCase):
    """
    Tests for ``tier_two_bar``.
    """

    def _scores(self, spec):
        """``spec`` maps persona id to (split, passed)."""
        return [
            {'persona_id': pid, 'domain': split, 'split': split, 'scoreable': True,
             'expect_no_coverage': False, 'expected_course_count': 1, 'cells_planned': 1,
             'cells_ran': 1, 'passed': passed, 'best_recall': 1.0 if passed else 0.0,
             'cells': []}
            for pid, (split, passed) in spec.items()
        ]

    def test_the_count_bar_is_applied(self):
        scores = self._scores({
            'p1': (SPLIT_TECHNOLOGY, True), 'p2': (SPLIT_NON_TECHNOLOGY, True),
        })

        self.assertTrue(tier_two_bar(scores, min_passing=2)['count_met'])
        self.assertFalse(tier_two_bar(scores, min_passing=3)['count_met'])

    def test_a_zero_split_fails_even_when_the_count_is_met(self):
        """
        The rule an aggregate cannot express: concentrated failure is not shippable
        regardless of the total.
        """
        scores = self._scores({
            'p1': (SPLIT_NON_TECHNOLOGY, True),
            'p2': (SPLIT_NON_TECHNOLOGY, True),
            'p3': (SPLIT_TECHNOLOGY, False),
        })

        result = tier_two_bar(scores, min_passing=2)

        self.assertTrue(result['count_met'])
        self.assertFalse(result['no_zero_split'])
        self.assertFalse(result['passed'])
        self.assertEqual(result['zero_splits'], [SPLIT_TECHNOLOGY])

    def test_a_split_with_no_scoreable_personas_does_not_fail_the_rule(self):
        """
        Silence is not failure. Treating an unpopulated split as zero would block the bar
        on ground-truth authoring rather than on pipeline quality.
        """
        scores = self._scores({
            'p1': (SPLIT_NON_TECHNOLOGY, True), 'p2': (SPLIT_NON_TECHNOLOGY, True),
        })

        result = tier_two_bar(scores, min_passing=2)

        self.assertTrue(result['passed'])
        self.assertEqual(result['per_split'][SPLIT_TECHNOLOGY]['scoreable'], 0)

    def test_unscoreable_personas_are_excluded_from_both_sides_of_the_ratio(self):
        scores = self._scores({'p1': (SPLIT_TECHNOLOGY, True)})
        scores.append({
            'persona_id': 'p2', 'domain': 'technology', 'split': SPLIT_TECHNOLOGY,
            'scoreable': False, 'expect_no_coverage': False, 'expected_course_count': 0,
            'cells_planned': 1, 'cells_ran': 1, 'passed': None, 'best_recall': None,
            'cells': [],
        })

        result = tier_two_bar(scores, min_passing=1)

        self.assertEqual(result['scoreable'], 1)
        self.assertEqual(result['passing'], 1)

    def test_passing_and_failing_ids_are_listed(self):
        scores = self._scores({
            'p1': (SPLIT_TECHNOLOGY, True), 'p2': (SPLIT_NON_TECHNOLOGY, False),
        })

        result = tier_two_bar(scores, min_passing=1)

        self.assertEqual(result['passing_persona_ids'], ['p1'])
        self.assertEqual(result['failing_persona_ids'], ['p2'])


class TestTierThreeMetrics(TestCase):
    """
    Tests for ``tier_three_metrics``.
    """

    def test_the_technology_gap_is_quantified(self):
        """Scenario: The technology gap is quantified."""
        personas = [
            make_persona('p1', domain='technology'),
            make_persona('p2', domain='healthcare'),
        ]
        cells = [
            cell('p1', keys=five_keys('Other+1')),
            cell('p2', keys=('IBM+DA0101EN',)),
        ]
        scores = [score_persona(p, cells) for p in personas]

        metrics = tier_three_metrics(scores, cells)

        self.assertEqual(metrics['splits'][SPLIT_TECHNOLOGY]['mean_recall'], 0.0)
        self.assertEqual(metrics['splits'][SPLIT_NON_TECHNOLOGY]['mean_recall'], 1.0)
        self.assertEqual(metrics['technology_delta'], -1.0)

    def test_consistency_is_measured_across_runs(self):
        """Scenario: Consistency is measured across runs."""
        personas = [make_persona('p1')]
        cells = [
            cell('p1', run=1, keys=five_keys('A+1', 'B+2')),
            cell('p1', run=2, keys=five_keys('A+1', 'C+3')),
            cell('p1', run=3, keys=five_keys('A+1', 'B+2')),
        ]
        scores = [score_persona(p, cells) for p in personas]

        consistency = tier_three_metrics(scores, cells)['cross_run_consistency']

        self.assertEqual(consistency['pairs_compared'], 3)
        self.assertIsNotNone(consistency['mean_jaccard'])

    def test_a_single_run_reports_no_consistency_rather_than_perfect(self):
        """Reporting 1.0 would claim perfect stability from no evidence."""
        personas = [make_persona('p1')]
        cells = [cell('p1')]
        scores = [score_persona(p, cells) for p in personas]

        consistency = tier_three_metrics(scores, cells)['cross_run_consistency']

        self.assertEqual(consistency['pairs_compared'], 0)
        self.assertIsNone(consistency['mean_jaccard'])

    def test_the_career_mode_delta_is_reported(self):
        personas = [make_persona('p1')]
        cells = [
            cell('p1', mode='auto', keys=five_keys('Other+1')),
            cell('p1', mode='oracle', keys=five_keys('IBM+DA0101EN')),
        ]
        scores = [score_persona(p, cells) for p in personas]

        modes = tier_three_metrics(scores, cells)['career_mode_delta']

        self.assertEqual(modes['auto'], 0)
        self.assertEqual(modes['oracle'], 1)
        self.assertEqual(modes['delta'], 1)

    def test_zero_hit_and_completion_rates_are_reported(self):
        personas = [make_persona('p1'), make_persona('p2')]
        cells = [cell('p1'), cell('p2', keys=(), complete=False)]
        scores = [score_persona(p, cells) for p in personas]

        metrics = tier_three_metrics(scores, cells)

        self.assertEqual(metrics['completion_rate'], 0.5)
        self.assertEqual(metrics['zero_hit_rate'], 0.5)

    def test_the_unexplained_pathway_rate_is_tracked(self):
        """A pathway that shipped without rationales is milder than one that failed."""
        personas = [make_persona('p1'), make_persona('p2')]
        cells = [cell('p1'), cell('p2', rationale_count=0)]
        scores = [score_persona(p, cells) for p in personas]

        metrics = tier_three_metrics(scores, cells)

        self.assertEqual(metrics['unexplained_pathway_rate'], 0.5)

    def test_unfilled_rung_rates_are_reported_per_level(self):
        personas = [make_persona('p1')]
        cells = [cell('p1', unfilled=['Advanced'])]
        scores = [score_persona(p, cells) for p in personas]

        rates = tier_three_metrics(scores, cells)['unfilled_rung_rate']

        self.assertEqual(rates['Advanced'], 1.0)
        self.assertEqual(rates['Introductory'], 0.0)


class TestScoreRun(TestCase):
    """
    Tests for ``score_run``.
    """

    def test_shippable_requires_tier_one_and_tier_two(self):
        personas = [make_persona('p1'), make_persona('p2', domain='healthcare')]
        cells = [cell('p1'), cell('p2')]

        report = score_run(personas, cells, min_passing=2)

        self.assertTrue(report['tier_one']['passed'])
        self.assertTrue(report['tier_two']['passed'])
        self.assertTrue(report['shippable'])

    def test_a_tier_one_failure_makes_the_run_unshippable_regardless_of_quality(self):
        """Tier 1 failing invalidates the quality numbers rather than sitting beside them."""
        personas = [make_persona('p1'), make_persona('p2', domain='healthcare')]
        cells = [cell('p1', violations=['bad']), cell('p2')]

        report = score_run(personas, cells, min_passing=2)

        self.assertTrue(report['tier_two']['passed'])
        self.assertFalse(report['tier_one']['passed'])
        self.assertFalse(report['shippable'])

    def test_tier_three_never_gates(self):
        personas = [make_persona('p1'), make_persona('p2', domain='healthcare')]
        cells = [cell('p1'), cell('p2')]

        report = score_run(personas, cells, min_passing=2)

        self.assertTrue(report['shippable'])
        self.assertIsNotNone(report['tier_three']['technology_delta'])


class TestRegressionVerdict(TestCase):
    """
    Tests for ``regression_verdict``.
    """

    def _report(self, passing, tech_recall):
        return {
            'tier_two': {'passing': passing},
            'tier_three': {
                'completion_rate': 1.0,
                'splits': {
                    SPLIT_TECHNOLOGY: {'mean_recall': tech_recall},
                    SPLIT_NON_TECHNOLOGY: {'mean_recall': 0.5},
                },
            },
        }

    def test_no_previous_run_yields_no_verdict_rather_than_a_pass(self):
        """Inventing a baseline would report a pass from no evidence."""
        self.assertIsNone(regression_verdict(self._report(3, 0.2), None))

    def test_a_decrease_is_a_regression(self):
        verdict = regression_verdict(self._report(2, 0.2), self._report(3, 0.2))

        self.assertFalse(verdict['passed'])
        self.assertEqual(verdict['regressions'][0]['metric'], 'tier_two.passing')

    def test_an_increase_is_reported_as_an_improvement(self):
        verdict = regression_verdict(self._report(4, 0.2), self._report(3, 0.2))

        self.assertTrue(verdict['passed'])
        self.assertEqual(verdict['improvements'][0]['metric'], 'tier_two.passing')

    def test_a_split_recall_drop_is_caught(self):
        verdict = regression_verdict(self._report(3, 0.1), self._report(3, 0.3))

        self.assertFalse(verdict['passed'])
        self.assertIn('technology', verdict['regressions'][0]['metric'])

    def test_an_unmeasurable_metric_is_skipped_rather_than_treated_as_zero(self):
        verdict = regression_verdict(self._report(3, None), self._report(3, 0.3))

        self.assertTrue(verdict['passed'])


class TestReportPathwayHarnessCommand(TestCase):
    """
    Tests for the ``report_pathway_harness`` command.
    """

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.persona_dir = self.root / 'personas'
        self.persona_dir.mkdir()
        for pid, domain in (('p001-tech', 'technology'), ('p002-health', 'healthcare')):
            (self.persona_dir / f'{pid}.yaml').write_text(
                yaml.safe_dump(persona_dict(pid, domain=domain), sort_keys=False),
            )

    def write_traces(self, name, cells, **config):
        """Write a traces file in the shape run_pathway_harness exports."""
        path = self.root / name
        path.write_text(json.dumps({
            'run_config': {'customer_uuid': '417306cb-b24a-4d06-b83c-fb2a61d7fb96',
                           'model_backend': 'xpert', 'rerank_enabled': True, 'runs': 1,
                           **config},
            'cells': cells,
        }))
        return path

    def call(self, traces, **kwargs):
        """Invoke the report command and capture stdout."""
        stdout = StringIO()
        kwargs.setdefault('persona_dir', str(self.persona_dir))
        call_command('report_pathway_harness', str(traces), stdout=stdout, **kwargs)
        return stdout.getvalue()

    def test_a_passing_run_reports_that_it_meets_the_bar(self):
        traces = self.write_traces('t.json', [cell('p001-tech'), cell('p002-health')])

        output = self.call(traces, min_passing=2)

        self.assertIn('MEETS THE BAR', output)
        self.assertIn('TIER 1', output)
        self.assertIn('TIER 2', output)
        self.assertIn('TIER 3', output)

    def test_a_zero_split_is_called_out_explicitly(self):
        traces = self.write_traces('t.json', [
            cell('p001-tech', keys=five_keys('Other+1')), cell('p002-health'),
        ])

        output = self.call(traces, min_passing=1)

        self.assertIn('ZERO', output)
        self.assertIn('DOES NOT MEET THE BAR', output)

    def test_an_unscoped_run_is_flagged(self):
        traces = self.write_traces('t.json', [cell('p001-tech')], customer_uuid='')

        output = self.call(traces, min_passing=1)

        self.assertIn('upper bound', output)

    def test_the_regression_bar_compares_against_a_previous_run(self):
        current = self.write_traces('cur.json', [
            cell('p001-tech', keys=five_keys('Other+1')), cell('p002-health'),
        ])
        previous = self.write_traces('prev.json', [
            cell('p001-tech'), cell('p002-health'),
        ])

        output = self.call(current, previous=str(previous), min_passing=1)

        self.assertIn('REGRESSION BAR', output)
        self.assertIn('tier_two.passing', output)

    def test_no_previous_run_says_so_rather_than_claiming_a_pass(self):
        traces = self.write_traces('t.json', [cell('p001-tech')])

        output = self.call(traces, min_passing=1)

        self.assertIn('nothing to compare', output)

    def test_the_report_is_exportable(self):
        traces = self.write_traces('t.json', [cell('p001-tech'), cell('p002-health')])
        out = self.root / 'nested' / 'report.json'

        self.call(traces, min_passing=2, output_json=str(out))

        report = json.loads(out.read_text())
        self.assertIn('tier_one', report)
        self.assertIn('tier_two', report)
        self.assertTrue(report['shippable'])

    def test_a_missing_traces_file_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, 'does not exist'):
            self.call(self.root / 'nope.json')

    def test_a_non_traces_json_file_is_a_command_error(self):
        path = self.root / 'other.json'
        path.write_text(json.dumps({'something': 'else'}))

        with self.assertRaisesRegex(CommandError, 'not a harness traces file'):
            self.call(path)

    def test_malformed_json_is_a_command_error(self):
        path = self.root / 'bad.json'
        path.write_text('{not json')

        with self.assertRaisesRegex(CommandError, 'not valid JSON'):
            self.call(path)
