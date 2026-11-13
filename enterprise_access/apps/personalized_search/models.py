"""
Personalized Search (Chat) tables: sessions, their turns, and learners' item feedback.

**Droppable by design.** All three tables belong to this app alone: nothing outside
``apps/personalized_search`` has a foreign key to them or imports these models, and they come from
one initial migration. Tearing the feature down is therefore:

    ./manage.py migrate personalized_search zero    # drops the three tables
    # then remove 'enterprise_access.apps.personalized_search' from PROJECT_APPS

``test_models.py`` checks the zero migration runs clean. See ``docs/references/personalized_search.md``.
"""
from uuid import uuid4

from django.db import models
from django.utils import timezone
from model_utils.models import SoftDeletableModel, TimeStampedModel


class ChatSession(TimeStampedModel, SoftDeletableModel):
    """
    One learner's conversation in one enterprise (tech spec "Data Model").

    A session is soft-deleted by default (``SoftDeletableModel``): ``delete()`` only sets ``is_removed``, so the
    cascade to ``ChatTurn`` and the ``SET_NULL`` on ``ItemFeedback`` do not run. Any purge must use
    ``delete(soft=False)`` or ``ChatSession.all_objects``.

    .. pii: first_prompt and title hold learner-written text; lms_user_id links the row to the learner;
       merged_intent and asked_attributes are derived from what the learner wrote. Retirement is deferred
       with retention (tech spec, out of scope): this is a stage-only proof of concept, and the feature
       flags must stay off outside stage until a retirement path (a hard delete by lms_user_id) ships.
    .. pii_types: id, other
    .. pii_retirement: retained
    """

    uuid = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    # Server-only; never serialized. The dev backend gives Xpert a fresh id per call instead.
    upstream_conversation_id = models.UUIDField(default=uuid4, editable=False)
    lms_user_id = models.IntegerField(db_index=True)
    enterprise_customer_uuid = models.UUIDField()
    turn_count = models.IntegerField(default=0)
    item_count = models.IntegerField(default=0)
    active_turn_started_at = models.DateTimeField(null=True, blank=True)
    title = models.CharField(max_length=60, null=True, blank=True)
    first_prompt = models.CharField(max_length=500)
    last_message_at = models.DateTimeField(default=timezone.now)
    shown_item_ids = models.JSONField(default=list, blank=True)
    asked_attributes = models.JSONField(default=list, blank=True)
    merged_intent = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['lms_user_id', 'enterprise_customer_uuid', '-last_message_at'],
                         name='ps_session_learner_recent'),
        ]

    def __str__(self):
        return f'ChatSession {self.uuid} (learner {self.lms_user_id}, {self.turn_count} turns)'


class ChatTurn(TimeStampedModel):
    """
    One finished turn, with the stream's parts kept for replay (history reopen).

    .. pii: user_message, reply_text, intent and parts hold learner-written text, the reply, and data
       derived from them. Kept for the same reason as ChatSession: retirement is deferred, so the feature
       flags stay off outside stage until a retirement path ships.
    .. pii_types: other
    .. pii_retirement: retained
    """

    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='turns')
    sequence = models.IntegerField()
    user_message = models.CharField(max_length=500)
    reply_text = models.TextField(blank=True)
    intent = models.JSONField(null=True, blank=True)
    parts = models.JSONField(default=list)
    item_ids = models.JSONField(default=list, blank=True)
    follow_up_attribute = models.CharField(max_length=32, blank=True)
    backend = models.CharField(max_length=16, blank=True)
    timing = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['session', 'sequence'], name='ps_turn_session_sequence')]
        ordering = ['sequence']

    def __str__(self):
        return f'ChatTurn {self.sequence} of {self.session_id}'


class ItemFeedback(TimeStampedModel):
    """
    A learner's saved or not-interested item, scoped to the learner and the enterprise.

    .. pii: lms_user_id links the row to the learner; card_snapshot is catalog metadata only. Kept for the
       same reason as ChatSession: retirement is deferred, so the feature flags stay off outside stage.
    .. pii_types: id
    .. pii_retirement: retained
    """

    KIND_SAVED = 'saved'
    KIND_NOT_INTERESTED = 'not_interested'
    KIND_CHOICES = ((KIND_SAVED, 'Saved'), (KIND_NOT_INTERESTED, 'Not interested'))

    lms_user_id = models.IntegerField(db_index=True)
    enterprise_customer_uuid = models.UUIDField()
    item_id = models.CharField(max_length=255)
    item_type = models.CharField(max_length=32)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    session = models.ForeignKey(ChatSession, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='feedback')
    card_snapshot = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['lms_user_id', 'enterprise_customer_uuid', 'item_id', 'kind'],
                                               name='ps_feedback_unique_per_learner')]

    def __str__(self):
        return f'ItemFeedback {self.kind} {self.item_id} (learner {self.lms_user_id})'
