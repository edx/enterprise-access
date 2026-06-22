"""
Tests for BasePromptViewSet and PromptRequestException.
"""
# pylint: disable=protected-access
import json
from unittest import mock

import ddt
from django.test import TestCase
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError

from enterprise_access.apps.api.v1.views.prompt import BasePromptViewSet, PromptRequestException
from enterprise_access.apps.prompts.api_client import (
    XpertAPIConfigurationError,
    XpertAPIError,
    XpertAPIRequestError,
    XpertAPIResponseError
)

PATCH_XPERT_CLIENT = 'enterprise_access.apps.api.v1.views.prompt.XpertAPIClient'
PATCH_GET_REQUEST_ID = 'enterprise_access.apps.api.v1.views.prompt.get_request_id'
PATCH_UUID4 = 'enterprise_access.apps.api.v1.views.prompt.uuid_module.uuid4'


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
