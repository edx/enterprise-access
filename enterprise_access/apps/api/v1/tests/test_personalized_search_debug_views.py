"""Tests for the Personalized Search debug model menu."""
import uuid

from django.test import override_settings
from edx_toggles.toggles.testutils import override_waffle_flag
from rest_framework.reverse import reverse

from enterprise_access.apps.api.v1.tests.test_personalized_search_views import DEV_SCOPE, ENTERPRISE, MODELS
from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.toggles import PERSONALIZED_SEARCH_DEBUG, PERSONALIZED_SEARCH_ENABLED
from test_utils import APITest


@override_settings(PERSONALIZED_SEARCH_OPENAI_MODELS=MODELS, PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE=DEV_SCOPE)
class PersonalizedSearchDebugConfigViewTests(APITest):
    """GET /api/v1/personalized-search/debug/"""

    def setUp(self):
        super().setUp()
        self.url = reverse('api:v1:personalized-search-debug') + f'?enterprise_customer_uuid={ENTERPRISE}'
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])

    def set_user_flags(self, **flags):
        """Set is_superuser / is_staff for this test only: the test user object is shared by the class."""
        before = {name: getattr(self.user, name) for name in flags}
        for name, value in flags.items():
            setattr(self.user, name, value)
        self.user.save()

        def restore():
            for name, value in before.items():
                setattr(self.user, name, value)
            self.user.save()
        self.addCleanup(restore)

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_404_unless_enabled(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)

    @override_settings(PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED=True)
    def test_the_feature_flag_comes_before_the_learner_permission(self):
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': str(uuid.uuid4())}])
        with override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=False):
            self.assertEqual(self.client.get(self.url).status_code, 404)  # off: 404 for everyone, as the other views
        with override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True):
            self.assertEqual(self.client.get(self.url).status_code, 403)  # on: a learner of another enterprise

    @override_settings(PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED=True)
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_404_for_a_learner_without_the_debug_role(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)

    @override_settings(PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED=True)
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_a_superuser_gets_the_menu_without_the_flag(self):
        self.set_user_flags(is_superuser=True)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    @override_settings(PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED=True)
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_staff_alone_is_not_enough(self):
        # is_staff comes from the JWT's administrator claim; debug mode needs a superuser or the debug flag.
        self.set_user_flags(is_staff=True, is_superuser=False)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    @override_settings(PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED=True)
    @override_waffle_flag(PERSONALIZED_SEARCH_DEBUG, active=True)
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_menu(self):
        body = self.client.get(self.url).json()
        models = {m['id']: m for m in body['models']}
        self.assertEqual(set(models), set(MODELS))
        self.assertEqual(models['gpt-4.1']['efforts'], [None])
        self.assertIn('low', models['gpt-5.4-mini']['efforts'])
        self.assertNotIn('minimal', models['gpt-5.4-mini']['efforts'])
        # The November 2026 spike's pair is the default; its quality alternative is in the switcher.
        self.assertEqual((body['defaults']['intent_model'], body['defaults']['compose_model']),
                         ('gpt-6-luna', 'gpt-6-luna'))
        self.assertEqual(models['gpt-6.1-terra']['efforts'], ['none', 'low', 'medium', 'high'])
        self.assertEqual(models['gpt-6.1-sol']['efforts'], ['low', 'medium', 'high'])
        self.assertEqual(body['catalog'], 'Test catalog')
        self.assertNotIn('xpert', models)  # XPERT_API_BASE_URL is unset in test settings

    @override_settings(PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED=True,
                       XPERT_API_BASE_URL='https://xpert.test', XPERT_API_CLIENT_ID='client-123')
    @override_waffle_flag(PERSONALIZED_SEARCH_DEBUG, active=True)
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_menu_offers_xpert_when_configured(self):
        xpert_entry = [m for m in self.client.get(self.url).json()['models'] if m['id'] == 'xpert'][0]
        self.assertEqual(xpert_entry['label'], 'Xpert platform')
        self.assertIsNone(xpert_entry['price_in'])
        self.assertEqual(xpert_entry['efforts'], [None])
