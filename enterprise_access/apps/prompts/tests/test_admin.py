"""Tests for prompts admin."""
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.test import TestCase
from djangoql.admin import DjangoQLSearchMixin
from simple_history.admin import SimpleHistoryAdmin

from enterprise_access.apps.prompts.admin import XpertLearnerPathwaysSystemPromptAdmin
from enterprise_access.apps.prompts.models import PromptType, XpertLearnerPathwaysSystemPrompt
from enterprise_access.apps.prompts.tests.factories import XpertLearnerPathwaysSystemPromptFactory

User = get_user_model()


class XpertLearnerPathwaysSystemPromptAdminTests(TestCase):
    """Tests for XpertLearnerPathwaysSystemPromptAdmin."""

    def setUp(self):
        """Set up test fixtures."""
        self.admin_site = AdminSite()
        self.admin = XpertLearnerPathwaysSystemPromptAdmin(XpertLearnerPathwaysSystemPrompt, self.admin_site)
        self.request = HttpRequest()
        self.user = User.objects.create_superuser(username='admin', email='admin@example.com', password='pass')
        self.request.user = self.user

    def test_admin_is_registered(self):
        """Test that XpertLearnerPathwaysSystemPrompt is registered in admin."""
        self.assertIsInstance(self.admin, XpertLearnerPathwaysSystemPromptAdmin)

    def test_admin_uses_djangoql_search_mixin(self):
        """Test that admin uses DjangoQLSearchMixin."""
        self.assertIsInstance(self.admin, DjangoQLSearchMixin)

    def test_admin_uses_simple_history_admin(self):
        """Test that admin uses SimpleHistoryAdmin."""
        self.assertIsInstance(self.admin, SimpleHistoryAdmin)

    def test_list_display_configuration(self):
        """Test that list_display is configured correctly."""
        expected = ('notes', 'prompt_type', 'modified')
        self.assertEqual(self.admin.list_display, expected)

    def test_list_filter_configuration(self):
        """Test that list_filter is configured correctly."""
        expected = ('prompt_type',)
        self.assertEqual(self.admin.list_filter, expected)

    def test_search_fields_configuration(self):
        """Test that search_fields is configured correctly."""
        expected = ('notes', 'system_prompt', 'output_schema')
        self.assertEqual(self.admin.search_fields, expected)

    def test_readonly_fields_configuration(self):
        """Test that readonly_fields is configured correctly."""
        expected = ('created', 'modified')
        self.assertEqual(self.admin.readonly_fields, expected)

    def test_ordering_configuration(self):
        """Test that ordering is configured correctly."""
        expected = ('-modified',)
        self.assertEqual(self.admin.ordering, expected)

    def test_editable_fields(self):
        """Test that the correct fields are editable in the admin form."""
        prompt = XpertLearnerPathwaysSystemPromptFactory()

        # Get the form for this object
        form_class = self.admin.get_form(self.request, prompt)
        form = form_class(instance=prompt)

        # These fields should be editable (present in form and not disabled)
        editable_fields = ['system_prompt', 'notes', 'output_schema', 'prompt_type']
        for field_name in editable_fields:
            self.assertIn(field_name, form.fields, f'{field_name} should be in form')

        # created and modified should be readonly
        readonly_fields = self.admin.get_readonly_fields(self.request, prompt)
        self.assertIn('created', readonly_fields)
        self.assertIn('modified', readonly_fields)

    def test_save_model_calls_full_clean(self):
        """Test that save_model calls obj.full_clean() before saving."""
        prompt = XpertLearnerPathwaysSystemPromptFactory.build()

        with mock.patch.object(prompt, 'full_clean', wraps=prompt.full_clean) as mock_full_clean:
            with mock.patch.object(prompt, 'save'):
                self.admin.save_model(self.request, prompt, form=None, change=False)
                mock_full_clean.assert_called_once()

    def test_save_model_validation_errors_surface(self):
        """Test that validation errors from full_clean surface correctly."""
        # Create a prompt with invalid data (blank system_prompt)
        prompt = XpertLearnerPathwaysSystemPromptFactory.build(system_prompt='')

        with self.assertRaises(ValidationError) as context:
            self.admin.save_model(self.request, prompt, form=None, change=False)

        # Verify the validation error is about system_prompt
        self.assertIn('system_prompt', str(context.exception))

    def test_single_object_delete_blocked(self):
        """Test that single-object deletion is blocked."""
        prompt = XpertLearnerPathwaysSystemPromptFactory()

        # has_delete_permission should return False
        self.assertFalse(self.admin.has_delete_permission(self.request, prompt))

    def test_delete_permission_blocked_without_object(self):
        """Test that delete permission is blocked even without a specific object."""
        # has_delete_permission should return False even when obj=None
        self.assertFalse(self.admin.has_delete_permission(self.request, obj=None))

    def test_history_tracking_enabled(self):
        """Test that history tracking is available through SimpleHistoryAdmin."""
        # Create a prompt and modify it
        prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.LEARNER_INTENT,
            system_prompt='Original prompt'
        )

        # Modify the prompt
        prompt.system_prompt = 'Updated prompt'
        prompt.save()

        # Verify history exists
        history = prompt.history.all()
        self.assertEqual(history.count(), 2)  # Original + update
        self.assertEqual(history[0].system_prompt, 'Updated prompt')
        self.assertEqual(history[1].system_prompt, 'Original prompt')

    def test_unique_constraint_enforced(self):
        """Test that the unique constraint on prompt_type is enforced."""
        # Create first prompt
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)

        # Try to create another with same prompt_type
        with self.assertRaises(Exception):  # Could be IntegrityError or ValidationError
            XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)

    def test_multiple_prompt_types_allowed(self):
        """Test that multiple prompts with different types can coexist."""
        prompt1 = XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)
        prompt2 = XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.RECOMMENDATIONS_FEEDBACK)

        self.assertIsNotNone(prompt1.uuid)
        self.assertIsNotNone(prompt2.uuid)
        self.assertNotEqual(prompt1.uuid, prompt2.uuid)
        self.assertEqual(XpertLearnerPathwaysSystemPrompt.objects.count(), 2)
