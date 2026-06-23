"""
Tests for BasePromptViewSet, PromptRequestException, and LearnerPathwaysViewSet.
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
from enterprise_access.apps.api.v1.views.prompt import (
    BasePromptViewSet,
    IsEnterpriseLearner,
    LearnerPathwaysViewSet,
    PromptRequestException
)
from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.prompts.api_client import (
    XpertAPIConfigurationError,
    XpertAPIError,
    XpertAPIRequestError,
    XpertAPIResponseError
)
from enterprise_access.apps.prompts.models import PromptType, XpertLearnerPathwaysSystemPrompt
from enterprise_access.apps.prompts.tests.factories import XpertLearnerPathwaysSystemPromptFactory
from test_utils import APITest

PATCH_XPERT_CLIENT = 'enterprise_access.apps.api.v1.views.prompt.XpertAPIClient'
PATCH_GET_REQUEST_ID = 'enterprise_access.apps.api.v1.views.prompt.get_request_id'
PATCH_UUID4 = 'enterprise_access.apps.api.v1.views.prompt.uuid_module.uuid4'
PATCH_CONTEXTS_ACCESSIBLE = 'enterprise_access.apps.api.v1.views.prompt.contexts_accessible_from_request'

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
        original = XpertAPIError('original error')
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


@ddt.ddt
class TestGetCurrentPrompt(TestCase):
    """Tests for _get_current_prompt."""

    def setUp(self):
        self.viewset = _make_viewset()

    def test_returns_prompt_when_found(self):
        prompt = mock.Mock()
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = prompt

        result = self.viewset._get_current_prompt(
            prompt_model=prompt_model,
            prompt_type='learner_intent',
        )

        self.assertIs(result, prompt)

    def test_exact_prompt_type_passed_to_get_current(self):
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = mock.Mock()

        self.viewset._get_current_prompt(
            prompt_model=prompt_model,
            prompt_type='learner_intent',
        )

        prompt_model.get_current.assert_called_once_with(prompt_type='learner_intent')

    @ddt.data(
        (None, 'learner_intent'),
        (mock.Mock(), None),
    )
    @ddt.unpack
    def test_missing_configuration_raises_500(self, prompt_model, prompt_type):
        with self.assertRaises(PromptRequestException) as ctx:
            self.viewset._get_current_prompt(
                prompt_model=prompt_model,
                prompt_type=prompt_type,
            )

        self.assertEqual(ctx.exception.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_missing_prompt_raises_500(self):
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = None

        with self.assertRaises(PromptRequestException) as ctx:
            self.viewset._get_current_prompt(
                prompt_model=prompt_model,
                prompt_type='learner_intent',
            )

        self.assertEqual(ctx.exception.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_prompt_for_another_type_cannot_satisfy_lookup(self):
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = None

        with self.assertRaises(PromptRequestException):
            self.viewset._get_current_prompt(
                prompt_model=prompt_model,
                prompt_type='recommendations_feedback',
            )

        prompt_model.get_current.assert_called_once_with(
            prompt_type='recommendations_feedback',
        )


@ddt.ddt
class TestBuildSystemPrompt(TestCase):
    """Tests for _build_system_prompt."""

    def setUp(self):
        self.viewset = _make_viewset()

    def _make_prompt(self, system_prompt, output_schema=None):
        prompt = mock.Mock()
        prompt.system_prompt = system_prompt
        prompt.output_schema = output_schema
        return prompt

    def test_strips_surrounding_whitespace(self):
        prompt = self._make_prompt('  Be helpful.  ')
        result = self.viewset._build_system_prompt(prompt)
        self.assertEqual(result, 'Be helpful.')

    def test_non_empty_schema_appended(self):
        schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}}
        prompt = self._make_prompt('Be helpful.', output_schema=schema)

        result = self.viewset._build_system_prompt(prompt)

        self.assertIn('\n\nEXPECTED OUTPUT SCHEMA:\n', result)
        self.assertIn(json.dumps(schema, indent=2, sort_keys=True), result)

    @ddt.data(None, {})
    def test_empty_schema_not_appended(self, output_schema):
        prompt = self._make_prompt('Be helpful.', output_schema=output_schema)

        result = self.viewset._build_system_prompt(prompt)

        self.assertEqual(result, 'Be helpful.')
        self.assertNotIn('EXPECTED OUTPUT SCHEMA:', result)

    def test_prompt_instance_not_mutated(self):
        schema = {'key': 'value'}
        prompt = self._make_prompt('  Original.  ', output_schema=schema)

        self.viewset._build_system_prompt(prompt)

        self.assertEqual(prompt.system_prompt, '  Original.  ')
        self.assertIs(prompt.output_schema, schema)


@ddt.ddt
class TestBuildMessages(TestCase):
    """Tests for _build_messages."""

    def setUp(self):
        self.viewset = _make_viewset()

    def test_builds_single_user_message_with_string_content(self):
        messages = self.viewset._build_messages({'name': 'Alice'})

        self.assertEqual(messages, [
            {'role': 'user', 'content': '{"name":"Alice"}'},
        ])
        self.assertIsInstance(messages[0]['content'], str)

    def test_content_is_compact_json(self):
        messages = self.viewset._build_messages({'name': 'Alice', 'count': 3})
        content = messages[0]['content']

        self.assertNotIn(': ', content)
        self.assertNotIn(', ', content)
        self.assertEqual(json.loads(content), {'name': 'Alice', 'count': 3})

    def test_nested_json_round_trips(self):
        data = {
            'name': 'Alice',
            'items': [1, 2, 3],
            'metadata': {'active': True, 'notes': None},
        }

        messages = self.viewset._build_messages(data)

        self.assertEqual(json.loads(messages[0]['content']), data)


@ddt.ddt
class TestGetConversationId(TestCase):
    """Tests for _get_conversation_id."""

    def setUp(self):
        self.viewset = _make_viewset()

    @mock.patch(PATCH_GET_REQUEST_ID, return_value='from-crum')
    def test_repo_request_id_helper_takes_precedence(self, mock_get_request_id):
        request = _make_request(headers={'X-Request-ID': 'from-header'})

        result = self.viewset._get_conversation_id(request)

        self.assertEqual(result, 'enterprise-access:from-crum')
        mock_get_request_id.assert_called_once_with()

    @mock.patch(PATCH_GET_REQUEST_ID, return_value=None)
    def test_header_used_when_repo_helper_returns_none(self, mock_get_request_id):
        request = _make_request(headers={'X-Request-ID': 'from-header'})

        result = self.viewset._get_conversation_id(request)

        self.assertEqual(result, 'enterprise-access:from-header')
        mock_get_request_id.assert_called_once_with()

    @mock.patch(PATCH_UUID4, return_value='generated-uuid')
    @mock.patch(PATCH_GET_REQUEST_ID, return_value=None)
    def test_uuid_generated_when_no_request_id(self, mock_get_request_id, mock_uuid4):
        request = _make_request(headers={})

        result = self.viewset._get_conversation_id(request)

        self.assertEqual(result, 'enterprise-access:generated-uuid')
        mock_get_request_id.assert_called_once_with()
        mock_uuid4.assert_called_once_with()

    @ddt.data(
        ('from-crum', {'X-Request-ID': 'from-header'}),
        (None, {'X-Request-ID': 'from-header'}),
        (None, {}),
    )
    @ddt.unpack
    def test_result_always_has_prefix(self, helper_value, headers):
        request = _make_request(headers=headers)

        with mock.patch(PATCH_GET_REQUEST_ID, return_value=helper_value):
            result = self.viewset._get_conversation_id(request)

        self.assertTrue(result.startswith('enterprise-access:'))


@ddt.ddt
class TestSendXpertMessage(TestCase):
    """Tests for _send_xpert_message."""

    def setUp(self):
        self.viewset = _make_viewset()
        self.system_prompt = 'You are helpful.'
        self.messages = [{'role': 'user', 'content': '{"q":1}'}]
        self.conversation_id = 'enterprise-access:test-123'

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_client_called_once_with_correct_args(self, mock_client_class):
        mock_response = {'role': 'assistant', 'content': '{"answer":"yes"}'}
        mock_client_class.return_value.send_message.return_value = mock_response

        result = self.viewset._send_xpert_message(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
            tags=('tag1', 'tag2'),
            prompt_type='learner_intent',
        )

        self.assertEqual(result, mock_response)
        mock_client_class.return_value.send_message.assert_called_once_with(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
            tags=['tag1', 'tag2'],
        )

    @ddt.data(None, [], ())
    @mock.patch(PATCH_XPERT_CLIENT)
    def test_empty_tags_passed_as_none(self, tags, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {}

        self.viewset._send_xpert_message(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
            tags=tags,
        )

        self.assertIsNone(
            mock_client_class.return_value.send_message.call_args.kwargs['tags'],
        )

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_no_second_call_made(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = {}

        self.viewset._send_xpert_message(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
        )

        self.assertEqual(mock_client_class.return_value.send_message.call_count, 1)


@ddt.ddt
class TestSendXpertMessageErrors(TestCase):
    """Tests for XpertAPIError mapping."""

    def setUp(self):
        self.viewset = _make_viewset()

    @ddt.data(
        XpertAPIError,
        XpertAPIConfigurationError,
        XpertAPIRequestError,
        XpertAPIResponseError,
    )
    @mock.patch(PATCH_XPERT_CLIENT)
    def test_xpert_errors_become_prompt_request_exception(
        self,
        error_class,
        mock_client_class,
    ):
        original = error_class('original error text')
        mock_client_class.return_value.send_message.side_effect = original

        with self.assertRaises(PromptRequestException) as ctx:
            self.viewset._send_xpert_message(
                system_prompt='prompt',
                messages=[],
                conversation_id='enterprise-access:x',
                prompt_type='learner_intent',
            )

        self.assertEqual(ctx.exception.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIs(ctx.exception.__cause__, original)
        self.assertIn('original error text', ctx.exception.args[0])
        self.assertEqual(mock_client_class.return_value.send_message.call_count, 1)


@ddt.ddt
class TestExtractXpertContent(TestCase):
    """Tests for _extract_xpert_content."""

    def setUp(self):
        self.viewset = _make_viewset()

    def test_valid_response_returns_content_string(self):
        response = {'role': 'assistant', 'content': '{"answer":"yes"}'}

        result = self.viewset._extract_xpert_content(response)

        self.assertEqual(result, '{"answer":"yes"}')

    @ddt.data(
        {'role': 'assistant'},
        {'role': 'assistant', 'content': None},
        {'role': 'assistant', 'content': {'nested': 'dict'}},
        {'role': 'assistant', 'content': 123},
    )
    def test_invalid_content_raises_500(self, response):
        with self.assertRaises(PromptRequestException) as ctx:
            self.viewset._extract_xpert_content(response)

        self.assertEqual(ctx.exception.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)


@ddt.ddt
class TestParseJsonContent(TestCase):
    """Tests for _parse_json_content."""

    def setUp(self):
        self.viewset = _make_viewset()

    @ddt.data(
        ('{"answer":42}', {'answer': 42}),
        ('[1,2,3]', [1, 2, 3]),
        ('"hello"', 'hello'),
        ('99', 99),
        ('false', False),
        ('true', True),
        ('null', None),
        ('  {"trimmed":true}  ', {'trimmed': True}),
    )
    @ddt.unpack
    def test_valid_json_values_returned_unchanged(self, raw_content, expected):
        result = self.viewset._parse_json_content(raw_content)
        self.assertEqual(result, expected)

    @ddt.data(
        'not valid json',
        '```json\n{"key":"value"}\n```',
        '```\n{"key":"value"}\n```',
        '{"unterminated": true',
        '',
    )
    def test_invalid_or_fenced_json_raises_500(self, raw_content):
        with self.assertRaises(PromptRequestException) as ctx:
            self.viewset._parse_json_content(raw_content)

        self.assertEqual(ctx.exception.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_invalid_json_exception_is_chained(self):
        with self.assertRaises(PromptRequestException) as ctx:
            self.viewset._parse_json_content('not valid json')

        self.assertIsNotNone(ctx.exception.__cause__)
        self.assertIsInstance(ctx.exception.__cause__, json.JSONDecodeError)

    def test_no_repair_or_fallback_on_bad_json(self):
        with self.assertRaises(PromptRequestException):
            self.viewset._parse_json_content('garbage')

    def test_parse_json_content_requires_string_contract(self):
        with self.assertRaises(AttributeError):
            self.viewset._parse_json_content({'not': 'a string'})


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

    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_learning_intent_get_rejected(self, _mock_contexts):
        _mock_contexts.return_value = {str(uuid.uuid4())}
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

    def test_learning_intent_is_enterprise_learner_permission(self):
        pc = self._get_action('learning_intent').kwargs.get('permission_classes', ())
        self.assertIn(IsEnterpriseLearner, pc)

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

    @ddt.data(_LEARNING_INTENT_URL_NAME)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE, return_value=set())
    def test_authenticated_non_enterprise_user_rejected(self, url_name, _mock_contexts):
        self.set_jwt_cookie([])
        url = reverse(url_name)
        response = self.client.post(url, data={}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @ddt.data(
        (_LEARNING_INTENT_URL_NAME, _VALID_LEARNING_INTENT_PAYLOAD),
    )
    @ddt.unpack
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_enterprise_learner_is_allowed(
        self, url_name, payload, mock_contexts, mock_client_class,
    ):
        mock_contexts.return_value = {str(uuid.uuid4())}
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
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE, return_value=set())
    def test_xpert_not_called_when_auth_fails(self, url_name, _mock_contexts, mock_client_class):
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
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_learning_intent_throttled_after_rate_exceeded(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        url = reverse(_LEARNING_INTENT_URL_NAME)
        for _ in range(2):
            resp = self.client.post(url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = self.client.post(url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE, return_value=set())
    def test_auth_failure_does_not_call_xpert(self, _mock_contexts):
        with mock.patch(PATCH_XPERT_CLIENT) as mock_client_class:
            url = reverse(_LEARNING_INTENT_URL_NAME)
            self.client.post(url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
            mock_client_class.return_value.send_message.assert_not_called()


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

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_http_200_with_valid_payload(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': '{"skills_required":["python"]}',
        }
        resp = self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_correct_prompt_type_used(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
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

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_server_controlled_tags_passed(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        with override_settings(XPERT_LEARNER_PATHWAYS_RAG_TAGS=['tag-a', 'tag-b']):
            self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        call_kwargs = mock_client_class.return_value.send_message.call_args.kwargs
        self.assertEqual(call_kwargs['tags'], ['tag-a', 'tag-b'])

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_xpert_called_exactly_once(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        self.assertEqual(mock_client_class.return_value.send_message.call_count, 1)

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_full_parsed_json_returned(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
        payload_json = '{"skills_required":["python","ml"],"condensed_algolia_query":"data"}'
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant',
            'content': payload_json,
        }
        resp = self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), json.loads(payload_json))

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_validated_data_encoded_as_user_message(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
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

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_conversation_id_has_prefix(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': '{"r":1}',
        }
        self.client.post(self.url, data=_VALID_LEARNING_INTENT_PAYLOAD, format='json')
        call_kwargs = mock_client_class.return_value.send_message.call_args.kwargs
        self.assertTrue(call_kwargs['conversation_id'].startswith('enterprise-access:'))

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_role_field_not_returned(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
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
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_extra_top_level_fields_preserved(
        self, _action, url_name, payload, mock_contexts, mock_client_class,
    ):
        mock_contexts.return_value = {str(uuid.uuid4())}
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
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_list_response_returned_as_list(
        self, _action, url_name, payload, mock_contexts, mock_client_class,
    ):
        mock_contexts.return_value = {str(uuid.uuid4())}
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
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_nested_values_preserved(
        self, _action, url_name, payload, mock_contexts, mock_client_class,
    ):
        mock_contexts.return_value = {str(uuid.uuid4())}
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
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_missing_prompt_returns_500(self, url_name, payload, mock_contexts):
        mock_contexts.return_value = {str(uuid.uuid4())}
        with mock.patch.object(XpertLearnerPathwaysSystemPrompt, 'get_current', return_value=None):
            resp = self.client.post(reverse(url_name), data=payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @ddt.data(
        XpertAPIConfigurationError,
        XpertAPIRequestError,
        XpertAPIResponseError,
    )
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_xpert_error_returns_500(self, error_class, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
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
        ('non_string', {'role': 'assistant', 'content': 123}),
    )
    @ddt.unpack
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_bad_content_returns_500(
        self, _case, xpert_response, mock_contexts, mock_client_class,
    ):
        mock_contexts.return_value = {str(uuid.uuid4())}
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
    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_invalid_json_content_returns_500(self, bad_content, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
        mock_client_class.return_value.send_message.return_value = {
            'role': 'assistant', 'content': bad_content,
        }
        resp = self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_no_second_xpert_call_on_failure(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
        mock_client_class.return_value.send_message.side_effect = XpertAPIRequestError('fail')
        self.client.post(
            reverse(_LEARNING_INTENT_URL_NAME),
            data=_VALID_LEARNING_INTENT_PAYLOAD,
            format='json',
        )
        self.assertEqual(mock_client_class.return_value.send_message.call_count, 1)

    @mock.patch(PATCH_XPERT_CLIENT)
    @mock.patch(PATCH_CONTEXTS_ACCESSIBLE)
    def test_no_fallback_object_returned(self, mock_contexts, mock_client_class):
        mock_contexts.return_value = {str(uuid.uuid4())}
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
