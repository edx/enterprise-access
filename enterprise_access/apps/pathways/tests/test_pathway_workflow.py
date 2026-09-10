"""
Tests for the pathway assembly steps and the workflow that composes them.

The end-to-end tests here exercise the whole five-step chain against patched externals,
which is the only place the conditional-skip behaviour and the accumulated-output
plumbing are checked together.
"""
from unittest import mock
from uuid import uuid4

from django.test import TestCase
from edx_toggles.toggles.testutils import override_waffle_switch

from enterprise_access.apps.pathways.models import (
    AssemblePathwayInput,
    AssemblePathwayOutput,
    AssemblePathwayStep,
    AssemblePathwayStepException,
    CourseCandidate,
    EnrichRationaleInput,
    EnrichRationaleStep,
    EnrichRationaleStepException,
    PathwayAssemblyWorkflow,
    PathwayCourse,
    RerankCandidatesInput,
    RerankCandidatesOutput,
    RerankCandidatesStep,
    RetrieveCandidatesInput,
    RetrieveCandidatesOutput,
    RetrieveCandidatesStep,
    RetrieveCandidatesStepException,
    TranslateToCatalogOutput
)
from enterprise_access.apps.prompts.api import PromptError
from enterprise_access.apps.prompts.api_client import XpertAPIError
from enterprise_access.toggles import LEARNER_PATHWAYS_DISABLE_CANDIDATE_RERANK

PATCH_RETRIEVE = 'enterprise_access.apps.pathways.course_retrieval.retrieve_candidate_courses'
PATCH_RERANK = 'enterprise_access.apps.pathways.reranking.rerank_candidates'
PATCH_SNAPSHOT = 'enterprise_access.apps.pathways.catalog_translation.snapshot_catalog_facets'
PATCH_ENRICH = 'enterprise_access.apps.pathways.models.pathways_api.enrich_rationales'

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


class TestPathwayAssemblyWorkflow(TestCase):
    """
    End-to-end tests for the five-step workflow.
    """

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
        """Scenario: The composition is inspectable afterwards."""
        snapshot, retrieve, rerank, enrich = self._patches()
        workflow = self._workflow()

        with snapshot, retrieve, rerank, enrich:
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
