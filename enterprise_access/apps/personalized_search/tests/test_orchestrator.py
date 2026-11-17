"""Tests for the turn orchestrator with fake steps (no OpenAI or Algolia), persisting through the Django store."""
import dataclasses
import itertools
import json
import uuid
from unittest import TestCase as PlainTestCase
from unittest import mock

import ddt
from django.test import TestCase

from enterprise_access.apps.personalized_search.adapters.django_store import DjangoSessionStore
from enterprise_access.apps.personalized_search.adapters.memory_store import InMemorySessionStore
from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.compose import SYSTEM_PROMPT_NO_PLAN
from enterprise_access.apps.personalized_search.core.errors import ModelConfigurationError, ModelError, ModelRejected
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, Steps, TurnContext, run_turn
from enterprise_access.apps.personalized_search.core.ports import Usage
from enterprise_access.apps.personalized_search.core.retrieval import CatalogScope, RetrievalFailed, SearchResults
from enterprise_access.apps.personalized_search.models import ChatSession, ChatTurn, ItemFeedback
from enterprise_access.apps.personalized_search.tests.helpers import candidate, default_candidates, program_hit

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
        self.assertEqual(types[:4], ['start', 'data-session', 'data-analytics', 'data-status'])
        analytics = parts[2]['data']
        self.assertEqual((analytics['conversation_id'], analytics['turn_id'], analytics['turn_index']),
                         (self.session['uuid'], TURN_UUID, 1))
        section = [p['data'] for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'item_section'][0]
        self.assertEqual({k: section['props']['items'][0][k] for k in ('section', 'position')},
                         {'section': 'course', 'position': 1})
        self.assertEqual(types[-3:], ['data-session', 'data-timing', 'finish'])
        self.assertEqual(parts[0]['messageId'], f'turn_{TURN_UUID}')
        self.assertEqual([p['data']['step'] for p in parts if p['type'] == 'data-status'],
                         ['understanding', 'searching', 'writing'])
        timing = parts[-2]['data']
        self.assertEqual(timing['backend'], 'openai')
        self.assertLessEqual(timing['first_text_ms'], timing['first_section_ms'])
        self.assertIsNotNone(timing['debug']['cost_usd'])
        saved = self.saved()
        # The scripted course section, plus the related programs offered alongside it.
        self.assertEqual((saved.turn_count, saved.item_count, saved.title), (1, 3, 'Leading'))
        self.assertEqual(saved.shown_item_ids, ['edX+C1', 'p-1', 'p-2'])
        self.assertIsNone(saved.active_turn_started_at)
        turn = ChatTurn.objects.get(session=saved)
        self.assertEqual((str(turn.uuid), turn.sequence, turn.reply_text), (TURN_UUID, 1, 'Your page.'))
        self.assertEqual(turn.item_ids, ['edX+C1', 'p-1', 'p-2'])
        stored = [p['type'] for p in turn.parts]
        self.assertNotIn('data-timing', stored)
        self.assertNotIn('data-status', stored)
        self.assertNotIn('finish', stored)
        self.assertEqual(stored.count('text-delta'), 1)  # coalesced for replay

    def test_the_suggested_order_switch_off_drops_the_plan_from_the_prompt_and_the_results(self):
        systems = []
        steps = fake_steps()
        model_compose = steps.compose

        def compose(**kwargs):
            systems.append(kwargs['system'])
            return model_compose(**kwargs)

        ctx = dataclasses.replace(self.ctx, message='What should I take next?', plan_enabled=False)
        parts = list(run_turn(ctx, dataclasses.replace(steps, compose=compose)))
        self.assertEqual(systems, [SYSTEM_PROMPT_NO_PLAN])
        self.assertFalse([p for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'plan'])
        self.assertEqual(parts[-1]['type'], 'finish')

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

    def redirect(self, reason, history=()):
        """Run a turn (in a new session) the intent step redirects for ``reason``; return its parts and reply text."""
        self.session = new_locked_session('x')
        self.session['history'] = [{'user': 'x', 'reply': reply} for reply in history]
        steps = fake_steps(intent_text=json.dumps({'on_topic': False, 'redirect_reason': reason}),
                           retrieve=lambda *a, **kw: self.fail('no retrieval on redirect'))
        parts = list(run_turn(dataclasses.replace(self.ctx, session=self.session), steps))
        return parts, ''.join(p['delta'] for p in parts if p['type'] == 'text-delta')

    def test_distress_gets_its_own_reply_and_nothing_that_pushes_courses(self):
        parts, reply = self.redirect('distress', history=[constants.REDIRECT_REPLIES['distress']])
        self.assertEqual(reply, constants.REDIRECT_REPLIES['distress'])  # never shortened
        self.assertIn('someone you trust', reply)
        self.assertFalse([p for p in parts if p['type'] in ('data-followups', 'data-block')])
        self.assertEqual(parts[-1]['type'], 'finish')
        turn = ChatTurn.objects.get(session_id=self.session['uuid'])
        self.assertEqual((turn.reply_text, turn.follow_up_attribute), (reply, ''))

    def test_each_redirect_reason_has_its_reply_and_a_heading_that_recommends_nothing(self):
        for reason in ('off_topic', 'unsafe', 'injection', 'external_provider'):
            parts, reply = self.redirect(reason)
            self.assertEqual(reply, constants.REDIRECT_REPLIES[reason], reason)
            followups = [p for p in parts if p['type'] == 'data-followups'][0]['data']
            self.assertEqual((followups['question'], followups['suggestions']),
                             (constants.REDIRECT_FOLLOWUP_QUESTION, constants.OPENING_CHIPS))
        self.assertTrue(constants.REDIRECT_REPLIES['unsafe'].startswith("I can't help with that."))

    def test_a_repeated_redirect_alternates_with_a_shorter_reply(self):
        _, reply = self.redirect('off_topic', history=[constants.REDIRECT_REPLY])
        self.assertEqual(reply, constants.REDIRECT_REPEAT_REPLY)
        self.assertLess(len(reply), len(constants.REDIRECT_REPLY))
        _, reply = self.redirect('off_topic', history=[constants.REDIRECT_REPLY, constants.REDIRECT_REPEAT_REPLY])
        self.assertEqual(reply, constants.REDIRECT_REPLY)

    def test_another_providers_courses_with_a_topic_search_it_and_say_only_this_catalog_is_searched(self):
        steps = fake_steps(intent_text=json.dumps({
            'on_topic': True, 'redirect_reason': 'external_provider', 'merge': 'replace',
            'intent': {'query': 'Python', 'skills': ['Python']}}))
        parts = list(run_turn(self.ctx, steps))
        reply = ''.join(p['delta'] for p in parts if p['type'] == 'text-delta')
        self.assertEqual(reply, f'{constants.EXTERNAL_PROVIDER_NOTE} Your page.')
        self.assertEqual([p['type'] for p in parts].count('text-start'), 1)
        self.assertTrue([p for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'item_section'])
        self.assertEqual(ChatTurn.objects.get(session_id=self.session['uuid']).reply_text, reply)

    def run_with(self, intent_json, scope):
        """Run a turn with ``scope``; return what retrieval and compose were given."""
        seen = {}
        steps = fake_steps(intent_text=json.dumps(intent_json))
        retrieve, compose = steps.retrieve, steps.compose
        steps.retrieve = lambda merged, sc, *a, **kw: seen.update(scope=sc, merged=merged) or retrieve(merged, sc)
        steps.compose = lambda **kw: seen.update(user=kw['user']) or compose(**kw)
        list(run_turn(dataclasses.replace(self.ctx, scope=scope), steps))
        return seen

    def test_replies_follow_the_learners_language_and_courses_the_language_asked_for(self):
        scope = CatalogScope(filter='f', reply_languages=('English', 'Spanish'))
        wrote_spanish = {'on_topic': True, 'redirect_reason': None, 'merge': 'replace', 'message_language': 'Spanish',
                         'intent': {'query': 'servicio al cliente', 'skills': ['Customer Service']}}
        seen = self.run_with(wrote_spanish, scope)
        self.assertTrue(seen['user'].startswith('RESPONSE LANGUAGE: Spanish\n'))
        self.assertEqual(seen['scope'].language_name, 'English')  # taught in the portal's language by default
        asked = dict(wrote_spanish, intent=dict(wrote_spanish['intent'], constraints={'course_language': 'Spanish'}))
        self.assertEqual(self.run_with(asked, scope)['scope'].language_name, 'Spanish')
        unsupported = dict(wrote_spanish, message_language='Japanese')
        self.assertTrue(self.run_with(unsupported, scope)['user'].startswith('RESPONSE LANGUAGE: English\n'))

    def test_a_finished_course_and_its_programs_leave_the_results_and_stay_out(self):
        finished = {'on_topic': True, 'redirect_reason': None, 'merge': 'replace',
                    'intent': {'query': 'leadership', 'skills': ['Leadership'], 'completed': ['Course edX+C1']}}
        steps = fake_steps(intent_text=json.dumps(finished), retrieve=lambda *a, **kw: default_candidates() + [
            candidate(dict(program_hit('p-with-c1'), course_keys=['edX+C1']))])
        parts = list(run_turn(self.ctx, steps))
        shown = [i['item_id'] for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'item_section'
                 for i in p['data']['props']['items']]
        self.assertNotIn('edX+C1', shown)
        self.assertNotIn('p-with-c1', shown)
        saved = self.saved().merged_intent
        self.assertEqual((saved['completed'], saved['completed_keys']), (['Course edX+C1'], ['edX+C1']))

    def test_the_previous_screen_reaches_both_steps_and_pointed_at_items_are_fetched_again(self):
        self.session['previous_screen'] = [{'where': 'top pick', 'item_id': 'p-1', 'title': 'Program p-1'},
                                           {'where': 'compare 2', 'item_id': 'edX+C3', 'title': 'Course edX+C3'}]
        seen = {}
        steps = fake_steps()
        intent, compose = steps.intent, steps.compose
        steps.intent = lambda **kw: seen.update(intent=kw['user']) or intent(**kw)
        steps.retrieve = lambda merged, *a, **kw: seen.update(merged=merged) or default_candidates()
        steps.compose = lambda **kw: seen.update(compose=kw['user']) or compose(**kw)
        ctx = dataclasses.replace(self.ctx, message='Compare the first two')
        parts = list(run_turn(ctx, steps))
        line = '1. top pick: "Program p-1" (item_id p-1)'
        self.assertIn(f'PREVIOUS SCREEN (in order):\n{line}', seen['intent'])
        self.assertIn(line, seen['compose'])
        self.assertEqual(seen['merged']['pinned'], ['p-1', 'edX+C3'])
        # After the turn, the session holds this turn's screen for the next one.
        self.assertEqual(self.session['previous_screen'][0], {'where': 'Courses 1', 'item_id': 'edX+C1',
                                                              'title': 'Course edX+C1'})
        self.assertTrue([p for p in parts if p['type'] == 'finish'])

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

    def test_a_turn_past_its_deadline_keeps_the_follow_up_the_model_wrote(self):
        now = [0.0]

        def compose(**_):
            yield COMPOSE
            now[0] = constants.TURN_DEADLINE_S + 1  # the deadline passes after the followup line arrived
            yield '{"block":"insight","title":"T","body":"B"}\n'

        ctx = TurnContext(session=self.session, message='I want to lead a team',
                          intent_model=ModelChoice('gpt-4.1-mini', None),
                          compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None,
                          clock=lambda: now[0], id_factory=lambda: TURN_UUID, platform=openai_platform)
        parts = list(run_turn(ctx, dataclasses.replace(fake_steps(), compose=compose)))
        self.assertEqual(parts[-1], {'type': 'error', 'errorText': 'deadline_exceeded'})
        self.assertEqual((parts[-2]['type'], parts[-2]['data']['question']), ('data-followups', 'Level?'))
        self.assertEqual([p['type'] for p in parts].count('data-followups'), 1)

    def test_errors_persist_nothing(self):
        cases = [
            (fake_steps(intent_text='not json'), 'intent_invalid'),
            (fake_steps(intent_error=ModelConfigurationError('x')), 'model_failed'),
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

    def test_only_dismissed_items_are_excluded_and_new_cards_are_flagged_on_the_next_turn(self):
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
        compose = ('Updated.\n@@BLOCKS@@\n'
                   '{"block":"item_section","item_type":"course","dek":"d","items":['
                   '{"item_id":"edX+C1","why":"w","fit":[]},{"item_id":"edX+C2","why":"w","fit":[]}]}\n')
        parts = list(run_turn(ctx, fake_steps(compose_text=compose, retrieve=lambda intent, scope, excluded, **kw: (
            seen.append(excluded) or default_candidates()))))
        # Results update in place: edX+C1 (shown on turn 1) may come back; only the dismissed item is excluded.
        self.assertEqual(seen, [['edX+C4']])
        section = [p['data'] for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'item_section'][0]
        self.assertEqual({i['item_id']: i.get('status') for i in section['props']['items']},
                         {'edX+C1': None, 'edX+C2': 'new'})
        self.assertEqual(ChatTurn.objects.filter(session_id=self.session['uuid']).count(), 2)

    def test_only_the_most_recent_dismissals_reach_the_search_but_compose_drops_them_all(self):
        recent = [f'x-{i}' for i in range(constants.MAX_RETRIEVAL_EXCLUDED_IDS)]
        self.session['not_interested_ids'] = recent + ['edX+C1']  # newest first, as the store lists them
        seen = []
        parts = list(run_turn(self.ctx, fake_steps(retrieve=lambda intent, scope, excluded, **kw: (
            seen.append(excluded) or default_candidates()))))
        self.assertEqual(seen, [recent])
        shown = [i['item_id'] for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'item_section'
                 for i in p['data']['props']['items']]
        self.assertTrue(shown)
        self.assertNotIn('edX+C1', shown)  # returned by the search, but still dismissed

    def test_catalog_totals_reach_the_section(self):
        steps = fake_steps(retrieve=lambda *a, **kw: SearchResults(default_candidates(), {'course': 37},
                                                                   {'course': 'team leadership'}))
        sections = {p['data']['props']['item_type']: p['data']['props'] for p in run_turn(self.ctx, steps)
                    if p['type'] == 'data-block' and p['data']['type'] == 'item_section'}
        self.assertEqual((sections['course']['total'], sections['course']['search_query']),
                         (37, 'team leadership'))  # "See all (37)", linking to the query counted
        self.assertFalse({'total', 'search_query'} & set(sections['program']))

    def test_first_tiles_are_timed_from_the_first_block_with_cards(self):
        reads, ticks = [], itertools.count()

        def clock():
            reads.append(next(ticks) * 0.1)
            return reads[-1]

        compose = COMPOSE.replace('@@BLOCKS@@\n', '@@BLOCKS@@\n{"block":"top_pick","item_id":"p-1","reason":"R"}\n')
        at_top_pick, timing = None, None
        for part in run_turn(dataclasses.replace(self.ctx, clock=clock), fake_steps(compose_text=compose)):
            if part['type'] == 'data-block' and part['data']['type'] == 'top_pick':
                at_top_pick = round(reads[-1] * 1000)  # the clock read just before the top pick went out
            timing = part['data'] if part['type'] == 'data-timing' else timing
        self.assertEqual(timing['first_section_ms'], at_top_pick)

    def test_first_turn_cards_carry_no_status(self):
        parts = list(run_turn(self.ctx, fake_steps()))
        items = [i for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'item_section'
                 for i in p['data']['props']['items']]
        self.assertTrue(items)
        self.assertTrue(all('status' not in i for i in items))


@ddt.ddt
class ModelResilienceTests(TestCase):
    """A model step that can't be reached is retried once, then the turn shows server-built results."""

    LOGGER = 'enterprise_access.apps.personalized_search.core.orchestrator'

    def setUp(self):
        super().setUp()
        ticks = itertools.count()
        self.session = new_locked_session('I want to lead a team')
        self.ctx = TurnContext(session=self.session, message='I want to lead a team',
                               intent_model=ModelChoice('gpt-4.1-mini', None),
                               compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None, debug=True,
                               clock=lambda: next(ticks) * 0.1, platform=openai_platform)
        self.calls = {'intent': 0, 'compose': 0}

    def steps(self, *, intent_failures=0, compose_failures=0, intent_error=ModelError('timeout'),
              compose_error=ModelError('timeout')):
        """Steps whose intent and compose calls fail (before any text) the given number of times, then work."""
        base = fake_steps()

        def intent(**kwargs):
            self.calls['intent'] += 1
            if self.calls['intent'] <= intent_failures:
                raise intent_error
            return base.intent(**kwargs)

        def compose(**kwargs):
            self.calls['compose'] += 1
            if self.calls['compose'] <= compose_failures:
                raise compose_error
            yield from base.compose(**kwargs)

        return dataclasses.replace(base, intent=intent, compose=compose)

    def run_logged(self, steps):
        """The turn's parts and its one turn-log record."""
        with self.assertLogs(self.LOGGER, level='INFO') as logs:
            parts = list(run_turn(self.ctx, steps))
        (record,) = [json.loads(r.getMessage().split(' ', 1)[1]) for r in logs.records
                     if r.getMessage().startswith('personalized_search.turn ')]
        return parts, record

    @staticmethod
    def reply(parts):
        return ''.join(p['delta'] for p in parts if p['type'] == 'text-delta')

    @staticmethod
    def blocks(parts):
        return [p['data']['type'] for p in parts if p['type'] == 'data-block']

    def test_an_intent_timeout_is_retried_once(self):
        parts, record = self.run_logged(self.steps(intent_failures=1))
        self.assertEqual(parts[-1], {'type': 'finish'})
        self.assertEqual(self.reply(parts), 'Your page.')  # the model's own reply
        self.assertEqual(self.calls, {'intent': 2, 'compose': 1})
        self.assertEqual((record['retries'], record['model_failures'], record['degraded']),
                         ({'intent': 1, 'compose': 0}, {'intent': 1, 'compose': 0}, None))

    def test_intent_that_fails_twice_shows_server_built_results_without_a_model_reply(self):
        parts, record = self.run_logged(self.steps(intent_failures=2))
        self.assertEqual(parts[-1], {'type': 'finish'})
        self.assertEqual(self.reply(parts), constants.DEGRADED_REPLY)
        self.assertIn('top_pick', self.blocks(parts))
        self.assertIn('compare', self.blocks(parts))
        # The message was never classified, so no model writes text from it: compose isn't called.
        self.assertEqual(self.calls, {'intent': 2, 'compose': 0})
        self.assertEqual((record['outcome'], record['degraded']), ('done', 'intent'))
        self.assertEqual(ChatTurn.objects.get().intent['fallback'], True)

    @ddt.data(None, {'query': 'team leadership', 'skills': ['Leadership'], 'roles': [], 'level': None,
                     'item_types': ['course'], 'constraints': {'max_weeks': None, 'pacing': None}})
    def test_an_intent_fallback_searches_with_the_message_but_keeps_the_sessions_intent(self, prior):
        self.session['merged_intent'] = prior
        self.ctx.message = 'quick ones please'  # would add a 4-week limit to the merged intent
        searched = []
        steps = dataclasses.replace(self.steps(intent_failures=2),
                                    retrieve=lambda intent, *a, **kw: searched.append(intent) or default_candidates())
        parts = list(run_turn(self.ctx, steps))
        self.assertEqual(parts[-1], {'type': 'finish'})
        # This turn's search uses the unclassified message; the session doesn't carry it to later turns.
        self.assertEqual(searched[0]['constraints']['max_weeks'], 4)
        self.assertEqual(self.session['merged_intent'], prior)
        self.assertEqual(ChatSession.objects.get(uuid=self.session['uuid']).merged_intent, prior)

    @ddt.data(ModelConfigurationError('no key'), ModelRejected('bad request'))
    def test_a_configuration_error_or_a_rejected_request_is_not_retried(self, error):
        cases = [({'intent_failures': 2, 'intent_error': error}, {'intent': 1, 'compose': 0}),
                 ({'compose_failures': 2, 'compose_error': error}, {'intent': 1, 'compose': 1})]
        for kwargs, calls in cases:
            self.calls = {'intent': 0, 'compose': 0}
            parts, record = self.run_logged(self.steps(**kwargs))
            self.assertEqual(parts[-1], {'type': 'error', 'errorText': 'model_failed'})
            self.assertEqual((self.calls, record['retries'], record['degraded']),
                             (calls, {'intent': 0, 'compose': 0}, None))

    def test_compose_that_fails_before_any_text_is_retried(self):
        parts, record = self.run_logged(self.steps(compose_failures=1))
        self.assertEqual(self.reply(parts), 'Your page.')
        self.assertEqual(self.calls['compose'], 2)
        self.assertEqual((record['retries']['compose'], record['degraded']), (1, None))

    def test_compose_that_fails_twice_shows_server_built_results(self):
        parts, record = self.run_logged(self.steps(compose_failures=2))
        self.assertEqual(parts[-1], {'type': 'finish'})
        self.assertEqual(self.reply(parts), constants.DEGRADED_REPLY)
        self.assertEqual(self.blocks(parts)[:2], ['page_header', 'profile'])
        self.assertIn('item_section', self.blocks(parts))
        self.assertEqual(len([p for p in parts if p['type'] == 'data-followups']), 1)
        self.assertEqual((record['outcome'], record['degraded'], record['model_failures']['compose']),
                         ('done', 'compose', 2))
        self.assertEqual(ChatTurn.objects.get().reply_text, constants.DEGRADED_REPLY)

    def test_compose_is_not_retried_near_the_deadline(self):
        self.ctx.t0 = -(constants.TURN_DEADLINE_S - constants.COMPOSE_RETRY_MIN_S + 1)  # less than the retry needs
        parts, record = self.run_logged(self.steps(compose_failures=1))
        self.assertEqual(self.reply(parts), constants.DEGRADED_REPLY)
        self.assertEqual((self.calls['compose'], record['retries']['compose'], record['degraded']), (1, 0, 'compose'))

    def test_compose_that_fails_after_text_has_streamed_still_ends_in_an_error(self):
        parts = list(run_turn(self.ctx, fake_steps(compose_error=ModelError('reset'))))
        self.assertEqual(parts[-1], {'type': 'error', 'errorText': 'model_failed'})
        # The follow-up the model wrote before failing still goes out, so its chips aren't lost.
        self.assertEqual((parts[-2]['type'], parts[-2]['data']['question']), ('data-followups', 'Level?'))
        self.assertFalse(ChatTurn.objects.exists())


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
        self.assertEqual(record['item_ids'], ['edX+C1', 'p-1', 'p-2'])
        self.assertEqual(record['item_count'], 3)  # the course plus the related programs
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
        self.assertEqual(record['item_ids'], ['edX+C1'])  # stopped before the related programs were added

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
        self.assertEqual(memory.sessions[session['uuid']]['shown_item_ids'], ['edX+C1', 'p-1', 'p-2'])
        self.assertIsNone(memory.sessions[session['uuid']]['locked_at'])

    def test_retrieval_timeout_is_capped_by_the_deadline(self):
        seen = []
        session = InMemorySessionStore().load_or_create(session_uuid=None, lms_user_id=1,
                                                        enterprise_customer_uuid='e', message='m')
        times = iter([0.0, 0.0, 19.25] + [19.5] * 50)  # t0, intent cap, intent done, then near the deadline
        ctx = TurnContext(session=session, message='m', intent_model=ModelChoice('m1', None),
                          compose_model=ModelChoice('m1', None), scope=None, clock=lambda: next(times))
        steps = Steps(intent=lambda **_: (self.INTENT, Usage(model='m1', effort=None)),
                      retrieve=lambda *a, timeout_s, **_: seen.append(timeout_s) or [],
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
