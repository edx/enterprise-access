"""
Tests for the career discovery endpoint.

HTTP-layer behaviour only: the feature flag, validation, permissions, error mapping and
response shape. Workflow and query-construction behaviour is tested in
``enterprise_access.apps.pathways.tests``.

Xpert and Algolia are mocked in every test; nothing here issues a network call.
"""
import uuid
from unittest import mock

import ddt
from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, status
from rest_framework.reverse import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from enterprise_access.apps.api.v1.views.pathways import CareerDiscoveryViewSet
from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchError
from enterprise_access.apps.core.constants import LEARNER_PATHWAYS_LEARNER_ROLE, SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathways.models import CareerDiscoveryWorkflow, ExtractIntentStep, RetrieveCareersStep
from enterprise_access.apps.prompts.api_client import XpertAPIRequestError, XpertResponseMessage
from enterprise_access.apps.prompts.models import PromptType, XpertLearnerPathwaysSystemPrompt
from enterprise_access.apps.prompts.tests.factories import XpertLearnerPathwaysSystemPromptFactory
from test_utils import APITest

PATCH_XPERT_CLIENT = 'enterprise_access.apps.prompts.api.XpertAPIClient'
PATCH_ALGOLIA_CLIENT = 'enterprise_access.apps.pathways.api.AlgoliaSearchClient'

_CAREERS_URL_NAME = 'api:v1:career-discovery-careers'

_VALID_PAYLOAD = {
    'selected_goals': 'move into data analysis',
    'free_text': 'I report on spreadsheets all day and want to automate it',
    'known_context': 'operations analyst, five years',
    'interested_industries': 'healthcare, technology',
}

_XPERT_CONTENT = (
    '{"skills_required": ["SQL"], "skills_preferred": ["Tableau"], '
    '"condensed_algolia_query": "data analyst"}'
)

_JOBS_RESPONSE = {
    'hits': [{
        'external_id': 'ETE78CD2CDFFFAC66B',
        'name': 'Data Analyst',
        'skills': [{'name': 'SQL (Programming Language)'}],
        'industry_names': ['Health Care'],
    }],
    'nbHits': 1,
}


class CareerDiscoveryAPITestMixin:
    """Shared set-up: a configured prompt, mocked Xpert and Algolia, an authorized learner."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)

    def setUp(self):
        super().setUp()
        self.addCleanup(django_cache.clear)
        self.url = reverse(_CAREERS_URL_NAME)

        self.xpert_patcher = mock.patch(PATCH_XPERT_CLIENT)
        self.mock_xpert = self.xpert_patcher.start().return_value
        self.mock_xpert.send_message.return_value = XpertResponseMessage(
            role='assistant',
            content=_XPERT_CONTENT,
        )
        self.addCleanup(self.xpert_patcher.stop)

        self.algolia_patcher = mock.patch(PATCH_ALGOLIA_CLIENT)
        self.mock_algolia = self.algolia_patcher.start().return_value
        self.mock_algolia.search_jobs_index.return_value = _JOBS_RESPONSE
        self.addCleanup(self.algolia_patcher.stop)

    def authenticate_as_enterprise_learner(self):
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

    def post_careers(self, payload=None):
        body = _VALID_PAYLOAD if payload is None else payload
        return self.client.post(self.url, data=body, format='json')


@override_settings(LEARNER_PATHWAYS_SERVER_PIPELINE_ENABLED=True)
class TestCareerDiscoverySuccess(CareerDiscoveryAPITestMixin, APITest):
    """Tests for a successful career discovery request."""

    def setUp(self):
        super().setUp()
        self.authenticate_as_enterprise_learner()

    def test_intake_returns_careers(self):
        response = self.post_careers()

        assert response.status_code == status.HTTP_200_OK
        assert response.json()['careers'] == [{
            'external_id': 'ETE78CD2CDFFFAC66B',
            'name': 'Data Analyst',
            'skills': ['SQL (Programming Language)'],
            'industries': ['Health Care'],
        }]

    def test_match_percentage_is_absent_rather_than_fabricated(self):
        response = self.post_careers()

        career = response.json()['careers'][0]
        assert 'match_percentage' not in career
        assert not any('match' in key for key in career)

    def test_response_carries_the_trace_handle(self):
        response = self.post_careers()

        workflow = CareerDiscoveryWorkflow.objects.get()
        assert response.json()['workflow_uuid'] == str(workflow.uuid)

    def test_every_execution_leaves_a_trace(self):
        self.post_careers()

        workflow = CareerDiscoveryWorkflow.objects.get()
        assert workflow.succeeded_at is not None

        step_records = [
            ExtractIntentStep.objects.get(workflow_record_uuid=workflow.uuid),
            RetrieveCareersStep.objects.get(workflow_record_uuid=workflow.uuid),
        ]
        for step_record in step_records:
            assert step_record.input_data is not None
            assert step_record.output_data
            assert step_record.succeeded_at is not None

    def test_explicit_db_role_assignment_is_allowed(self):
        self.client.logout()
        self.client.cookies.clear()
        user = UserFactory(is_active=True)
        role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(name=LEARNER_PATHWAYS_LEARNER_ROLE)
        EnterpriseAccessRoleAssignment.objects.create(
            user=user,
            role=role,
            enterprise_customer_uuid=uuid.uuid4(),
        )
        self.client.force_authenticate(user=user)

        assert self.post_careers().status_code == status.HTTP_200_OK


@ddt.ddt
@override_settings(LEARNER_PATHWAYS_SERVER_PIPELINE_ENABLED=True)
class TestCareerDiscoveryAuthorization(CareerDiscoveryAPITestMixin, APITest):
    """Authorization and validation tests."""

    def test_unauthenticated_caller_is_rejected(self):
        self.client.logout()
        self.client.cookies.clear()

        response = self.post_careers()

        assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        assert not CareerDiscoveryWorkflow.objects.exists()
        self.mock_xpert.send_message.assert_not_called()

    def test_authenticated_non_enterprise_learner_is_rejected(self):
        self.client.force_authenticate(user=UserFactory(is_active=True))

        response = self.post_careers()

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not CareerDiscoveryWorkflow.objects.exists()
        self.mock_xpert.send_message.assert_not_called()

    @ddt.data(
        {},
        {**_VALID_PAYLOAD, 'free_text': ''},
        {key: value for key, value in _VALID_PAYLOAD.items() if key != 'known_context'},
    )
    def test_invalid_payload_is_rejected(self, payload):
        self.authenticate_as_enterprise_learner()

        response = self.post_careers(payload)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        # No workflow record for a request that never ran.
        assert not CareerDiscoveryWorkflow.objects.exists()
        self.mock_xpert.send_message.assert_not_called()

    def test_get_is_rejected(self):
        self.authenticate_as_enterprise_learner()

        assert self.client.get(self.url).status_code == status.HTTP_405_METHOD_NOT_ALLOWED


class TestCareerDiscoveryFeatureFlag(CareerDiscoveryAPITestMixin, APITest):
    """The flag defaults off, so the endpoint must behave as though it does not exist."""

    def setUp(self):
        super().setUp()
        self.authenticate_as_enterprise_learner()

    def test_disabled_pipeline_returns_404(self):
        response = self.post_careers()

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert not CareerDiscoveryWorkflow.objects.exists()
        self.mock_xpert.send_message.assert_not_called()

    def test_disabled_pipeline_returns_404_for_an_invalid_payload_too(self):
        # The flag is checked before validation, so a disabled endpoint cannot be
        # distinguished from a missing one by probing it with a bad body.
        assert self.post_careers({}).status_code == status.HTTP_404_NOT_FOUND

    @override_settings(LEARNER_PATHWAYS_SERVER_PIPELINE_ENABLED=True)
    def test_enabled_pipeline_serves_the_endpoint(self):
        assert self.post_careers().status_code == status.HTTP_200_OK


@override_settings(LEARNER_PATHWAYS_SERVER_PIPELINE_ENABLED=True)
class TestCareerDiscoveryFailures(CareerDiscoveryAPITestMixin, APITest):
    """A failed step returns 500 and leaves the failure on the record."""

    def setUp(self):
        super().setUp()
        self.authenticate_as_enterprise_learner()

    def test_xpert_failure_returns_500_without_partial_results(self):
        self.mock_xpert.send_message.side_effect = XpertAPIRequestError('xpert exploded')

        response = self.post_careers()

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert 'careers' not in response.json()

        workflow = CareerDiscoveryWorkflow.objects.get()
        intent_step = ExtractIntentStep.objects.get(workflow_record_uuid=workflow.uuid)
        assert intent_step.failed_at is not None
        assert 'xpert exploded' in intent_step.exception_message
        assert not RetrieveCareersStep.objects.exists()

    def test_algolia_failure_returns_500_and_marks_the_step_failed(self):
        self.mock_algolia.search_jobs_index.side_effect = AlgoliaSearchError('algolia exploded')

        response = self.post_careers()

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert 'careers' not in response.json()

        careers_step = RetrieveCareersStep.objects.get()
        assert careers_step.failed_at is not None
        assert 'algolia exploded' in careers_step.exception_message

    def test_missing_prompt_returns_500(self):
        XpertLearnerPathwaysSystemPrompt.objects.all().delete()

        response = self.post_careers()

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        self.mock_algolia.search_jobs_index.assert_not_called()


class TestCareerDiscoveryRouteConfig(TestCase):
    """Route-level configuration for the careers action."""

    def test_url_reverses_under_learner_pathways(self):
        url = reverse(_CAREERS_URL_NAME)
        assert url.endswith('/learner-pathways/careers/')

    def test_route_does_not_shadow_the_prompt_endpoints(self):
        assert reverse('api:v1:learner-pathways-learning-intent') != reverse(_CAREERS_URL_NAME)

    def test_post_is_routed(self):
        response = APIClient().post(reverse(_CAREERS_URL_NAME), data={}, format='json')
        assert response.status_code != status.HTTP_405_METHOD_NOT_ALLOWED

    def test_action_configuration(self):
        # pylint: disable=no-member  # DRF @action adds .kwargs at decoration time.
        action_kwargs = CareerDiscoveryViewSet.careers.kwargs
        assert JwtAuthentication in action_kwargs['authentication_classes']
        assert permissions.IsAuthenticated in action_kwargs['permission_classes']
        assert ScopedRateThrottle in action_kwargs['throttle_classes']
        assert action_kwargs['throttle_scope'] == 'learner_pathways_careers'

    def test_no_class_level_throttle_classes(self):
        assert 'throttle_classes' not in CareerDiscoveryViewSet.__dict__
        assert CareerDiscoveryViewSet.throttle_scope is None
