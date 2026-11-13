"""The Personalized Search tables can be dropped and recreated cleanly (they belong to this app alone)."""
import importlib
from types import SimpleNamespace
from unittest import TestCase as PlainTestCase
from unittest import mock
from uuid import uuid4

from django.apps import apps
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from enterprise_access.apps.personalized_search.models import ChatSession, ChatTurn, ItemFeedback

TABLES = {'personalized_search_chatsession', 'personalized_search_chatturn', 'personalized_search_itemfeedback'}


class DroppableTablesTests(TransactionTestCase):
    """``migrate personalized_search zero`` removes exactly this app's tables and nothing depends on them."""

    def tables(self):
        return set(connection.introspection.table_names()) & TABLES

    def test_migrate_to_zero_and_back(self):
        self.assertEqual(self.tables(), TABLES)
        call_command('migrate', 'personalized_search', 'zero', verbosity=0)
        self.assertEqual(self.tables(), set())
        call_command('migrate', 'personalized_search', verbosity=0)
        self.assertEqual(self.tables(), TABLES)

    def test_no_other_app_references_these_models(self):
        ours = set(apps.get_app_config('personalized_search').get_models())
        for model in apps.get_models():
            if model in ours:
                continue
            for field in model._meta.get_fields():
                related = getattr(field, 'related_model', None)
                message = f'{model._meta.label}.{field.name} points at a Personalized Search table'
                self.assertNotIn(related, ours, message)


class ModelBehaviourTests(TestCase):
    """The constraints and delete behaviour the stores rely on."""

    def setUp(self):
        super().setUp()
        self.enterprise = uuid4()
        self.session = ChatSession.objects.create(
            lms_user_id=7, enterprise_customer_uuid=self.enterprise, first_prompt='Build data skills',
        )

    def turn(self, sequence):
        return ChatTurn.objects.create(session=self.session, sequence=sequence, user_message='hi')

    def feedback(self, kind=ItemFeedback.KIND_SAVED, **kwargs):
        return ItemFeedback.objects.create(
            lms_user_id=7, enterprise_customer_uuid=self.enterprise, item_id='course:x', item_type='course',
            kind=kind, **kwargs,
        )

    def test_string_forms_carry_ids_and_counts_but_no_learner_text(self):
        turn = self.turn(1)
        feedback = self.feedback()
        self.assertEqual(str(self.session), f'ChatSession {self.session.uuid} (learner 7, 0 turns)')
        self.assertEqual(str(turn), f'ChatTurn 1 of {self.session.uuid}')
        self.assertEqual(str(feedback), 'ItemFeedback saved course:x (learner 7)')
        self.assertNotIn('Build data skills', str(self.session))

    def test_a_session_cannot_have_two_turns_with_the_same_sequence(self):
        self.turn(1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.turn(1)
        self.turn(2)

    def test_a_learner_can_save_or_dismiss_an_item_once_each(self):
        self.feedback()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.feedback()
        self.feedback(kind=ItemFeedback.KIND_NOT_INTERESTED)
        self.assertEqual(ItemFeedback.objects.count(), 2)

    def test_hard_deleting_a_session_removes_its_turns_and_keeps_feedback(self):
        self.turn(1)
        feedback = self.feedback(session=self.session)
        self.session.delete(soft=False)
        self.assertEqual(ChatTurn.objects.count(), 0)
        feedback.refresh_from_db()
        self.assertIsNone(feedback.session)

    def test_deleting_a_session_is_soft_by_default_and_keeps_its_turns(self):
        self.turn(1)
        self.session.delete()
        self.assertFalse(ChatSession.objects.filter(pk=self.session.pk).exists())
        self.assertTrue(ChatSession.all_objects.get(pk=self.session.pk).is_removed)
        self.assertEqual(ChatTurn.objects.count(), 1)


class Utf8mb4MigrationTests(PlainTestCase):
    """``0003_utf8mb4_tables`` converts only MySQL tables that aren't utf8mb4 yet, with foreign key checks paused."""

    migration = importlib.import_module('enterprise_access.apps.personalized_search.migrations.0003_utf8mb4_tables')

    def run_on(self, vendor, pending=()):
        """Run the migration on a fake connection whose tables ``pending`` aren't utf8mb4; return the SQL it ran."""
        cursor = mock.MagicMock()
        cursor.fetchall.return_value = [(table,) for table in pending]
        fake = mock.MagicMock(vendor=vendor)
        fake.cursor.return_value.__enter__.return_value = cursor
        self.migration.to_utf8mb4(None, SimpleNamespace(connection=fake))
        return [c.args[0] for c in cursor.execute.call_args_list]

    def test_other_engines_are_untouched(self):
        self.assertEqual(self.run_on('sqlite', pending=TABLES), [])

    def test_tables_already_utf8mb4_are_left_alone(self):
        statements = self.run_on('mysql')
        self.assertEqual(len(statements), 1)
        self.assertIn('information_schema.TABLES', statements[0])

    def test_tables_still_utf8mb3_are_converted(self):
        statements = self.run_on('mysql', pending=['personalized_search_chatturn'])
        self.assertEqual(statements[1:], [
            'SET FOREIGN_KEY_CHECKS = 0',
            'ALTER TABLE `personalized_search_chatturn` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci',
            'SET FOREIGN_KEY_CHECKS = 1',
        ])
