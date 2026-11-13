"""
``SessionStore`` in memory: same contract as the Django store, no database.

For the harness, tests and any host that keeps sessions itself. Not shared between processes.
"""
import copy
import time
import uuid

from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.errors import SessionNotFound, TurnInProgress
from enterprise_access.apps.personalized_search.core.stream import stored_parts


class InMemorySessionStore:
    """Sessions, turns and dismissed items in dicts. ``clock`` is injectable for lock-expiry tests."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.sessions: dict[str, dict] = {}
        self.turns: dict[str, list[dict]] = {}
        self.dismissed: dict[tuple, list[str]] = {}

    def not_interested_ids(self, lms_user_id, enterprise_customer_uuid) -> list[str]:
        """Items this learner dismissed in this enterprise."""
        return list(self.dismissed.get((lms_user_id, str(enterprise_customer_uuid)), []))

    def load_or_create(self, *, session_uuid: str | None, lms_user_id, enterprise_customer_uuid: str,
                       message: str) -> dict:
        """The learner's session, or a new unsaved one; other learners' and enterprises' are not found."""
        excluded = self.not_interested_ids(lms_user_id, enterprise_customer_uuid)
        if session_uuid:
            row = self.sessions.get(str(session_uuid))
            if not row or row['lms_user_id'] != lms_user_id or \
                    row['enterprise_customer_uuid'] != str(enterprise_customer_uuid):
                raise SessionNotFound()
            turns = self.turns.get(row['uuid'], [])[-constants.HISTORY_TURNS:]
            return dict(copy.deepcopy({k: v for k, v in row.items() if k != 'locked_at'}), is_new=False,
                        history=[{'user': t['user_message'], 'reply': t['reply_text']} for t in turns],
                        not_interested_ids=excluded)
        return {
            'uuid': str(uuid.uuid4()), 'is_new': True, 'lms_user_id': lms_user_id,
            'enterprise_customer_uuid': str(enterprise_customer_uuid), 'title': None,
            'first_prompt': message[:constants.MAX_MESSAGE_CHARS], 'turn_count': 0, 'item_count': 0,
            'merged_intent': None, 'history': [], 'shown_item_ids': [], 'asked_attributes': [],
            'not_interested_ids': excluded,
        }

    def lock(self, session: dict) -> None:
        """
        Take the turn lock (saving a new session); a lock younger than SESSION_LOCK_S blocks. As in the
        Django store, ``session['lock_token']`` is what lets only this turn release it.
        """
        now = self._clock()
        if session['is_new']:
            self.sessions[session['uuid']] = {
                'uuid': session['uuid'], 'lms_user_id': session['lms_user_id'],
                'enterprise_customer_uuid': session['enterprise_customer_uuid'], 'title': None,
                'first_prompt': session['first_prompt'], 'turn_count': 0, 'item_count': 0, 'merged_intent': None,
                'shown_item_ids': [], 'asked_attributes': [], 'locked_at': now,
            }
            session.update(is_new=False, lock_token=now)
            return
        row = self.sessions[session['uuid']]
        if row['locked_at'] is not None and now - row['locked_at'] < constants.SESSION_LOCK_S:
            raise TurnInProgress()
        if row['turn_count'] != session['turn_count']:  # another turn finished since this one loaded the session
            raise TurnInProgress()
        row['locked_at'] = session['lock_token'] = now

    def _holds_lock(self, session: dict) -> bool:
        row = self.sessions.get(session['uuid'])
        return row is not None and row['locked_at'] == session.get('lock_token')

    def unlock(self, session: dict) -> None:
        """Release the turn lock if this turn still holds it."""
        if self._holds_lock(session):
            self.sessions[session['uuid']]['locked_at'] = None

    def persist_turn(self, session: dict, turn: dict) -> None:
        """
        Save the session's new state and the turn with its stored parts, and release the lock. A turn that
        no longer holds the lock writes nothing: ``TurnInProgress``.
        """
        if not self._holds_lock(session):
            raise TurnInProgress()
        row = self.sessions[session['uuid']]
        row.update(title=session['title'], turn_count=session['turn_count'],
                   merged_intent=copy.deepcopy(session['merged_intent']),
                   shown_item_ids=list(session['shown_item_ids']), asked_attributes=list(session['asked_attributes']),
                   item_count=session.get('item_count', 0) + len(turn['item_ids']), locked_at=None)
        self.turns.setdefault(session['uuid'], []).append(dict(
            turn, sequence=session['turn_count'], user_message=turn['user_message'][:constants.MAX_MESSAGE_CHARS],
            parts=stored_parts(turn['parts']),
        ))
