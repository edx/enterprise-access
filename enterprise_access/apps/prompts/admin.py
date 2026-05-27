"""Django admin for prompts. Full admin implementation tracked in ENT-11857."""
from django.contrib import admin
from djangoql.admin import DjangoQLSearchMixin
from simple_history.admin import SimpleHistoryAdmin

from enterprise_access.apps.prompts.models import XpertLearnerPathwaysSystemPrompt


@admin.register(XpertLearnerPathwaysSystemPrompt)
class XpertLearnerPathwaysSystemPromptAdmin(DjangoQLSearchMixin, SimpleHistoryAdmin):
    """
    Admin configuration for XpertLearnerPathwaysSystemPrompt.

    This admin manages system prompts for the Xpert learner pathways workflow.
    Each prompt_type has exactly one configured prompt (enforced by unique constraint).
    """

    list_display = (
        'notes',
        'prompt_type',
        'modified',
    )

    list_filter = (
        'prompt_type',
    )

    search_fields = (
        'notes',
        'system_prompt',
        'output_schema',
    )

    readonly_fields = (
        'created',
        'modified',
    )

    ordering = ('-modified',)

    def save_model(self, request, obj, form, change):
        """
        Override to call full_clean() before saving to ensure model validation.
        Validation errors are automatically handled by Django's admin form.
        """
        obj.full_clean()
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        """
        Prevent deletion of prompt configurations.

        Since each prompt_type has exactly one configured prompt, deletion would
        remove critical system configuration. Use the admin interface to edit
        prompts instead.
        """
        return False
