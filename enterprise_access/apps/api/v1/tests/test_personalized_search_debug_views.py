"""Tests for the Personalized Search debug model menu."""
from django.test import override_settings
from edx_toggles.toggles.testutils import override_waffle_flag
from rest_framework.reverse import reverse

from enterprise_access.apps.api.v1.tests.test_personalized_search_views import DEV_SCOPE, ENTERPRISE, MODELS
from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.toggles import PERSONALIZED_SEARCH_ENABLED
from test_utils import APITest


@override_settings(PERSONALIZED_SEARCH_OPENAI_MODELS=MODELS, PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE=DEV_SCOPE)
class PersonalizedSearchDebugConfigViewTests(APITest):
    """GET /api/v1/personalized-search/debug/"""

    def setUp(self):
        super().setUp()
        self.url = reverse('api:v1:personalized-search-debug') + f'?enterprise_customer_uuid={ENTERPRISE}'
        self.set_jwt_cookie([{'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE, 'context': ENTERPRISE}])

    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_404_unless_enabled(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)

    @override_settings(PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED=True)
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_menu(self):
        body = self.client.get(self.url).json()
        models = {m['id']: m for m in body['models']}
        self.assertEqual(set(models), set(MODELS))
        self.assertEqual(models['gpt-4.1']['efforts'], [None])
        self.assertIn('low', models['gpt-5.4-mini']['efforts'])
        self.assertNotIn('minimal', models['gpt-5.4-mini']['efforts'])
        self.assertEqual(body['catalog'], 'Test catalog')
        self.assertNotIn('xpert', models)  # XPERT_API_BASE_URL is unset in test settings

    @override_settings(PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED=True,
                       XPERT_API_BASE_URL='https://xpert.test', XPERT_API_CLIENT_ID='client-123')
    @override_waffle_flag(PERSONALIZED_SEARCH_ENABLED, active=True)
    def test_menu_offers_xpert_when_configured(self):
        xpert_entry = [m for m in self.client.get(self.url).json()['models'] if m['id'] == 'xpert'][0]
        self.assertEqual(xpert_entry['label'], 'Xpert platform')
        self.assertIsNone(xpert_entry['price_in'])
        self.assertEqual(xpert_entry['efforts'], [None])
