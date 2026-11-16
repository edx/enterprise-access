"""Tests for ``purge_personalized_search_sessions``, the 30-day retention purge."""
import uuid
from datetime import timedelta
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone
from edx_toggles.toggles.testutils import override_waffle_switch

from enterprise_access.apps.personalized_search.adapters.django_store import delete_conversation
from enterprise_access.apps.personalized_search.models import ChatSession, ChatTurn, ItemFeedback
from enterprise_access.toggles import PERSONALIZED_SEARCH_DISABLE_RETENTION_PURGE

ENTERPRISE = uuid.uuid4()
COMMAND = 'purge_personalized_search_sessions'
EMIT = 'enterprise_access.apps.personalized_search.events.segment_emit'
DELETE = ('enterprise_access.apps.personalized_search.management.commands.purge_personalized_search_sessions'
          '.delete_conversation')


class PurgePersonalizedSearchSessionsTests(TestCase):
    """Sessions with no message in the last 30 days are deleted for good, the same way as the learner's X."""

    def session(self, days_ago, *, turns=1, soft_deleted=False):
        """A session whose last message was ``days_ago`` days ago, with ``turns`` turns."""
        row = ChatSession.objects.create(lms_user_id=7, enterprise_customer_uuid=ENTERPRISE, first_prompt='Lead',
                                         turn_count=turns)
        for sequence in range(1, turns + 1):
            ChatTurn.objects.create(session=row, sequence=sequence, user_message=f'm{sequence}', parts=[])
        if soft_deleted:
            row.delete()  # saves the whole row, so it comes before the backdating below
        ChatSession.all_objects.filter(uuid=row.uuid).update(last_message_at=timezone.now() - timedelta(days=days_ago))
        return row

    def exists(self, row):
        return ChatSession.all_objects.filter(uuid=row.uuid).exists()

    @mock.patch(EMIT)
    def test_deletes_sessions_idle_past_30_days_with_their_turns_and_keeps_newer_ones(self, emit):
        old, recent = self.session(31), self.session(29)
        call_command(COMMAND)
        self.assertFalse(self.exists(old))
        self.assertFalse(ChatTurn.objects.filter(session_id=old.uuid).exists())
        self.assertTrue(self.exists(recent))
        self.assertEqual(ChatTurn.objects.filter(session_id=recent.uuid).count(), 1)
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[1], 'history.session-deleted')
        self.assertEqual(emit.call_args.args[2]['reason'], 'retention')
        self.assertEqual(emit.call_args.args[2]['conversation_id'], str(old.uuid))

    @mock.patch(EMIT)
    def test_keeps_the_learners_saved_items(self, _emit):
        old = self.session(45)
        saved = ItemFeedback.objects.create(lms_user_id=7, enterprise_customer_uuid=ENTERPRISE, item_id='c1',
                                            item_type='course', kind=ItemFeedback.KIND_SAVED, session=old,
                                            card_snapshot={'item_id': 'c1'})
        call_command(COMMAND)
        saved.refresh_from_db()
        self.assertIsNone(saved.session)

    @mock.patch(EMIT)
    def test_purges_soft_deleted_and_empty_sessions_too(self, _emit):
        removed = self.session(40, soft_deleted=True)
        empty = self.session(40, turns=0)
        call_command(COMMAND)
        self.assertFalse(self.exists(removed))
        self.assertFalse(self.exists(empty))

    @mock.patch(EMIT)
    def test_dry_run_deletes_nothing(self, emit):
        old = self.session(31)
        call_command(COMMAND, '--dry-run')
        self.assertTrue(self.exists(old))
        emit.assert_not_called()

    @mock.patch(EMIT)
    def test_days_and_batch_size_are_honoured(self, _emit):
        rows = [self.session(8) for _ in range(5)]
        kept = self.session(6)
        call_command(COMMAND, '--days=7', '--batch-size=2')
        self.assertFalse(any(self.exists(r) for r in rows))
        self.assertTrue(self.exists(kept))

    @mock.patch(EMIT)
    def test_skips_a_session_with_a_turn_running(self, _emit):
        busy = self.session(40)
        ChatSession.all_objects.filter(uuid=busy.uuid).update(active_turn_started_at=timezone.now())
        call_command(COMMAND)
        self.assertTrue(self.exists(busy))

    @mock.patch(EMIT)
    def test_keeps_a_session_the_learner_resumes_while_the_purge_runs(self, _emit):
        older, resumed = self.session(50), self.session(40)

        def resume_the_other(session, **kwargs):
            delete_conversation(session, **kwargs)
            ChatSession.all_objects.filter(uuid=resumed.uuid).update(last_message_at=timezone.now())

        with mock.patch(DELETE, side_effect=resume_the_other):
            call_command(COMMAND, '--batch-size=1')
        self.assertFalse(self.exists(older))
        self.assertTrue(self.exists(resumed))

    @mock.patch(EMIT)
    def test_the_kill_switch_pauses_it_and_dry_run_still_reports(self, emit):
        old = self.session(31)
        with override_waffle_switch(PERSONALIZED_SEARCH_DISABLE_RETENTION_PURGE, active=True):
            with self.assertLogs(level='INFO') as logs:
                call_command(COMMAND)
                call_command(COMMAND, '--dry-run')
        self.assertTrue(self.exists(old))
        emit.assert_not_called()
        self.assertTrue(any('paused' in line for line in logs.output))
        self.assertTrue(any('dry_run=True' in line and 'sessions=1' in line for line in logs.output))

    def test_rejects_a_window_under_one_day(self):
        with self.assertRaises(CommandError):
            call_command(COMMAND, '--days=0')

    @mock.patch(EMIT)
    def test_a_failed_delete_is_reported_after_the_rest_are_done(self, _emit):
        first, second = self.session(50), self.session(40)
        real = ChatSession.delete

        def flaky(session, *args, **kwargs):
            if session.uuid == first.uuid:
                raise RuntimeError('db hiccup')
            return real(session, *args, **kwargs)

        with mock.patch.object(ChatSession, 'delete', flaky), self.assertRaises(CommandError) as raised:
            call_command(COMMAND)
        self.assertIn(str(first.uuid), str(raised.exception))
        self.assertTrue(self.exists(first))
        self.assertFalse(self.exists(second))
