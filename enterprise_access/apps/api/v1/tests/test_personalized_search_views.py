"""Tests for the Personalized Search turn endpoint."""
import json
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from edx_toggles.toggles.testutils import override_waffle_flag, override_waffle_switch
from rest_framework.reverse import reverse

from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.personalized_search.models import ChatSession
from enterprise_access.apps.personalized_search.tests.test_orchestrator import fake_steps
from enterprise_access.toggles import LEARNER_PATHWAYS_SERVER_PIPELINE, PERSONALIZED_SEARCH_ENABLED
from test_utils import APITest

ENTERPRISE = str(uuid.uuid4())
OTHER_ENTERPRISE = str(uuid.uuid4())
MODELS = {'gpt-4.1-mini': (0.4, 0.1, 1.6), 'gpt-5.4-mini': (0.75, 0.075, 4.5), 'gpt-4.1': (2.0, 0.5, 8.0)}


def parse_sse(response):
    body = b''.join(response.streaming_content).decode()
    frames = [f[len('data: '):] for f in body.split('\n\n') if f.startswith('data: ')]
    assert frames[-1] == '[DONE]'
    return [json.loads(f) for f in frames[:-1]]


@override_settings(OPENAI_API_KEY='test-key', PERSONALIZED_SEARCH_OPENAI_MODELS=MODELS)
class PersonalizedSearchTurnViewTests(APITest):
    """POST /api/v1/personalized-search/turns/"""

    def setUp(self):
        super().setUp()
        self.addCleanup(cache.clear)
        self.url = reverse('api:v1:personalized-search-turns')
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])
        for target, value in (('steps', fake_steps()), ('get_and_cache_secured_algolia_search_keys', {}),
                              ('secured_key_from_payload', 'secured-key')):
            patcher = mock.patch(f'enterprise_access.apps.personalized_search.wiring.{target}', return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, **body):
        payload = {'enterprise_customer_uuid': ENTERPRISE, 'session_uuid': None, 'message': 'Lead a team', **body}
        return self.client.post(self.url, payload, format='json')

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=False)
    def test_flag_off_is_404(self):
        self.assertEqual(self.post().status_code, 404)

    @override_waffle_switch(LEARNER_PATHWAYS_SERVER_PIPELINE, active=False)
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_gated_by_its_own_flag_not_the_pathways_switch(self):
        self.assertEqual(self.post().status_code, 200)

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_streams_a_turn(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        self.assertEqual(response['X-Accel-Buffering'], 'no')
        self.assertEqual(response['x-vercel-ai-ui-message-stream'], 'v1')
        parts = parse_sse(response)
        self.assertEqual(parts[0]['type'], 'start')
        self.assertEqual(parts[-1]['type'], 'finish')
        session_uuid = parts[1]['data']['session_uuid']
        # The lock was released, and the session continues on the next turn.
        parts = parse_sse(self.post(session_uuid=session_uuid, message='Shorter'))
        self.assertEqual(parts[1]['data']['turn_sequence'], 2)

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_accepts_event_stream(self):
        # The portal's SSE transport sends this Accept header; DRF must not answer 406.
        payload = {'enterprise_customer_uuid': ENTERPRISE, 'message': 'Lead a team'}
        response = self.client.post(self.url, payload, format='json', HTTP_ACCEPT='text/event-stream')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_sse(response)[-1]['type'], 'finish')
        error = self.client.post(self.url, dict(payload, message='x' * 501), format='json',
                                 HTTP_ACCEPT='text/event-stream')
        self.assertEqual(error.status_code, 400)
        self.assertEqual(json.loads(error.content)['code'], 'invalid_request')

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_monitoring_attributes_and_timing_line_are_set_before_streaming(self):
        target = 'enterprise_access.apps.api.v1.views.personalized_search.set_custom_attribute'
        with mock.patch(target) as attribute, \
                self.assertLogs('enterprise_access.apps.api.v1.views.personalized_search', level='INFO') as logs:
            response = self.post()
            before = [c.args for c in attribute.call_args_list]
            parse_sse(response)
        self.assertEqual(before, [('personalized_search_enterprise_uuid', ENTERPRISE),
                                  ('personalized_search_backend', 'openai'),
                                  ('personalized_search_degraded', False)])
        self.assertEqual(attribute.call_count, 3)  # none from inside the generator
        lines = [r.getMessage() for r in logs.records if r.getMessage().startswith('personalized_search.pre_stream ')]
        self.assertEqual(len(lines), 1)
        self.assertIn('pre_stream_ms', json.loads(lines[0].split(' ', 1)[1]))

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_buffered_mode(self):
        response = self.client.post(f'{self.url}?mode=buffered', {
            'enterprise_customer_uuid': ENTERPRISE, 'message': 'Lead a team'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['parts'][-1]['type'], 'finish')

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_other_enterprise_is_403(self):
        self.assertEqual(self.post(enterprise_customer_uuid=OTHER_ENTERPRISE).status_code, 403)

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_message_too_long_is_400(self):
        response = self.post(message='x' * 501)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'invalid_request')

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_unknown_session_is_404(self):
        self.assertEqual(self.post(session_uuid=str(uuid.uuid4())).status_code, 404)

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_locked_session_is_409(self):
        session_uuid = parse_sse(self.post())[1]['data']['session_uuid']
        ChatSession.objects.filter(uuid=session_uuid).update(active_turn_started_at=timezone.now())
        response = self.post(session_uuid=session_uuid)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'turn_in_progress')

    @override_settings(OPENAI_API_KEY='')
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_no_backend_is_503(self):
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'backend_unsupported')

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_secured_key_failure_is_503(self):
        target = 'enterprise_access.apps.personalized_search.wiring.get_and_cache_secured_algolia_search_keys'
        with mock.patch(target, side_effect=RuntimeError('catalog down')):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'catalog_unavailable')


@override_settings(OPENAI_API_KEY='test-key', PERSONALIZED_SEARCH_OPENAI_MODELS=MODELS)
@override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
class PersonalizedSearchTurnAccessTests(APITest):
    """Who a turn may run for: the enterprise checked is the one used, sessions are the learner's own."""

    def setUp(self):
        super().setUp()
        self.addCleanup(cache.clear)
        self.url = reverse('api:v1:personalized-search-turns')
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])
        for target, value in (('steps', fake_steps()), ('get_and_cache_secured_algolia_search_keys', {}),
                              ('secured_key_from_payload', 'secured-key')):
            patcher = mock.patch(f'enterprise_access.apps.personalized_search.wiring.{target}', return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, query='', **body):
        payload = {'enterprise_customer_uuid': ENTERPRISE, 'session_uuid': None, 'message': 'Lead a team', **body}
        return self.client.post(self.url + query, payload, format='json')

    def test_a_query_string_enterprise_cannot_stand_in_for_the_body_enterprise(self):
        response = self.post(query=f'?enterprise_customer_uuid={ENTERPRISE}', enterprise_customer_uuid=OTHER_ENTERPRISE)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ChatSession.objects.exists())

    def test_another_learners_session_is_404(self):
        session_uuid = parse_sse(self.post())[1]['data']['session_uuid']
        ChatSession.objects.filter(uuid=session_uuid).update(lms_user_id=self.user.lms_user_id + 1000)
        self.assertEqual(self.post(session_uuid=session_uuid).status_code, 404)

    def test_the_same_session_under_another_enterprise_is_404(self):
        session_uuid = parse_sse(self.post())[1]['data']['session_uuid']
        ChatSession.objects.filter(uuid=session_uuid).update(enterprise_customer_uuid=OTHER_ENTERPRISE)
        self.assertEqual(self.post(session_uuid=session_uuid).status_code, 404)

    def test_a_user_without_an_lms_id_is_403(self):
        with mock.patch.object(type(self.user), 'lms_user_id', None, create=True):
            with mock.patch('enterprise_access.apps.api.v1.views.personalized_search.getattr',
                            side_effect=lambda obj, name, default=None: None if name == 'lms_user_id' else default,
                            create=True):
                response = self.post()
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_401(self):
        self.client.cookies.clear()
        self.client.logout()
        self.assertEqual(self.post().status_code, 401)

    def test_a_failure_before_streaming_releases_the_session_lock(self):
        session_uuid = parse_sse(self.post())[1]['data']['session_uuid']
        target = 'enterprise_access.apps.personalized_search.wiring.turn_context'
        with mock.patch(target, side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.post(session_uuid=session_uuid)
        self.assertIsNone(ChatSession.objects.get(uuid=session_uuid).active_turn_started_at)
