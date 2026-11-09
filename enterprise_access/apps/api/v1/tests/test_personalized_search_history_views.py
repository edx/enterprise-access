"""Tests for the Personalized Search sessions and item-feedback endpoints (chunks 28, 30)."""
import uuid

from edx_toggles.toggles.testutils import override_waffle_flag
from rest_framework.reverse import reverse

from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.personalized_search.models import ChatSession, ChatTurn
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
