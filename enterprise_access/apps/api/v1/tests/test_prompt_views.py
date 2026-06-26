"""
Tests for PromptRequestException, BasePromptViewSet, and LearnerPathwaysViewSet.

Domain logic tests are in enterprise_access.apps.prompts.tests.test_api.
This module focuses on HTTP-layer behavior: validation, permission checks,
throttling, error mapping, and response serialization.
"""
import json
import uuid
from unittest import mock

import ddt
from django.conf import settings as django_settings
from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, status
from rest_framework.reverse import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from enterprise_access.apps.api.v1.views.prompt import BasePromptViewSet, LearnerPathwaysViewSet, PromptRequestException
from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.prompts import api as prompts_api
from enterprise_access.apps.prompts.api_client import (
    XpertAPIConfigurationError,
    XpertAPIRequestError,
    XpertAPIResponseError
)
from enterprise_access.apps.prompts.models import PromptType, XpertLearnerPathwaysSystemPrompt
from enterprise_access.apps.prompts.tests.factories import XpertLearnerPathwaysSystemPromptFactory
from test_utils import APITest

PATCH_PROMPTS_API = 'enterprise_access.apps.api.v1.views.prompt.prompts_api'
PATCH_GET_REQUEST_ID = 'enterprise_access.apps.api.v1.views.prompt.get_request_id'
PATCH_UUID4 = 'enterprise_access.apps.api.v1.views.prompt.uuid_module.uuid4'

_LEARNING_INTENT_URL_NAME = 'api:v1:learner-pathways-learning-intent'

_VALID_LEARNING_INTENT_PAYLOAD = {
    'selected_goals': 'data science',
    'free_text': 'I want to become a data scientist',
    'known_context': 'currently a software engineer',
}


@ddt.ddt
class TestPromptRequestException(TestCase):
    """Tests for PromptRequestException."""

    def test_status_code_is_500(self):
        exc = PromptRequestException('something went wrong')
        assert exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_detail_message_is_preserved(self):
        exc = PromptRequestException('something went wrong')
        assert 'something went wrong' in str(exc.detail)

    def test_args_populated_with_message(self):
        exc = PromptRequestException('my error message')
        assert exc.args[0] == 'my error message'

    def test_exception_chaining_preserved(self):
        original = prompts_api.PromptError('original error')
        try:
            raise PromptRequestException('wrapped') from original
        except PromptRequestException as exc:
            assert exc.__cause__ is original


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------

class TestLearnerPathwaysRouting(TestCase):
    """Tests for URL resolution of LearnerPathwaysViewSet."""

    def test_learning_intent_url_reverses(self):
        url = reverse(_LEARNING_INTENT_URL_NAME)
        assert 'learner-pathways' in url
        assert 'learning-intent' in url

    def test_learning_intent_post_accepted(self):
        client = APIClient()
        url = reverse(_LEARNING_INTENT_URL_NAME)
        response = client.post(url, data={}, format='json')
        # Unauthenticated — 401 or 403, but NOT 405.
        assert response.status_code != status.HTTP_405_METHOD_NOT_ALLOWED

    def test_learning_intent_get_rejected(self):
        url = reverse(_LEARNING_INTENT_URL_NAME)
        client = APIClient()
        user = UserFactory(is_active=True)
        client.force_authenticate(user=user)
        response = client.get(url)
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


# ---------------------------------------------------------------------------
# Route configuration tests
# ---------------------------------------------------------------------------

class TestLearnerPathwaysRouteConfig(TestCase):
    """Assert route-level configuration for each action."""
    # pylint: disable=no-member  # DRF @action adds .kwargs at decoration time; pylint can't see it.

    def _get_action(self, name):
        return getattr(LearnerPathwaysViewSet, name)

    def test_learning_intent_authentication_classes(self):
        ac = self._get_action('learning_intent').kwargs.get('authentication_classes', ())
        assert JwtAuthentication in ac

    def test_learning_intent_is_authenticated_permission(self):
        pc = self._get_action('learning_intent').kwargs.get('permission_classes', ())
        assert permissions.IsAuthenticated in pc

    def test_learning_intent_throttle_class(self):
        tc = self._get_action('learning_intent').kwargs.get('throttle_classes', ())
        assert ScopedRateThrottle in tc

    def test_learning_intent_throttle_scope(self):
        scope = self._get_action('learning_intent').kwargs.get('throttle_scope')
        assert scope == 'learner_pathways_learning_intent'

    def test_no_throttle_on_base_prompt_viewset(self):
        # throttle_classes must not be explicitly defined on BasePromptViewSet itself
        assert 'throttle_classes' not in BasePromptViewSet.__dict__
        assert 'throttle_scope' not in BasePromptViewSet.__dict__

    def test_no_class_level_throttle_classes_on_learner_pathways_viewset(self):
        assert 'throttle_classes' not in LearnerPathwaysViewSet.__dict__

    def test_throttle_scope_sentinel_is_none(self):
        assert LearnerPathwaysViewSet.throttle_scope is None


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------

@ddt.ddt
class TestLearnerPathwaysAuthorization(APITest):
    """Authorization tests for the learning-intent endpoint."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.learning_intent_prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.LEARNER_INTENT,
        )

    def setUp(self):
        super().setUp()
        self.addCleanup(django_cache.clear)

    def test_unauthenticated_caller_is_rejected(self):
        self.client.logout()
        self.client.cookies.clear()

        response = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data={},
            format='json',
        )

        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_enterprise_learner_is_allowed(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '{"result":"ok"}',
        }

        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

        response = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )

        assert response.status_code == status.HTTP_200_OK

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_xpert_not_called_when_auth_fails(self, mock_client_class):
        self.set_jwt_cookie([])

        self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data={},
            format='json',
        )

        mock_client_class.return_value.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Throttle tests
# ---------------------------------------------------------------------------

@override_settings(REST_FRAMEWORK={
    'DEFAULT_THROTTLE_RATES': {
        'learner_pathways_learning_intent': '2/minute',
    },
})
@ddt.ddt
class TestLearnerPathwaysThrottle(APITest):
    """Throttle tests for the learning-intent endpoint."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.learning_intent_prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.LEARNER_INTENT,
        )

    def setUp(self):
        super().setUp()
        self.addCleanup(django_cache.clear)
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

    def test_learning_intent_scope_in_default_throttle_rates(self):
        rates = django_settings.REST_FRAMEWORK.get('DEFAULT_THROTTLE_RATES', {})
        assert 'learner_pathways_learning_intent' in rates

    @mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {
        'learner_pathways_learning_intent': '2/minute',
    })
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_learning_intent_throttled_after_rate_exceeded(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        url = reverse(_LEARNING_INTENT_URL_NAME)
        for _ in range(2):
            resp = self.client.post(url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
            assert resp.status_code == status.HTTP_200_OK
        resp = self.client.post(url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS


# ---------------------------------------------------------------------------
# Happy path tests — learning intent
# ---------------------------------------------------------------------------

class TestLearningIntentHappyPath(APITest):
    """Full happy-path tests for the learning-intent action."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.LEARNER_INTENT,
        )
        cls.other_prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.RECOMMENDATIONS_FEEDBACK,
        )

    def setUp(self):
        super().setUp()
        self.addCleanup(django_cache.clear)
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])
        self.url = reverse(_LEARNING_INTENT_URL_NAME)

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_http_200_with_valid_payload(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '{"skills_required":["python"]}',
        }
        resp = self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        assert resp.status_code == status.HTTP_200_OK

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_correct_prompt_type_used(self, mock_client_class):
        with mock.patch.object(
            XpertLearnerPathwaysSystemPrompt, 'get_current'
        ) as mock_get_current:
            mock_prompt = mock.Mock()
            mock_prompt.system_prompt = 'Be helpful.'
            mock_prompt.output_schema = None
            mock_get_current.return_value = mock_prompt
            mock_client_class.return_value.send_message.return_value = {
                'role': 'assistant', 'content': '{"r":1}',
            }
            self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
            mock_get_current.assert_called_once_with(
                prompt_type=PromptType.LEARNER_INTENT,
            )

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_server_controlled_tags_passed(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        with override_settings(XPERT_LEARNER_PATHWAYS_RAG_TAGS=['tag-a', 'tag-b']):
            self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        call_kwargs = mock_client_class.return_value.send_message.call_args.kwargs
        assert call_kwargs['tags'] == ['tag-a', 'tag-b']

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_xpert_called_exactly_once(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        assert mock_client_class.return_value.send_message.call_count == 1

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_full_parsed_json_returned(self, mock_client_class):
        payload_json = '{"skills_required":["python","ml"],"condensed_algolia_query":"data"}'
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': payload_json,
        }
        resp = self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json() == json.loads(payload_json)

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_validated_data_encoded_as_user_message(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        call_kwargs = mock_client_class.return_value.send_message.call_args.kwargs
        messages = call_kwargs['messages']
        assert len(messages) == 1
        assert messages[0]['role'] == 'user'
        assert isinstance(messages[0]['content'], str)
        parsed = json.loads(messages[0]['content'])
        assert parsed['selected_goals'] == _VALID_LEARNING_INTENT_PAYLOAD['selected_goals']

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_conversation_id_has_prefix(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        call_kwargs = mock_client_class.return_value.send_message.call_args.kwargs
        assert call_kwargs['conversation_id'].startswith('enterprise-access:')

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_role_field_not_returned(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '{"answer":"yes"}',
        }
        resp = self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        assert 'role' not in resp.json()


# ---------------------------------------------------------------------------
# Response passthrough tests
# ---------------------------------------------------------------------------

@ddt.ddt
class TestLearnerPathwaysResponsePassthrough(APITest):
    """Assert Xpert response content is returned verbatim without filtering."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.learning_intent_prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.LEARNER_INTENT,
        )

    def setUp(self):
        super().setUp()
        self.addCleanup(django_cache.clear)
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_extra_top_level_fields_preserved(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '{"result":"ok","extra_field":"preserved"}',
        }

        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )

        assert resp.status_code == status.HTTP_200_OK
        assert 'extra_field' in resp.json()

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_list_response_returned_as_list(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '[1,2,3]',
        }

        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )

        assert resp.status_code == status.HTTP_200_OK
        assert isinstance(resp.json(), list)

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_nested_values_preserved(self, mock_client_class):
        nested = {'a': {'b': {'c': [1, 2, 3]}}}

        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': json.dumps(nested),
        }

        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )

        assert resp.status_code == status.HTTP_200_OK
        assert resp.json() == nested


# ---------------------------------------------------------------------------
# Failure tests
# ---------------------------------------------------------------------------

@ddt.ddt
class TestLearnerPathwaysFailures(APITest):
    """500-series failure paths for the learning-intent endpoint."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.learning_intent_prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.LEARNER_INTENT,
        )

    def setUp(self):
        super().setUp()
        self.addCleanup(django_cache.clear)
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

    def test_missing_prompt_returns_500(self):
        with mock.patch.object(
                XpertLearnerPathwaysSystemPrompt,
                'get_current',
                return_value=None,
        ):
            resp = self.client.post(
                reverse(_LEARNING_INTENT_URL_NAME),
                data=_VALID_LEARNING_INTENT_PAYLOAD,
                format='json',
            )

        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    @ddt.data(
        XpertAPIConfigurationError,
        XpertAPIRequestError,
        XpertAPIResponseError,
    )
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_xpert_error_returns_500(self, error_class, mock_client_class):
        mock_client_class.return_value.send_message.side_effect = error_class('xpert error')
        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    @ddt.data(
        ('missing', {'role': 'assistant'}),
        ('none', {'role': 'assistant', 'content': None}),
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_bad_content_returns_500(
        self, _case, xpert_response, mock_client_class,
    ):
        mock_client_class.return_value.send_message.return_value = xpert_response
        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    @ddt.data(
        'not valid json',
        '```json\n{"key":"value"}\n```',
        '{"unterminated": true',
    )
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_invalid_json_content_returns_500(self, bad_content, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': bad_content,
        }
        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_no_second_xpert_call_on_failure(self, mock_client_class):
        mock_client_class.return_value.send_message.side_effect = XpertAPIRequestError('fail')
        self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        assert mock_client_class.return_value.send_message.call_count == 1

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_no_fallback_object_returned(self, mock_client_class):
        mock_client_class.return_value.send_message.side_effect = XpertAPIRequestError('fail')
        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        if resp.get('Content-Type', '').startswith('application/json'):
            body = resp.json()
            assert 'skills_required' not in body
            assert 'reasons' not in body
