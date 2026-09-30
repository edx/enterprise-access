"""Tests for the ``export_editorial_policy`` management command."""
import json
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from enterprise_access.apps.pathway_editorial.api import EditorialPolicy, load_policy
from enterprise_access.apps.pathway_editorial.models import PathwayCourseRule


class ExportEditorialPolicyTests(TestCase):
    """The command freezes ``load_policy()`` to JSON."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def export(self, path):
        stdout = StringIO()
        call_command('export_editorial_policy', output=str(path), stdout=stdout)
        return stdout.getvalue()

    def test_writes_the_active_policy(self):
        PathwayCourseRule.objects.create(course_key='Old+1', action='exclude', reason='r', is_active=False)
        path = self.tmp / 'nested' / 'policy.json'

        output = self.export(path)

        data = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(data, load_policy().to_dict())
        self.assertNotIn('Old+1', data['excluded_keys'])
        self.assertIn('AI', data['promoted'][0]['title_terms'])
        self.assertEqual(EditorialPolicy.from_dict(data), load_policy())
        self.assertIn('1 exclusion(s), 1 flagship(s), 1 promoted topic(s)', output)

    def test_output_is_byte_stable(self):
        first, second = self.tmp / 'a.json', self.tmp / 'b.json'
        self.export(first)
        self.export(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_output_is_required(self):
        with self.assertRaises(CommandError):
            call_command('export_editorial_policy')

    def test_unwritable_path_is_a_command_error(self):
        blocker = self.tmp / 'file'
        blocker.write_text('x')
        with self.assertRaises(CommandError):
            self.export(blocker / 'policy.json')
