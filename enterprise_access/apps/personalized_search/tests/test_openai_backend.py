"""Tests for the OpenAI backend: per-family request parameters (model benchmark findings), cost and config."""
from types import SimpleNamespace
from unittest import mock

import ddt
from django.test import TestCase

from enterprise_access.apps.personalized_search.adapters import openai_backend
from enterprise_access.apps.personalized_search.adapters.openai_backend import OpenAIBackend
from enterprise_access.apps.personalized_search.core.errors import ModelConfigurationError, ModelError
from enterprise_access.apps.personalized_search.core.model_registry import ModelRegistry
from enterprise_access.apps.personalized_search.core.ports import Usage

MODELS = {'gpt-4.1-mini': (0.4, 0.1, 1.6), 'gpt-5.4-mini': (0.75, 0.075, 4.5)}


def chunk(content=None, usage=None):
    choices = [SimpleNamespace(delta=SimpleNamespace(content=content))] if content is not None else []
    return SimpleNamespace(choices=choices, usage=usage)


@ddt.ddt
class RequestParamsTests(TestCase):
    """``request_params`` and ``resolve_effort`` follow each model family's rules."""

    @ddt.data(
        # model, requested effort, expected reasoning_effort (None = omitted), expected max_completion_tokens
        ('gpt-4.1-mini', None, None, 400),
        ('gpt-4o', 'low', None, 400),
        ('gpt-5', None, 'minimal', 400),
        ('gpt-5-mini', 'none', 'minimal', 400),
        ('gpt-5-nano-2025-08-07', 'minimal', 'minimal', 400),
        ('gpt-5.4-mini', None, 'none', 400),
        ('gpt-5.4-mini', 'minimal', 'none', 400),
        ('gpt-5.4-mini', 'low', 'low', 400 + openai_backend.REASONING_HEADROOM_TOKENS),
        ('gpt-5.6-terra', 'medium', 'medium', 400 + openai_backend.REASONING_HEADROOM_TOKENS),
        ('gpt-5.6-luna', 'minimal', 'none', 400),
        ('gpt-6-luna', None, 'none', 400),
        ('gpt-6.1-terra', 'xhigh', 'xhigh', 400 + openai_backend.REASONING_HEADROOM_TOKENS),
        # sol and astra start at low, which reasons, so it gets the headroom too.
        ('gpt-6.1-sol', None, 'low', 400 + openai_backend.REASONING_HEADROOM_TOKENS),
        ('gpt-6-astra', 'none', 'low', 400 + openai_backend.REASONING_HEADROOM_TOKENS),
    )
    @ddt.unpack
    def test_params(self, model, effort, expected_effort, expected_max):
        params = openai_backend.request_params(model, effort, 400)
        self.assertEqual(params.get('reasoning_effort'), expected_effort)
        self.assertEqual(params['max_completion_tokens'], expected_max)
        self.assertNotIn('max_tokens', params)

    @ddt.data(('gpt-6-luna', 'max'), ('gpt-6.1-sol', 'max'), ('gpt-6.1-terra', 'extreme'))
    @ddt.unpack
    def test_tiered_family_refuses_an_effort_it_does_not_take(self, model, effort):
        with self.assertRaises(ModelConfigurationError):
            openai_backend.resolve_effort(model, effort)

    def test_cost(self):
        usage = Usage(model='gpt-4.1-mini', effort=None, input_tokens=1000, cached_input_tokens=500,
                      output_tokens=100, prices=MODELS['gpt-4.1-mini'])
        self.assertAlmostEqual(usage.cost_usd(), (500 * 0.4 + 500 * 0.1 + 100 * 1.6) / 1e6)

    def test_unlisted_model_refused(self):
        registry = ModelRegistry([OpenAIBackend(api_key='k', models=MODELS)])
        with self.assertRaises(ModelConfigurationError):
            registry.check_model('gpt-9')

    def test_prices_and_effort_resolution_delegate_to_the_model_table(self):
        backend = OpenAIBackend(api_key='k', models=MODELS)
        self.assertEqual(backend.prices('gpt-4.1-mini'), MODELS['gpt-4.1-mini'])
        self.assertIsNone(backend.prices('gpt-9'))
        self.assertIsNone(backend.resolve_effort('gpt-4.1-mini', 'low'))
        self.assertEqual(backend.resolve_effort('gpt-5.4-mini', 'low'), 'low')

    def test_default_client_is_built_with_the_key_and_no_retries(self):
        with mock.patch('openai.OpenAI') as client_class:
            client = OpenAIBackend(api_key='k', models=MODELS)._client(2.5)  # pylint: disable=protected-access
        client_class.assert_called_once_with(api_key='k', timeout=2.5, max_retries=0)
        self.assertIs(client, client_class.return_value)

    def test_missing_key(self):
        registry = ModelRegistry([OpenAIBackend(api_key='', models=MODELS)])
        self.assertFalse(registry.is_configured('gpt-4.1-mini'))
        with self.assertRaises(ModelConfigurationError):
            registry.complete_json(model='gpt-4.1-mini', effort=None, system='s', user='u', max_tokens=10,
                                   timeout_s=1)


class OpenAICallTests(TestCase):
    """Calls go through the injected client factory with the family's parameters, and fill ``Usage``."""

    def setUp(self):
        super().setUp()
        self.client = mock.Mock()
        self.factory = mock.Mock(return_value=self.client)
        self.backend = OpenAIBackend(api_key='k', models=MODELS, client_factory=self.factory)

    def test_complete_json(self):
        raw_usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=0))
        self.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"a": 1}'))], usage=raw_usage, model='m-1')
        text, usage = self.backend.complete_json(model='gpt-4.1-mini', effort=None, system='S', user='U',
                                                 max_tokens=400, timeout_s=3)
        self.assertEqual(text, '{"a": 1}')
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens, usage.output_tokens), (100, 40, 20))
        self.assertIsNotNone(usage.cost_usd())
        self.factory.assert_called_once_with(api_key='k', timeout=3, max_retries=0)
        kwargs = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs['response_format'], {'type': 'json_object'})
        self.assertEqual(kwargs['max_completion_tokens'], 400)

    def test_stream_text_and_close(self):
        stream = mock.MagicMock()
        stream.__iter__.return_value = iter([chunk('Hel'), chunk('lo'), chunk(None, usage=SimpleNamespace(
            prompt_tokens=5, completion_tokens=2, prompt_tokens_details=None, completion_tokens_details=None))])
        self.client.chat.completions.create.return_value = stream
        usage = Usage(model='gpt-5.4-mini', effort='low')
        text = ''.join(self.backend.stream_text(model='gpt-5.4-mini', effort='low', system='S', user='U',
                                                max_tokens=1200, timeout_s=5, usage=usage))
        self.assertEqual(text, 'Hello')
        self.assertEqual((usage.input_tokens, usage.output_tokens), (5, 2))
        self.assertEqual(usage.prices, MODELS['gpt-5.4-mini'])
        stream.close.assert_called_once()

    def test_stream_text_records_the_effort_actually_sent(self):
        stream = mock.MagicMock()
        stream.__iter__.return_value = iter([chunk('Hi')])
        self.client.chat.completions.create.return_value = stream
        usage = Usage(model='gpt-6.1-sol', effort=None)
        ''.join(self.backend.stream_text(model='gpt-6.1-sol', effort=None, system='S', user='U', max_tokens=10,
                                         timeout_s=5, usage=usage))
        self.assertEqual(usage.effort, 'low')
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs['reasoning_effort'], 'low')

    def test_sdk_errors_become_model_errors_without_the_message(self):
        self.client.chat.completions.create.side_effect = RuntimeError('learner text echoed here')
        with self.assertRaises(ModelError) as ctx:
            self.backend.complete_json(model='gpt-4.1-mini', effort=None, system='S', user='U', max_tokens=10,
                                       timeout_s=1)
        self.assertNotIn('learner text', str(ctx.exception))

    def test_stopping_early_closes_the_stream(self):
        stream = mock.MagicMock()
        stream.__iter__.return_value = iter([chunk('a'), chunk('b'), chunk('c')])
        self.client.chat.completions.create.return_value = stream
        deltas = self.backend.stream_text(model='gpt-5.4-mini', effort=None, system='S', user='U', max_tokens=10,
                                          timeout_s=5, usage=Usage(model='gpt-5.4-mini', effort=None))
        self.assertEqual(next(deltas), 'a')
        deltas.close()  # the learner pressed Stop
        stream.close.assert_called_once()

    def test_failure_mid_stream_is_a_model_error_and_closes_the_stream(self):
        def broken():
            yield chunk('a')
            raise RuntimeError('connection reset with learner text')
        stream = mock.MagicMock()
        stream.__iter__.return_value = broken()
        self.client.chat.completions.create.return_value = stream
        usage = Usage(model='gpt-5.4-mini', effort=None)
        with self.assertRaises(ModelError) as ctx:
            list(self.backend.stream_text(model='gpt-5.4-mini', effort=None, system='S', user='U', max_tokens=10,
                                          timeout_s=5, usage=usage))
        self.assertNotIn('learner text', str(ctx.exception))
        stream.close.assert_called_once()
        self.assertIsNotNone(usage.elapsed_ms)

    def test_failure_creating_the_stream_is_a_model_error(self):
        self.client.chat.completions.create.side_effect = RuntimeError('rejected')
        with self.assertRaises(ModelError):
            list(self.backend.stream_text(model='gpt-5.4-mini', effort=None, system='S', user='U', max_tokens=10,
                                          timeout_s=5, usage=Usage(model='gpt-5.4-mini', effort=None)))

    def test_no_choices_is_a_model_error(self):
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[], usage=None, model='m')
        with self.assertRaises(ModelError):
            self.backend.complete_json(model='gpt-4.1-mini', effort=None, system='S', user='U', max_tokens=10,
                                       timeout_s=1)
