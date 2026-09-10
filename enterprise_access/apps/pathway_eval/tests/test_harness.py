"""
Tests for the pathway harness runner.

The budget and dry-run tests matter more than they look: this is the only place in the
codebase that can spend real money in a loop, so "the limit is never overshot" and "a dry
run issues nothing" are correctness properties, not conveniences.
"""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from enterprise_access.apps.pathway_eval.harness import (
    CALLS_PER_CELL,
    CAREER_MODE_AUTO,
    CAREER_MODE_ORACLE,
    CellResult,
    HarnessBudget,
    PathwayHarness
)
from enterprise_access.apps.pathway_eval.personas import persona_from_dict
from enterprise_access.apps.workflow.exceptions import UnitOfWorkException

PATCH_CAREER_WORKFLOW = 'enterprise_access.apps.pathway_eval.harness.CareerDiscoveryWorkflow'
PATCH_PATHWAY_WORKFLOW = 'enterprise_access.apps.pathway_eval.harness.PathwayAssemblyWorkflow'

CAREER_ID = 'ETE78CD2CDFFFAC66B'


def persona_dict(persona_id='p001-test', *, careers=(CAREER_ID,), courses=('IBM+DA0101EN',),
                 domain='technology', expect_no_coverage=False):
    """Build a persona in the on-disk schema, with ground truth nested under ``expected``."""
    return {
        'id': persona_id,
        'domain': domain,
        'tier': 'core',
        'inputs': {
            'selected_goals': 'change careers',
            'free_text': 'I want to work with data',
            'known_context': 'analyst, five years',
            'interested_industries': 'technology',
        },
        'expected': {
            'ground_truth_status': 'expert_authored',
            'expect_no_coverage': expect_no_coverage,
            'careers': [{'external_id': c} for c in careers],
            'courses': [{'key': k} for k in courses],
        },
    }


def make_persona(*args, **kwargs):
    """Build a validated persona."""
    return persona_from_dict(persona_dict(*args, **kwargs))


def fake_career_workflow(candidates, uuid='c-uuid', intent=None):
    """A stand-in CareerDiscoveryWorkflow class."""
    instance = mock.Mock()
    instance.uuid = uuid
    instance.career_candidates.return_value = candidates
    instance.output_data = {'extract_intent_output': intent or {
        'skills_required': ['SQL'], 'skills_preferred': ['Tableau'],
    }}
    cls = mock.Mock()
    cls.objects.create.return_value = instance
    cls.generate_input_dict.return_value = {}
    return cls, instance


def fake_pathway_workflow(output, uuid='p-uuid'):
    """A stand-in PathwayAssemblyWorkflow class."""
    instance = mock.Mock()
    instance.uuid = uuid
    instance.output_data = {'assemble_pathway_output': output}
    cls = mock.Mock()
    cls.objects.create.return_value = instance
    cls.generate_input_dict.return_value = {}
    return cls, instance


def pathway_output(keys=('IBM+DA0101EN',), complete=True, violations=(), unfilled=()):
    return {
        'courses': [{'key': key, 'title': key} for key in keys],
        'complete': complete,
        'violations': list(violations),
        'unfilled_rungs': list(unfilled),
    }


class TestHarnessBudget(TestCase):
    """
    Tests for ``HarnessBudget``.
    """

    def test_no_limit_always_affords(self):
        self.assertTrue(HarnessBudget().can_afford(1000))

    def test_the_limit_is_checked_before_spending_not_after(self):
        """A limit checked afterwards has already spent what it was meant to prevent."""
        budget = HarnessBudget(max_calls=2)

        self.assertTrue(budget.can_afford(2))
        budget.charge(2)
        self.assertFalse(budget.can_afford(1))

    def test_a_cell_that_would_overshoot_is_refused_whole(self):
        budget = HarnessBudget(max_calls=3)
        budget.charge(CALLS_PER_CELL)

        self.assertFalse(budget.can_afford(CALLS_PER_CELL))


class TestCellResult(TestCase):
    """
    Tests for ``CellResult``.
    """

    def test_a_skipped_or_errored_cell_did_not_run(self):
        self.assertFalse(CellResult('p', 'auto', 1, skipped_reason='dry run').ran)
        self.assertFalse(CellResult('p', 'auto', 1, error='boom').ran)
        self.assertTrue(CellResult('p', 'auto', 1).ran)

    def test_uuids_serialize_as_strings(self):
        cell = CellResult('p', 'auto', 1, career_workflow_uuid=None)

        self.assertEqual(cell.to_dict()['career_workflow_uuid'], '')


class TestHarnessPlan(TestCase):
    """
    Tests for ``PathwayHarness.plan``.
    """

    def test_the_plan_is_personas_times_modes_times_runs(self):
        personas = [make_persona('p001'), make_persona('p002')]

        cells = PathwayHarness(runs=3).plan(personas)

        self.assertEqual(len(cells), 2 * 2 * 3)

    def test_oracle_mode_is_skipped_for_a_persona_with_no_expected_career(self):
        """
        Running it anyway would quietly make the oracle arm a second auto-mode run, and
        the delta between the arms is the entire point of having two.
        """
        persona = make_persona(careers=())

        cells = PathwayHarness().plan([persona])

        oracle = next(c for c in cells if c.career_mode == CAREER_MODE_ORACLE)
        auto = next(c for c in cells if c.career_mode == CAREER_MODE_AUTO)
        self.assertIn('no expected career', oracle.skipped_reason)
        self.assertEqual(auto.skipped_reason, '')

    def test_a_single_mode_can_be_selected(self):
        cells = PathwayHarness(career_modes=(CAREER_MODE_AUTO,)).plan([make_persona()])

        self.assertEqual([c.career_mode for c in cells], [CAREER_MODE_AUTO])


class TestHarnessRun(TestCase):
    """
    Tests for ``PathwayHarness.run``.
    """

    def setUp(self):
        super().setUp()
        self.candidates = [{'external_id': CAREER_ID, 'name': 'Data Analyst',
                            'skills': ['SQL (Programming Language)']}]
        self.career_cls = None
        self.pathway_cls = None
        self.career_instance = None
        self.pathway_instance = None

    def _run(self, harness, personas, candidates=None, output=None):
        """Run the harness with both workflow classes patched, and keep the stand-ins."""
        career_cls, self.career_instance = fake_career_workflow(
            candidates if candidates is not None else self.candidates,
        )
        pathway_cls, self.pathway_instance = fake_pathway_workflow(
            output if output is not None else pathway_output(),
        )
        with mock.patch(PATCH_CAREER_WORKFLOW, career_cls), \
                mock.patch(PATCH_PATHWAY_WORKFLOW, pathway_cls):
            self.career_cls = career_cls
            self.pathway_cls = pathway_cls
            return harness.run(personas)

    def test_both_career_modes_run_and_are_recorded(self):
        """Scenario: Both career modes run."""
        result = self._run(PathwayHarness(), [make_persona()])

        modes = {c.career_mode for c in result['cells'] if c.ran}
        self.assertEqual(modes, {CAREER_MODE_AUTO, CAREER_MODE_ORACLE})

    def test_a_dry_run_makes_no_calls(self):
        """Scenario: A dry run makes no paid calls."""
        result = self._run(PathwayHarness(dry_run=True), [make_persona()])

        self.career_cls.objects.create.assert_not_called()
        self.pathway_cls.objects.create.assert_not_called()
        self.assertEqual(result['calls_made'], 0)
        self.assertTrue(all(c.skipped_reason for c in result['cells']))

    def test_cost_is_bounded_and_the_shortfall_is_reported(self):
        """Scenario: Cost is bounded."""
        personas = [make_persona(f'p00{i}') for i in range(1, 4)]

        result = self._run(
            PathwayHarness(max_calls=CALLS_PER_CELL * 2, career_modes=(CAREER_MODE_AUTO,)),
            personas,
        )

        self.assertTrue(result['budget_exhausted'])
        self.assertEqual(result['calls_made'], CALLS_PER_CELL * 2)
        self.assertEqual(result['personas_completed'], 2)
        self.assertEqual(result['personas_total'], 3)

    def test_oracle_mode_forces_the_expected_career(self):
        candidates = [
            {'external_id': 'ETOTHER0000000000', 'name': 'Wrong Career', 'skills': ['X']},
            {'external_id': CAREER_ID, 'name': 'Data Analyst', 'skills': ['SQL']},
        ]

        result = self._run(
            PathwayHarness(career_modes=(CAREER_MODE_ORACLE,)), [make_persona()], candidates,
        )

        cell = result['cells'][0]
        self.assertEqual(cell.career_external_id, CAREER_ID)

    def test_auto_mode_follows_the_top_ranked_career(self):
        candidates = [
            {'external_id': 'ETOTHER0000000000', 'name': 'Top Career', 'skills': ['X']},
            {'external_id': CAREER_ID, 'name': 'Data Analyst', 'skills': ['SQL']},
        ]

        result = self._run(
            PathwayHarness(career_modes=(CAREER_MODE_AUTO,)), [make_persona()], candidates,
        )

        self.assertEqual(result['cells'][0].career_name, 'Top Career')

    def test_an_unretrievable_expected_career_is_skipped_not_fabricated(self):
        """
        Forcing a career the pipeline cannot find would measure a pathway no learner
        could ever reach.
        """
        candidates = [{'external_id': 'ETOTHER0000000000', 'name': 'Other', 'skills': ['X']}]

        result = self._run(
            PathwayHarness(career_modes=(CAREER_MODE_ORACLE,)), [make_persona()], candidates,
        )

        self.assertIn('not present in retrieved candidates', result['cells'][0].skipped_reason)
        self.pathway_cls.objects.create.assert_not_called()

    def test_no_career_candidates_skips_assembly(self):
        result = self._run(
            PathwayHarness(career_modes=(CAREER_MODE_AUTO,)), [make_persona()], candidates=[],
        )

        self.assertIn('no candidates', result['cells'][0].skipped_reason)

    def test_the_derived_intent_is_read_off_the_discovery_trace(self):
        """Reading the trace cannot disagree with what the pipeline actually used."""
        self._run(PathwayHarness(career_modes=(CAREER_MODE_AUTO,)), [make_persona()])

        kwargs = self.pathway_cls.generate_input_dict.call_args.kwargs
        self.assertEqual(kwargs['skills_required'], ['SQL'])
        self.assertEqual(kwargs['skills_preferred'], ['Tableau'])

    def test_the_pathway_outcome_is_recorded_on_the_cell(self):
        result = self._run(
            PathwayHarness(career_modes=(CAREER_MODE_AUTO,)), [make_persona()],
            output=pathway_output(keys=('A+1', 'B+2'), unfilled=['Advanced']),
        )

        cell = result['cells'][0]
        self.assertEqual(cell.course_keys, ['A+1', 'B+2'])
        self.assertTrue(cell.complete)
        self.assertEqual(cell.unfilled_rungs, ['Advanced'])

    def test_tier_one_violations_survive_onto_the_cell(self):
        result = self._run(
            PathwayHarness(career_modes=(CAREER_MODE_AUTO,)), [make_persona()],
            output=pathway_output(violations=['3 courses from P1 exceeds the cap of 2']),
        )

        self.assertEqual(len(result['cells'][0].violations), 1)

    def test_a_failing_cell_does_not_abandon_the_run(self):
        """One persona's broken dependency should not cost the other seven."""
        career_cls, _ = fake_career_workflow(self.candidates)
        career_cls.objects.create.return_value.execute.side_effect = [
            UnitOfWorkException('boom'), None,
        ]
        pathway_cls, _ = fake_pathway_workflow(pathway_output())

        with mock.patch(PATCH_CAREER_WORKFLOW, career_cls), \
                mock.patch(PATCH_PATHWAY_WORKFLOW, pathway_cls):
            result = PathwayHarness(career_modes=(CAREER_MODE_AUTO,)).run(
                [make_persona('p001'), make_persona('p002')],
            )

        self.assertTrue(result['cells'][0].error)
        self.assertTrue(result['cells'][1].ran)


class TestCareerSkillNames(TestCase):
    """
    Tests for ``PathwayHarness.career_skill_names``.
    """

    def test_both_flattened_names_and_raw_dicts_are_accepted(self):
        self.assertEqual(
            PathwayHarness.career_skill_names({'skills': ['Welding']}), ['Welding'],
        )
        self.assertEqual(
            PathwayHarness.career_skill_names({'skills': [{'name': 'Welding'}]}), ['Welding'],
        )

    def test_blanks_and_duplicates_are_dropped(self):
        names = PathwayHarness.career_skill_names(
            {'skills': ['Welding', ' Welding ', '', None, {'name': ''}]},
        )

        self.assertEqual(names, ['Welding'])

    def test_a_career_with_no_skills_yields_an_empty_list(self):
        """Two thirds of Lightcast careers carry no skills."""
        self.assertEqual(PathwayHarness.career_skill_names({}), [])


class TestRunPathwayHarnessCommand(TestCase):
    """
    Tests for the ``run_pathway_harness`` command.

    Writes its own persona fixtures rather than using the shipped set: those are real
    product-authored ground truth that changes as it is edited, and coupling command tests
    to a particular persona id makes those edits look like command regressions.
    """

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.persona_dir = Path(self._tmpdir.name)
        (self.persona_dir / 'p001-test.yaml').write_text(
            yaml.safe_dump(persona_dict('p001-test'), sort_keys=False),
        )

    def call(self, **kwargs):
        stdout = StringIO()
        kwargs.setdefault('persona_dir', str(self.persona_dir))
        call_command('run_pathway_harness', stdout=stdout, **kwargs)
        return stdout.getvalue()

    def test_a_dry_run_reports_the_plan_without_calling_anything(self):
        career_cls, _ = fake_career_workflow([])
        with mock.patch(PATCH_CAREER_WORKFLOW, career_cls):
            output = self.call(dry_run=True)

        self.assertIn('DRY RUN', output)
        self.assertIn('p001-test', output)
        career_cls.objects.create.assert_not_called()

    def test_an_unscoped_run_is_flagged(self):
        career_cls, _ = fake_career_workflow([])
        with mock.patch(PATCH_CAREER_WORKFLOW, career_cls):
            output = self.call(dry_run=True)

        self.assertIn('NOT scoped', output)

    def test_a_scoped_run_names_the_customer(self):
        career_cls, _ = fake_career_workflow([])
        uuid = '417306cb-b24a-4d06-b83c-fb2a61d7fb96'
        with mock.patch(PATCH_CAREER_WORKFLOW, career_cls):
            output = self.call(dry_run=True, customer_uuid=uuid)

        self.assertIn(uuid, output)

    def test_a_malformed_customer_uuid_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, 'not a UUID'):
            self.call(dry_run=True, customer_uuid='2u')

    def test_zero_runs_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, 'at least 1'):
            self.call(dry_run=True, runs=0)

    def test_traces_are_exported_for_scoring(self):
        career_cls, _ = fake_career_workflow(
            [{'external_id': CAREER_ID, 'name': 'Data Analyst', 'skills': ['SQL']}],
        )
        pathway_cls, _ = fake_pathway_workflow(pathway_output())

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'nested' / 'traces.json'
            with mock.patch(PATCH_CAREER_WORKFLOW, career_cls), \
                    mock.patch(PATCH_PATHWAY_WORKFLOW, pathway_cls):
                self.call(output_json=str(path))
            payload = json.loads(path.read_text())

        self.assertIn('cells', payload)
        self.assertIn('run_config', payload)
        self.assertEqual(payload['cells'][0]['persona_id'], 'p001-test')

    def test_the_command_says_it_produces_traces_not_a_verdict(self):
        """
        The separation is the point: a re-score must never need a re-run, because a run
        costs money.
        """
        career_cls, _ = fake_career_workflow([])
        with mock.patch(PATCH_CAREER_WORKFLOW, career_cls):
            output = self.call(dry_run=True)

        self.assertIn('report_pathway_harness', output)
