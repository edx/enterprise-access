"""Models for the prompts app.

Owns versioned system prompt configuration for features that call the Xpert
`/v1/message` endpoint. The first consumer is the learner pathways
recommendation workflow.
"""
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from model_utils.models import TimeStampedModel
from simple_history.models import HistoricalRecords

PROMPT_TYPE_LEARNER_INTENT = 'learner_intent'
PROMPT_TYPE_RECOMMENDATIONS_FEEDBACK = 'recommendations_feedback'

PROMPT_TYPE_CHOICES = [
    (PROMPT_TYPE_LEARNER_INTENT, 'Learner Intent'),
    (PROMPT_TYPE_RECOMMENDATIONS_FEEDBACK, 'Recommendations Feedback'),
]
VALID_PROMPT_TYPES = {value for value, _label in PROMPT_TYPE_CHOICES}


class BaseSystemPrompt(TimeStampedModel):
    """
    Abstract base model for versioned Xpert system prompt configuration.

    .. no_pii:
    """
    uuid = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    notes = models.TextField(
        null=True,
        blank=True,
        help_text='Free-form internal notes about this prompt configuration.',
    )
    system_prompt = models.TextField(
        help_text='Raw system prompt text sent to Xpert.',
    )
    output_schema = models.JSONField(
        null=True,
        blank=True,
        help_text='Optional output schema appended to the system prompt at runtime.',
    )
    history = HistoricalRecords(inherit=True)

    class Meta:
        abstract = True
        app_label = 'prompts'

    def clean(self):
        super().clean()
        if not self.system_prompt or not self.system_prompt.strip():
            raise ValidationError(
                {'system_prompt': 'system_prompt is required and cannot be blank.'}
            )
        if self.output_schema is not None and not isinstance(self.output_schema, dict):
            raise ValidationError(
                {'output_schema': 'output_schema must be a JSON object (dict) when provided.'}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class XpertLearnerPathwaysSystemPrompt(BaseSystemPrompt):
    """
    Stores system prompt configuration for the Xpert learner pathways workflow.

    Concrete rows represent prompt configurations used by learner pathways
    Xpert calls, such as learner intent extraction and recommendations feedback.

    .. no_pii:
    """
    prompt_type = models.CharField(max_length=64, choices=PROMPT_TYPE_CHOICES)

    class Meta:
        app_label = 'prompts'
        verbose_name = 'Xpert Learner Pathways System Prompt'
        verbose_name_plural = 'Xpert Learner Pathways System Prompts'
        constraints = [
            models.UniqueConstraint(
                fields=['prompt_type'],
                name='unique_xpert_learner_pathways_prompt_type',
            ),
        ]

    def __str__(self):
        return f'XpertLearnerPathwaysSystemPrompt(type={self.prompt_type})'

    def clean(self):
        super().clean()
        if self.prompt_type not in VALID_PROMPT_TYPES:
            raise ValidationError(
                {'prompt_type': f'{self.prompt_type!r} is not a valid prompt_type.'}
            )

    @classmethod
    def get_active(cls, prompt_type):
        """Return the prompt for ``prompt_type``, or ``None`` if none exists."""
        return cls.objects.filter(prompt_type=prompt_type).first()
