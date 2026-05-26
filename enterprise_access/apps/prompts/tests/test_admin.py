"""Smoke tests for prompts admin. Full coverage tracked in ENT-11857."""
from django.test import TestCase

from enterprise_access.apps.prompts import admin


class AdminSmokeTest(TestCase):
    def test_module_imports(self):
        self.assertIsNotNone(admin)
