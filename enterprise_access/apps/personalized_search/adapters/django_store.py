"""
``SessionStore`` (and the history and feedback reads) over the app's own tables (``models.py``).

The turn view works with a plain dict (``load_or_create``), locks the session row for the turn
(``lock``/``unlock``), and the orchestrator writes a finished turn in one transaction
(``persist_turn``). Learners only ever see their own sessions in the current enterprise.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.errors import SessionNotFound, TurnInProgress
from enterprise_access.apps.personalized_search.core.stream import stored_parts
from enterprise_access.apps.personalized_search.models import ChatSession, ChatTurn, ItemFeedback


def _history(session: ChatSession) -> list[dict]:
    turns = session.turns.order_by('-sequence')[:constants.HISTORY_TURNS]
    return [{'user': t.user_message, 'reply': t.reply_text} for t in reversed(turns)]


class DjangoSessionStore:
    """Sessions and turns in ``ChatSession``/``ChatTurn``; dismissed items from ``ItemFeedback``."""

    def not_interested_ids(self, lms_user_id, enterprise_customer_uuid) -> list[str]:
        """This learner's dismissed items in this enterprise; every turn excludes them (REQ-009)."""
        return list(ItemFeedback.objects.filter(
            lms_user_id=lms_user_id, enterprise_customer_uuid=enterprise_customer_uuid,
            kind=ItemFeedback.KIND_NOT_INTERESTED,
        ).values_list('item_id', flat=True))

    def load_or_create(self, *, session_uuid: str | None, lms_user_id, enterprise_customer_uuid: str,
                       message: str) -> dict:
        """
        The learner's session as a dict, or a new one (not yet saved) when no uuid is given. Another
        learner's or another enterprise's session is ``SessionNotFound``, the same as a missing one.
        """
        excluded = self.not_interested_ids(lms_user_id, enterprise_customer_uuid)
        if session_uuid:
            try:
                row = ChatSession.objects.get(uuid=session_uuid, lms_user_id=lms_user_id,
                                              enterprise_customer_uuid=enterprise_customer_uuid)
            except (ChatSession.DoesNotExist, ValueError, ValidationError) as exc:
                raise SessionNotFound() from exc
            return {
                'uuid': str(row.uuid), 'is_new': False, 'lms_user_id': row.lms_user_id,
                'enterprise_customer_uuid': str(row.enterprise_customer_uuid), 'title': row.title,
                'first_prompt': row.first_prompt, 'turn_count': row.turn_count, 'item_count': row.item_count,
                'merged_intent': row.merged_intent, 'history': _history(row),
                'shown_item_ids': list(row.shown_item_ids or []),
                'asked_attributes': list(row.asked_attributes or []),
                'not_interested_ids': excluded,
            }
        return {
            'uuid': str(ChatSession._meta.pk.default()), 'is_new': True, 'lms_user_id': lms_user_id,
            'enterprise_customer_uuid': str(enterprise_customer_uuid), 'title': None,
            'first_prompt': message[:constants.MAX_MESSAGE_CHARS], 'turn_count': 0, 'item_count': 0,
            'merged_intent': None, 'history': [], 'shown_item_ids': [], 'asked_attributes': [],
            'not_interested_ids': excluded,
        }

    def lock(self, session: dict) -> None:
        """
        Take the session's turn lock (spec: 409 ``turn_in_progress`` while a turn under 20 s old holds it).
        A new session's row is created here, already locked, so it exists before the stream starts.
        """
        now = timezone.now()
        if session['is_new']:
            ChatSession.objects.create(
                uuid=session['uuid'], lms_user_id=session['lms_user_id'],
                enterprise_customer_uuid=session['enterprise_customer_uuid'],
                first_prompt=session['first_prompt'], active_turn_started_at=now,
            )
            session['is_new'] = False
            return
        stale = now - timedelta(seconds=constants.SESSION_LOCK_S)
        unlocked = Q(active_turn_started_at__isnull=True) | Q(active_turn_started_at__lt=stale)
        taken = ChatSession.objects.filter(unlocked, uuid=session['uuid']).update(active_turn_started_at=now)
        if not taken:
            raise TurnInProgress()

    def unlock(self, session_uuid: str) -> None:
        """Release the session's turn lock (every exit path; spec step 5)."""
        ChatSession.objects.filter(uuid=session_uuid).update(active_turn_started_at=None)

    def persist_turn(self, session: dict, turn: dict) -> None:
        """Write a finished turn and the session's new state in one transaction (spec step 5)."""
        with transaction.atomic():
            ChatSession.objects.filter(uuid=session['uuid']).update(
                title=session['title'], turn_count=session['turn_count'], merged_intent=session['merged_intent'],
                shown_item_ids=session['shown_item_ids'], asked_attributes=session['asked_attributes'],
                item_count=session.get('item_count', 0) + len(turn['item_ids']), last_message_at=timezone.now(),
                active_turn_started_at=None,
            )
            ChatTurn.objects.create(
                uuid=turn['uuid'], session_id=session['uuid'], sequence=session['turn_count'],
                user_message=turn['user_message'][:constants.MAX_MESSAGE_CHARS], reply_text=turn['reply_text'],
                intent=turn.get('intent'), parts=stored_parts(turn['parts']), item_ids=turn['item_ids'],
                follow_up_attribute=turn.get('follow_up_attribute') or '', backend=turn.get('backend') or '',
                timing=turn.get('timing'),
            )
