"""The Personalized Search tables can be dropped and recreated cleanly (they belong to this app alone)."""
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

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
