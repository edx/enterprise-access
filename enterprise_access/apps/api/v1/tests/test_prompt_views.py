"""
Tests for PromptRequestException, BasePromptViewSet, and LearnerPathwaysViewSet.

Domain logic tests are in enterprise_access.apps.prompts.tests.test_api.
This module focuses on HTTP-layer behavior: validation, permission checks,
throttling, error mapping, and response serialization.
"""
# pylint: disable=protected-access
import json
import uuid
from unittest import mock

import ddt
from django.conf import settings as django_settings
from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.reverse import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from enterprise_access.apps.api import serializers as api_serializers
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


def _make_viewset():
    """Return a bare BasePromptViewSet instance for helper tests."""
    viewset = BasePromptViewSet()
    viewset.request = mock.Mock()
    viewset.kwargs = {}
    viewset.format_kwarg = None
    return viewset


def _make_request(data=None, headers=None):
    """Return a mock DRF request."""
    request = mock.Mock()
    request.data = data or {}
    request.headers = headers or {}
    return request


class _SampleSerializer(serializers.Serializer):
    """Minimal serializer used in request-validation tests."""
    name = serializers.CharField()
    count = serializers.IntegerField(required=False, default=0)

    def create(self, validated_data):
        """Create is unused for validation-only tests."""
        return validated_data

    def update(self, instance, validated_data):
        """Update is unused for validation-only tests."""
        return validated_data


@ddt.ddt
class TestPromptRequestException(TestCase):
    """Tests for PromptRequestException."""

    def test_status_code_is_500(self):
        exc = PromptRequestException('something went wrong')
        self.assertEqual(exc.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_detail_message_is_preserved(self):
        exc = PromptRequestException('something went wrong')
        self.assertIn('something went wrong', str(exc.detail))

    def test_args_populated_with_message(self):
        exc = PromptRequestException('my error message')
        self.assertEqual(exc.args[0], 'my error message')

    def test_exception_chaining_preserved(self):
        original = prompts_api.PromptError('original error')
        try:
            raise PromptRequestException('wrapped') from original
        except PromptRequestException as exc:
            self.assertIs(exc.__cause__, original)


@ddt.ddt
class TestValidateRequest(TestCase):
    """Tests for _validate_request."""

    def setUp(self):
        self.viewset = _make_viewset()

    def test_valid_data_returns_validated_data(self):
        request = _make_request({'name': 'Alice', 'count': 3})
        result = self.viewset._validate_request(request, _SampleSerializer)
        self.assertEqual(result, {'name': 'Alice', 'count': 3})

    def test_valid_data_with_defaults(self):
        request = _make_request({'name': 'Bob'})
        result = self.viewset._validate_request(request, _SampleSerializer)
        self.assertEqual(result, {'name': 'Bob', 'count': 0})

    @ddt.ddt
    class _Unused:
        """Avoid nested TestCase discovery issues."""

    @ddt.data(
        {},
        {'name': 'Alice', 'count': 'not-an-int'},
    )
    def test_invalid_data_raises_validation_error(self, payload):
        request = _make_request(payload)
        with self.assertRaises(ValidationError) as ctx:
            self.viewset._validate_request(request, _SampleSerializer)
        self.assertEqual(ctx.exception.status_code, status.HTTP_400_BAD_REQUEST)

    def test_serializer_context_includes_request_format_and_view(self):
        request = _make_request({'name': 'Test'})
        captured = {}

        class ContextCapturingSerializer(_SampleSerializer):
            def is_valid(self, *, raise_exception=False):
                captured['context'] = self.context
                return super().is_valid(raise_exception=raise_exception)

        self.viewset._validate_request(request, ContextCapturingSerializer)

        self.assertIs(captured['context']['request'], request)
        self.assertIs(captured['context']['view'], self.viewset)
        self.assertIn('format', captured['context'])
# Domain logic tests are in enterprise_access.apps.prompts.tests.test_api.
# This test module focuses on HTTP-layer behavior in viewsets.


# ---------------------------------------------------------------------------
# Serializer tests
# ---------------------------------------------------------------------------

@ddt.ddt
class TestLearningIntentRequestSerializer(TestCase):
    """Tests for LearningIntentRequestSerializer."""

    def _valid(self):
        return dict(_VALID_LEARNING_INTENT_PAYLOAD)

    def test_valid_payload_succeeds(self):
        s = api_serializers.LearningIntentRequestSerializer(data=self._valid())
        self.assertTrue(s.is_valid(), s.errors)

    @ddt.data('selected_goals', 'free_text', 'known_context')
    def test_missing_field_fails(self, field):
        data = self._valid()
        del data[field]
        s = api_serializers.LearningIntentRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn(field, s.errors)

    @ddt.data('selected_goals', 'free_text', 'known_context')
    def test_blank_field_fails(self, field):
        data = self._valid()
        data[field] = ''
        s = api_serializers.LearningIntentRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn(field, s.errors)

    @ddt.data('selected_goals', 'free_text', 'known_context')
    def test_whitespace_only_field_fails(self, field):
        data = self._valid()
        data[field] = '   '
        s = api_serializers.LearningIntentRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn(field, s.errors)

    @ddt.data(
        ('selected_goals', 123),
        ('free_text', []),
        ('known_context', {'nested': True}),
    )
    @ddt.unpack
    def test_non_string_value_coerced_or_fails(self, field, value):
        data = self._valid()
        data[field] = value
        s = api_serializers.LearningIntentRequestSerializer(data=data)
        # DRF CharField coerces non-strings; result must still be non-blank.
        # 123 → '123' (valid), [] → '' (invalid blank), {} → repr (valid)
        if s.is_valid():
            self.assertIsInstance(s.validated_data[field], str)
            self.assertGreater(len(s.validated_data[field]), 0)
        else:
            self.assertIn(field, s.errors)


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------

class TestLearnerPathwaysRouting(TestCase):
    """Tests for URL resolution of LearnerPathwaysViewSet."""

    def test_learning_intent_url_reverses(self):
        url = reverse(_LEARNING_INTENT_URL_NAME)
        self.assertIn('learner-pathways', url)
        self.assertIn('learning-intent', url)

    def test_learning_intent_post_accepted(self):
        client = APIClient()
        url = reverse(_LEARNING_INTENT_URL_NAME)
        response = client.post(url, data={}, format='json')
        # Unauthenticated — 401 or 403, but NOT 405.
        self.assertNotEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_learning_intent_get_rejected(self):
        url = reverse(_LEARNING_INTENT_URL_NAME)
        client = APIClient()
        user = UserFactory(is_active=True)
        client.force_authenticate(user=user)
        response = client.get(url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


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
        self.assertIn(JwtAuthentication, ac)

    def test_learning_intent_is_authenticated_permission(self):
        pc = self._get_action('learning_intent').kwargs.get('permission_classes', ())
        self.assertIn(permissions.IsAuthenticated, pc)

    def test_learning_intent_throttle_class(self):
        tc = self._get_action('learning_intent').kwargs.get('throttle_classes', ())
        self.assertIn(ScopedRateThrottle, tc)

    def test_learning_intent_throttle_scope(self):
        scope = self._get_action('learning_intent').kwargs.get('throttle_scope')
        self.assertEqual(scope, 'learner_pathways_learning_intent')

    def test_no_throttle_on_base_prompt_viewset(self):
        # throttle_classes must not be explicitly defined on BasePromptViewSet itself
        self.assertNotIn('throttle_classes', BasePromptViewSet.__dict__)
        self.assertNotIn('throttle_scope', BasePromptViewSet.__dict__)

    def test_no_class_level_throttle_classes_on_learner_pathways_viewset(self):
        self.assertNotIn('throttle_classes', LearnerPathwaysViewSet.__dict__)

    def test_throttle_scope_sentinel_is_none(self):
        self.assertIsNone(LearnerPathwaysViewSet.throttle_scope)


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

    @ddt.data(_LEARNING_INTENT_URL_NAME)
    def test_unauthenticated_caller_is_rejected(self, url_name):
        self.client.logout()
        self.client.cookies.clear()
        url = reverse(url_name)
        response = self.client.post(url, data={}, format='json')
        self.assertIn(response.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])

    @ddt.data(
        (_LEARNING_INTENT_URL_NAME, _VALID_LEARNING_INTENT_PAYLOAD),
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_enterprise_learner_is_allowed(
        self, url_name, payload, mock_client_class,
    ):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '{"result":"ok"}',
        }
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])
        url = reverse(url_name)
        response = self.client.post(url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @ddt.data(_LEARNING_INTENT_URL_NAME)
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_xpert_not_called_when_auth_fails(self, url_name, mock_client_class):
        self.set_jwt_cookie([])
        url = reverse(url_name)
        self.client.post(url, data={}, format='json')
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
        self.assertIn('learner_pathways_learning_intent', rates)

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
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = self.client.post(url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


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
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

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
        self.assertEqual(call_kwargs['tags'], ['tag-a', 'tag-b'])

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_xpert_called_exactly_once(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        self.assertEqual(mock_client_class.return_value.send_message.call_count, 1)

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_full_parsed_json_returned(self, mock_client_class):
        payload_json = '{"skills_required":["python","ml"],"condensed_algolia_query":"data"}'
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': payload_json,
        }
        resp = self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), json.loads(payload_json))

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_validated_data_encoded_as_user_message(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        call_kwargs = mock_client_class.return_value.send_message.call_args.kwargs
        messages = call_kwargs['messages']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['role'], 'user')
        self.assertIsInstance(messages[0]['content'], str)
        parsed = json.loads(messages[0]['content'])
        self.assertEqual(parsed['selected_goals'], _VALID_LEARNING_INTENT_PAYLOAD['selected_goals'])

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_conversation_id_has_prefix(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        call_kwargs = mock_client_class.return_value.send_message.call_args.kwargs
        self.assertTrue(call_kwargs['conversation_id'].startswith('enterprise-access:'))

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_role_field_not_returned(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '{"answer":"yes"}',
        }
        resp = self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        self.assertNotIn('role', resp.json())


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

    @ddt.data(
        ('learning_intent', _LEARNING_INTENT_URL_NAME, _VALID_LEARNING_INTENT_PAYLOAD),
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_extra_top_level_fields_preserved(
        self, _action, url_name, payload, mock_client_class,
    ):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '{"result":"ok","extra_field":"preserved"}',
        }
        resp = self.client.post(reverse(url_name), data=payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('extra_field', resp.json())

    @ddt.data(
        ('learning_intent', _LEARNING_INTENT_URL_NAME, _VALID_LEARNING_INTENT_PAYLOAD),
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_list_response_returned_as_list(
        self, _action, url_name, payload, mock_client_class,
    ):
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '[1,2,3]',
        }
        resp = self.client.post(reverse(url_name), data=payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsInstance(resp.json(), list)

    @ddt.data(
        ('learning_intent', _LEARNING_INTENT_URL_NAME, _VALID_LEARNING_INTENT_PAYLOAD),
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_nested_values_preserved(
        self, _action, url_name, payload, mock_client_class,
    ):
        nested = {'a': {'b': {'c': [1, 2, 3]}}}
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': json.dumps(nested),
        }
        resp = self.client.post(reverse(url_name), data=payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), nested)


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

    @ddt.data(
        (_LEARNING_INTENT_URL_NAME, _VALID_LEARNING_INTENT_PAYLOAD),
    )
    @ddt.unpack
    def test_missing_prompt_returns_500(self, url_name, payload):
        with mock.patch.object(XpertLearnerPathwaysSystemPrompt, 'get_current', return_value=None):
            resp = self.client.post(reverse(url_name), data=payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

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
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

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
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

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
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_no_second_xpert_call_on_failure(self, mock_client_class):
        mock_client_class.return_value.send_message.side_effect = XpertAPIRequestError('fail')
        self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        self.assertEqual(mock_client_class.return_value.send_message.call_count, 1)

    @mock.patch('enterprise_access.apps.prompts.api.XpertAPIClient')
    def test_no_fallback_object_returned(self, mock_client_class):
        mock_client_class.return_value.send_message.side_effect = XpertAPIRequestError('fail')
        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        if resp.get('Content-Type', '').startswith('application/json'):
            body = resp.json()
            self.assertNotIn('skills_required', body)
            self.assertNotIn('reasons', body)
