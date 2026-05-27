"""Models for the prompts app.

Owns system prompt configuration for features that call the Xpert
`/v1/message` endpoint. Each concrete model holds exactly one row per
``prompt_type``; full edit history is preserved by django-simple-history.
The first consumer is the learner pathways recommendation workflow.
"""
import uuid
from typing import Self

from django.core.exceptions import ValidationError
from django.db import models
from model_utils.models import TimeStampedModel
from simple_history.models import HistoricalRecords


class PromptType(models.TextChoices):
    """Valid ``prompt_type`` values for ``XpertLearnerPathwaysSystemPrompt``."""
    LEARNER_INTENT = 'learner_intent', 'Learner Intent'
    RECOMMENDATIONS_FEEDBACK = 'recommendations_feedback', 'Recommendations Feedback'


class BaseSystemPrompt(TimeStampedModel):
    """
    Abstract base model for Xpert system prompt configuration.

    Subclasses persist one canonical row per discriminator (e.g. ``prompt_type``).
    Edits overwrite the row in place; every change is captured as a historical
    revision by django-simple-history, so prior wording is always recoverable.

    Important privacy and retention rules:
     - This model must not be used to persist any personally identifiable information (PII).
       Do not add fields that store user PII.
     - This model is intended only to hold non-PII system prompt configuration used to drive Xpert.
       It must not be used to store end-user messages, user inputs, or other user-provided content.
     - If you need to persist user-provided data in the future, you MUST integrate that model with
       the project's user-retirement / data-deletion pipeline so user data can be removed on request.
       Adding user-linked fields without that integration is not allowed.
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

    def clean(self) -> None:
        super().clean()
        if not self.system_prompt or not self.system_prompt.strip():
            raise ValidationError(
                {'system_prompt': 'system_prompt is required and cannot be blank.'}
            )
        if self.output_schema is not None and not isinstance(self.output_schema, dict):
            raise ValidationError(
                {'output_schema': 'output_schema must be a JSON object (dict) when provided.'}
            )

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)


class XpertLearnerPathwaysSystemPrompt(BaseSystemPrompt):
    """
    System prompt configuration for the Xpert learner pathways workflow.

    At most one row exists per ``prompt_type`` (enforced by a unique constraint).
    Admin edits overwrite that row; history rows preserve every prior revision.

    .. no_pii:
    """
    prompt_type = models.CharField(max_length=64, choices=PromptType.choices)

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

    def __str__(self) -> str:
        return f'XpertLearnerPathwaysSystemPrompt(type={self.prompt_type})'

    def clean(self) -> None:
        super().clean()
        if self.prompt_type not in PromptType.values:
            raise ValidationError(
                {'prompt_type': f'{self.prompt_type!r} is not a valid prompt_type.'}
            )

    @classmethod
    def get_current(cls, prompt_type: str) -> Self | None:
        """Return the configured prompt row for ``prompt_type``, or ``None``."""
        return cls.objects.filter(prompt_type=prompt_type).first()
