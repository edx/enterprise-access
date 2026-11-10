"""Tests for the turn usage events (KD-12): raw facts and join keys, never aggregates or learner text."""
import dataclasses
import itertools
import json
from unittest import mock

from django.test import TestCase, override_settings

from enterprise_access.apps.personalized_search import wiring
from enterprise_access.apps.personalized_search.adapters.segment_sink import segment_emit
from enterprise_access.apps.personalized_search.constants import SegmentEvents
from enterprise_access.apps.personalized_search.core import telemetry
from enterprise_access.apps.personalized_search.core.errors import ModelError
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, TurnContext, run_turn
from enterprise_access.apps.personalized_search.core.retrieval import CatalogScope, RetrievalFailed
from enterprise_access.apps.personalized_search.models import ChatTurn
from enterprise_access.apps.personalized_search.tests.helpers import candidate, course_hit
from enterprise_access.apps.personalized_search.tests.test_orchestrator import (
    COMPOSE,
    TURN_UUID,
    fake_steps,
    new_locked_session,
    openai_platform
)

MARKER = 'zq-learner-marker-4410'
JOIN_KEYS = {'conversation_id', 'turn_id', 'turn_index', 'enterprise_customer_uuid', 'cohort', 'engine', 'is_debug'}
AGGREGATE_HINTS = ('count', 'cost', 'rate', 'total', 'age_days')


class TurnEventTests(TestCase):
    """``run_turn`` sends exactly one event per turn through ``Steps.emit``."""

    def setUp(self):
        super().setUp()
        ticks = itertools.count()
        self.session = new_locked_session(f'I want to lead a team {MARKER}')
        self.sent = []
        self.ctx = TurnContext(session=self.session, message=f'I want to lead a team {MARKER}',
                               intent_model=ModelChoice('gpt-4.1-mini', None),
                               compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None,
                               clock=lambda: next(ticks) * 0.1, id_factory=lambda: TURN_UUID,
                               platform=openai_platform, analytics={'cohort': 'internal_pilot', 'engine': 'b2b_mvp_v1'})

    def run_with(self, **kwargs):
        steps = dataclasses.replace(fake_steps(**kwargs), emit=lambda *args: self.sent.append(args))
        return list(run_turn(self.ctx, steps))

    def test_completed_turn_sends_raw_facts_and_join_keys_only(self):
        self.run_with()
        self.assertEqual(len(self.sent), 1)
        user_id, name, props = self.sent[0]
        self.assertEqual((user_id, name), (7, telemetry.TURN_COMPLETED))
        self.assertLessEqual(JOIN_KEYS, set(props))
        self.assertEqual((props['conversation_id'], props['turn_id'], props['turn_index']),
                         (self.session['uuid'], TURN_UUID, 1))
        self.assertEqual((props['cohort'], props['engine'], props['is_debug']), ('internal_pilot', 'b2b_mvp_v1', False))
        self.assertEqual(props['outcome'], 'done')
        # The scripted course, then the related programs offered alongside it.
        self.assertEqual(props['shown_item_keys'], ['edX+C1', 'p-1', 'p-2'])
        self.assertEqual(props['shown_item_sections'], ['course', 'program', 'program'])
        self.assertEqual(props['shown_item_positions'], [1, 2, 3])
        self.assertEqual(props['retrieved_item_keys'][:2], ['p-1', 'p-2'])  # related programs lead the shortlist
        self.assertIn('edX+C1', props['retrieved_item_keys'])
        self.assertEqual(props['intent_merge_mode'], 'replace')
        self.assertEqual(props['prompt_length'], len(self.ctx.message))
        self.assertEqual(props['compose_output_tokens'], 50)
        self.assertEqual((props['intent_max_hours_per_week'], props['content_locale']), (None, 'en'))
        self.assert_flat_and_private(props)

    def test_completed_turn_carries_the_hours_limit_and_the_content_language(self):
        intent = json.dumps({'on_topic': True, 'redirect_reason': None, 'merge': 'replace',
                             'intent': {'query': 'q', 'skills': ['Leadership'],
                                        'constraints': {'max_hours_per_week': 5}}})
        self.ctx.scope = CatalogScope(filter='f', locale='es', language_name='Spanish')
        self.run_with(intent_text=intent)
        props = self.sent[0][2]
        self.assertEqual((props['intent_max_hours_per_week'], props['content_locale']), (5, 'es'))

    def test_is_debug_is_whether_this_turn_used_debug_overrides(self):
        self.ctx.debug = True  # the environment allows overrides (and shows the debug read-out)
        self.run_with(persist=None)
        self.ctx.analytics['is_debug'] = True  # this turn's request applied them
        self.run_with(persist=None)
        self.assertEqual([sent[2]['is_debug'] for sent in self.sent], [False, True])

    def test_dropped_reasons_never_carry_model_text(self):
        compose = (COMPOSE + f'{{"block":"{MARKER}"}}\n'
                   f'{{"block":"item_section","item_type":"{MARKER}","items":[]}}\n'
                   '{"block":"item_section","item_type":"executive_education","items":[{"item_id":"nope"}]}\n')
        self.run_with(compose_text=compose)
        props = self.sent[0][2]
        self.assertEqual(props['dropped_reasons'],
                         ['unknown_block', 'section_rejected', 'section_no_valid_items:executive_education'])
        self.assert_flat_and_private(props)

    def test_the_event_reports_the_follow_up_the_learner_was_asked(self):
        levels = ('Introductory', 'Introductory', 'Advanced', 'Advanced')
        cands = [candidate(course_hit(f'edX+L{i}', level=level)) for i, level in enumerate(levels)]
        compose = ('Here.\n@@BLOCKS@@\n{"block":"item_section","item_type":"course","dek":"d","items":'
                   '[{"item_id":"edX+L0","why":"w"},{"item_id":"edX+L1","why":"w"}]}\n')
        self.run_with(retrieve=lambda *a, **kw: cands, compose_text=compose)
        # Level was planned, but both results shown are introductory, so the learner is asked what's next.
        self.assertEqual(self.sent[0][2]['followup_attribute'], 'open')
        self.assertEqual(ChatTurn.objects.get(uuid=TURN_UUID).follow_up_attribute, 'open')

    def test_completed_turn_lists_its_blocks_with_content_digests(self):
        parts = self.run_with()
        # One block per id, as clients apply them: the final profile replaces the early one in place.
        blocks = list({p['id']: p['data'] for p in parts if p['type'] == 'data-block'}.values())
        props = self.sent[0][2]
        self.assertEqual(props['shown_block_types'].count('profile'), 1)
        self.assertEqual(props['shown_block_types'], [b['type'] for b in blocks])
        self.assertEqual(props['shown_block_digests'], [telemetry.block_digest(b) for b in blocks])
        self.assertTrue(all(len(d) == 16 for d in props['shown_block_digests']))

    def test_block_digest_ignores_server_card_fields_and_changes_with_content(self):
        card = {'item_id': 'edX+C1', 'title': 'Course', 'why': 'Fits.'}
        block = {'v': 1, 'type': 'item_section', 'props': {'dek': 'Start here', 'items': [card]}}
        same = {'v': 1, 'type': 'item_section',
                'props': {'dek': 'Start here', 'items': [dict(card, status='new', section='course', position=4)]}}
        reworded = {'v': 1, 'type': 'item_section', 'props': {'dek': 'Start here', 'items': [dict(card, why='Other.')]}}
        self.assertEqual(telemetry.block_digest(block), telemetry.block_digest(same))
        # Per-turn catalog counts and reserve cards are the server's, so a repeated section still matches.
        refreshed = {'v': 1, 'type': 'item_section',
                     'props': dict(block['props'], total=37, reserve=[dict(card, item_id='edX+C9')])}
        self.assertEqual(telemetry.block_digest(block), telemetry.block_digest(refreshed))
        self.assertNotEqual(telemetry.block_digest(block), telemetry.block_digest(reworded))
        self.assertNotEqual(telemetry.block_digest(block), telemetry.block_digest(dict(block, type='compare')))

    def test_text_only_turn_is_completed_as_no_match(self):
        self.run_with(retrieve=lambda *a, **kw: [])
        self.assertEqual(self.sent[0][2]['outcome'], 'no_match')
        self.assertEqual(self.sent[0][2]['shown_item_keys'], [])

    def test_redirected_turn(self):
        self.run_with(intent_text='{"on_topic": false, "redirect_reason": "off_topic"}')
        props = self.sent[0][2]
        self.assertEqual((props['outcome'], props['redirect_reason']), ('redirected', 'off_topic'))

    def test_failures_name_the_error_and_the_stage(self):
        cases = [({'intent_error': ModelError('x')}, 'model_failed', 'intent'),
                 ({'retrieve': mock.Mock(side_effect=RetrievalFailed('x'))}, 'retrieval_failed', 'search'),
                 ({'compose_error': ModelError('x')}, 'model_failed', 'compose')]
        for kwargs, error_type, stage in cases:
            self.sent.clear()
            self.session.update(turn_count=0)
            self.run_with(**kwargs)
            name, props = self.sent[0][1], self.sent[0][2]
            self.assertEqual((name, props['error_type'], props['stage']), (telemetry.TURN_FAILED, error_type, stage))
            self.assertIsNotNone(props['elapsed_ms'])

    def test_a_storage_failure_is_reported_at_the_persist_stage(self):
        self.run_with(persist=mock.Mock(side_effect=RuntimeError('db down')))
        name, props = self.sent[0][1], self.sent[0][2]
        self.assertEqual((name, props['error_type'], props['stage']), (telemetry.TURN_FAILED, 'internal', 'persist'))

    def test_a_stopped_turn_sends_turn_stopped(self):
        steps = dataclasses.replace(fake_steps(), emit=lambda *args: self.sent.append(args))
        parts = run_turn(self.ctx, steps)
        for part in parts:
            if part['type'] == 'data-status' and part['data']['step'] == 'writing':
                break
        parts.close()
        name, props = self.sent[0][1], self.sent[0][2]
        self.assertEqual((name, props['stage']), (telemetry.TURN_STOPPED, 'compose'))

    def test_a_failing_sink_never_breaks_the_turn(self):
        steps = dataclasses.replace(fake_steps(), emit=mock.Mock(side_effect=RuntimeError('down')))
        with self.assertLogs('enterprise_access.apps.personalized_search.core.orchestrator', level='ERROR'):
            parts = list(run_turn(self.ctx, steps))
        self.assertEqual(parts[-1]['type'], 'finish')

    def assert_flat_and_private(self, props):
        """Primitives (or lists of them) only; no aggregate-like keys; no learner text."""
        for key, value in props.items():
            self.assertFalse(any(hint in key for hint in AGGREGATE_HINTS) and not key.endswith('_tokens'), key)
            values = value if isinstance(value, list) else [value]
            self.assertTrue(all(v is None or isinstance(v, (str, int, float, bool)) for v in values), key)
            self.assertFalse(any(isinstance(v, str) and MARKER in v for v in values), key)


class SegmentSinkTests(TestCase):
    """The adapter prefixes the event name and hands it to ``track_event``."""

    @mock.patch('enterprise_access.apps.personalized_search.adapters.segment_sink.track_event')
    def test_prefix(self, track_event):
        segment_emit(7, 'turn.completed', {'a': 1})
        track_event.assert_called_once_with(lms_user_id=7, event_name=SegmentEvents.TURN_COMPLETED,
                                            properties={'a': 1})
        self.assertEqual(SegmentEvents.TURN_FAILED, 'edx.server.enterprise-access.personalized-search.turn.failed')
        self.assertEqual(SegmentEvents.TURN_STOPPED, SegmentEvents.name('turn.stopped'))

    @override_settings(OPENAI_API_KEY='k', PERSONALIZED_SEARCH_ANALYTICS_COHORT='public',
                       PERSONALIZED_SEARCH_ENGINE_LABEL='xpert')
    def test_wiring_tags_turns_and_emits_only_when_persisting(self):
        intent, compose = wiring.default_models()
        ctx = wiring.turn_context(session={}, message='m', intent_model=intent, compose_model=compose, scope=None)
        self.assertEqual(ctx.analytics, {'cohort': 'public', 'engine': 'xpert', 'is_debug': False})
        overridden = wiring.turn_context(session={}, message='m', intent_model=intent, compose_model=compose,
                                         scope=None, debug_overrides=True)
        self.assertTrue(overridden.analytics['is_debug'])
        self.assertIs(wiring.steps(catalog=mock.Mock(), store=mock.Mock()).emit, segment_emit)
        self.assertIsNone(wiring.steps(catalog=mock.Mock(), store=mock.Mock(), persist=False).emit)
