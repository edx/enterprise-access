"""
The ``SessionStore`` contract, run against both stores: the Django ORM one and the in-memory one.

Anything that passes these can back the turn view or the harness.
"""
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from enterprise_access.apps.personalized_search.adapters.django_store import DjangoSessionStore
from enterprise_access.apps.personalized_search.adapters.memory_store import InMemorySessionStore
from enterprise_access.apps.personalized_search.core.errors import SessionNotFound, TurnInProgress
from enterprise_access.apps.personalized_search.core.ports import SessionStore
from enterprise_access.apps.personalized_search.models import ChatSession, ChatTurn, ItemFeedback

ENTERPRISE = str(uuid.UUID(int=1))
OTHER_ENTERPRISE = str(uuid.UUID(int=9))
PARTS = [{'type': 'start', 'messageId': 'turn_x'}, {'type': 'data-status', 'data': {}, 'transient': True},
         {'type': 'text-start', 'id': 'reply'}, {'type': 'text-delta', 'id': 'reply', 'delta': 'Hel'},
         {'type': 'text-delta', 'id': 'reply', 'delta': 'lo'}, {'type': 'text-end', 'id': 'reply'},
         {'type': 'data-timing', 'id': 'timing', 'data': {}, 'transient': True}]


class StoreContract:  # pylint: disable=no-member  # mixed into TestCase subclasses below
    """Shared cases; subclasses provide ``store`` and the store-specific helpers."""

    store: SessionStore

    def expire_lock(self, session_uuid):
        raise NotImplementedError

    def dismiss(self, lms_user_id, enterprise, item_id, saved=False):
        raise NotImplementedError

    def stored_parts(self, session_uuid) -> list[dict]:
        raise NotImplementedError

    def new_session(self, message='Lead a team', lms_user_id=7, enterprise=ENTERPRISE):
        return self.store.load_or_create(session_uuid=None, lms_user_id=lms_user_id,
                                         enterprise_customer_uuid=enterprise, message=message)

    def test_is_a_session_store(self):
        self.assertIsInstance(self.store, SessionStore)

    def test_new_session_is_truncated_and_unsaved_until_locked(self):
        session = self.new_session('x' * 600)
        self.assertTrue(session['is_new'])
        self.assertEqual(len(session['first_prompt']), 500)
        with self.assertRaises(SessionNotFound):
            self.store.load_or_create(session_uuid=session['uuid'], lms_user_id=7,
                                      enterprise_customer_uuid=ENTERPRISE, message='y')
        self.store.lock(session)
        self.assertFalse(session['is_new'])

    def test_lock_conflicts_within_the_window_and_releases(self):
        session = self.new_session()
        self.store.lock(session)
        again = self.store.load_or_create(session_uuid=session['uuid'], lms_user_id=7,
                                          enterprise_customer_uuid=ENTERPRISE, message='y')
        with self.assertRaises(TurnInProgress):
            self.store.lock(again)
        self.store.unlock(session['uuid'])
        self.store.lock(again)

    def test_stale_lock_is_taken_over(self):
        session = self.new_session()
        self.store.lock(session)
        self.expire_lock(session['uuid'])
        self.store.lock(self.store.load_or_create(session_uuid=session['uuid'], lms_user_id=7,
                                                  enterprise_customer_uuid=ENTERPRISE, message='y'))

    def test_other_learners_and_enterprises_cannot_load_it(self):
        session = self.new_session()
        self.store.lock(session)
        for lms_user_id, enterprise in ((8, ENTERPRISE), (7, OTHER_ENTERPRISE)):
            with self.assertRaises(SessionNotFound):
                self.store.load_or_create(session_uuid=session['uuid'], lms_user_id=lms_user_id,
                                          enterprise_customer_uuid=enterprise, message='y')
        with self.assertRaises(SessionNotFound):
            self.store.load_or_create(session_uuid='not-a-uuid', lms_user_id=7, enterprise_customer_uuid=ENTERPRISE,
                                      message='y')

    def test_persist_turn_and_history(self):
        session = self.new_session()
        self.store.lock(session)
        session.update(turn_count=1, title='Leading', merged_intent={'query': 'q'}, shown_item_ids=['a'])
        self.store.persist_turn(session, {'uuid': str(uuid.uuid4()), 'user_message': 'Lead a team',
                                          'reply_text': 'Hello', 'item_ids': ['a'], 'parts': PARTS})
        parts = self.stored_parts(session['uuid'])
        self.assertEqual([p['type'] for p in parts], ['start', 'text-start', 'text-delta', 'text-end'])
        self.assertEqual(parts[2]['delta'], 'Hello')
        loaded = self.store.load_or_create(session_uuid=session['uuid'], lms_user_id=7,
                                           enterprise_customer_uuid=ENTERPRISE, message='next')
        self.assertEqual(loaded['history'], [{'user': 'Lead a team', 'reply': 'Hello'}])
        self.assertEqual((loaded['turn_count'], loaded['item_count'], loaded['title']), (1, 1, 'Leading'))
        self.assertEqual((loaded['merged_intent'], loaded['shown_item_ids']), ({'query': 'q'}, ['a']))
        self.store.lock(loaded)  # persist_turn released the lock

    def test_not_interested_ids_are_scoped(self):
        self.dismiss(7, ENTERPRISE, 'a')
        self.dismiss(7, ENTERPRISE, 'b', saved=True)
        self.dismiss(8, ENTERPRISE, 'c')
        self.assertEqual(self.new_session()['not_interested_ids'], ['a'])


class DjangoSessionStoreTests(StoreContract, TestCase):
    """The ORM store, plus what only it guarantees (rows, the transaction)."""

    def setUp(self):
        super().setUp()
        self.store = DjangoSessionStore()

    def expire_lock(self, session_uuid):
        ChatSession.objects.filter(uuid=session_uuid).update(
            active_turn_started_at=timezone.now() - timedelta(seconds=30))

    def dismiss(self, lms_user_id, enterprise, item_id, saved=False):
        ItemFeedback.objects.create(lms_user_id=lms_user_id, enterprise_customer_uuid=enterprise, item_id=item_id,
                                    item_type='course',
                                    kind=ItemFeedback.KIND_SAVED if saved else ItemFeedback.KIND_NOT_INTERESTED)

    def stored_parts(self, session_uuid):
        return ChatTurn.objects.get(session_id=session_uuid).parts

    def test_new_session_row_is_created_locked(self):
        session = self.new_session()
        self.assertFalse(ChatSession.objects.exists())  # nothing written until the turn locks it
        self.store.lock(session)
        row = ChatSession.objects.get(uuid=session['uuid'])
        self.assertEqual(row.turn_count, 0)
        self.assertIsNotNone(row.active_turn_started_at)


class InMemorySessionStoreTests(StoreContract, TestCase):
    """The memory store (harness, tests, other hosts)."""

    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.store = InMemorySessionStore(clock=lambda: self.now)

    def expire_lock(self, session_uuid):
        self.now += 30

    def dismiss(self, lms_user_id, enterprise, item_id, saved=False):
        if not saved:
            self.store.dismissed.setdefault((lms_user_id, enterprise), []).append(item_id)

    def stored_parts(self, session_uuid):
        return self.store.turns[session_uuid][-1]['parts']
