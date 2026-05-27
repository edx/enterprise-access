"""Django admin for prompts."""
import json

from django import forms
from django.contrib import admin
from djangoql.admin import DjangoQLSearchMixin
from simple_history.admin import SimpleHistoryAdmin

from enterprise_access.apps.prompts.models import XpertLearnerPathwaysSystemPrompt


class PrettyJSONWidget(forms.Textarea):
    """Custom widget that formats JSON with proper indentation."""

    def format_value(self, value):
        """Format the JSON value with indentation for display."""
        if value is None or value == '':
            return value
        try:
            # If value is already a dict/list, format it
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2, ensure_ascii=False)
            # If value is a string, try to parse and reformat it
            if isinstance(value, str):
                obj = json.loads(value)
                return json.dumps(obj, indent=2, ensure_ascii=False)
            return value
        except (json.JSONDecodeError, TypeError):
            return value


class XpertLearnerPathwaysSystemPromptForm(forms.ModelForm):
    """Custom form with pretty JSON formatting."""

    class Meta:
        model = XpertLearnerPathwaysSystemPrompt
        fields = '__all__'
        widgets = {
            'output_schema': PrettyJSONWidget(attrs={
                'rows': 20,
                'cols': 80,
                'style': 'font-family: monospace;'
            }),
        }


@admin.register(XpertLearnerPathwaysSystemPrompt)
class XpertLearnerPathwaysSystemPromptAdmin(DjangoQLSearchMixin, SimpleHistoryAdmin):
    """
    Admin configuration for XpertLearnerPathwaysSystemPrompt.

    This admin manages system prompts for the Xpert learner pathways workflow.
    Each prompt_type has exactly one configured prompt (enforced by unique constraint).
    """

    form = XpertLearnerPathwaysSystemPromptForm

    list_display = (
        'prompt_type',
        'notes',
        'modified',
        'created',
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

    def has_delete_permission(self, request, obj=None):
        """
        Prevent deletion of prompt configurations.

        Since each prompt_type has exactly one configured prompt, deletion would
        remove critical system configuration. Use the admin interface to edit
        prompts instead.
        """
        return False
