"""Tests for the turn orchestrator with fake steps (no OpenAI or Algolia), persisting through the Django store."""
import itertools
import json
import uuid
from unittest import TestCase as PlainTestCase
from unittest import mock

from django.test import TestCase

from enterprise_access.apps.personalized_search.adapters.django_store import DjangoSessionStore
from enterprise_access.apps.personalized_search.adapters.memory_store import InMemorySessionStore
from enterprise_access.apps.personalized_search.core.errors import ModelError
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, Steps, TurnContext, run_turn
from enterprise_access.apps.personalized_search.core.ports import Usage
from enterprise_access.apps.personalized_search.core.retrieval import RetrievalFailed
from enterprise_access.apps.personalized_search.models import ChatSession, ChatTurn, ItemFeedback
from enterprise_access.apps.personalized_search.tests.helpers import course_hit, default_candidates

ON_TOPIC = json.dumps({'on_topic': True, 'redirect_reason': None, 'merge': 'replace',
                       'intent': {'query': 'team leadership', 'skills': ['Leadership']}})
COMPOSE = ('Your page.\n@@BLOCKS@@\n'
           '{"block":"page_header","title":"Lead","dek":"D"}\n'
           '{"block":"item_section","item_type":"course","dek":"d",'
           '"items":[{"item_id":"edX+C1","why":"w","fit":["f"]}]}\n'
           '{"block":"followup","question":"Level?","suggestions":["A","B","C"],"session_title":"Leading"}\n')


ENTERPRISE = str(uuid.UUID(int=1))
TURN_UUID = str(uuid.UUID(int=2))
PRICES = {'gpt-4.1-mini': (0.4, 0.1, 1.6), 'gpt-5.4-mini': (0.75, 0.075, 4.5)}
store = DjangoSessionStore()


def openai_platform(_model):
    return 'openai'


def new_locked_session(message, lms_user_id=7):
    """A new session whose row exists and is locked, as the view leaves it before streaming."""
    session = store.load_or_create(session_uuid=None, lms_user_id=lms_user_id,
                                   enterprise_customer_uuid=ENTERPRISE, message=message)
    store.lock(session)
    return session


def fake_steps(intent_text=ON_TOPIC, compose_text=COMPOSE, retrieve=None, compose_error=None, intent_error=None,
               persist=store.persist_turn):
    """Steps that return canned intent, candidates and compose text (streamed in 9-character chunks)."""
    def intent(**_):
        if intent_error:
            raise intent_error
        return intent_text, Usage(model='gpt-4.1-mini', effort=None, input_tokens=10, output_tokens=5,
                                  prices=PRICES['gpt-4.1-mini'])

    def compose(**kwargs):
        kwargs['usage'].output_tokens = 50
        kwargs['usage'].prices = PRICES['gpt-5.4-mini']
        for i in range(0, len(compose_text), 9):
            yield compose_text[i:i + 9]
        if compose_error:
            raise compose_error

    return Steps(intent=intent, retrieve=retrieve or (lambda *a, **kw: default_candidates()), compose=compose,
                 persist=persist)


class RunTurnTests(TestCase):
    """``run_turn`` emits the spec's part sequence and persists only finished turns."""

    def setUp(self):
        super().setUp()
        ticks = itertools.count()
        self.session = new_locked_session('I want to lead a team')

        def clock():
            return next(ticks) * 0.1

        self.clock = clock
        self.ctx = TurnContext(session=self.session, message='I want to lead a team',
                               intent_model=ModelChoice('gpt-4.1-mini', None),
                               compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None, debug=True,
                               clock=clock, id_factory=lambda: TURN_UUID, platform=openai_platform)

    def saved(self):
        return ChatSession.objects.get(uuid=self.session['uuid'])

    def test_normal_turn(self):
        parts = list(run_turn(self.ctx, fake_steps()))
        types = [p['type'] for p in parts]
        self.assertEqual(types[:3], ['start', 'data-session', 'data-status'])
        self.assertEqual(types[-3:], ['data-session', 'data-timing', 'finish'])
        self.assertEqual(parts[0]['messageId'], f'turn_{TURN_UUID}')
        self.assertEqual([p['data']['step'] for p in parts if p['type'] == 'data-status'],
                         ['understanding', 'searching', 'writing'])
        timing = parts[-2]['data']
        self.assertEqual(timing['backend'], 'openai')
        self.assertLessEqual(timing['first_text_ms'], timing['first_section_ms'])
        self.assertIsNotNone(timing['debug']['cost_usd'])
        saved = self.saved()
        self.assertEqual((saved.turn_count, saved.item_count, saved.title), (1, 1, 'Leading'))
        self.assertEqual(saved.shown_item_ids, ['edX+C1'])
        self.assertIsNone(saved.active_turn_started_at)
        turn = ChatTurn.objects.get(session=saved)
        self.assertEqual((str(turn.uuid), turn.sequence, turn.reply_text), (TURN_UUID, 1, 'Your page.'))
        self.assertEqual(turn.item_ids, ['edX+C1'])
        stored = [p['type'] for p in turn.parts]
        self.assertNotIn('data-timing', stored)
        self.assertNotIn('data-status', stored)
        self.assertNotIn('finish', stored)
        self.assertEqual(stored.count('text-delta'), 1)  # coalesced for replay

    def test_redirect_persists_a_turn_without_items(self):
        steps = fake_steps(intent_text='{"on_topic": false, "redirect_reason": "off_topic"}',
                           retrieve=lambda *a, **kw: self.fail('no retrieval on redirect'))
        parts = list(run_turn(self.ctx, steps))
        followups = [p for p in parts if p['type'] == 'data-followups'][0]['data']
        self.assertEqual(followups['attribute'], 'open')
        self.assertFalse([p for p in parts if p['type'] == 'data-block'])
        self.assertEqual(parts[-1]['type'], 'finish')
        self.assertEqual(ChatTurn.objects.get(session_id=self.session['uuid']).item_ids, [])
        self.assertIsNone(self.saved().merged_intent)

    def test_an_unexpected_failure_ends_with_an_internal_error_and_persists_nothing(self):
        steps = fake_steps(retrieve=lambda *a, **k: (_ for _ in ()).throw(KeyError('bug')))
        with self.assertLogs('enterprise_access.apps.personalized_search.core.orchestrator', level='INFO') as logs:
            parts = list(run_turn(self.ctx, steps))
        self.assertEqual(parts[-1], {'type': 'error', 'errorText': 'internal'})
        self.assertFalse(ChatTurn.objects.exists())
        record = [json.loads(r.getMessage().split(' ', 1)[1]) for r in logs.records
                  if r.getMessage().startswith('personalized_search.turn ')][0]
        self.assertEqual((record['outcome'], record['error']), ('error', 'internal'))

    def test_a_turn_past_its_deadline_stops_with_deadline_exceeded(self):
        ticks = itertools.count()
        # Each clock read advances 5 s, so the deadline (20 s) passes while the reply is still streaming.
        ctx = TurnContext(session=self.session, message='I want to lead a team',
                          intent_model=ModelChoice('gpt-4.1-mini', None),
                          compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None, debug=True,
                          clock=lambda: next(ticks) * 5.0, id_factory=lambda: TURN_UUID, platform=openai_platform)
        parts = list(run_turn(ctx, fake_steps()))
        self.assertEqual(parts[-1], {'type': 'error', 'errorText': 'deadline_exceeded'})
        self.assertFalse(ChatTurn.objects.exists())

    def test_errors_persist_nothing(self):
        cases = [
            (fake_steps(intent_text='not json'), 'intent_invalid'),
            (fake_steps(intent_error=ModelError('x')), 'model_failed'),
            (fake_steps(retrieve=lambda *a, **kw: (_ for _ in ()).throw(RetrievalFailed('x'))), 'retrieval_failed'),
            (fake_steps(compose_error=ModelError('x')), 'model_failed'),
        ]
        for steps, code in cases:
            parts = list(run_turn(self.ctx, steps))
            self.assertEqual(parts[-1], {'type': 'error', 'errorText': code})
        self.assertEqual(self.saved().turn_count, 0)
        self.assertFalse(ChatTurn.objects.exists())

    def test_abort_does_not_persist(self):
        gen = run_turn(self.ctx, fake_steps())
        for part in gen:
            if part['type'] == 'text-delta':
                break
        gen.close()
        self.assertFalse(ChatTurn.objects.exists())

    def test_persist_failure_ends_in_an_error_instead_of_finish(self):
        parts = list(run_turn(self.ctx, fake_steps(persist=mock.Mock(side_effect=RuntimeError('db down')))))
        self.assertEqual(parts[-1], {'type': 'error', 'errorText': 'internal'})
        self.assertNotIn('finish', [p['type'] for p in parts])

    def test_not_persisted_when_disabled(self):
        self.assertEqual(list(run_turn(self.ctx, fake_steps(persist=None)))[-1]['type'], 'finish')
        self.assertFalse(ChatTurn.objects.exists())

    def test_shown_and_dismissed_items_excluded_on_next_turn(self):
        list(run_turn(self.ctx, fake_steps()))
        ItemFeedback.objects.create(lms_user_id=7, enterprise_customer_uuid=ENTERPRISE, item_id='edX+C4',
                                    item_type='course', kind=ItemFeedback.KIND_NOT_INTERESTED)
        seen = []
        session = store.load_or_create(session_uuid=self.session['uuid'], lms_user_id=7,
                                       enterprise_customer_uuid=ENTERPRISE, message='more')
        self.assertEqual(session['history'], [{'user': 'I want to lead a team', 'reply': 'Your page.'}])
        store.lock(session)
        ctx = TurnContext(session=session, message='more', intent_model=self.ctx.intent_model,
                          compose_model=self.ctx.compose_model, scope=None, clock=self.clock)
        list(run_turn(ctx, fake_steps(retrieve=lambda intent, scope, excluded, **kw: seen.append(excluded) or [
            {'hit': course_hit('edX+C9'), 'card': default_candidates()[0]['card'], 'tier': 1, 'rank': 0}])))
        self.assertEqual(seen, [['edX+C1', 'edX+C4']])
        self.assertEqual(ChatTurn.objects.filter(session_id=self.session['uuid']).count(), 2)


class TurnObservabilityTests(TestCase):
    """Chunks 20 and 21: wiring checks and exactly one turn log record without learner text."""

    LOGGER = 'enterprise_access.apps.personalized_search.core.orchestrator'
    MARKER = 'zq-learner-marker-7731'

    def setUp(self):
        super().setUp()
        ticks = itertools.count()
        self.session = new_locked_session(f'I want to lead a team {self.MARKER}')
        self.ctx = TurnContext(session=self.session, message=f'I want to lead a team {self.MARKER}',
                               intent_model=ModelChoice('gpt-4.1-mini', None),
                               compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None, debug=True,
                               clock=lambda: next(ticks) * 0.1, platform=openai_platform)

    def turn_records(self, logs):
        return [json.loads(r.getMessage().split(' ', 1)[1]) for r in logs.records
                if r.getMessage().startswith('personalized_search.turn ')]

    def test_completed_turn_logs_once_with_fields(self):
        with self.assertLogs(self.LOGGER, level='INFO') as logs:
            parts = list(run_turn(self.ctx, fake_steps()))
        (record,) = self.turn_records(logs)
        self.assertEqual(record['outcome'], 'done')
        self.assertEqual(record['session_uuid'], self.session['uuid'])
        self.assertEqual((record['sequence'], record['backend']), (1, 'openai'))
        self.assertEqual(record['item_ids'], ['edX+C1'])
        self.assertEqual(record['item_count'], 1)
        self.assertEqual(len(record['candidate_ids']), 8)
        self.assertFalse(record['fallback'])
        self.assertEqual(record['dropped_lines'], 0)
        timing = [p for p in parts if p['type'] == 'data-timing'][0]['data']
        self.assertLess(timing['first_text_ms'], timing['first_section_ms'])
        self.assertLess(timing['first_section_ms'], timing['complete_ms'])

    def test_errored_redirect_and_aborted_turns_log_once(self):
        cases = [
            (fake_steps(intent_text='not json'), 'error', 'intent_invalid'),
            (fake_steps(intent_text='{"on_topic": false, "redirect_reason": "unsafe"}'), 'redirect', None),
        ]
        for steps, outcome, error in cases:
            with self.assertLogs(self.LOGGER, level='INFO') as logs:
                list(run_turn(self.ctx, steps))
            (record,) = self.turn_records(logs)
            self.assertEqual((record['outcome'], record['error']), (outcome, error))
        with self.assertLogs(self.LOGGER, level='INFO') as logs:
            gen = run_turn(self.ctx, fake_steps())
            for part in gen:
                if part['type'] == 'data-block' and part['data']['type'] == 'item_section':
                    break
            gen.close()
        (record,) = self.turn_records(logs)
        self.assertEqual(record['outcome'], 'aborted')
        self.assertEqual(record['item_ids'], ['edX+C1'])

    def test_redirect_makes_exactly_one_model_call(self):
        calls = []
        steps = fake_steps(intent_text='{"on_topic": false, "redirect_reason": "off_topic"}',
                           retrieve=lambda *a, **kw: calls.append('retrieve') or [])
        base_intent, base_compose = steps.intent, steps.compose
        steps.intent = lambda **kw: calls.append('intent') or base_intent(**kw)
        steps.compose = lambda **kw: calls.append('compose') or base_compose(**kw)
        list(run_turn(self.ctx, steps))
        self.assertEqual(calls, ['intent'])

    def test_no_learner_text_in_any_log_record(self):
        with self.assertLogs(level='DEBUG') as logs:
            list(run_turn(self.ctx, fake_steps()))
        self.assertTrue(logs.records)
        self.assertFalse([r for r in logs.records if self.MARKER in r.getMessage()])
        self.assertFalse([r for r in logs.records if 'Your page.' in r.getMessage()])  # nor the reply text


class PortableTurnTests(PlainTestCase):
    """A whole turn on the in-memory store, with no Django test case, database or network."""

    INTENT = json.dumps({'on_topic': True, 'redirect_reason': None, 'merge': 'replace',
                         'intent': {'query': 'team leadership', 'skills': ['Leadership']}})
    COMPOSE = ('Your page.\n@@BLOCKS@@\n{"block":"page_header","title":"Lead","dek":"D"}\n'
               '{"block":"item_section","item_type":"course","dek":"d","items":[{"item_id":"edX+C1","why":"w"}]}\n')

    def test_turn_runs_and_persists_in_memory(self):
        memory = InMemorySessionStore()
        session = memory.load_or_create(session_uuid=None, lms_user_id=1, enterprise_customer_uuid='e',
                                        message='Lead a team')
        memory.lock(session)
        steps = Steps(intent=lambda **_: (self.INTENT, Usage(model='m1', effort=None)),
                      retrieve=lambda *a, **kw: default_candidates(),
                      compose=lambda **_: (d for d in [self.COMPOSE]), persist=memory.persist_turn)
        ticks = itertools.count()
        ctx = TurnContext(session=session, message='Lead a team', intent_model=ModelChoice('m1', None),
                          compose_model=ModelChoice('m1', None), scope=None, clock=lambda: next(ticks) * 0.1,
                          platform=lambda _m: 'fake')
        parts = list(run_turn(ctx, steps))
        self.assertEqual(parts[-1]['type'], 'finish')
        self.assertEqual(parts[-2]['data']['backend'], 'fake')
        self.assertEqual(memory.sessions[session['uuid']]['shown_item_ids'], ['edX+C1'])
        self.assertIsNone(memory.sessions[session['uuid']]['locked_at'])

    def test_retrieval_timeout_is_capped_by_the_deadline(self):
        seen = []
        session = InMemorySessionStore().load_or_create(session_uuid=None, lms_user_id=1,
                                                        enterprise_customer_uuid='e', message='m')
        times = iter([0.0, 0.0, 19.25] + [19.5] * 50)  # t0, intent cap, intent done, then near the deadline
        ctx = TurnContext(session=session, message='m', intent_model=ModelChoice('m1', None),
                          compose_model=ModelChoice('m1', None), scope=None, clock=lambda: next(times))
        steps = Steps(intent=lambda **_: (self.INTENT, Usage(model='m1', effort=None)),
                      retrieve=lambda *a, timeout_s: seen.append(timeout_s) or [],
                      compose=lambda **_: (d for d in ['Nothing fits.']))
        list(run_turn(ctx, steps))
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0], 2)  # 20 s deadline - 19.5 s elapsed = 0.5 s, not the full 2 s

    def test_timeouts_come_from_the_context(self):
        seen = {}

        def intent(**kwargs):
            seen['intent'] = kwargs['timeout_s']
            raise ModelError('x')

        session = InMemorySessionStore().load_or_create(session_uuid=None, lms_user_id=1,
                                                        enterprise_customer_uuid='e', message='m')
        ctx = TurnContext(session=session, message='m', intent_model=ModelChoice('m1', None),
                          compose_model=ModelChoice('m1', None), scope=None, intent_timeout_s=1.25,
                          clock=mock.Mock(return_value=0.0))
        list(run_turn(ctx, Steps(intent=intent, retrieve=None, compose=None)))
        self.assertEqual(seen['intent'], 1.25)
