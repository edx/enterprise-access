"""Smoke tests for prompts API views. Full coverage tracked in ENT-11858."""
from django.test import TestCase

from enterprise_access.apps.prompts.api import views


class ViewsSmokeTest(TestCase):
    def test_module_imports(self):
        self.assertIsNotNone(views)
