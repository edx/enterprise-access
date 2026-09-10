"""
Tests for the career discovery workflow, its steps, and the trace they leave behind.

Every test mocks Xpert and Algolia; nothing here issues a network call.
"""
from unittest import mock

from django.test import TestCase

from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchError
from enterprise_access.apps.pathways.models import (
    CareerDiscoveryWorkflow,
    ExtractIntentInput,
    ExtractIntentOutput,
    ExtractIntentStep,
    RetrieveCareersInput,
    RetrieveCareersOutput,
    RetrieveCareersStep,
    RetrieveCareersStepException
)
from enterprise_access.apps.prompts.api_client import XpertAPIRequestError, XpertResponseMessage
from enterprise_access.apps.prompts.models import PromptType
from enterprise_access.apps.prompts.tests.factories import XpertLearnerPathwaysSystemPromptFactory
from enterprise_access.apps.workflow.exceptions import UnitOfWorkException

PATCH_XPERT_CLIENT = 'enterprise_access.apps.prompts.api.XpertAPIClient'
PATCH_ALGOLIA_CLIENT = 'enterprise_access.apps.pathways.api.AlgoliaSearchClient'

INTAKE = {
    'selected_goals': 'move into data analysis',
    'free_text': 'I report on spreadsheets all day and want to automate it',
    'known_context': 'operations analyst, five years',
    'interested_industries': 'healthcare, technology',
}

XPERT_CONTENT = (
    '{"skills_required": ["SQL"], "skills_preferred": ["Tableau"], '
    '"condensed_algolia_query": "data analyst"}'
)

JOBS_RESPONSE = {
    'hits': [{
        'external_id': 'ETE78CD2CDFFFAC66B',
        'name': 'Data Analyst',
        'skills': [{'name': 'SQL (Programming Language)'}],
        'industry_names': ['Health Care'],
    }],
    'nbHits': 1,
}


class CareerDiscoveryWorkflowTestMixin:
    """Shared set-up for workflow executions with Xpert and Algolia mocked."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)

    def setUp(self):
        super().setUp()
        self.xpert_patcher = mock.patch(PATCH_XPERT_CLIENT)
        self.mock_xpert = self.xpert_patcher.start().return_value
        self.mock_xpert.send_message.return_value = XpertResponseMessage(
            role='assistant',
            content=XPERT_CONTENT,
        )
        self.addCleanup(self.xpert_patcher.stop)

        self.algolia_patcher = mock.patch(PATCH_ALGOLIA_CLIENT)
        self.mock_algolia = self.algolia_patcher.start().return_value
        self.mock_algolia.search_jobs_index.return_value = JOBS_RESPONSE
        self.addCleanup(self.algolia_patcher.stop)

    def create_workflow(self, intake=None):
        return CareerDiscoveryWorkflow.objects.create(
            input_data=CareerDiscoveryWorkflow.generate_input_dict(intake or INTAKE),
        )


class TestCareerDiscoveryWorkflowInput(TestCase):
    """Tests for how a workflow record's input is built."""

    def test_generate_input_dict(self):
        input_dict = CareerDiscoveryWorkflow.generate_input_dict(INTAKE)

        assert input_dict[ExtractIntentInput.KEY] == INTAKE
        # Empty on purpose: the intake's free-text industries are not facet values, and a
        # hard filter on a non-facet value returns zero hits with no signal that it did.
        assert input_dict[RetrieveCareersInput.KEY] == {}

    def test_steps_are_composed_in_order(self):
        assert CareerDiscoveryWorkflow.steps == [ExtractIntentStep, RetrieveCareersStep]


class TestCareerDiscoveryWorkflowExecution(CareerDiscoveryWorkflowTestMixin, TestCase):
    """Tests for a successful end-to-end execution."""

    def test_workflow_returns_career_candidates(self):
        workflow = self.create_workflow()

        workflow.execute()

        assert workflow.career_candidates() == [{
            'external_id': 'ETE78CD2CDFFFAC66B',
            'name': 'Data Analyst',
            'skills': ['SQL (Programming Language)'],
            'industries': ['Health Care'],
        }]

    def test_derived_intent_drives_the_jobs_search(self):
        self.create_workflow().execute()

        args, kwargs = self.mock_algolia.search_jobs_index.call_args
        assert args == ('data analyst',)
        assert kwargs['optionalFilters'] == ['skills.name:"SQL"', 'skills.name:"Tableau"<score=1>']

    def test_every_executed_step_leaves_a_trace(self):
        workflow = self.create_workflow()

        workflow.execute()

        intent_step = ExtractIntentStep.objects.get(workflow_record_uuid=workflow.uuid)
        careers_step = RetrieveCareersStep.objects.get(workflow_record_uuid=workflow.uuid)

        for step_record in (intent_step, careers_step):
            assert step_record.input_data is not None
            assert step_record.output_data
            assert step_record.succeeded_at is not None
            assert step_record.failed_at is None
            assert step_record.created is not None

        assert intent_step.input_data == INTAKE
        assert intent_step.output_data['skills_required'] == ['SQL']
        assert careers_step.output_data['query'] == 'data analyst'
        assert careers_step.output_data['hit_count'] == 1
        # The soft UUID linkage is what makes the executed order reconstructable.
        assert careers_step.preceding_step_uuid == intent_step.uuid

    def test_workflow_record_persists_both_step_outputs(self):
        workflow = self.create_workflow()

        workflow.execute()

        assert workflow.succeeded_at is not None
        assert set(workflow.output_data) == {ExtractIntentOutput.KEY, RetrieveCareersOutput.KEY}

    def test_no_match_percentage_is_persisted(self):
        workflow = self.create_workflow()

        workflow.execute()

        assert 'match' not in str(workflow.output_data)

    def test_conversation_id_identifies_the_step_record(self):
        # A step can be re-executed outside the request that created it, so the trace
        # handle -- not the request id -- is what ties an Xpert call to a record.
        workflow = self.create_workflow()

        workflow.execute()

        intent_step = ExtractIntentStep.objects.get(workflow_record_uuid=workflow.uuid)
        _, kwargs = self.mock_xpert.send_message.call_args
        assert str(intent_step.uuid) in kwargs['conversation_id']

    def test_output_round_trips_through_json(self):
        workflow = self.create_workflow()
        workflow.execute()

        reloaded = CareerDiscoveryWorkflow.objects.get(uuid=workflow.uuid)
        careers_output = RetrieveCareersOutput.from_dict(reloaded.output_data[RetrieveCareersOutput.KEY])

        assert careers_output.careers[0].external_id == 'ETE78CD2CDFFFAC66B'
        assert careers_output.to_dict() == reloaded.output_data[RetrieveCareersOutput.KEY]


class TestCareerDiscoveryWorkflowFailures(CareerDiscoveryWorkflowTestMixin, TestCase):
    """Tests that a failing step is recorded rather than swallowed."""

    def test_xpert_failure_is_recorded_and_stops_the_workflow(self):
        self.mock_xpert.send_message.side_effect = XpertAPIRequestError('xpert exploded')
        workflow = self.create_workflow()

        with self.assertRaises(UnitOfWorkException):
            workflow.execute()

        intent_step = ExtractIntentStep.objects.get(workflow_record_uuid=workflow.uuid)
        assert intent_step.failed_at is not None
        assert 'xpert exploded' in intent_step.exception_message
        assert intent_step.output_data is None
        # No partial results: the second step never ran, so it has no record at all.
        assert RetrieveCareersStep.objects.count() == 0
        assert workflow.failed_at is not None
        assert workflow.output_data is None
        self.mock_algolia.search_jobs_index.assert_not_called()

    def test_algolia_failure_is_recorded_on_its_own_step(self):
        self.mock_algolia.search_jobs_index.side_effect = AlgoliaSearchError('algolia exploded')
        workflow = self.create_workflow()

        with self.assertRaises(UnitOfWorkException):
            workflow.execute()

        careers_step = RetrieveCareersStep.objects.get(workflow_record_uuid=workflow.uuid)
        assert careers_step.failed_at is not None
        assert 'algolia exploded' in careers_step.exception_message
        # The step that did succeed keeps its record, so a re-run skips it.
        assert ExtractIntentStep.objects.get(workflow_record_uuid=workflow.uuid).succeeded_at is not None
        assert workflow.career_candidates() == []

    def test_careers_step_requires_the_intent_output(self):
        # Executed outside its workflow, the step must refuse rather than search on nothing.
        step = RetrieveCareersStep.objects.create(
            workflow_record_uuid=self.create_workflow().uuid,
            input_data={},
        )

        with self.assertRaises(RetrieveCareersStepException):
            step.execute(accumulated_output=object())

        step.refresh_from_db()
        assert step.failed_at is not None
        self.mock_algolia.search_jobs_index.assert_not_called()
