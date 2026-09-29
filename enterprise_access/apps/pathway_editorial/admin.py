"""
Django admin for pathway editorial rules.

Rules are switched off with ``is_active`` rather than deleted, so deletion is disabled:
the row and its history are the record of why a pathway once looked the way it did.
"""
from django.contrib import admin
from djangoql.admin import DjangoQLSearchMixin
from simple_history.admin import SimpleHistoryAdmin

from enterprise_access.apps.pathway_editorial.models import PathwayCourseRule, PathwayPromotedTopic


class NoDeleteEditorialAdmin(DjangoQLSearchMixin, SimpleHistoryAdmin):
    """Shared behaviour: searchable, history-tracked, never deleted."""

    readonly_fields = ('created', 'modified')
    ordering = ('-modified',)

    def has_delete_permission(self, request, obj=None):
        """
        Prevent deletion of editorial rules.

        Untick ``is_active`` instead. A deleted rule loses the reason it existed, and an
        experiment that froze the policy could no longer be traced back to its rows.
        """
        return False


@admin.register(PathwayCourseRule)
class PathwayCourseRuleAdmin(NoDeleteEditorialAdmin):
    """
    Admin for per-course editorial rules: regional exclusions and flagship courses.
    """

    list_display = (
        'course_key',
        'action',
        'level',
        'is_active',
        'modified',
    )
    list_filter = (
        'action',
        'level',
        'is_active',
    )
    # JSONField is left out: the default __icontains lookup does not apply to it.
    # DjangoQL search covers scope_skills.
    search_fields = (
        'course_key',
        'reason',
    )
    fields = (
        'course_key',
        'action',
        'level',
        'scope_skills',
        'reason',
        'is_active',
        'created',
        'modified',
    )


@admin.register(PathwayPromotedTopic)
class PathwayPromotedTopicAdmin(NoDeleteEditorialAdmin):
    """
    Admin for promoted topics, each capped at a few seats per pathway.
    """

    list_display = (
        'name',
        'max_per_pathway',
        'gate_top_k',
        'is_active',
        'modified',
    )
    list_filter = (
        'is_active',
    )
    search_fields = (
        'name',
        'reason',
    )
    fields = (
        'name',
        'subjects',
        'title_terms',
        'skill_names',
        'max_per_pathway',
        'gate_top_k',
        'reason',
        'is_active',
        'created',
        'modified',
    )
