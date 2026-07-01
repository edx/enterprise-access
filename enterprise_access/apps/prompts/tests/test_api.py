"""
Tests for the prompts domain API module.
"""
import json
from unittest import mock

import ddt
import pytest
from django.test import TestCase

from enterprise_access.apps.prompts import api as prompts_api
from enterprise_access.apps.prompts.api_client import (
    XpertAPIConfigurationError,
    XpertAPIError,
    XpertAPIRequestError,
    XpertAPIResponseError,
    XpertRequestMessage,
    XpertResponseMessage
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

        assert result is prompt

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

        with pytest.raises(prompts_api.PromptError):
            prompts_api.get_current_prompt(
                prompt_model=prompt_model,
                prompt_type='learner_intent',
            )

    def test_prompt_for_another_type_cannot_satisfy_lookup(self):
        prompt_model = mock.Mock()
        prompt_model.get_current.return_value = None

        with pytest.raises(prompts_api.PromptError):
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
        assert result == 'Be helpful.'

    def test_non_empty_schema_appended(self):
        schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}}
        prompt = self._make_prompt('Be helpful.', output_schema=schema)

        result = prompts_api.build_system_prompt(prompt)

        assert '\n\nEXPECTED OUTPUT SCHEMA:\n' in result
        assert json.dumps(schema, indent=2, sort_keys=True) in result

    @ddt.data(None, {})
    def test_empty_schema_not_appended(self, output_schema):
        prompt = self._make_prompt('Be helpful.', output_schema=output_schema)

        result = prompts_api.build_system_prompt(prompt)

        assert result == 'Be helpful.'
        assert 'EXPECTED OUTPUT SCHEMA:' not in result

    def test_prompt_instance_not_mutated(self):
        schema = {'key': 'value'}
        prompt = self._make_prompt('  Original.  ', output_schema=schema)

        prompts_api.build_system_prompt(prompt)

        assert prompt.system_prompt == '  Original.  '
        assert prompt.output_schema is schema


@ddt.ddt
class TestBuildMessages(TestCase):
    """Tests for build_messages."""

    def test_builds_single_user_message_with_string_content(self):
        messages = prompts_api.build_messages({'name': 'Alice'})

        assert messages == [
            XpertRequestMessage(role='user', content='{"name":"Alice"}'),
        ]
        assert isinstance(messages[0].content, str)

    def test_content_is_compact_json(self):
        messages = prompts_api.build_messages({'name': 'Alice', 'count': 3})
        content = messages[0].content

        assert ': ' not in content
        assert ', ' not in content
        assert json.loads(content) == {'name': 'Alice', 'count': 3}

    def test_nested_json_round_trips(self):
        data = {
            'name': 'Alice',
            'items': [1, 2, 3],
            'metadata': {'active': True, 'notes': None},
        }

        messages = prompts_api.build_messages(data)

        assert json.loads(messages[0].content) == data


@ddt.ddt
class TestSendXpertMessage(TestCase):
    """Tests for send_xpert_message."""

    def setUp(self):
        self.system_prompt = 'You are helpful.'
        self.messages = [XpertRequestMessage(role='user', content='{"q":1}')]
        self.conversation_id = 'enterprise-access:test-123'

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_client_called_once_with_correct_args(self, mock_client_class):
        mock_response = XpertResponseMessage(role='assistant', content='{"answer":"yes"}')
        mock_client_class.return_value.send_message.return_value = mock_response

        result = prompts_api.send_xpert_message(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
            tags=('tag1', 'tag2'),
            prompt_type='learner_intent',
        )

        assert result == prompts_api.ParsedXpertResponse(
            message=XpertResponseMessage(role='assistant', content='{"answer":"yes"}')
        )
        mock_client_class.return_value.send_message.assert_called_once_with(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
            tags=['tag1', 'tag2'],
        )

    @ddt.data(None, [], ())
    @mock.patch(PATCH_XPERT_CLIENT)
    def test_empty_tags_passed_as_none(self, tags, mock_client_class):
        mock_client_class.return_value.send_message.return_value = XpertResponseMessage(
            role='assistant',
            content='null',
        )

        prompts_api.send_xpert_message(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
            tags=tags,
        )

        assert mock_client_class.return_value.send_message.call_args.kwargs['tags'] is None

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_no_second_call_made(self, mock_client_class):
        mock_client_class.return_value.send_message.return_value = XpertResponseMessage(
            role='assistant',
            content='null',
        )

        prompts_api.send_xpert_message(
            system_prompt=self.system_prompt,
            messages=self.messages,
            conversation_id=self.conversation_id,
        )

        assert mock_client_class.return_value.send_message.call_count == 1


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

        with pytest.raises(prompts_api.PromptError) as exc_info:
            prompts_api.send_xpert_message(
                system_prompt='prompt',
                messages=[],
                conversation_id='enterprise-access:x',
                prompt_type='learner_intent',
            )

        assert exc_info.value.__cause__ is original
        assert 'original error text' in str(exc_info.value)
        assert mock_client_class.return_value.send_message.call_count == 1


class TestParsedXpertResponse(TestCase):
    """Tests for ParsedXpertResponse construction."""

    def test_can_be_constructed_with_xpert_response_message(self):
        message = XpertResponseMessage(role='assistant', content='{"answer":"yes"}')
        response = prompts_api.ParsedXpertResponse(message=message)
        assert response.message is message


@ddt.ddt
class TestXpertResponseAsJson(TestCase):
    """Tests for XpertResponse.as_json."""

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
        response = prompts_api.ParsedXpertResponse(message=XpertResponseMessage(role='assistant', content=raw_content))

        result = response.as_json()

        assert result == expected

    @ddt.data(
        'not valid json',
        '```json\n{"key":"value"}\n```',
        '```\n{"key":"value"}\n```',
        '{"unterminated": true',
        '',
    )
    def test_invalid_or_fenced_json_raises_error(self, raw_content):
        response = prompts_api.ParsedXpertResponse(message=XpertResponseMessage(role='assistant', content=raw_content))

        with pytest.raises(prompts_api.PromptError):
            response.as_json()

    def test_invalid_json_exception_is_chained(self):
        response = prompts_api.ParsedXpertResponse(
            message=XpertResponseMessage(role='assistant', content='not valid json')
        )

        with pytest.raises(prompts_api.PromptError) as exc_info:
            response.as_json()

        assert exc_info.value.__cause__ is not None
