"""
``SessionStore`` (and the history and feedback reads) over the app's own tables (``models.py``).

The turn view works with a plain dict (``load_or_create``), locks the session row for the turn
(``lock``/``unlock``), and the orchestrator writes a finished turn in one transaction
(``persist_turn``). Learners only ever see their own sessions in the current enterprise.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, Paginator
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


class DjangoHistoryStore:
    """The learner's past sessions and their stored turns (history panel, REQ-010)."""

    page_size = 20

    def list_sessions(self, *, lms_user_id, enterprise_customer_uuid, page: int) -> dict | None:
        """One page of sessions with at least one turn, newest first; None past the last page."""
        sessions = ChatSession.objects.filter(
            lms_user_id=lms_user_id, enterprise_customer_uuid=enterprise_customer_uuid, turn_count__gt=0,
        ).order_by('-last_message_at')
        paginator = Paginator(sessions, self.page_size)
        try:
            current = paginator.page(page)
        except EmptyPage:
            return None
        return {
            'count': paginator.count,
            'next_page': current.next_page_number() if current.has_next() else None,
            'previous_page': current.previous_page_number() if current.has_previous() else None,
            'results': [{'uuid': str(s.uuid), 'title': s.title, 'first_prompt': s.first_prompt,
                         'item_count': s.item_count, 'last_message_at': s.last_message_at, 'created': s.created}
                        for s in current.object_list],
        }

    def get_session(self, *, session_uuid, lms_user_id, enterprise_customer_uuid) -> dict:
        """One session with its turns' stored parts, in sequence order. Raises ``SessionNotFound``."""
        try:
            session = ChatSession.objects.get(uuid=session_uuid, lms_user_id=lms_user_id,
                                              enterprise_customer_uuid=enterprise_customer_uuid)
        except ChatSession.DoesNotExist as exc:
            raise SessionNotFound() from exc
        return {
            'uuid': str(session.uuid), 'title': session.title, 'created': session.created,
            'turns': [{'sequence': t.sequence, 'user_message': t.user_message, 'parts': t.parts,
                       'created': t.created} for t in session.turns.order_by('sequence')],
        }


class DjangoFeedbackStore:
    """A learner's saved and not-interested items (REQ-009)."""

    kinds = (ItemFeedback.KIND_SAVED, ItemFeedback.KIND_NOT_INTERESTED)

    @staticmethod
    def _row(row: ItemFeedback) -> dict:
        return {'item_id': row.item_id, 'item_type': row.item_type, 'kind': row.kind,
                'card_snapshot': row.card_snapshot, 'created': row.created}

    def list(self, *, lms_user_id, enterprise_customer_uuid, kind: str) -> list[dict]:
        """The learner's feedback of one kind in this enterprise, newest first."""
        rows = ItemFeedback.objects.filter(
            lms_user_id=lms_user_id, enterprise_customer_uuid=enterprise_customer_uuid, kind=kind,
        ).order_by('-created')
        return [self._row(r) for r in rows]

    def add(self, *, lms_user_id, enterprise_customer_uuid, item_id: str, item_type: str, kind: str,
            session_uuid=None, card_snapshot=None) -> tuple[dict, bool]:
        """Record feedback; an existing row for the same item and kind is returned as is. Returns (row, created)."""
        session = None
        if session_uuid:
            session = ChatSession.objects.filter(
                uuid=session_uuid, lms_user_id=lms_user_id, enterprise_customer_uuid=enterprise_customer_uuid,
            ).first()
        row, created = ItemFeedback.objects.get_or_create(
            lms_user_id=lms_user_id, enterprise_customer_uuid=enterprise_customer_uuid, item_id=item_id, kind=kind,
            defaults={'item_type': item_type, 'session': session, 'card_snapshot': card_snapshot},
        )
        return self._row(row), created

    def remove(self, *, lms_user_id, enterprise_customer_uuid, item_id: str, kind: str) -> None:
        """Undo: delete the row for this item and kind, if any."""
        ItemFeedback.objects.filter(
            lms_user_id=lms_user_id, enterprise_customer_uuid=enterprise_customer_uuid, item_id=item_id, kind=kind,
        ).delete()
