"""Tests for the Personalized Search sessions and item-feedback endpoints (chunks 28, 30)."""
import uuid
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from edx_toggles.toggles.testutils import override_waffle_flag
from rest_framework.exceptions import PermissionDenied
from rest_framework.reverse import reverse

from enterprise_access.apps.api.v1.views.personalized_search_history import learner_id
from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.personalized_search.models import ChatSession, ChatTurn, ItemFeedback
from enterprise_access.toggles import PERSONALIZED_SEARCH_ENABLED
from test_utils import APITest

ENTERPRISE = str(uuid.uuid4())
OTHER_ENTERPRISE = str(uuid.uuid4())


@override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
class SessionEndpointsTests(APITest):
    """GET sessions/ and sessions/{uuid}/"""

    def setUp(self):
        super().setUp()
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])
        self.me = self.user.lms_user_id or self.user.id

    def session(self, lms_user_id=None, enterprise=ENTERPRISE, turns=1, title='Leading'):
        """A session with ``turns`` stored turns, owned by this learner unless told otherwise."""
        row = ChatSession.objects.create(lms_user_id=lms_user_id or self.me, enterprise_customer_uuid=enterprise,
                                         first_prompt='Lead a team', title=title, turn_count=turns)
        for sequence in range(1, turns + 1):
            ChatTurn.objects.create(session=row, sequence=sequence, user_message=f'm{sequence}',
                                    parts=[{'type': 'start', 'messageId': f'turn_{sequence}'}])
        return row

    def test_list_is_mine_only_with_turns_newest_first(self):
        older, newer = self.session(title='older'), self.session(title='newer')
        ChatSession.objects.filter(uuid=older.uuid).update(last_message_at='2026-01-01T00:00:00Z')
        self.session(turns=0)  # hidden: no turns
        self.session(lms_user_id=999)  # another learner
        self.session(enterprise=OTHER_ENTERPRISE)  # another enterprise
        body = self.client.get(reverse('api:v1:personalized-search-sessions'),
                               {'enterprise_customer_uuid': ENTERPRISE}).json()
        self.assertEqual(body['count'], 2)
        self.assertEqual([r['title'] for r in body['results']], ['newer', 'older'])
        self.assertEqual(set(body['results'][0]), {'uuid', 'title', 'first_prompt', 'item_count', 'last_message_at',
                                                   'created'})
        self.assertEqual(str(newer.uuid), body['results'][0]['uuid'])

    def test_list_pages_by_20(self):
        for _ in range(21):
            self.session()
        url = reverse('api:v1:personalized-search-sessions')
        first = self.client.get(url, {'enterprise_customer_uuid': ENTERPRISE}).json()
        self.assertEqual((len(first['results']), first['count']), (20, 21))
        self.assertIsNotNone(first['next'])
        second = self.client.get(url, {'enterprise_customer_uuid': ENTERPRISE, 'page': 2}).json()
        self.assertEqual(len(second['results']), 1)

    def test_a_page_past_the_last_is_404(self):
        self.session()
        response = self.client.get(reverse('api:v1:personalized-search-sessions'),
                                   {'enterprise_customer_uuid': ENTERPRISE, 'page': 5})
        self.assertEqual(response.status_code, 404)

    def test_retrieve_returns_parts_but_never_the_upstream_id(self):
        row = self.session(turns=2)
        response = self.client.get(reverse('api:v1:personalized-search-session', args=[row.uuid]),
                                   {'enterprise_customer_uuid': ENTERPRISE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t['sequence'] for t in response.json()['turns']], [1, 2])
        self.assertEqual(response.json()['turns'][0]['parts'], [{'type': 'start', 'messageId': 'turn_1'}])
        self.assertNotIn(str(row.upstream_conversation_id), response.content.decode())

    def test_retrieve_someone_elses_session_is_404(self):
        theirs = self.session(lms_user_id=999)
        url = reverse('api:v1:personalized-search-session', args=[theirs.uuid])
        self.assertEqual(self.client.get(url, {'enterprise_customer_uuid': ENTERPRISE}).status_code, 404)

    def test_other_enterprise_is_403(self):
        response = self.client.get(reverse('api:v1:personalized-search-sessions'),
                                   {'enterprise_customer_uuid': OTHER_ENTERPRISE})
        self.assertEqual(response.status_code, 403)

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=False)
    def test_switch_off_is_404(self):
        response = self.client.get(reverse('api:v1:personalized-search-sessions'),
                                   {'enterprise_customer_uuid': ENTERPRISE})
        self.assertEqual(response.status_code, 404)


@override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
class ItemFeedbackEndpointTests(APITest):
    """GET/POST/DELETE item-feedback/"""

    def setUp(self):
        super().setUp()
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])
        self.url = reverse('api:v1:personalized-search-item-feedback')
        self.body = {'enterprise_customer_uuid': ENTERPRISE, 'item_id': 'edX+C1', 'item_type': 'course',
                     'kind': 'saved', 'card_snapshot': {'title': 'Course C1'}}

    def test_post_is_idempotent_then_listed(self):
        self.assertEqual(self.client.post(self.url, self.body, format='json').status_code, 201)
        self.assertEqual(self.client.post(self.url, self.body, format='json').status_code, 200)
        self.assertEqual(ItemFeedback.objects.count(), 1)
        results = self.client.get(self.url, {'enterprise_customer_uuid': ENTERPRISE, 'kind': 'saved'}).json()['results']
        self.assertEqual([(r['item_id'], r['card_snapshot']['title']) for r in results], [('edX+C1', 'Course C1')])

    def test_delete_undoes(self):
        self.client.post(self.url, self.body, format='json')
        response = self.client.delete(f'{self.url}?enterprise_customer_uuid={ENTERPRISE}&item_id=edX%2BC1&kind=saved')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(ItemFeedback.objects.exists())

    def test_rows_are_scoped_to_the_learner(self):
        ItemFeedback.objects.create(lms_user_id=999, enterprise_customer_uuid=ENTERPRISE, item_id='x',
                                    item_type='course', kind='saved')
        results = self.client.get(self.url, {'enterprise_customer_uuid': ENTERPRISE}).json()['results']
        self.assertEqual(results, [])

    def test_validation_and_other_enterprise(self):
        self.assertEqual(self.client.post(self.url, dict(self.body, kind='love'), format='json').status_code, 400)
        response = self.client.post(self.url, dict(self.body, enterprise_customer_uuid=OTHER_ENTERPRISE), format='json')
        self.assertEqual(response.status_code, 403)


class LearnerIdTests(TestCase):
    """The owner id is the LMS user id; a request without one is refused rather than falling back to the pk."""

    def test_lms_user_id_is_used(self):
        request = SimpleNamespace(user=SimpleNamespace(lms_user_id=77, id=5))
        self.assertEqual(learner_id(request), 77)

    def test_no_lms_user_id_is_refused(self):
        for lms_user_id in (None, 0):
            with self.assertRaises(PermissionDenied):
                learner_id(SimpleNamespace(user=SimpleNamespace(lms_user_id=lms_user_id, id=5)))


@override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
class ItemFeedbackAccessTests(APITest):
    """Feedback is written under the enterprise that was permission-checked, and its snapshot is bounded."""

    def setUp(self):
        super().setUp()
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])
        self.url = reverse('api:v1:personalized-search-item-feedback')

    def body(self, **extra):
        return {'enterprise_customer_uuid': ENTERPRISE, 'item_id': 'edX+C1', 'item_type': 'course', 'kind': 'saved',
                **extra}

    def test_a_query_string_enterprise_cannot_stand_in_for_the_body_enterprise(self):
        response = self.client.post(f'{self.url}?enterprise_customer_uuid={ENTERPRISE}',
                                    self.body(enterprise_customer_uuid=OTHER_ENTERPRISE), format='json')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ItemFeedback.objects.exists())

    def test_snapshot_must_be_an_object_within_the_size_limit_and_keeps_only_card_fields(self):
        self.assertEqual(self.client.post(self.url, self.body(card_snapshot=['x']), format='json').status_code, 400)
        big = {'title': 'x' * 9000}
        self.assertEqual(self.client.post(self.url, self.body(card_snapshot=big), format='json').status_code, 400)
        ok = self.client.post(self.url, self.body(card_snapshot={'title': 'T', 'secret': 'drop me'}), format='json')
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(ok.json()['card_snapshot'], {'title': 'T'})

    def test_a_null_snapshot_is_stored_as_none(self):
        response = self.client.post(self.url, self.body(card_snapshot=None), format='json')
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()['card_snapshot'])

    def test_a_foreign_session_uuid_is_ignored_not_linked(self):
        other = ChatSession.objects.create(lms_user_id=self.user.lms_user_id + 1000,
                                           enterprise_customer_uuid=ENTERPRISE, first_prompt='x')
        self.assertEqual(self.client.post(self.url, self.body(session_uuid=str(other.uuid)), format='json').status_code,
                         201)
        self.assertIsNone(ItemFeedback.objects.get().session)

    def test_saved_and_not_interested_are_separate_rows(self):
        self.client.post(self.url, self.body(kind='saved'), format='json')
        self.client.post(self.url, self.body(kind='not_interested'), format='json')
        self.assertEqual(ItemFeedback.objects.count(), 2)

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=False)
    def test_flag_off_is_404(self):
        self.assertEqual(self.client.get(f'{self.url}?enterprise_customer_uuid={ENTERPRISE}').status_code, 404)


@override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
class TurnFeedbackTests(APITest):
    """POST sessions/{uuid}/turns/{sequence}/feedback/: thumbs on the learner's own replies."""

    def setUp(self):
        super().setUp()
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])
        self.session = ChatSession.objects.create(lms_user_id=self.user.lms_user_id,
                                                  enterprise_customer_uuid=ENTERPRISE, first_prompt='Lead a team',
                                                  turn_count=1)
        ChatTurn.objects.create(session=self.session, sequence=1, user_message='m1', parts=[])

    def url(self, session=None, sequence=1):
        return reverse('api:v1:personalized-search-turn-feedback',
                       kwargs={'session_uuid': session or self.session.uuid, 'sequence': sequence})

    def post(self, rating, **kwargs):
        return self.client.post(self.url(**kwargs), {'enterprise_customer_uuid': ENTERPRISE, 'rating': rating},
                                format='json')

    def test_rate_then_clear_and_replay_shows_it(self):
        response = self.post(1)
        self.assertEqual((response.status_code, response.json()), (200, {'sequence': 1, 'rating': 1}))
        detail_url = reverse('api:v1:personalized-search-session', kwargs={'session_uuid': self.session.uuid})
        detail = self.client.get(detail_url, {'enterprise_customer_uuid': ENTERPRISE}).json()
        self.assertEqual(detail['turns'][0]['rating'], 1)
        self.assertEqual(self.post(None).json()['rating'], None)
        self.assertIsNone(ChatTurn.objects.get(session=self.session).rating)

    def test_invalid_rating_is_400(self):
        self.assertEqual(self.post(5).status_code, 400)

    def test_someone_elses_or_a_missing_turn_is_404(self):
        other = ChatSession.objects.create(lms_user_id=self.user.lms_user_id + 1000,
                                           enterprise_customer_uuid=ENTERPRISE, first_prompt='x', turn_count=1)
        ChatTurn.objects.create(session=other, sequence=1, user_message='m', parts=[])
        self.assertEqual(self.post(1, session=other.uuid).status_code, 404)
        self.assertEqual(self.post(1, sequence=9).status_code, 404)
        self.assertIsNone(ChatTurn.objects.get(session=other).rating)

    def test_other_enterprise_is_403(self):
        response = self.client.post(self.url(), {'enterprise_customer_uuid': OTHER_ENTERPRISE, 'rating': 1},
                                    format='json')
        self.assertEqual(response.status_code, 403)

    def test_a_turn_from_another_enterprise_is_404_even_with_access_to_both(self):
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE},
                             {'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': OTHER_ENTERPRISE}])
        response = self.client.post(self.url(), {'enterprise_customer_uuid': OTHER_ENTERPRISE, 'rating': 1},
                                    format='json')
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(ChatTurn.objects.get(session=self.session).rating)

    def test_flag_off_is_404(self):
        with override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=False):
            self.assertEqual(self.post(1).status_code, 404)


EMIT = 'enterprise_access.apps.personalized_search.events.segment_emit'


@override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
@override_settings(PERSONALIZED_SEARCH_ANALYTICS_COHORT='internal_pilot', PERSONALIZED_SEARCH_ENGINE_LABEL='b2b_mvp_v1')
class HistoryEventTests(APITest):
    """Reopening a search and rating a reply each send one event with the join keys (KD-12)."""

    def setUp(self):
        super().setUp()
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])
        self.session = ChatSession.objects.create(lms_user_id=self.user.lms_user_id,
                                                  enterprise_customer_uuid=ENTERPRISE, first_prompt='Lead a team',
                                                  turn_count=1)
        self.turn = ChatTurn.objects.create(session=self.session, sequence=1, user_message='m1', parts=[])

    @mock.patch(EMIT)
    def test_reopening_a_search_sends_session_opened(self, emit):
        self.client.get(reverse('api:v1:personalized-search-session', kwargs={'session_uuid': self.session.uuid}),
                        {'enterprise_customer_uuid': ENTERPRISE})
        emit.assert_called_once_with(self.user.lms_user_id, 'history.session-opened', {
            'conversation_id': str(self.session.uuid), 'enterprise_customer_uuid': ENTERPRISE,
            'cohort': 'internal_pilot', 'engine': 'b2b_mvp_v1',
        })

    @mock.patch(EMIT)
    def test_a_missing_session_sends_nothing(self, emit):
        self.client.get(reverse('api:v1:personalized-search-session', kwargs={'session_uuid': uuid.uuid4()}),
                        {'enterprise_customer_uuid': ENTERPRISE})
        emit.assert_not_called()

    @mock.patch(EMIT)
    def test_rating_a_reply_sends_turn_rated(self, emit):
        url = reverse('api:v1:personalized-search-turn-feedback',
                      kwargs={'session_uuid': self.session.uuid, 'sequence': 1})
        response = self.client.post(url, {'enterprise_customer_uuid': ENTERPRISE, 'rating': -1}, format='json')
        self.assertEqual(response.json(), {'sequence': 1, 'rating': -1})
        name, props = emit.call_args.args[1], emit.call_args.args[2]
        self.assertEqual(name, 'turn.rated')
        self.assertEqual((props['turn_id'], props['turn_index'], props['rating']), (str(self.turn.uuid), 1, -1))

    @mock.patch(EMIT)
    def test_clearing_a_rating_sends_turn_rated_with_null(self, emit):
        url = reverse('api:v1:personalized-search-turn-feedback',
                      kwargs={'session_uuid': self.session.uuid, 'sequence': 1})
        response = self.client.post(url, {'enterprise_customer_uuid': ENTERPRISE, 'rating': None}, format='json')
        self.assertEqual(response.json(), {'sequence': 1, 'rating': None})
        name, props = emit.call_args.args[1], emit.call_args.args[2]
        self.assertEqual((name, props['turn_id'], props['rating']), ('turn.rated', str(self.turn.uuid), None))

    @mock.patch(EMIT)
    def test_rating_a_missing_turn_sends_nothing(self, emit):
        url = reverse('api:v1:personalized-search-turn-feedback',
                      kwargs={'session_uuid': self.session.uuid, 'sequence': 9})
        response = self.client.post(url, {'enterprise_customer_uuid': ENTERPRISE, 'rating': 1}, format='json')
        self.assertEqual(response.status_code, 404)
        emit.assert_not_called()
