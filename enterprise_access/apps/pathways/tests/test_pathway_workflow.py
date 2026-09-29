"""
Tests for the pathway assembly steps and the workflow that composes them.

The end-to-end tests here exercise the whole five-step chain against patched externals,
which is the only place the conditional-skip behaviour and the accumulated-output
plumbing are checked together.
"""
import json
from unittest import mock
from uuid import uuid4

from django.test import TestCase
from edx_toggles.toggles.testutils import override_waffle_switch

from enterprise_access.apps.pathways.model_backends import ModelBackendRequestError
from enterprise_access.apps.pathways.models import (
    AssemblePathwayInput,
    AssemblePathwayOutput,
    AssemblePathwayStep,
    AssemblePathwayStepException,
    BuildVariantsInput,
    BuildVariantsOutput,
    BuildVariantsStep,
    CourseCandidate,
    EnrichRationaleInput,
    EnrichRationaleStep,
    EnrichRationaleStepException,
    JudgePathwaysInput,
    JudgePathwaysOutput,
    JudgePathwaysStep,
    JudgePathwaysStepException,
    PathwayAssemblyWorkflow,
    PathwayCourse,
    PathwayJudgement,
    PathwayVariant,
    RerankCandidatesInput,
    RerankCandidatesOutput,
    RerankCandidatesStep,
    RetrieveCandidatesInput,
    RetrieveCandidatesOutput,
    RetrieveCandidatesStep,
    RetrieveCandidatesStepException,
    TranslateToCatalogOutput
)
from enterprise_access.apps.pathways.pathway_variants import estimated_model_calls
from enterprise_access.apps.pathways.tests.test_pathway_variants import FakePlanner, FakePolicy, FakeSeat
from enterprise_access.apps.pathways.tests.test_reranking import FakeBackend
from enterprise_access.apps.prompts.api import PromptError
from enterprise_access.apps.prompts.api_client import XpertAPIError
from enterprise_access.toggles import LEARNER_PATHWAYS_DISABLE_CANDIDATE_RERANK

PATCH_RETRIEVE = 'enterprise_access.apps.pathways.course_retrieval.retrieve_candidate_courses'
PATCH_RERANK = 'enterprise_access.apps.pathways.reranking.rerank_candidates'
PATCH_SNAPSHOT = 'enterprise_access.apps.pathways.catalog_translation.snapshot_catalog_facets'
PATCH_ENRICH = 'enterprise_access.apps.pathways.models.pathways_api.enrich_rationales'
PATCH_JUDGE = 'enterprise_access.apps.pathways.models.judging.judge_pathway'
PATCH_VARIANT_BACKEND = 'enterprise_access.apps.pathways.pathway_variants.get_variant_backend'

CUSTOMER_UUID = '417306cb-b24a-4d06-b83c-fb2a61d7fb96'


class Accumulator:
    """Stands in for the workflow's dynamically-built accumulated-output object."""

    def __init__(self, **outputs):
        for key, value in outputs.items():
            setattr(self, key, value)


def course_hit(key, *, title=None, level='Introductory', partner='edX', language='English'):
    return {
        'key': key,
        'title': title or f'Course {key}',
        'short_description': 'short',
        'full_description': 'long',
        'level_type': level,
        'partners': [{'name': partner}],
        'language': language,
    }


def spanning_hits():
    """Six candidates that can fill a 2/2/1 quota across four providers."""
    return [
        course_hit('A+1', partner='P1'),
        course_hit('A+2', partner='P1'),
        course_hit('A+3', partner='P2'),
        course_hit('B+1', level='Intermediate', partner='P3'),
        course_hit('B+2', level='Intermediate', partner='P4'),
        course_hit('C+1', level='Advanced', partner='P4'),
    ]


def retrieval_result(hits, **overrides):
    return {
        'query': 'Welder Welding',
        'hit_count': len(hits),
        'courses': hits,
        'strict_filters_applied': ['Welding'],
        'strict_hit_count': len(hits),
        'strict_rungs_spanned': len({h['level_type'] for h in hits}),
        'broadened': False,
        'zero_hits': not hits,
        **overrides,
    }


def translation_output():
    return TranslateToCatalogOutput(strict=[], boost=[], unresolved=[], resolution_rate=1.0)


def judge_result(verdict='good', keys=(), error=''):
    """What ``judging.judge_pathway`` returns."""
    return {
        'verdict': verdict if not error else '', 'reason': 'Fits.' if not error else '',
        'on_topic': {key: True for key in keys}, 'fabricated_keys': [],
        'unjudged_keys': [], 'error': error, 'n_on_topic': len(keys), 'n_courses': len(keys),
        'trace': {'backend': 'openai', 'model': 'gpt-5.4-mini', 'elapsed_ms': 3},
    }


class TestCourseCandidate(TestCase):
    """
    Tests for ``CourseCandidate``.
    """

    def test_a_hit_round_trips_through_the_assembly_shape(self):
        candidate = CourseCandidate.from_hit(course_hit('A+1', partner='edX'))

        assembly_hit = candidate.to_assembly_hit()

        self.assertEqual(assembly_hit['key'], 'A+1')
        self.assertEqual(assembly_hit['partners'], [{'name': 'edX'}])
        self.assertEqual(assembly_hit['language'], 'English')

    def test_an_unattributed_course_yields_no_partner_entry(self):
        candidate = CourseCandidate.from_hit({'key': 'A+1', 'partners': []})

        self.assertEqual(candidate.to_assembly_hit()['partners'], [])

    def test_descriptions_are_truncated_before_persistence(self):
        """A trace holding five full marketing descriptions is a blob, not a trace."""
        candidate = CourseCandidate.from_hit(
            {'key': 'A+1', 'full_description': 'x' * 10000, 'short_description': 'y' * 10000},
        )

        self.assertLess(len(candidate.full_description), 10000)
        self.assertLess(len(candidate.short_description), 10000)

    def test_skill_tags_are_kept_for_the_judge_blanks_dropped_and_capped(self):
        candidate = CourseCandidate.from_hit({
            'key': 'A+1', 'skill_names': [' Welding ', '', None] + [f'S{n}' for n in range(20)],
        })

        self.assertEqual(candidate.skill_names[0], 'Welding')
        self.assertEqual(len(candidate.skill_names), 10)

    def test_a_candidate_persisted_before_skill_tags_existed_still_loads(self):
        candidate = CourseCandidate.from_dict({'key': 'A+1', 'title': 'Old'})

        self.assertEqual(candidate.skill_names, [])


class TestRetrieveCandidatesStep(TestCase):
    """
    Tests for ``RetrieveCandidatesStep``.
    """

    def _step(self, **kwargs):
        kwargs.setdefault('career_name', 'Welder')
        return RetrieveCandidatesStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=RetrieveCandidatesInput(**kwargs).to_dict(),
        )

    @mock.patch(PATCH_RETRIEVE)
    def test_candidates_are_persisted_with_the_query_and_hit_count(self, mock_retrieve):
        mock_retrieve.return_value = retrieval_result([course_hit('A+1')])

        output = self._step().execute(accumulated_output=Accumulator(
            translate_to_catalog_output=translation_output(),
        ))

        self.assertEqual([c.key for c in output.courses], ['A+1'])
        self.assertEqual(output.query, 'Welder Welding')
        self.assertEqual(output.hit_count, 1)
        self.assertFalse(output.zero_hits)

    @mock.patch(PATCH_RETRIEVE)
    def test_the_customer_scope_is_passed_through(self, mock_retrieve):
        mock_retrieve.return_value = retrieval_result([])

        self._step(customer_uuid=CUSTOMER_UUID).execute(accumulated_output=Accumulator(
            translate_to_catalog_output=translation_output(),
        ))

        self.assertEqual(mock_retrieve.call_args.kwargs['customer_uuid'], CUSTOMER_UUID)

    @mock.patch(PATCH_RETRIEVE)
    def test_zero_hits_is_recorded_rather_than_raised(self, mock_retrieve):
        """
        Replaces the plan's "scope-only fallback is recorded" scenario, which measured a
        ladder step this design removed.
        """
        mock_retrieve.return_value = retrieval_result([], broadened=True)

        output = self._step().execute(accumulated_output=Accumulator(
            translate_to_catalog_output=translation_output(),
        ))

        self.assertTrue(output.zero_hits)
        self.assertTrue(output.broadened)
        self.assertEqual(output.courses, [])

    def test_a_missing_translation_fails_the_step_explicitly(self):
        step = self._step()

        with self.assertRaises(RetrieveCandidatesStepException) as ctx:
            step.execute(accumulated_output=Accumulator())

        self.assertIn('catalog translation', str(ctx.exception))


class TestRerankCandidatesStep(TestCase):
    """
    Tests for ``RerankCandidatesStep``, including its skip conditions.
    """

    def _step(self, **kwargs):
        kwargs.setdefault('career_name', 'Welder')
        return RerankCandidatesStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=RerankCandidatesInput(**kwargs).to_dict(),
        )

    def test_it_is_skipped_when_disabled(self):
        """A disabled re-rank is the baseline arm of the A/B, not a broken run."""
        workflow = mock.Mock(input_data={RerankCandidatesInput.KEY: {'enabled': False}})
        accumulated = Accumulator(retrieve_candidates_output=RetrieveCandidatesOutput(
            courses=[CourseCandidate(key='A+1')],
        ))

        self.assertFalse(RerankCandidatesStep.should_execute(accumulated, workflow))

    def test_it_is_skipped_when_there_are_no_candidates(self):
        workflow = mock.Mock(input_data={RerankCandidatesInput.KEY: {'enabled': True}})
        accumulated = Accumulator(retrieve_candidates_output=RetrieveCandidatesOutput(courses=[]))

        self.assertFalse(RerankCandidatesStep.should_execute(accumulated, workflow))

    def test_it_runs_when_enabled_with_candidates(self):
        workflow = mock.Mock(input_data={RerankCandidatesInput.KEY: {'enabled': True}})
        accumulated = Accumulator(retrieve_candidates_output=RetrieveCandidatesOutput(
            courses=[CourseCandidate(key='A+1')],
        ))

        self.assertTrue(RerankCandidatesStep.should_execute(accumulated, workflow))

    @override_waffle_switch(LEARNER_PATHWAYS_DISABLE_CANDIDATE_RERANK, True)
    def test_the_admin_kill_switch_stops_it_even_when_the_caller_asked_for_it(self):
        """
        The administrator switch overrides the workflow's own ``enabled`` input.

        That ordering is the point of the switch: the harness and any other offline
        caller supply their own input, so a toggle that only narrowed the request path
        would not actually stop paid model calls.
        """
        workflow = mock.Mock(input_data={RerankCandidatesInput.KEY: {'enabled': True}})
        accumulated = Accumulator(retrieve_candidates_output=RetrieveCandidatesOutput(
            courses=[CourseCandidate(key='A+1')],
        ))

        self.assertFalse(RerankCandidatesStep.should_execute(accumulated, workflow))

    @mock.patch(PATCH_RERANK)
    def test_the_model_trace_is_persisted_on_the_output(self, mock_rerank):
        mock_rerank.return_value = {
            'ordered_keys': ['B+2', 'A+1'],
            'rationales': {'B+2': 'closest fit'},
            'fabricated_keys': ['Nope+1'],
            'prompt_revision': '7',
            'trace': {'backend': 'xpert', 'model': 'candidate_rerank',
                      'input_tokens': 100, 'output_tokens': 20, 'elapsed_ms': 350},
        }

        output = self._step().execute(accumulated_output=Accumulator(
            retrieve_candidates_output=RetrieveCandidatesOutput(
                courses=[CourseCandidate(key='A+1'), CourseCandidate(key='B+2')],
            ),
        ))

        self.assertEqual(output.ordered_keys, ['B+2', 'A+1'])
        self.assertEqual(output.fabricated_keys, ['Nope+1'])
        self.assertEqual(output.backend, 'xpert')
        self.assertEqual(output.prompt_revision, '7')
        self.assertEqual(output.elapsed_ms, 350)
        self.assertTrue(output.executed)


class TestAssemblePathwayStep(TestCase):
    """
    Tests for ``AssemblePathwayStep``.
    """

    def _step(self):
        return AssemblePathwayStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=AssemblePathwayInput().to_dict(),
        )

    def _candidates(self, hits=None):
        return RetrieveCandidatesOutput(
            courses=[CourseCandidate.from_hit(hit) for hit in (hits or spanning_hits())],
        )

    def test_five_courses_are_selected_and_pass_the_tier_one_gates(self):
        output = self._step().execute(accumulated_output=Accumulator(
            retrieve_candidates_output=self._candidates(),
        ))

        self.assertTrue(output.complete)
        self.assertEqual(len(output.courses), 5)
        self.assertEqual(output.violations, [])
        self.assertEqual(output.level_mix, {'Introductory': 2, 'Intermediate': 2, 'Advanced': 1})

    def test_the_rerank_order_is_applied_when_it_ran(self):
        output = self._step().execute(accumulated_output=Accumulator(
            retrieve_candidates_output=self._candidates(),
            rerank_candidates_output=RerankCandidatesOutput(
                ordered_keys=['A+3', 'A+1', 'A+2', 'B+1', 'B+2', 'C+1'],
                rationales={'A+3': 'best starting point'},
                executed=True,
            ),
        ))

        intro = [c for c in output.courses if c.level_type == 'Introductory']
        self.assertEqual(intro[0].key, 'A+3')
        self.assertEqual(intro[0].rationale, 'best starting point')

    def test_unranked_candidates_are_kept_behind_the_ranked_ones(self):
        """
        The model may return fewer keys than it was given; dropping the remainder would
        shrink the window assembly needs to span the rungs.
        """
        output = self._step().execute(accumulated_output=Accumulator(
            retrieve_candidates_output=self._candidates(),
            rerank_candidates_output=RerankCandidatesOutput(
                ordered_keys=['C+1'], executed=True,
            ),
        ))

        self.assertTrue(output.complete)
        self.assertEqual(len(output.courses), 5)

    def test_a_skipped_rerank_still_yields_a_pathway(self):
        """Chunk 9a's assembly is sufficient on its own -- that is the baseline arm."""
        output = self._step().execute(accumulated_output=Accumulator(
            retrieve_candidates_output=self._candidates(),
        ))

        self.assertTrue(output.complete)
        self.assertTrue(all(course.rationale == '' for course in output.courses))

    def test_too_few_candidates_yields_an_incomplete_pathway_not_a_padded_one(self):
        output = self._step().execute(accumulated_output=Accumulator(
            retrieve_candidates_output=self._candidates([course_hit('A+1')]),
        ))

        self.assertFalse(output.complete)
        self.assertEqual(len(output.courses), 1)

    def test_ineligible_candidates_are_reported(self):
        output = self._step().execute(accumulated_output=Accumulator(
            retrieve_candidates_output=self._candidates(
                spanning_hits() + [course_hit('D+1', language='Spanish')],
            ),
        ))

        self.assertEqual(output.ineligible.get('unsupported_language'), 1)

    def test_a_missing_candidate_set_fails_the_step_explicitly(self):
        step = self._step()

        with self.assertRaises(AssemblePathwayStepException):
            step.execute(accumulated_output=Accumulator())

    def test_output_round_trips_through_the_database(self):
        step = self._step()

        step.execute(accumulated_output=Accumulator(
            retrieve_candidates_output=self._candidates(),
        ))
        step.refresh_from_db()

        self.assertEqual(len(step.output_object.courses), 5)


class TestEnrichRationaleStep(TestCase):
    """
    Tests for ``EnrichRationaleStep``.
    """

    def _step(self, **kwargs):
        kwargs.setdefault('selected_career', 'Welder')
        return EnrichRationaleStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=EnrichRationaleInput(**kwargs).to_dict(),
        )

    def _assembled(self, complete=True, keys=('A+1', 'B+1')):
        return AssemblePathwayOutput(
            courses=[PathwayCourse(key=key, title=key) for key in keys],
            complete=complete,
        )

    def test_it_is_skipped_when_there_is_no_pathway_to_explain(self):
        workflow = mock.Mock(input_data={EnrichRationaleInput.KEY: {'enabled': True}})
        accumulated = Accumulator(assemble_pathway_output=self._assembled(complete=False))

        self.assertFalse(EnrichRationaleStep.should_execute(accumulated, workflow))

    def test_it_is_skipped_when_disabled(self):
        workflow = mock.Mock(input_data={EnrichRationaleInput.KEY: {'enabled': False}})
        accumulated = Accumulator(assemble_pathway_output=self._assembled())

        self.assertFalse(EnrichRationaleStep.should_execute(accumulated, workflow))

    def test_it_runs_when_a_complete_pathway_exists(self):
        workflow = mock.Mock(input_data={EnrichRationaleInput.KEY: {'enabled': True}})
        accumulated = Accumulator(assemble_pathway_output=self._assembled())

        self.assertTrue(EnrichRationaleStep.should_execute(accumulated, workflow))

    @mock.patch(PATCH_ENRICH)
    def test_only_the_delivered_courses_are_sent_for_explanation(self, mock_enrich):
        """
        Not the candidate twenty. Four fifths of the explanation work would be paid for
        and thrown away.
        """
        mock_enrich.return_value = {'reasons': {}, 'prompt_revision': '3'}

        self._step().execute(accumulated_output=Accumulator(
            assemble_pathway_output=self._assembled(keys=('A+1', 'B+1')),
        ))

        self.assertEqual(mock_enrich.call_args.kwargs['course_keys'], ['A+1', 'B+1'])

    @mock.patch(PATCH_ENRICH)
    def test_reasons_and_the_prompt_revision_are_persisted(self, mock_enrich):
        mock_enrich.return_value = {'reasons': {'A+1': 'because'}, 'prompt_revision': '9'}

        step = self._step()
        step.execute(accumulated_output=Accumulator(
            assemble_pathway_output=self._assembled(),
        ))
        step.refresh_from_db()

        self.assertEqual(step.output_object.reasons, {'A+1': 'because'})
        self.assertEqual(step.output_object.prompt_revision, '9')
        self.assertTrue(step.output_object.executed)

    @mock.patch(PATCH_ENRICH)
    def test_a_prompt_failure_is_recorded_rather_than_raised(self, mock_enrich):
        """
        A pathway with no rationales is still a pathway. Losing the explanations is a much
        smaller loss than losing the recommendation.
        """
        mock_enrich.side_effect = PromptError('no prompt configured')

        output = self._step().execute(accumulated_output=Accumulator(
            assemble_pathway_output=self._assembled(),
        ))

        self.assertIn('PromptError', output.error)
        self.assertEqual(output.reasons, {})
        self.assertTrue(output.executed)

    @mock.patch(PATCH_ENRICH)
    def test_an_xpert_failure_is_also_recorded_rather_than_raised(self, mock_enrich):
        mock_enrich.side_effect = XpertAPIError('upstream down')

        output = self._step().execute(accumulated_output=Accumulator(
            assemble_pathway_output=self._assembled(),
        ))

        self.assertIn('XpertAPIError', output.error)

    def test_a_missing_assembly_fails_the_step_explicitly(self):
        step = self._step()

        with self.assertRaises(EnrichRationaleStepException):
            step.execute(accumulated_output=Accumulator())


class PathwayWorkflowMixin:
    """Builds a workflow and patches its externals, for the end-to-end test classes."""

    def _workflow(self, **kwargs):
        kwargs.setdefault('career_name', 'Welder')
        kwargs.setdefault('career_skills', ['Welding'])
        return PathwayAssemblyWorkflow.objects.create(
            input_data=PathwayAssemblyWorkflow.generate_input_dict(**kwargs),
        )

    def _patches(self, hits=None, rerank=None, enrich=None):
        """Context managers patching the three external-touching functions."""
        snapshot = mock.patch(PATCH_SNAPSHOT, return_value={
            'skill_names': ['Welding'], 'skills.name': [], 'subjects': [], 'truncated': [],
        })
        retrieve = mock.patch(PATCH_RETRIEVE, return_value=retrieval_result(
            hits if hits is not None else spanning_hits(),
        ))
        rerank_patch = mock.patch(PATCH_RERANK, return_value=rerank or {
            'ordered_keys': [], 'rationales': {}, 'fabricated_keys': [],
            'prompt_revision': '', 'trace': {},
        })
        enrich_patch = mock.patch(PATCH_ENRICH, return_value=enrich or {
            'reasons': {}, 'prompt_revision': '',
        })
        return snapshot, retrieve, rerank_patch, enrich_patch


class TestPathwayAssemblyWorkflow(PathwayWorkflowMixin, TestCase):
    """
    End-to-end tests for the five-step workflow.
    """

    def test_a_pathway_is_produced_end_to_end(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow()

        with snapshot, retrieve, rerank, enrich:
            workflow.execute()

        pathway = workflow.pathway()
        self.assertIsNotNone(pathway)
        self.assertEqual(len(pathway['courses']), 5)
        self.assertEqual(pathway['violations'], [])

    def test_every_step_is_inspectable_afterwards(self):
        """
        Scenario: The composition is inspectable afterwards.

        The experiment steps skip unless asked for, so this run asks for both -- with the
        free variant arm and a patched judge -- to reach every step.
        """
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow(variant_sizes=[3], judge_enabled=True)

        with snapshot, retrieve, rerank, enrich, mock.patch(PATCH_JUDGE, return_value=judge_result()):
            workflow.execute()

        for step_class in PathwayAssemblyWorkflow.steps:
            record = step_class.objects.filter(workflow_record_uuid=workflow.uuid).first()
            self.assertIsNotNone(record, f'{step_class.__name__} left no record')
            self.assertIsNotNone(record.input_data)

    def test_a_disabled_rerank_skips_the_model_call_but_still_delivers(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow(rerank_enabled=False)

        with snapshot, retrieve, enrich, rerank as mock_rerank:
            workflow.execute()

        mock_rerank.assert_not_called()
        self.assertEqual(len(workflow.pathway()['courses']), 5)

    def test_a_career_with_no_courses_yields_no_pathway_rather_than_failing(self):
        snapshot, retrieve, rerank, enrich = self._patches(hits=[])
        workflow = self._workflow(career_name='Underwater Basket Weaver')

        with snapshot, retrieve, enrich, rerank as mock_rerank:
            workflow.execute()

        # Nothing to re-rank, so the model call is skipped rather than paid for.
        mock_rerank.assert_not_called()
        self.assertIsNone(workflow.pathway())

    def test_a_skipped_step_serializes_as_null(self):
        """
        Guards the cattrs defect that made ``Optional`` necessary on the generated IO
        classes: a skipped step's output must round-trip as null, not crash.
        """
        snapshot, retrieve, rerank, enrich = self._patches(hits=[])
        workflow = self._workflow()

        with snapshot, retrieve, rerank, enrich:
            workflow.execute()

        workflow.refresh_from_db()
        self.assertIsNone(workflow.output_data.get(RerankCandidatesOutput.KEY))

    def test_rationales_from_enrichment_reach_the_delivered_pathway(self):
        """
        Merged in ``pathway()`` rather than by the assembly step, which runs earlier and
        must not depend on a later step.
        """
        snapshot, retrieve, rerank, enrich = self._patches(
            enrich={'reasons': {'A+1': 'a solid starting point'}, 'prompt_revision': '4'},
        )
        workflow = self._workflow()

        with snapshot, retrieve, rerank, enrich:
            workflow.execute()

        rationales = {c['key']: c['rationale'] for c in workflow.pathway()['courses']}
        self.assertEqual(rationales.get('A+1'), 'a solid starting point')

    def test_a_course_without_a_rationale_still_ships(self):
        snapshot, retrieve, rerank, enrich = self._patches(
            enrich={'reasons': {'A+1': 'only this one'}, 'prompt_revision': ''},
        )
        workflow = self._workflow()

        with snapshot, retrieve, rerank, enrich:
            workflow.execute()

        courses = workflow.pathway()['courses']
        self.assertEqual(len(courses), 5)
        self.assertEqual(len([c for c in courses if not c['rationale']]), 4)

    def test_enrichment_is_skipped_when_there_is_no_pathway(self):
        """No pathway means nothing to explain, so the paid call is not made."""
        snapshot, retrieve, rerank, enrich = self._patches(hits=[])
        workflow = self._workflow()

        with snapshot, retrieve, rerank, enrich as mock_enrich:
            workflow.execute()

        mock_enrich.assert_not_called()

    def test_a_disabled_enrichment_still_delivers_a_pathway(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow(enrich_enabled=False)

        with snapshot, retrieve, rerank, enrich as mock_enrich:
            workflow.execute()

        mock_enrich.assert_not_called()
        self.assertEqual(len(workflow.pathway()['courses']), 5)

    def test_the_rerank_order_reaches_assembly(self):
        snapshot, retrieve, rerank, enrich = self._patches(rerank={
            'ordered_keys': ['C+1', 'B+1', 'A+3', 'A+1', 'A+2', 'B+2'],
            'rationales': {'A+3': 'start here'},
            'fabricated_keys': [], 'prompt_revision': '3',
            'trace': {'backend': 'xpert', 'model': 'candidate_rerank', 'elapsed_ms': 1},
        })
        workflow = self._workflow()

        with snapshot, retrieve, rerank, enrich:
            workflow.execute()

        rationales = {c['key']: c['rationale'] for c in workflow.pathway()['courses']}
        self.assertEqual(rationales.get('A+3'), 'start here')


class TestPathwayExperimentInputs(TestCase):
    """
    Scenario: A run's experiment request is normalised before it is persisted.
    """

    def _inputs(self, **kwargs):
        data = PathwayAssemblyWorkflow.generate_input_dict(career_name='Welder', **kwargs)
        return data[BuildVariantsInput.KEY], data[JudgePathwaysInput.KEY]

    def test_by_default_nothing_is_requested(self):
        variants, judge = self._inputs()

        self.assertEqual((variants['sizes'], variants['strategies']), ([], []))
        self.assertFalse(judge['enabled'])

    def test_sizes_alone_run_the_free_ranked_arm(self):
        variants, _ = self._inputs(variant_sizes=[4, 2, 4])

        self.assertEqual(variants['sizes'], [2, 4])
        self.assertEqual(variants['strategies'], ['ranked_cut'])

    def test_strategies_alone_run_every_size(self):
        variants, _ = self._inputs(variant_strategies=['model_sized'])

        self.assertEqual(variants['sizes'], [2, 3, 4, 5])

    def test_an_invalid_size_is_refused_before_anything_is_persisted(self):
        with self.assertRaises(ValueError):
            self._inputs(variant_sizes=[7])

    def test_shapes_are_persisted_beside_the_size_arms(self):
        variants, _ = self._inputs(
            variant_strategies=['model_sized', 'shape_pick'], variant_shapes=['0/2/0', '2/0/0'],
        )

        self.assertEqual(variants['strategies'], ['model_sized', 'shape_pick'])
        self.assertEqual(variants['shapes'], ['0/2/0', '2/0/0'])

    def test_a_malformed_shape_is_refused_before_anything_is_persisted(self):
        with self.assertRaises(ValueError):
            self._inputs(variant_shapes=['2/2'])


class TestBuildVariantsStep(TestCase):
    """
    Tests for ``BuildVariantsStep``.
    """

    def _step(self, **input_data):
        data = {'career_name': 'Welder', 'career_skills': ['Welding'], **input_data}
        return BuildVariantsStep.objects.create(workflow_record_uuid=uuid4(), input_data=data)

    @staticmethod
    def _accumulated(hits=None, rerank=None):
        return Accumulator(**{
            RetrieveCandidatesOutput.KEY: RetrieveCandidatesOutput(
                courses=[
                    CourseCandidate.from_hit(hit)
                    for hit in (spanning_hits() if hits is None else hits)
                ],
            ),
            RerankCandidatesOutput.KEY: rerank,
        })

    def test_it_is_skipped_when_no_strategy_was_requested(self):
        workflow = mock.Mock(input_data={BuildVariantsInput.KEY: {'sizes': [3], 'strategies': []}})

        self.assertFalse(BuildVariantsStep.should_execute(self._accumulated(), workflow))

    def test_it_is_skipped_when_there_are_no_candidates(self):
        workflow = mock.Mock(input_data={BuildVariantsInput.KEY: {'strategies': ['ranked_cut']}})

        self.assertFalse(BuildVariantsStep.should_execute(self._accumulated(hits=[]), workflow))

    def test_the_ranked_arm_follows_the_rerank_order(self):
        rerank = RerankCandidatesOutput(ordered_keys=['C+1', 'B+2'], executed=True)
        step = self._step(sizes=[2], strategies=['ranked_cut'])

        output = step.process_input(accumulated_output=self._accumulated(rerank=rerank))

        variant = output.variants[0]
        self.assertEqual(variant.label, 'ranked_cut:2')
        self.assertEqual(sorted(course.key for course in variant.courses), ['B+2', 'C+1'])
        self.assertTrue(variant.complete)
        self.assertEqual(variant.violations, [])

    def test_a_failed_model_arm_is_recorded_beside_the_arms_that_worked(self):
        failing = mock.Mock()
        failing.complete.side_effect = ModelBackendRequestError('down')
        step = self._step(sizes=[2], strategies=['ranked_cut', 'model_pick'])

        with mock.patch(PATCH_VARIANT_BACKEND, return_value=failing):
            output = step.process_input(accumulated_output=self._accumulated())

        ranked, picked = output.variants
        self.assertTrue(ranked.complete)
        self.assertEqual(picked.courses, [])
        self.assertIn('ModelBackendRequestError', picked.error)
        self.assertEqual(picked.violations, [])

    def test_a_model_arm_records_its_trace(self):
        backend = FakeBackend(content=json.dumps({'keys': ['A+1', 'B+1', 'C+1']}))
        step = self._step(strategies=['model_sized'])

        with mock.patch(PATCH_VARIANT_BACKEND, return_value=backend):
            output = step.process_input(accumulated_output=self._accumulated())

        variant = output.variants[0]
        self.assertEqual(variant.label, 'model_sized:2-5')
        self.assertIsNone(variant.requested_size)
        self.assertEqual((variant.model, variant.input_tokens), ('fake-1', 10))
        self.assertIn(str(step.uuid), backend.calls[0]['trace_id'])

    def test_a_shape_arm_records_its_shape(self):
        step = self._step(strategies=['shape_cut'], shapes=['1/1/1'])

        output = step.process_input(accumulated_output=self._accumulated())

        variant = output.variants[0]
        self.assertEqual((variant.label, variant.shape, variant.requested_size), ('shape_cut:1/1/1', '1/1/1', 3))
        self.assertEqual(variant.level_mix, {'Introductory': 1, 'Intermediate': 1, 'Advanced': 1})
        self.assertTrue(variant.complete)

    def test_an_input_persisted_before_shapes_existed_still_loads(self):
        restored = BuildVariantsInput.from_dict({'career_name': 'Welder', 'sizes': [2], 'strategies': ['ranked_cut']})

        self.assertEqual(restored.shapes, [])

    def test_output_round_trips_through_the_database(self):
        step = self._step(sizes=[2, 3], strategies=['ranked_cut'])
        output = step.process_input(accumulated_output=self._accumulated())

        restored = BuildVariantsOutput.from_dict(output.to_dict())

        self.assertEqual([v.label for v in restored.variants], ['ranked_cut:2', 'ranked_cut:3'])


class TestJudgePathwaysStep(TestCase):
    """
    Tests for ``JudgePathwaysStep``.
    """

    def _step(self):
        return JudgePathwaysStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data={'career_name': 'Welder', 'career_skills': ['Welding'], 'enabled': True},
        )

    @staticmethod
    def _accumulated(default_keys=('A+1', 'A+2', 'B+1', 'B+2', 'C+1'), variants=()):
        courses = [CourseCandidate.from_hit(hit) for hit in spanning_hits()]
        return Accumulator(**{
            RetrieveCandidatesOutput.KEY: RetrieveCandidatesOutput(courses=courses),
            AssemblePathwayOutput.KEY: AssemblePathwayOutput(
                courses=[PathwayCourse(key=key) for key in default_keys],
                complete=bool(default_keys),
            ),
            BuildVariantsOutput.KEY: BuildVariantsOutput.from_dict({'variants': [
                {'label': label, 'strategy': label.split(':')[0],
                 'courses': [{'key': key} for key in keys]}
                for label, keys in variants
            ]}),
        })

    def test_it_is_skipped_unless_enabled(self):
        workflow = mock.Mock(input_data={JudgePathwaysInput.KEY: {'enabled': False}})

        self.assertFalse(JudgePathwaysStep.should_execute(self._accumulated(), workflow))

    def test_it_is_skipped_when_nothing_is_long_enough_to_judge(self):
        workflow = mock.Mock(input_data={JudgePathwaysInput.KEY: {'enabled': True}})
        accumulated = self._accumulated(default_keys=(), variants=[('model_sized:2-5', ['A+1'])])

        self.assertFalse(JudgePathwaysStep.should_execute(accumulated, workflow))

    def test_the_delivered_pathway_and_each_variant_are_judged_with_course_details(self):
        step = self._step()
        accumulated = self._accumulated(variants=[('ranked_cut:2', ['A+1', 'B+1'])])

        with mock.patch(PATCH_JUDGE, return_value=judge_result(keys=['A+1'])) as mock_judge:
            output = step.process_input(accumulated_output=accumulated)

        self.assertEqual([j.label for j in output.judgements], ['default', 'ranked_cut:2'])
        courses_sent = mock_judge.call_args_list[1].kwargs['courses']
        self.assertEqual(courses_sent[0]['short_description'], 'short')
        self.assertIn(':ranked_cut:2', mock_judge.call_args_list[1].kwargs['trace_id'])
        self.assertEqual(output.judgements[0].model, 'gpt-5.4-mini')

    def test_an_identical_course_list_reuses_the_verdict_instead_of_paying_twice(self):
        step = self._step()
        default = ('A+1', 'A+2', 'B+1', 'B+2', 'C+1')
        accumulated = self._accumulated(variants=[('ranked_cut:5', list(default))])

        with mock.patch(PATCH_JUDGE, return_value=judge_result()) as mock_judge:
            output = step.process_input(accumulated_output=accumulated)

        self.assertEqual(mock_judge.call_count, 1)
        self.assertEqual(output.judgements[1].same_as, 'default')
        self.assertEqual(output.judgements[1].verdict, 'good')

    def test_a_failed_judgement_is_recorded_and_not_reused(self):
        step = self._step()
        default = ('A+1', 'A+2', 'B+1', 'B+2', 'C+1')
        accumulated = self._accumulated(variants=[('ranked_cut:5', list(default))])

        with mock.patch(PATCH_JUDGE, return_value=judge_result(error='judge request failed')) as mock_judge:
            output = step.process_input(accumulated_output=accumulated)

        self.assertEqual(mock_judge.call_count, 2)
        self.assertEqual(output.judgements[0].error, 'judge request failed')
        self.assertEqual(output.judgements[1].same_as, '')

    def test_a_variant_below_two_courses_is_not_judged(self):
        step = self._step()
        accumulated = self._accumulated(variants=[('model_sized:2-5', ['A+1'])])

        with mock.patch(PATCH_JUDGE, return_value=judge_result()):
            output = step.process_input(accumulated_output=accumulated)

        self.assertEqual([j.label for j in output.judgements], ['default'])

    def test_output_round_trips_through_the_database(self):
        step = self._step()
        with mock.patch(PATCH_JUDGE, return_value=judge_result(keys=['A+1'])):
            output = step.process_input(accumulated_output=self._accumulated())

        restored = JudgePathwaysOutput.from_dict(output.to_dict())

        self.assertEqual(restored.judgements[0].on_topic, {'A+1': True})


class TestPathwayExperimentsEndToEnd(PathwayWorkflowMixin, TestCase):
    """
    Scenario: Experiments run beside the delivered pathway without changing it.
    """

    def test_variants_and_judgements_never_change_the_delivered_pathway(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        plain = self._workflow()
        with snapshot, retrieve, rerank, enrich:
            plain.execute()

        snapshot, retrieve, rerank, enrich = self._patches()
        experimented = self._workflow(variant_sizes=[2, 3], judge_enabled=True)
        with snapshot, retrieve, rerank, enrich, mock.patch(PATCH_JUDGE, return_value=judge_result()):
            experimented.execute()

        self.assertEqual(
            [c['key'] for c in plain.pathway()['courses']],
            [c['key'] for c in experimented.pathway()['courses']],
        )

    def test_variants_carry_their_judgement(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow(variant_sizes=[2], judge_enabled=True)

        with snapshot, retrieve, rerank, enrich, mock.patch(PATCH_JUDGE, return_value=judge_result()):
            workflow.execute()

        variants = workflow.variants()
        self.assertEqual([v['label'] for v in variants], ['ranked_cut:2'])
        self.assertEqual(variants[0]['judgement']['verdict'], 'good')
        self.assertEqual(workflow.default_judgement()['label'], 'default')

    def test_a_run_without_experiments_leaves_no_experiment_records(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow()

        with snapshot, retrieve, rerank, enrich, mock.patch(PATCH_JUDGE) as mock_judge:
            workflow.execute()

        mock_judge.assert_not_called()
        self.assertFalse(BuildVariantsStep.objects.filter(workflow_record_uuid=workflow.uuid).exists())
        self.assertFalse(JudgePathwaysStep.objects.filter(workflow_record_uuid=workflow.uuid).exists())
        self.assertEqual(workflow.variants(), [])
        self.assertIsNone(workflow.default_judgement())


# ---------------------------------------------------------------------------------------
# Editorial policy, shape_pick_v2 inputs and the v2 judge rubric through the workflow. The
# editorial app is stood in for throughout: these tests never import it.
# ---------------------------------------------------------------------------------------

PATCH_EDITORIAL_API = 'enterprise_access.apps.pathways.pathway_variants.load_editorial_api'


def fake_editorial_api(active=None, seats=()):
    """A stand-in for ``pathway_editorial.api``: snapshots and the active policy, and a planner."""
    editorial_api = mock.Mock()
    editorial_api.EditorialPolicy.from_dict.side_effect = lambda data: FakePolicy(data.get('excluded_keys') or ())
    editorial_api.load_policy.return_value = active or FakePolicy()
    editorial_api.plan_seats.side_effect = FakePlanner(seats)
    return editorial_api


def v2_judge_result(keys=(), error=''):
    """What ``judging.judge_pathway`` returns under v2."""
    return {
        **judge_result(keys=keys, error=error), 'rubric': 'v2',
        'flags': {key: {'too_specific': False, 'redundant_with': '', 'level_mismatch': False,
                        'role_misfit': key == keys[-1]} for key in keys},
    }


def window_accumulator():
    """The accumulated output a variant step reads: ``spanning_hits`` in retrieval order."""
    return Accumulator(**{
        RetrieveCandidatesOutput.KEY: RetrieveCandidatesOutput(
            courses=[CourseCandidate.from_hit(hit) for hit in spanning_hits()],
        ),
        RerankCandidatesOutput.KEY: None,
    })


def judged_accumulator(default_keys=('A+1', 'A+2', 'B+1', 'B+2', 'C+1'), variants=()):
    """The accumulated output a judge step reads: a delivered pathway and ``(label, keys)`` variants."""
    return Accumulator(**{
        RetrieveCandidatesOutput.KEY: RetrieveCandidatesOutput(
            courses=[CourseCandidate.from_hit(hit) for hit in spanning_hits()],
        ),
        AssemblePathwayOutput.KEY: AssemblePathwayOutput(
            courses=[PathwayCourse(key=key) for key in default_keys], complete=bool(default_keys),
        ),
        BuildVariantsOutput.KEY: BuildVariantsOutput.from_dict({'variants': [
            {'label': label, 'strategy': label.split(':')[0], 'courses': [{'key': key} for key in keys]}
            for label, keys in variants
        ]}),
    })


def judge_by_rubric(**kwargs):
    """A ``judge_pathway`` stand-in answering as the requested rubric would."""
    if kwargs.get('rubric', 'v1') == 'v2':
        return v2_judge_result(keys=[course['key'] for course in kwargs['courses']])
    return judge_result(keys=[course['key'] for course in kwargs['courses']])


class TestEditorialAndRubricInputs(TestCase):
    """
    Scenario: The new options are opt-in, normalised, and persisted as they will run.
    """

    def _inputs(self, **kwargs):
        return PathwayAssemblyWorkflow.generate_input_dict(career_name='Welder', **kwargs)

    def test_by_default_nothing_new_is_requested(self):
        data = self._inputs()

        self.assertEqual(data[AssemblePathwayInput.KEY], {'editorial_policy': False, 'editorial_snapshot': {}})
        variants = data[BuildVariantsInput.KEY]
        self.assertEqual(
            {key: variants[key] for key in ('career_description', 'family_titles', 'family_size',
                                            'editorial_policy', 'editorial_snapshot')},
            {'career_description': '', 'family_titles': [], 'family_size': 0,
             'editorial_policy': False, 'editorial_snapshot': {}},
        )
        self.assertEqual(data[JudgePathwaysInput.KEY]['rubrics'], ['v1'])

    def test_the_new_options_are_persisted_normalised(self):
        snapshot = {'excluded_keys': ['A+1'], 'flagships': [], 'promoted': []}
        data = self._inputs(
            career_description='  Joins metal.  ', family_titles=['Welder', ' Welder ', 'Pipe Welder'],
            family_size=7, editorial_policy=True, editorial_snapshot=snapshot, judge_rubrics=['v2', 'v1'],
            judge_enabled=True,
        )

        variants, judge = data[BuildVariantsInput.KEY], data[JudgePathwaysInput.KEY]
        self.assertEqual(variants['career_description'], 'Joins metal.')
        self.assertEqual(variants['family_titles'], ['Welder', 'Pipe Welder'])
        self.assertEqual(variants['family_size'], 7)
        self.assertEqual(variants['editorial_snapshot'], snapshot)
        self.assertEqual(data[AssemblePathwayInput.KEY], {'editorial_policy': True, 'editorial_snapshot': snapshot})
        self.assertEqual(judge['rubrics'], ['v1', 'v2'])
        self.assertEqual((judge['career_description'], judge['family_titles'], judge['family_size']),
                         ('Joins metal.', ['Welder', 'Pipe Welder'], 7))

    def test_a_bad_option_is_refused_before_anything_is_persisted(self):
        for kwargs in ({'judge_rubrics': ['v3']}, {'family_size': -1}, {'editorial_snapshot': ['A+1']}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self._inputs(**kwargs)

    def test_inputs_persisted_before_these_options_existed_still_load(self):
        assemble = AssemblePathwayInput.from_dict({})
        variants = BuildVariantsInput.from_dict({'career_name': 'Welder', 'strategies': ['ranked_cut']})
        judge = JudgePathwaysInput.from_dict({'career_name': 'Welder', 'enabled': True})

        self.assertEqual((assemble.editorial_policy, assemble.editorial_snapshot), (False, {}))
        self.assertEqual((variants.career_description, variants.family_titles, variants.family_size,
                          variants.editorial_policy, variants.editorial_snapshot), ('', [], 0, False, {}))
        self.assertEqual((judge.rubrics, judge.career_description, judge.family_size), (['v1'], '', 0))

    def test_outputs_persisted_before_seats_and_rubrics_existed_still_load(self):
        variant = PathwayVariant.from_dict({'label': 'shape_cut:2/0/0', 'strategy': 'shape_cut'})
        judgement = PathwayJudgement.from_dict({'label': 'default', 'verdict': 'good'})
        output = BuildVariantsOutput.from_dict({'variants': []})

        self.assertEqual(variant.seats, [])
        self.assertEqual((judgement.rubric, judgement.flags), ('v1', {}))
        self.assertEqual(output.editorial_policy, {})


class TestAssemblePathwayStepWithAPolicy(TestCase):
    """
    Scenario: The delivered pathway honours a policy's exclusions only when opted in.
    """

    def _run(self, **input_data):
        step = AssemblePathwayStep.objects.create(
            workflow_record_uuid=uuid4(), input_data=AssemblePathwayInput(**input_data).to_dict(),
        )
        return step.execute(accumulated_output=Accumulator(
            retrieve_candidates_output=RetrieveCandidatesOutput(
                courses=[CourseCandidate.from_hit(hit) for hit in spanning_hits()],
            ),
        ))

    def test_without_an_opt_in_the_editorial_app_is_never_consulted(self):
        with mock.patch(PATCH_EDITORIAL_API) as loader:
            output = self._run()

        loader.assert_not_called()
        self.assertIn('B+1', [course.key for course in output.courses])
        self.assertNotIn('editorial_excluded', output.ineligible)

    def test_a_snapshot_excludes_its_courses_from_the_delivered_pathway(self):
        with mock.patch(PATCH_EDITORIAL_API, return_value=fake_editorial_api()):
            output = self._run(editorial_snapshot={'excluded_keys': ['B+1']})

        self.assertNotIn('B+1', [course.key for course in output.courses])
        self.assertEqual(output.ineligible, {'editorial_excluded': 1})
        self.assertTrue(output.complete)

    def test_the_active_policy_is_used_when_asked_for(self):
        editorial_api = fake_editorial_api(active=FakePolicy({'C+1'}))
        with mock.patch(PATCH_EDITORIAL_API, return_value=editorial_api):
            output = self._run(editorial_policy=True)

        self.assertNotIn('C+1', [course.key for course in output.courses])
        editorial_api.load_policy.assert_called_once_with()

    def test_a_policy_that_cannot_be_had_fails_the_step(self):
        with mock.patch(PATCH_EDITORIAL_API, side_effect=ImportError('no editorial app')):
            with self.assertRaisesRegex(AssemblePathwayStepException, 'editorial policy'):
                self._run(editorial_policy=True)


class TestBuildVariantsStepWithAPolicy(TestCase):
    """
    Scenario: The variant step resolves the policy, passes the career on, and persists seats.
    """

    def _step(self, **input_data):
        data = {'career_name': 'Welder', 'career_skills': ['Welding'], **input_data}
        return BuildVariantsStep.objects.create(workflow_record_uuid=uuid4(), input_data=data)

    def test_seats_and_the_policy_that_ran_are_persisted(self):
        step = self._step(strategies=['shape_cut'], shapes=['2/0/0'],
                          editorial_snapshot={'excluded_keys': ['A+1']})
        editorial_api = fake_editorial_api(seats=[FakeSeat('A+3')])

        with mock.patch(PATCH_EDITORIAL_API, return_value=editorial_api):
            output = step.process_input(accumulated_output=window_accumulator())

        variant = output.variants[0]
        self.assertEqual(sorted(course.key for course in variant.courses), ['A+2', 'A+3'])
        self.assertEqual(variant.seats, [FakeSeat('A+3').to_dict()])
        self.assertEqual(output.editorial_policy['excluded_keys'], ['A+1'])
        restored = BuildVariantsOutput.from_dict(output.to_dict())
        self.assertEqual(restored.variants[0].seats[0]['rule'], 'flagship')

    def test_without_a_policy_nothing_is_recorded(self):
        with mock.patch(PATCH_EDITORIAL_API) as loader:
            output = self._step(strategies=['shape_cut'], shapes=['2/0/0']).process_input(
                accumulated_output=window_accumulator(),
            )

        loader.assert_not_called()
        self.assertEqual((output.variants[0].seats, output.editorial_policy), ([], {}))

    def test_shape_pick_v2_is_shown_the_career_and_its_family(self):
        backend = FakeBackend(content=json.dumps({'keys': ['B+1', 'B+2']}))
        step = self._step(strategies=['shape_pick_v2'], shapes=['0/2/0'], career_description='Joins metal.',
                          family_titles=['Welder', 'Pipe Welder'], family_size=5)

        with mock.patch(PATCH_VARIANT_BACKEND, return_value=backend):
            output = step.process_input(accumulated_output=window_accumulator())

        shown = json.loads(backend.calls[0]['user_content'])
        self.assertEqual((shown['career_description'], shown['family_titles'], shown['family_size']),
                         ('Joins metal.', ['Welder', 'Pipe Welder'], 5))
        self.assertEqual(output.variants[0].label, 'shape_pick_v2:0/2/0')
        self.assertTrue(output.variants[0].complete)


class TestJudgePathwaysStepRubrics(TestCase):
    """
    Scenario: Each target is judged under each rubric, reusing identical lists per rubric only.
    """

    DEFAULT = ('A+1', 'A+2', 'B+1', 'B+2', 'C+1')

    def _step(self, **input_data):
        data = {'career_name': 'Welder', 'career_skills': ['Welding'], 'enabled': True, **input_data}
        return JudgePathwaysStep.objects.create(workflow_record_uuid=uuid4(), input_data=data)

    def test_every_target_is_judged_under_every_rubric_and_reuse_stays_within_a_rubric(self):
        step = self._step(rubrics=['v1', 'v2'], career_description='Joins metal.',
                          family_titles=['Welder'], family_size=3)
        accumulated = judged_accumulator(variants=[
            ('ranked_cut:5', list(self.DEFAULT)), ('ranked_cut:2', ['A+1', 'B+1']),
        ])

        with mock.patch(PATCH_JUDGE, side_effect=judge_by_rubric) as mock_judge:
            output = step.process_input(accumulated_output=accumulated)

        # default and ranked_cut:2 under each rubric; ranked_cut:5 reuses default within each.
        self.assertEqual(mock_judge.call_count, 4)
        self.assertEqual(
            [(j.rubric, j.label, j.same_as) for j in output.judgements],
            [('v1', 'default', ''), ('v1', 'ranked_cut:5', 'default'), ('v1', 'ranked_cut:2', ''),
             ('v2', 'default', ''), ('v2', 'ranked_cut:5', 'default'), ('v2', 'ranked_cut:2', '')],
        )
        v2_call = mock_judge.call_args_list[2].kwargs
        self.assertEqual((v2_call['rubric'], v2_call['career_description'], v2_call['family_titles'],
                          v2_call['family_size']), ('v2', 'Joins metal.', ['Welder'], 3))
        self.assertIn(f'{step.uuid}:v2:default', v2_call['trace_id'])
        self.assertTrue(mock_judge.call_args_list[0].kwargs['trace_id'].endswith(f'{step.uuid}:default'))
        self.assertEqual(output.judgements[1].flags, {})
        self.assertTrue(output.judgements[3].flags['C+1']['role_misfit'])
        self.assertTrue(output.judgements[4].flags['C+1']['role_misfit'])

    def test_by_default_only_v1_is_judged(self):
        with mock.patch(PATCH_JUDGE, side_effect=judge_by_rubric) as mock_judge:
            output = self._step().process_input(accumulated_output=judged_accumulator())

        self.assertEqual(mock_judge.call_args.kwargs['rubric'], 'v1')
        self.assertEqual([j.rubric for j in output.judgements], ['v1'])

    def test_an_unknown_rubric_fails_the_step(self):
        with mock.patch(PATCH_JUDGE) as mock_judge:
            with self.assertRaisesRegex(JudgePathwaysStepException, 'Unknown judge rubrics'):
                self._step(rubrics=['v9']).process_input(accumulated_output=judged_accumulator())

        mock_judge.assert_not_called()

    def test_the_cost_bound_counts_every_rubric(self):
        self.assertEqual(
            estimated_model_calls(sizes=[2], strategies=['ranked_cut'], judge_enabled=True,
                                  judge_rubrics=['v1', 'v2']),
            2 * (1 + 1),
        )


class TestJudgementsByRubric(PathwayWorkflowMixin, TestCase):
    """
    Scenario: A finished run's judgements are read one rubric at a time.
    """

    def test_v1_and_v2_are_read_apart_and_variants_carry_both(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow(variant_sizes=[2], judge_enabled=True, judge_rubrics=['v1', 'v2'])

        with snapshot, retrieve, rerank, enrich, mock.patch(PATCH_JUDGE, side_effect=judge_by_rubric):
            workflow.execute()

        self.assertEqual(workflow.default_judgement()['rubric'], 'v1')
        self.assertEqual(workflow.default_judgement('v2')['rubric'], 'v2')
        self.assertEqual(set(workflow.judgements('v2')), {'default', 'ranked_cut:2'})
        variant = workflow.variants()[0]
        self.assertEqual((variant['judgement']['rubric'], variant['judgement_v2']['rubric']), ('v1', 'v2'))

    def test_a_run_without_v2_reads_exactly_as_before(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow(variant_sizes=[2], judge_enabled=True)

        with snapshot, retrieve, rerank, enrich, mock.patch(PATCH_JUDGE, side_effect=judge_by_rubric):
            workflow.execute()

        self.assertNotIn('judgement_v2', workflow.variants()[0])
        self.assertEqual(workflow.judgements('v2'), {})

    def test_judgements_persisted_before_rubrics_existed_read_as_v1(self):
        workflow = self._workflow()
        workflow.output_data = {JudgePathwaysOutput.KEY: {'judgements': [{'label': 'default', 'verdict': 'weak'}]}}

        self.assertEqual(workflow.default_judgement()['verdict'], 'weak')
        self.assertIsNone(workflow.default_judgement('v2'))


class TestEditorialPolicyEndToEnd(PathwayWorkflowMixin, TestCase):
    """
    Scenario: Only an opted-in run's delivered pathway can differ, and only by exclusion.
    """

    def test_the_delivered_pathway_drops_excluded_courses_only_when_opted_in(self):
        snapshot, retrieve, rerank, enrich = self._patches()
        plain = self._workflow()
        with snapshot, retrieve, rerank, enrich:
            plain.execute()

        snapshot, retrieve, rerank, enrich = self._patches()
        opted_in = self._workflow(editorial_snapshot={'excluded_keys': ['B+1']},
                                  variant_shapes=['2/0/0'], variant_strategies=['shape_cut'])
        with snapshot, retrieve, rerank, enrich, \
                mock.patch(PATCH_EDITORIAL_API, return_value=fake_editorial_api(seats=[FakeSeat('A+3')])):
            opted_in.execute()

        self.assertIn('B+1', [c['key'] for c in plain.pathway()['courses']])
        self.assertNotIn('B+1', [c['key'] for c in opted_in.pathway()['courses']])
        variant = opted_in.variants()[0]
        self.assertEqual(variant['seats'][0]['key'], 'A+3')
        self.assertIn('A+3', [c['key'] for c in variant['courses']])
