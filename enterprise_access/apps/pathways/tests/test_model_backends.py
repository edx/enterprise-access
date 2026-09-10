"""
Tests for the model-backend adapter.

The interchangeability tests are the point of the chunk: they assert all three backends are
substitutable through the same call, which is what makes "compare the models" a query
rather than a rig. Nothing here touches the network -- Xpert is patched at the domain
function, and the two metered backends take an injected client.
"""
import json
from types import SimpleNamespace
from unittest import mock

import ddt
from django.test import TestCase, override_settings

from enterprise_access.apps.pathways.model_backends import (
    CLAUDE_BACKEND,
    OPENAI_BACKEND,
    XPERT_BACKEND,
    ClaudeBackend,
    ModelBackendConfigurationError,
    ModelBackendRequestError,
    ModelResponse,
    ModelResponseParseError,
    OpenAIBackend,
    XpertBackend,
    get_model_backend
)
from enterprise_access.apps.prompts.api import PromptError
from enterprise_access.apps.prompts.api_client import XpertAPIError
from enterprise_access.apps.prompts.models import PromptType

PATCH_GET_PROMPT = 'enterprise_access.apps.pathways.model_backends.xpert.prompts_api.get_current_prompt'
PATCH_SEND = 'enterprise_access.apps.pathways.model_backends.xpert.prompts_api.send_xpert_message'


def fake_anthropic_message(text='{"ok": true}', *, input_tokens=120, output_tokens=45,
                           model='claude-sonnet-5', stop_reason='end_turn'):
    """Build a stand-in for a Messages API response object."""
    return SimpleNamespace(
        content=[SimpleNamespace(text=text, type='text')],
        model=model,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def fake_openai_completion(text='{"ok": true}', *, prompt_tokens=90, completion_tokens=30,
                           model='gpt-4o', finish_reason='stop'):
    """Build a stand-in for a chat-completions response object."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text), finish_reason=finish_reason,
        )],
        model=model,
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def fake_openai_client(completion=None, error=None):
    """A client whose ``chat.completions.create`` returns a completion or raises."""
    create = (mock.Mock(side_effect=error) if error
              else mock.Mock(return_value=completion or fake_openai_completion()))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def fake_client(message=None, error=None):
    """A client whose ``messages.create`` returns ``message`` or raises ``error``."""
    create = mock.Mock(side_effect=error) if error else mock.Mock(return_value=message or fake_anthropic_message())
    return SimpleNamespace(messages=SimpleNamespace(create=create))


class TestModelResponse(TestCase):
    """
    Tests for ``ModelResponse``.
    """

    def test_total_tokens_sums_both_sides(self):
        response = ModelResponse(content='', backend='x', input_tokens=10, output_tokens=5)

        self.assertEqual(response.total_tokens, 15)

    def test_total_tokens_is_none_when_either_side_is_unreported(self):
        """
        Xpert reports no usage. Zero would read as "this call was free", which is a
        measurement rather than the absence of one.
        """
        self.assertIsNone(ModelResponse(content='', backend='x', output_tokens=5).total_tokens)
        self.assertIsNone(ModelResponse(content='', backend='x', input_tokens=5).total_tokens)

    def test_as_json_parses_content(self):
        response = ModelResponse(content=' {"a": 1} ', backend='x')

        self.assertEqual(response.as_json(), {'a': 1})

    def test_as_json_raises_a_typed_error_without_echoing_the_body(self):
        response = ModelResponse(content='sorry, I cannot do that', backend='xpert')

        with self.assertRaises(ModelResponseParseError) as ctx:
            response.as_json()

        message = str(ctx.exception)
        self.assertIn('xpert', message)
        self.assertNotIn('sorry, I cannot do that', message)

    def test_the_trace_dict_excludes_the_response_body(self):
        """
        A response can quote the learner's own intake back, so it stays out of the trace.
        The step persists its own parsed output instead.
        """
        response = ModelResponse(
            content='learner said something private', backend='claude',
            model='claude-sonnet-5', input_tokens=1, output_tokens=2, elapsed_ms=99,
        )

        trace = response.to_trace_dict()

        self.assertNotIn('content', trace)
        self.assertEqual(trace, {
            'backend': 'claude', 'model': 'claude-sonnet-5',
            'input_tokens': 1, 'output_tokens': 2, 'elapsed_ms': 99,
        })


@ddt.ddt
class TestXpertBackend(TestCase):
    """
    Tests for ``XpertBackend``.
    """

    def setUp(self):
        super().setUp()
        self.prompt = mock.Mock(modified=None)
        self.prompt.history.first.return_value = SimpleNamespace(history_id=7)

    @mock.patch(PATCH_SEND)
    @mock.patch(PATCH_GET_PROMPT)
    def test_a_completion_returns_a_normalised_response(self, mock_prompt, mock_send):
        mock_prompt.return_value = self.prompt
        mock_send.return_value = SimpleNamespace(role='assistant', content='{"a": 1}')

        response = XpertBackend(prompt_type=PromptType.LEARNER_INTENT).complete(
            system_prompt='ignored', user_content='{}', trace_id='trace-1',
        )

        self.assertEqual(response.content, '{"a": 1}')
        self.assertEqual(response.backend, XPERT_BACKEND)
        self.assertEqual(response.as_json(), {'a': 1})

    @mock.patch(PATCH_SEND)
    @mock.patch(PATCH_GET_PROMPT)
    def test_the_prompt_revision_is_recorded(self, mock_prompt, mock_send):
        """
        A pathway generated last week was generated by wording that may have changed
        since, so a regression between runs has to be attributable to the prompt.
        """
        mock_prompt.return_value = self.prompt
        mock_send.return_value = SimpleNamespace(role='assistant', content='{}')

        response = XpertBackend(prompt_type=PromptType.LEARNER_INTENT).complete(
            system_prompt='', user_content='{}', trace_id='trace-1',
        )

        self.assertEqual(response.metadata['prompt_revision'], '7')

    @mock.patch(PATCH_GET_PROMPT)
    def test_a_missing_prompt_row_is_a_configuration_error(self, mock_prompt):
        """Not transient: no request was sent, so a caller must not retry it."""
        mock_prompt.side_effect = PromptError('nope')

        with self.assertRaises(ModelBackendConfigurationError):
            XpertBackend(prompt_type=PromptType.LEARNER_INTENT).complete(
                system_prompt='', user_content='{}', trace_id='t',
            )

    @ddt.data(PromptError('boom'), XpertAPIError('boom'))
    def test_a_failed_request_is_a_request_error(self, error):
        with mock.patch(PATCH_GET_PROMPT) as mock_prompt, mock.patch(PATCH_SEND) as mock_send:
            mock_prompt.return_value = self.prompt
            mock_send.side_effect = error

            with self.assertRaises(ModelBackendRequestError):
                XpertBackend(prompt_type=PromptType.LEARNER_INTENT).complete(
                    system_prompt='', user_content='{}', trace_id='t',
                )

    @mock.patch(PATCH_SEND)
    @mock.patch(PATCH_GET_PROMPT)
    def test_the_user_content_is_sent_verbatim(self, mock_prompt, mock_send):
        mock_prompt.return_value = self.prompt
        mock_send.return_value = SimpleNamespace(role='assistant', content='{}')
        payload = json.dumps({'career': 'Welder'})

        XpertBackend(prompt_type=PromptType.LEARNER_INTENT).complete(
            system_prompt='', user_content=payload, trace_id='t',
        )

        sent = mock_send.call_args.kwargs['messages']
        self.assertEqual([message.content for message in sent], [payload])


class TestClaudeBackend(TestCase):
    """
    Tests for ``ClaudeBackend``.
    """

    def test_a_completion_returns_content_and_token_counts(self):
        backend = ClaudeBackend(client=fake_client(), api_key='k')

        response = backend.complete(system_prompt='sys', user_content='hi', trace_id='t')

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(response.backend, CLAUDE_BACKEND)
        self.assertEqual(response.input_tokens, 120)
        self.assertEqual(response.output_tokens, 45)
        self.assertEqual(response.total_tokens, 165)

    def test_the_callers_system_prompt_is_used(self):
        """Unlike Xpert, this backend takes the prompt from the caller."""
        client = fake_client()
        backend = ClaudeBackend(client=client, api_key='k')

        backend.complete(system_prompt='be terse', user_content='hi', trace_id='t')

        self.assertEqual(client.messages.create.call_args.kwargs['system'], 'be terse')

    def test_only_text_blocks_are_concatenated(self):
        """A future block type must not silently corrupt a JSON payload."""
        message = SimpleNamespace(
            content=[
                SimpleNamespace(text='{"a":', type='text'),
                SimpleNamespace(type='thinking'),
                SimpleNamespace(text=' 1}', type='text'),
            ],
            model='m', stop_reason='end_turn',
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        backend = ClaudeBackend(client=fake_client(message), api_key='k')

        self.assertEqual(backend.complete(
            system_prompt='', user_content='', trace_id='t',
        ).as_json(), {'a': 1})

    def test_a_response_without_usage_reports_none_rather_than_zero(self):
        message = SimpleNamespace(
            content=[SimpleNamespace(text='{}', type='text')],
            model='m', stop_reason='end_turn', usage=None,
        )
        backend = ClaudeBackend(client=fake_client(message), api_key='k')

        response = backend.complete(system_prompt='', user_content='', trace_id='t')

        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.total_tokens)

    @override_settings(ANTHROPIC_API_KEY='')
    def test_a_missing_api_key_is_a_configuration_error(self):
        with self.assertRaisesRegex(ModelBackendConfigurationError, 'ANTHROPIC_API_KEY'):
            ClaudeBackend().complete(system_prompt='', user_content='', trace_id='t')

    def test_a_provider_failure_is_a_request_error_naming_only_the_type(self):
        """The SDK's message can echo the request body, so only its type is reported."""
        backend = ClaudeBackend(
            client=fake_client(error=ValueError('learner said something private')),
            api_key='k',
        )

        with self.assertRaises(ModelBackendRequestError) as ctx:
            backend.complete(system_prompt='', user_content='', trace_id='t')

        message = str(ctx.exception)
        self.assertIn('ValueError', message)
        self.assertNotIn('learner said something private', message)


class TestOpenAIBackend(TestCase):
    """
    Tests for ``OpenAIBackend``.
    """

    def test_a_completion_returns_content_and_token_counts(self):
        backend = OpenAIBackend(client=fake_openai_client(), api_key='k')

        response = backend.complete(system_prompt='sys', user_content='hi', trace_id='t')

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(response.backend, OPENAI_BACKEND)
        self.assertEqual(response.input_tokens, 90)
        self.assertEqual(response.output_tokens, 30)
        self.assertEqual(response.total_tokens, 120)

    def test_json_mode_is_requested(self):
        """
        Every prompt in this pipeline requires JSON, and OpenAI can enforce it
        server-side -- which removes a failure mode rather than handling it.
        """
        client = fake_openai_client()

        OpenAIBackend(client=client, api_key='k').complete(
            system_prompt='sys', user_content='hi', trace_id='t',
        )

        self.assertEqual(
            client.chat.completions.create.call_args.kwargs['response_format'],
            {'type': 'json_object'},
        )

    def test_the_system_and_user_messages_are_sent_separately(self):
        client = fake_openai_client()

        OpenAIBackend(client=client, api_key='k').complete(
            system_prompt='be terse', user_content='the payload', trace_id='t',
        )

        messages = client.chat.completions.create.call_args.kwargs['messages']
        self.assertEqual(messages[0], {'role': 'system', 'content': 'be terse'})
        self.assertEqual(messages[1], {'role': 'user', 'content': 'the payload'})

    def test_a_truncated_response_is_visible_in_the_metadata(self):
        """``length`` is how a silently-cut-off ordering becomes diagnosable."""
        completion = fake_openai_completion(finish_reason='length')
        backend = OpenAIBackend(client=fake_openai_client(completion), api_key='k')

        response = backend.complete(system_prompt='', user_content='', trace_id='t')

        self.assertEqual(response.metadata['finish_reason'], 'length')

    def test_a_response_with_no_choices_yields_empty_content_rather_than_raising(self):
        """The caller already degrades an unusable response; this is that same case."""
        completion = SimpleNamespace(choices=[], model='gpt-4o', usage=None)
        backend = OpenAIBackend(client=fake_openai_client(completion), api_key='k')

        response = backend.complete(system_prompt='', user_content='', trace_id='t')

        self.assertEqual(response.content, '')
        self.assertIsNone(response.input_tokens)

    @override_settings(OPENAI_API_KEY='')
    def test_a_missing_api_key_is_a_configuration_error(self):
        with self.assertRaisesRegex(ModelBackendConfigurationError, 'OPENAI_API_KEY'):
            OpenAIBackend().complete(system_prompt='', user_content='', trace_id='t')

    def test_a_provider_failure_is_a_request_error_naming_only_the_type(self):
        """The SDK's message can echo the request body, so only its type is reported."""
        backend = OpenAIBackend(
            client=fake_openai_client(error=ValueError('learner said something private')),
            api_key='k',
        )

        with self.assertRaises(ModelBackendRequestError) as ctx:
            backend.complete(system_prompt='', user_content='', trace_id='t')

        message = str(ctx.exception)
        self.assertIn('ValueError', message)
        self.assertNotIn('learner said something private', message)


class TestBackendInterchangeability(TestCase):
    """
    Scenario: Backends are interchangeable.
    """

    @mock.patch(PATCH_SEND)
    @mock.patch(PATCH_GET_PROMPT)
    def test_both_backends_expose_the_same_response_surface(self, mock_prompt, mock_send):
        mock_prompt.return_value = mock.Mock(modified=None, **{'history.first.return_value': None})
        mock_send.return_value = SimpleNamespace(role='assistant', content='{"a": 1}')

        responses = [
            XpertBackend(prompt_type=PromptType.LEARNER_INTENT).complete(
                system_prompt='s', user_content='u', trace_id='t'),
            ClaudeBackend(client=fake_client(), api_key='k').complete(
                system_prompt='s', user_content='u', trace_id='t'),
            OpenAIBackend(client=fake_openai_client(), api_key='k').complete(
                system_prompt='s', user_content='u', trace_id='t'),
        ]

        for response in responses:
            self.assertIsInstance(response.content, str)
            self.assertIsInstance(response.elapsed_ms, int)
            self.assertIn('elapsed_ms', response.to_trace_dict())
            self.assertIn(response.backend, (XPERT_BACKEND, CLAUDE_BACKEND, OPENAI_BACKEND))
            # Every backend's content must survive the same JSON parse.
            self.assertIsInstance(response.as_json(), dict)

    @mock.patch(PATCH_SEND)
    @mock.patch(PATCH_GET_PROMPT)
    def test_elapsed_milliseconds_is_measured_by_the_base_class(self, mock_prompt, mock_send):
        """
        A backend that reported its own timing could report it differently; the base owns
        it so the number means the same thing everywhere.
        """
        mock_prompt.return_value = mock.Mock(modified=None, **{'history.first.return_value': None})
        mock_send.return_value = SimpleNamespace(role='assistant', content='{}')

        response = XpertBackend(prompt_type=PromptType.LEARNER_INTENT).complete(
            system_prompt='', user_content='', trace_id='t',
        )

        self.assertGreaterEqual(response.elapsed_ms, 0)


@ddt.ddt
class TestGetModelBackend(TestCase):
    """
    Scenario: Backend selection is configuration.
    """

    @override_settings(PATHWAYS_MODEL_BACKEND='xpert')
    def test_the_configured_backend_is_returned(self):
        backend = get_model_backend(prompt_type=PromptType.LEARNER_INTENT)

        self.assertIsInstance(backend, XpertBackend)

    @override_settings(PATHWAYS_MODEL_BACKEND='claude')
    def test_the_claude_backend_is_selectable_by_settings_alone(self):
        self.assertIsInstance(get_model_backend(prompt_type=PromptType.LEARNER_INTENT), ClaudeBackend)

    @override_settings(PATHWAYS_MODEL_BACKEND='openai')
    def test_the_openai_backend_is_selectable_by_settings_alone(self):
        self.assertIsInstance(get_model_backend(prompt_type=PromptType.LEARNER_INTENT), OpenAIBackend)

    @override_settings(PATHWAYS_MODEL_BACKEND='xpert')
    def test_an_explicit_name_overrides_the_setting(self):
        """A comparison run needs both backends live in one process."""
        backend = get_model_backend(
            prompt_type=PromptType.LEARNER_INTENT, backend_name='claude',
        )

        self.assertIsInstance(backend, ClaudeBackend)

    @ddt.data('gpt', '', 'XPERTT', None)
    def test_an_unknown_backend_name_raises_rather_than_defaulting(self, name):
        """
        Falling back to a default would hide a configuration typo -- and could silently
        route traffic to a paid backend nobody selected.
        """
        with override_settings(PATHWAYS_MODEL_BACKEND=name):
            with self.assertRaises(ModelBackendConfigurationError):
                get_model_backend(prompt_type=PromptType.LEARNER_INTENT)

    @override_settings(PATHWAYS_MODEL_BACKEND='  XPERT  ')
    def test_the_setting_is_normalised(self):
        self.assertIsInstance(
            get_model_backend(prompt_type=PromptType.LEARNER_INTENT), XpertBackend,
        )
