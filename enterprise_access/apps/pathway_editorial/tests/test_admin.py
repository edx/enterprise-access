"""Tests for the pathway editorial admin."""
import ddt
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.http import HttpRequest
from django.test import TestCase
from djangoql.admin import DjangoQLSearchMixin
from simple_history.admin import SimpleHistoryAdmin

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_editorial.admin import PathwayCourseRuleAdmin, PathwayPromotedTopicAdmin
from enterprise_access.apps.pathway_editorial.models import PathwayCourseRule, PathwayPromotedTopic


@ddt.ddt
class EditorialAdminTests(TestCase):
    """Both editorial models are registered, searchable, history-tracked and undeletable."""

    def setUp(self):
        super().setUp()
        self.request = HttpRequest()
        self.request.user = UserFactory(is_staff=True, is_superuser=True)

    @ddt.data(
        (PathwayCourseRule, PathwayCourseRuleAdmin),
        (PathwayPromotedTopic, PathwayPromotedTopicAdmin),
    )
    @ddt.unpack
    def test_registered_with_search_history_and_no_delete(self, model, admin_class):
        # pylint: disable=protected-access
        self.assertIsInstance(admin.site._registry[model], admin_class)
        model_admin = admin_class(model, AdminSite())
        self.assertIsInstance(model_admin, DjangoQLSearchMixin)
        self.assertIsInstance(model_admin, SimpleHistoryAdmin)
        self.assertFalse(model_admin.has_delete_permission(self.request))
        self.assertFalse(model_admin.has_delete_permission(self.request, model.objects.first()))
        self.assertNotIn('delete_selected', model_admin.get_actions(self.request))
        self.assertEqual(model_admin.get_readonly_fields(self.request), ('created', 'modified'))

    def test_course_rule_list_configuration(self):
        model_admin = PathwayCourseRuleAdmin(PathwayCourseRule, AdminSite())
        self.assertEqual(model_admin.list_display, ('course_key', 'action', 'level', 'is_active', 'modified'))
        self.assertEqual(model_admin.list_filter, ('action', 'level', 'is_active'))
        self.assertEqual(model_admin.search_fields, ('course_key', 'reason'))

    def test_promoted_topic_list_configuration(self):
        model_admin = PathwayPromotedTopicAdmin(PathwayPromotedTopic, AdminSite())
        self.assertEqual(model_admin.list_display, ('name', 'max_per_pathway', 'gate_top_k', 'is_active', 'modified'))
        self.assertEqual(model_admin.list_filter, ('is_active',))

    @ddt.data(
        (PathwayCourseRule, PathwayCourseRuleAdmin, ['course_key', 'action', 'level', 'scope_skills', 'reason',
                                                     'is_active']),
        (PathwayPromotedTopic, PathwayPromotedTopicAdmin, ['name', 'subjects', 'title_terms', 'skill_names',
                                                           'max_per_pathway', 'gate_top_k', 'reason', 'is_active']),
    )
    @ddt.unpack
    def test_form_edits_every_rule_field(self, model, admin_class, fields):
        form_class = admin_class(model, AdminSite()).get_form(self.request, None)
        self.assertEqual(list(form_class.base_fields), fields)
