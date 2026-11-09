"""Tests for the exact-match model response cache (KD-13)."""
import dataclasses
import itertools
import uuid
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings

from enterprise_access.apps.personalized_search import wiring
from enterprise_access.apps.personalized_search.adapters.response_cache import ResponseCache
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, TurnContext, run_turn
from enterprise_access.apps.personalized_search.core.ports import Usage
from enterprise_access.apps.personalized_search.tests.test_orchestrator import (
    fake_steps,
    new_locked_session,
    openai_platform
)


class FakeCache:
    """The TieredCache calls the adapter uses, backed by a dict."""

    def __init__(self):
        self.store = {}

    def get_cached_response(self, key):
        return SimpleNamespace(is_found=key in self.store, value=self.store.get(key))

    def set_all_tiers(self, key, value, timeout):  # pylint: disable=unused-argument
        self.store[key] = value


def intent_call(calls):
    """A fake intent step that counts its calls."""
    def call(*, system, user, model, effort, timeout_s):  # pylint: disable=unused-argument
        calls.append(model)
        usage = Usage(model=model, effort=effort, input_tokens=900, cached_input_tokens=400, output_tokens=60,
                      reasoning_tokens=0)
        return '{"intent": 1}', usage
    return call


def compose_call(calls, *, fail_after=None):
    """A fake compose step that streams three deltas (or breaks after ``fail_after``) and counts its calls."""
    def call(*, system, user, model, effort, timeout_s, usage):  # pylint: disable=unused-argument
        calls.append(model)
        for i, delta in enumerate(['Hello ', 'learner', '.']):
            if fail_after is not None and i == fail_after:
                raise RuntimeError('stream broke')
            yield delta
        usage.input_tokens, usage.output_tokens, usage.reasoning_tokens = 3000, 700, 120
    return call


ARGS = {'system': 'S', 'user': 'U', 'model': 'gpt-5.4-mini', 'effort': 'low', 'timeout_s': 5}


class ResponseCacheTests(TestCase):
    """Miss, store and replay for both steps; never store an unfinished call."""

    def setUp(self):
        super().setUp()
        self.cache = ResponseCache(scope='ent-1', seconds=60, cache=FakeCache())

    def test_intent_miss_then_hit_reports_the_tokens_it_saved(self):
        calls = []
        intent = self.cache.intent(intent_call(calls))
        text, usage = intent(**ARGS)
        self.assertEqual((text, usage.extra['cache'], usage.output_tokens), ('{"intent": 1}', 'miss', 60))
        text, usage = intent(**ARGS)
        self.assertEqual((text, calls), ('{"intent": 1}', ['gpt-5.4-mini']))
        self.assertEqual((usage.extra['cache'], usage.input_tokens, usage.output_tokens), ('hit', 0, 0))
        self.assertEqual(usage.extra['saved'], {'input_tokens': 900, 'cached_input_tokens': 400,
                                                'output_tokens': 60, 'reasoning_tokens': 0})

    def test_compose_miss_streams_and_stores_then_hit_replays(self):
        calls = []
        compose = self.cache.compose(compose_call(calls))
        miss = Usage(model='gpt-5.4-mini', effort='low')
        self.assertEqual(''.join(compose(**ARGS, usage=miss)), 'Hello learner.')
        self.assertEqual(miss.extra['cache'], 'miss')
        hit = Usage(model='gpt-5.4-mini', effort='low')
        self.assertEqual(''.join(compose(**ARGS, usage=hit)), 'Hello learner.')
        self.assertEqual(len(calls), 1)
        self.assertEqual((hit.extra['cache'], hit.output_tokens), ('hit', 0))
        self.assertEqual(hit.extra['saved']['output_tokens'], 700)

    def test_a_stopped_or_failed_stream_is_not_stored(self):
        calls = []
        stopped = self.cache.compose(compose_call(calls))(**ARGS, usage=Usage(model='m', effort=None))
        next(stopped)
        stopped.close()  # the learner pressed Stop
        with self.assertRaises(RuntimeError):
            list(self.cache.compose(compose_call(calls, fail_after=2))(**ARGS, usage=Usage(model='m', effort=None)))
        self.assertEqual(self.cache.cache.store, {})

    def test_the_key_changes_with_every_input_and_the_enterprise(self):
        base = {'step': 'compose', 'model': 'm', 'effort': 'low', 'system': 'S', 'user': 'U'}
        keys = {self.cache.key(**base)}
        for change in ({'model': 'n'}, {'effort': None}, {'system': 'S2'}, {'user': 'U2'}, {'step': 'intent'}):
            keys.add(self.cache.key(**{**base, **change}))
        keys.add(ResponseCache(scope='ent-2', seconds=60).key(**base))
        self.assertEqual(len(keys), 7)

    def test_switching_models_never_replays_another_models_output(self):
        calls = []
        compose = self.cache.compose(compose_call(calls))
        for model in ('model-a', 'model-a', 'model-b', 'model-a'):
            args = {**ARGS, 'model': model}
            list(compose(**args, usage=Usage(model=model, effort='low')))
        self.assertEqual(calls, ['model-a', 'model-b'])

    def test_a_cache_outage_falls_through_to_the_model(self):
        broken = mock.Mock(get_cached_response=mock.Mock(side_effect=OSError('down')),
                           set_all_tiers=mock.Mock(side_effect=OSError('down')))
        calls = []
        intent = ResponseCache(scope='ent-1', seconds=60, cache=broken).intent(intent_call(calls))
        self.assertEqual(intent(**ARGS)[0], '{"intent": 1}')
        self.assertEqual(calls, ['gpt-5.4-mini'])


class WiringTests(TestCase):
    """The cache is on only for a scoped, non-zero configuration."""

    @override_settings(PERSONALIZED_SEARCH_RESPONSE_CACHE_SECONDS=60)
    def test_scoped_and_configured(self):
        self.assertIsInstance(wiring.response_cache('ent-1'), ResponseCache)
        self.assertIsNone(wiring.response_cache(None))

    @override_settings(PERSONALIZED_SEARCH_RESPONSE_CACHE_SECONDS=0)
    def test_zero_turns_it_off(self):
        self.assertIsNone(wiring.response_cache('ent-1'))


class ReplayedTurnTests(TestCase):
    """A replayed turn streams the same blocks, and its debug read-out and event say it was cached."""

    def run_turn(self, steps, *, debug=False):
        """One turn through ``steps``: its parts and the properties of the event it sent."""
        ticks = itertools.count()
        session = new_locked_session('I want to lead a team')
        ctx = TurnContext(session=session, message='I want to lead a team',
                          intent_model=ModelChoice('gpt-4.1-mini', None),
                          compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None, debug=debug,
                          clock=lambda: next(ticks) * 0.1, id_factory=lambda: str(uuid.uuid4()),
                          platform=openai_platform)
        sent = []
        parts = list(run_turn(ctx, dataclasses.replace(steps, emit=lambda *args: sent.append(args))))
        return parts, sent[0][2]

    def test_same_blocks_debug_hit_and_saved_tokens_and_event_flags(self):
        cache = ResponseCache(scope='ent-1', seconds=60, cache=FakeCache())
        base = fake_steps()
        cached = dataclasses.replace(base, intent=cache.intent(base.intent), compose=cache.compose(base.compose))

        def shape(parts):
            return ([p for p in parts if p['type'] in ('data-block', 'data-followups')],
                    ''.join(p['delta'] for p in parts if p['type'] == 'text-delta'))

        first, first_event = self.run_turn(cached, debug=True)
        second, second_event = self.run_turn(cached, debug=True)
        self.assertEqual(shape(first), shape(second))
        debug = [p for p in second if p['type'] == 'data-timing'][-1]['data']['debug']
        self.assertEqual((debug['intent']['cache'], debug['compose']['cache']), ('hit', 'hit'))
        self.assertEqual(debug['compose']['output_tokens'], 0)
        self.assertEqual(debug['tokens_saved']['output_tokens'],
                         first_event['intent_output_tokens'] + first_event['compose_output_tokens'])
        self.assertEqual((first_event['intent_cache_hit'], first_event['compose_cache_hit']), (False, False))
        self.assertEqual((second_event['intent_cache_hit'], second_event['compose_cache_hit']), (True, True))
        self.assertEqual(second_event['compose_output_tokens'], 0)
        self.assertEqual(second_event['compose_cache_saved_output_tokens'], first_event['compose_output_tokens'])
