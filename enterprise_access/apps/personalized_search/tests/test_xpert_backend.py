"""Tests for the Xpert backend: stream decoding (spike 2 framing), requests and model routing."""
import json
from pathlib import Path
from unittest import mock

import requests
from django.test import TestCase

from enterprise_access.apps.personalized_search.adapters import xpert_backend as xpert
from enterprise_access.apps.personalized_search.adapters.openai_backend import OpenAIBackend
from enterprise_access.apps.personalized_search.core.errors import ModelConfigurationError, ModelError
from enterprise_access.apps.personalized_search.core.model_registry import ModelRegistry
from enterprise_access.apps.personalized_search.core.ports import Usage

SAMPLE = (Path(__file__).parent / 'fixtures' / 'xpert_stream_sample_prefix.txt').read_bytes()
XPERT = {'base_url': 'https://xpert.test', 'client_id': 'client-123'}
MODELS = {'gpt-4.1-mini': (0.4, 0.1, 1.6)}


def final_content(raw: bytes) -> str:
    """The last non-null content in a recorded Xpert body."""
    text, decoder, last = raw.decode(), json.JSONDecoder(), None
    index = 0
    while index < len(text):
        unit, index = decoder.raw_decode(text, index)
        last = unit[0]['content'] or last
    return last


class XpertStreamDecoderTests(TestCase):
    """``XpertStreamDecoder`` turns cumulative, undelimited units into deltas."""

    def test_recorded_sample_split_at_every_byte_offset(self):
        expected = final_content(SAMPLE)
        for offset in range(1, len(SAMPLE)):
            decoder = xpert.XpertStreamDecoder()
            deltas = decoder.feed(SAMPLE[:offset]) + decoder.feed(SAMPLE[offset:])
            self.assertEqual(''.join(deltas), expected, offset)
        self.assertEqual(decoder.units, 20)

    def test_several_units_in_one_read_and_null_first_unit(self):
        units = [[{'role': 'assistant', 'content': None}], [{'role': 'assistant', 'content': 'Hel'}],
                 [{'role': 'assistant', 'content': 'Hello'}], [{'role': 'assistant', 'content': 'Hello'}]]
        decoder = xpert.XpertStreamDecoder()
        self.assertEqual(decoder.feed(''.join(json.dumps(u) for u in units).encode()), ['Hel', 'lo'])

    def test_multibyte_character_split_across_reads(self):
        raw = json.dumps([{'content': 'café'}], ensure_ascii=False).encode()
        cut = raw.index('é'.encode()) + 1  # inside the two-byte character
        decoder = xpert.XpertStreamDecoder()
        self.assertEqual(decoder.feed(raw[:cut]) + decoder.feed(raw[cut:]), ['café'])


def fake_response(*, body=None, chunks=(), status=200):
    """A requests.Response stand-in."""
    response = mock.Mock(status_code=status)
    response.json.return_value = body
    response.iter_content.return_value = iter(chunks)
    response.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(str(status))
    return response


class XpertRequestTests(TestCase):
    """Requests send the caller's prompts with a fresh conversation id."""

    def setUp(self):
        super().setUp()
        self.session = mock.Mock()
        self.registry = ModelRegistry([OpenAIBackend(api_key='k', models=MODELS),
                                       xpert.XpertBackend(**XPERT, session=self.session)])

    def test_complete_json(self):
        post = self.session.post
        post.return_value = fake_response(body=[{'role': 'assistant', 'content': '{"on_topic": true}'}])
        text, usage = self.registry.complete_json(model='xpert', effort='low', system='SYS', user='USER',
                                                  max_tokens=400, timeout_s=3)
        self.assertEqual(text, '{"on_topic": true}')
        self.assertIsNone(usage.effort)
        self.assertIsNone(usage.cost_usd())
        url, payload = post.call_args.args[0], post.call_args.kwargs['json']
        self.assertEqual(url, 'https://xpert.test/v1/message')
        self.assertEqual(payload['client_id'], 'client-123')
        self.assertEqual((payload['system_message'], payload['stream']), ('SYS', False))
        self.assertEqual(payload['messages'], [{'role': 'user', 'content': 'USER'}])
        self.assertTrue(payload['conversation_id'].startswith('ps-'))

    def test_stream_text(self):
        post = self.session.post
        post.return_value = fake_response(chunks=[SAMPLE[:1000], SAMPLE[1000:]])
        usage = Usage(model='xpert', effort=None)
        text = ''.join(self.registry.stream_text(model='xpert', effort=None, system='S', user='U', max_tokens=1200,
                                                 timeout_s=5, usage=usage))
        self.assertEqual(text, final_content(SAMPLE))
        self.assertTrue(post.call_args.kwargs['json']['stream'])
        self.assertIsNotNone(usage.first_token_ms)
        self.assertEqual(usage.extra['xpert_units'], 20)
        post.return_value.close.assert_called_once()

    def test_http_error_is_a_model_error(self):
        post = self.session.post
        post.return_value = fake_response(status=502)
        with self.assertRaises(ModelError):
            list(self.registry.stream_text(model='xpert', effort=None, system='S', user='U', max_tokens=10,
                                           timeout_s=1, usage=Usage(model='xpert', effort=None)))

    def test_available_only_when_configured(self):
        self.assertIn('xpert', self.registry.available_models())
        self.assertEqual(self.registry.platform_of('xpert'), 'xpert')
        unconfigured = ModelRegistry([OpenAIBackend(api_key='k', models=MODELS),
                                      xpert.XpertBackend(base_url='', client_id='client-123')])
        self.assertNotIn('xpert', unconfigured.available_models())
        self.assertFalse(unconfigured.is_configured('xpert'))
        with self.assertRaises(ModelConfigurationError):
            unconfigured.check_model('xpert')
