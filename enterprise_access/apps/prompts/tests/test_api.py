"""
Tests for the prompts domain API module.
"""
import json
from unittest import mock

import ddt
from django.test import TestCase

from enterprise_access.apps.prompts import api as prompts_api
from enterprise_access.apps.prompts.api_client import (
    XpertAPIConfigurationError,
    XpertAPIError,
    XpertAPIRequestError,
    XpertAPIResponseError
)

PATCH_XPERT_CLIENT = 'enterprise_access.apps.prompts.api.XpertAPIClient'


@ddt.ddt
class TestGetCurrentPrompt(TestCase):
    """Tests for get_current_prompt."""

    def test_returns_prompt_when_found(self):
        prompt = mock.Mock()
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = prompt

        result = prompts_api.get_current_prompt(
            prompt_model=prompt_model,
            prompt_type='learner_intent',
        )

        self.assertIs(result, prompt)

    def test_exact_prompt_type_passed_to_get_current(self):
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = mock.Mock()

        prompts_api.get_current_prompt(
            prompt_model=prompt_model,
            prompt_type='learner_intent',
        )

        prompt_model.get_current.assert_called_once_with(prompt_type='learner_intent')

    def test_missing_prompt_raises_error(self):
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = None

        with self.assertRaises(prompts_api.PromptError):
            prompts_api.get_current_prompt(
                prompt_model=prompt_model,
                prompt_type='learner_intent',
            )

    def test_prompt_for_another_type_cannot_satisfy_lookup(self):
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = None

        with self.assertRaises(prompts_api.PromptError):
            prompts_api.get_current_prompt(
                prompt_model=prompt_model,
                prompt_type='recommendations_feedback',
            )

        prompt_model.get_current.assert_called_once_with(
            prompt_type='recommendations_feedback',
        )


@ddt.ddt
class TestBuildSystemPrompt(TestCase):
    """Tests for build_system_prompt."""

    def _make_prompt(self, system_prompt, output_schema=None):
        prompt = mock.Mock()
        prompt.system_prompt = system_prompt
        prompt.output_schema = output_schema
        return prompt

    def test_strips_surrounding_whitespace(self):
        prompt = self._make_prompt('  Be helpful.  ')
        result = prompts_api.build_system_prompt(prompt)
        self.assertEqual(result, 'Be helpful.')

    def test_non_empty_schema_appended(self):
        schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}}
        prompt = self._make_prompt('Be helpful.', output_schema=schema)

        result = prompts_api.build_system_prompt(prompt)

        self.assertIn('\n\nEXPECTED OUTPUT SCHEMA:\n', result)
        self.assertIn(json.dumps(schema, indent=2, sort_keys=True), result)

    @ddt.data(None, {})
    def test_empty_schema_not_appended(self, output_schema):
        prompt = self._make_prompt('Be helpful.', output_schema=output_schema)

        result = prompts_api.build_system_prompt(prompt)

        self.assertEqual(result, 'Be helpful.')
        self.assertNotIn('EXPECTED OUTPUT SCHEMA:', result)

    def test_prompt_instance_not_mutated(self):
        schema = {'key': 'value'}
        prompt = self._make_prompt('  Original.  ', output_schema=schema)

        prompts_api.build_system_prompt(prompt)

        self.assertEqual(prompt.system_prompt, '  Original.  ')
        self.assertIs(prompt.output_schema, schema)


@ddt.ddt
class TestBuildMessages(TestCase):
    """Tests for build_messages."""

    def test_builds_single_user_message_with_string_content(self):
        messages = prompts_api.build_messages({'name': 'Alice'})

        self.assertEqual(messages, [
            {'role': 'user', 'content': '{"name":"Alice"}'},
        ])
        self.assertIsInstance(messages[0]['content'], str)

    def test_content_is_compact_json(self):
        messages = prompts_api.build_messages({'name': 'Alice', 'count': 3})
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

        messages = prompts_api.build_messages(data)

        self.assertEqual(json.loads(messages[0]['content']), data)


@ddt.ddt
class TestSendXpertMessage(TestCase):
    """Tests for send_xpert_message."""

    def setUp(self):
        self.system_prompt = 'You are helpful.'
        self.messages = [{'role': 'user', 'content': '{"q":1}'}]
        self.conversation_id = 'enterprise-access:test-123'

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_client_called_once_with_correct_args(self, mock_client_class):
        mock_response = {'role': 'assistant', 'content': '{"answer":"yes"}'}
        mock_client_class.return_value.send_message.return_value = mock_response

        result = prompts_api.send_xpert_message(
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

        prompts_api.send_xpert_message(
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

        prompts_api.send_xpert_message(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
        )

        self.assertEqual(mock_client_class.return_value.send_message.call_count, 1)


@ddt.ddt
class TestSendXpertMessageErrors(TestCase):
    """Tests for XpertAPIError mapping to PromptError."""

    @ddt.data(
        XpertAPIError,
        XpertAPIConfigurationError,
        XpertAPIRequestError,
        XpertAPIResponseError,
    )
    @mock.patch(PATCH_XPERT_CLIENT)
    def test_xpert_errors_become_prompt_error(
        self,
        error_class,
        mock_client_class,
    ):
        original = error_class('original error text')
        mock_client_class.return_value.send_message.side_effect = original

        with self.assertRaises(prompts_api.PromptError) as ctx:
            prompts_api.send_xpert_message(
                system_prompt='prompt',
                messages=[],
                conversation_id='enterprise-access:x',
                prompt_type='learner_intent',
            )

        self.assertIs(ctx.exception.__cause__, original)
        self.assertIn('original error text', str(ctx.exception))
        self.assertEqual(mock_client_class.return_value.send_message.call_count, 1)


@ddt.ddt
class TestExtractXpertContent(TestCase):
    """Tests for extract_xpert_content."""

    def test_valid_response_returns_content_string(self):
        response = {'role': 'assistant', 'content': '{"answer":"yes"}'}

        result = prompts_api.extract_xpert_content(response)

        self.assertEqual(result, '{"answer":"yes"}')

    @ddt.data(
        {'role': 'assistant'},
        {'role': 'assistant', 'content': None},
    )
    def test_invalid_content_raises_error(self, response):
        with self.assertRaises(prompts_api.PromptError):
            prompts_api.extract_xpert_content(response)


@ddt.ddt
class TestParseJsonContent(TestCase):
    """Tests for parse_json_content."""

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
        result = prompts_api.parse_json_content(raw_content)
        self.assertEqual(result, expected)

    @ddt.data(
        'not valid json',
        '```json\n{"key":"value"}\n```',
        '```\n{"key":"value"}\n```',
        '{"unterminated": true',
        '',
    )
    def test_invalid_or_fenced_json_raises_error(self, raw_content):
        with self.assertRaises(prompts_api.PromptError):
            prompts_api.parse_json_content(raw_content)

    def test_invalid_json_exception_is_chained(self):
        with self.assertRaises(prompts_api.PromptError) as ctx:
            prompts_api.parse_json_content('not valid json')

        self.assertIsNotNone(ctx.exception.__cause__)
