"""
Tests for the ``enterprise_access.apps.customer_billing.utils`` module.
"""

import datetime
from unittest import mock
from uuid import uuid4

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.models import CheckoutIntent, SspProduct, StripeEventData
from enterprise_access.apps.customer_billing.stripe_event_handlers import _get_ssp_product_slug_from_stripe_event
from enterprise_access.apps.customer_billing.utils import (
    datetime_from_timestamp,
    get_academy_name_from_slug,
    get_campaign_id,
    get_product_type_from_slug
)


class TestCustomerBillingUtils(TestCase):
    """
    Tests for customer billing utility functions.
    """

    def test_datetime_from_timestamp_returns_aware_datetime(self):
        """datetime_from_timestamp should return a timezone-aware datetime."""
        ts = 1767285545

        dt = datetime_from_timestamp(ts)

        self.assertTrue(timezone.is_aware(dt))

    def test_datetime_from_timestamp_returns_utc(self):
        """
        datetime_from_timestamp should always return a UTC-aware datetime.
        """
        ts = 1767285545

        dt = datetime_from_timestamp(ts)

        self.assertEqual(dt.tzinfo, datetime.timezone.utc)

    def test_datetime_from_timestamp_has_expected_components(self):
        """
        Validate that datetime_from_timestamp returns the correct UTC
        representation for the given timestamp.
        """
        ts = 1767285545

        expected = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)

        dt = datetime_from_timestamp(ts)

        self.assertIsInstance(dt, datetime.datetime)
        self.assertTrue(timezone.is_aware(dt))
        self.assertEqual(dt.date(), expected.date())


class TestGetProductTypeFromSlug(TestCase):
    """Tests for get_product_type_from_slug."""

    def test_returns_teams_when_flag_disabled(self):
        """Should always return 'teams' when ENABLE_SSP_ESSENTIALS_CAMPAIGNS is False."""
        with override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=False):
            self.assertEqual(get_product_type_from_slug('essentials-monthly'), 'teams')
            self.assertEqual(get_product_type_from_slug('teams-yearly'), 'teams')
            self.assertEqual(get_product_type_from_slug(None), 'teams')

    @override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=True)
    def test_returns_teams_for_none_slug(self):
        """None slug should route to teams even when flag is enabled."""
        self.assertEqual(get_product_type_from_slug(None), 'teams')

    @override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=True)
    def test_returns_teams_for_teams_slug(self):
        """Slugs starting with 'teams' should route to teams."""
        self.assertEqual(get_product_type_from_slug('teams-yearly'), 'teams')
        self.assertEqual(get_product_type_from_slug('teams-monthly'), 'teams')
        self.assertEqual(get_product_type_from_slug('TEAMS-yearly'), 'teams')

    @override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=True)
    def test_returns_essentials_for_non_teams_slug(self):
        """Non-teams slugs should route to essentials when flag is enabled."""
        self.assertEqual(get_product_type_from_slug('essentials-monthly'), 'essentials')
        self.assertEqual(get_product_type_from_slug('essentials-yearly'), 'essentials')
        self.assertEqual(get_product_type_from_slug('other-slug'), 'essentials')


class TestGetCampaignId(TestCase):
    """Tests for get_campaign_id."""

    @override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=False)
    def test_teams_campaign_routing_for_all_email_types(self):
        """All email types should route to teams campaigns when flag is disabled."""
        email_types = [
            ('signup_confirmation', 'BRAZE_ENTERPRISE_PROVISION_SIGNUP_CONFIRMATION_CAMPAIGN'),
            ('trial_ending_soon', 'BRAZE_ENTERPRISE_PROVISION_TRIAL_ENDING_SOON_CAMPAIGN'),
            ('trial_cancellation', 'BRAZE_TRIAL_CANCELLATION_CAMPAIGN'),
            ('payment_receipt', 'BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN'),
            ('trial_end_subscription_started', 'BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN'),
            ('billing_error', 'BRAZE_BILLING_ERROR_CAMPAIGN'),
            ('paid_cancellation', 'BRAZE_PAID_CANCELLATION_CAMPAIGN'),
        ]
        for email_type, settings_key in email_types:
            expected = getattr(settings, settings_key)
            result = get_campaign_id(email_type, ssp_product_slug=None)
            self.assertEqual(result, expected, f'Mismatch for email_type={email_type}')

    @override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=True)
    def test_essentials_campaign_routing(self):
        """Essentials slugs should route to essentials campaigns."""
        email_types = [
            ('signup_confirmation', 'BRAZE_ESSENTIALS_SIGNUP_CONFIRMATION_CAMPAIGN'),
            ('trial_ending_soon', 'BRAZE_ESSENTIALS_TRIAL_ENDING_SOON_CAMPAIGN'),
            ('trial_cancellation', 'BRAZE_ESSENTIALS_TRIAL_CANCELLATION_CAMPAIGN'),
            ('payment_receipt', 'BRAZE_ESSENTIALS_PAYMENT_RECEIPT_CAMPAIGN'),
            ('trial_end_subscription_started', 'BRAZE_ESSENTIALS_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN'),
            ('billing_error', 'BRAZE_ESSENTIALS_BILLING_ERROR_CAMPAIGN'),
            ('paid_cancellation', 'BRAZE_ESSENTIALS_PAID_CANCELLATION_CAMPAIGN'),
        ]
        for email_type, settings_key in email_types:
            expected = getattr(settings, settings_key)
            result = get_campaign_id(email_type, ssp_product_slug='essentials-monthly')
            self.assertEqual(result, expected, f'Mismatch for email_type={email_type}')

    @override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=True)
    def test_teams_slug_still_routes_to_teams_campaigns(self):
        """Teams slugs should always route to teams campaigns."""
        result = get_campaign_id('signup_confirmation', ssp_product_slug='teams-yearly')
        self.assertEqual(result, settings.BRAZE_ENTERPRISE_PROVISION_SIGNUP_CONFIRMATION_CAMPAIGN)

    def test_raises_value_error_for_unknown_email_type(self):
        """ValueError should be raised for unrecognised email types."""
        with self.assertRaises(ValueError):
            get_campaign_id('unknown_email_type', ssp_product_slug=None)

    @override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=False, BRAZE_BILLING_ERROR_CAMPAIGN='')
    def test_raises_value_error_when_setting_not_configured(self):
        """ValueError should be raised when the resolved campaign setting is empty."""
        with self.assertRaises(ValueError):
            get_campaign_id('billing_error', ssp_product_slug=None)


class TestGetAcademyNameFromSlug(TestCase):
    """Tests for get_academy_name_from_slug."""

    def test_returns_none_for_none_slug(self):
        """None slug should return None."""
        result = get_academy_name_from_slug(None)
        self.assertIsNone(result)

    def test_returns_none_for_teams_slug(self):
        """Teams slugs should return None (teams products have no academy)."""
        result = get_academy_name_from_slug('teams-yearly')
        self.assertIsNone(result)

    def test_returns_none_when_product_not_found(self):
        """Missing SspProduct should return None without raising."""
        result = get_academy_name_from_slug('essentials-monthly-nonexistent')
        self.assertIsNone(result)

    def test_returns_academy_title_from_ssp_product(self):
        """Should return academy_title from the SspProduct when found."""
        mock_product = mock.Mock()
        mock_product.academy_title = 'Test Academy'

        with mock.patch('django.apps.apps.get_model') as mock_get_model:
            mock_model = mock_get_model.return_value
            mock_model.objects.get.return_value = mock_product
            # Ensure essentials routing is enabled for this test

            with override_settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=True):
                result = get_academy_name_from_slug('essentials-monthly')

        self.assertEqual(result, 'Test Academy')


class TestAcademyLookupAndCheckoutIntentSelectionAdditional(TestCase):
    """Additional tests for academy lookup and checkout intent selection logic."""
    def test_get_academy_name_from_slug_uses_cached_academy_title(self):
        # Create an SspProduct with an academy_uuid and patch the cached fetch
        slug = 'essentials-sample'
        academy_uuid = uuid4()
        SspProduct.objects.create(
            slug=slug,
            stripe_price_lookup_key='k',
            catalog_query_uuid=uuid4(),
            academy_uuid=academy_uuid,
        )
        # Ensure feature flag is enabled so slug is treated as essentials
        with self.settings(ENABLE_SSP_ESSENTIALS_CAMPAIGNS=True):
            # Patch the get_cached_academy_data used by SspProduct._academy_data
            patch_path = 'enterprise_access.apps.customer_billing.models.get_cached_academy_data'
            with mock.patch(patch_path, return_value={'title': 'My Academy'}):
                result = get_academy_name_from_slug(slug)
            self.assertEqual(result, 'My Academy')

    def test_get_academy_name_from_slug_returns_none_for_teams(self):
        # Teams-like slugs should be treated as teams regardless of case
        self.assertIsNone(get_academy_name_from_slug('TEAMS-yearly'))

    def test_get_ssp_product_slug_prefers_most_recent_checkout_intent(self):
        # Create two CheckoutIntent records with the same stripe_subscription_id
        subscription_id = 'sub_test_123'
        # Ensure a default SspProduct exists for CheckoutIntent FK default
        SspProduct.objects.get_or_create(
            slug='teams-yearly',
            defaults={
                'stripe_price_lookup_key': 'default',
                'catalog_query_uuid': uuid4(),
            },
        )
        user1 = UserFactory()
        user2 = UserFactory()
        c1 = CheckoutIntent.create_intent(user=user1, slug='e1', name='E1', quantity=1)
        c1.stripe_subscription_id = subscription_id
        c1.save()
        c2 = CheckoutIntent.create_intent(user=user2, slug='e2', name='E2', quantity=2)
        c2.stripe_subscription_id = subscription_id
        c2.save()
        # Create StripeEventData + StripeEventSummary for each checkout intent
        ev1 = StripeEventData.objects.create(
            event_id=f'evt-{subscription_id}-1',
            event_type='customer.subscription.created',
            checkout_intent=c1,
            data={'data': {'object': {'object': 'subscription', 'id': subscription_id, 'created': 1}}},
        )
        # A post_save signal will create the StripeEventSummary; update it instead
        s1 = ev1.summary
        s1.stripe_event_created_at = datetime.datetime.fromtimestamp(1, tz=datetime.timezone.utc)
        s1.checkout_intent = c1
        s1.stripe_subscription_id = subscription_id
        s1.save()
        ev2 = StripeEventData.objects.create(
            event_id=f'evt-{subscription_id}-2',
            event_type='customer.subscription.created',
            checkout_intent=c2,
            data={'data': {'object': {'object': 'subscription', 'id': subscription_id, 'created': 2}}},
        )
        s2 = ev2.summary
        s2.stripe_event_created_at = datetime.datetime.fromtimestamp(2, tz=datetime.timezone.utc)
        s2.checkout_intent = c2
        s2.stripe_subscription_id = subscription_id
        s2.save()
        # Ensure most recent (c2) is returned
        event_data = {'subscription': subscription_id}
        slug = _get_ssp_product_slug_from_stripe_event(event_data)
        # c2 has no ssp_product by default; create ssp_product attached to c2 and test
        sp, _ = SspProduct.objects.get_or_create(
            slug='essentials-recent',
            defaults={
                'stripe_price_lookup_key': 'k2',
                'catalog_query_uuid': uuid4(),
            },
        )
        c2.ssp_product = sp
        c2.save()

        slug = _get_ssp_product_slug_from_stripe_event(event_data)
        self.assertEqual(slug, 'essentials-recent')

    def test_get_ssp_product_slug_uses_id_fallback(self):
        # When 'subscription' key is absent, 'id' should be used
        subscription_id = 'sub_fallback_1'
        # ensure default SspProduct exists for the second user as well
        SspProduct.objects.get_or_create(
            slug='teams-yearly',
            defaults={
                'stripe_price_lookup_key': 'default2',
                'catalog_query_uuid': uuid4(),
            },
        )
        user2 = UserFactory()
        c = CheckoutIntent.create_intent(user=user2, slug='e3', name='E3', quantity=1)
        c.stripe_subscription_id = subscription_id
        sp, _ = SspProduct.objects.get_or_create(
            slug='essentials-fallback',
            defaults={
                'stripe_price_lookup_key': 'k3',
                'catalog_query_uuid': uuid4(),
            },
        )
        c.ssp_product = sp
        c.save()
        # Create summary for fallback
        ev = StripeEventData.objects.create(
            event_id=f'evt-{subscription_id}-fallback',
            event_type='customer.subscription.created',
            checkout_intent=c,
            data={'data': {'object': {'object': 'subscription', 'id': subscription_id, 'created': 3}}},
        )
        s = ev.summary
        s.stripe_event_created_at = datetime.datetime.fromtimestamp(3, tz=datetime.timezone.utc)
        s.checkout_intent = c
        s.stripe_subscription_id = subscription_id
        s.save()

        event_data = {'id': subscription_id}
        slug = _get_ssp_product_slug_from_stripe_event(event_data)
        self.assertEqual(slug, 'essentials-fallback')

    def test_get_ssp_product_slug_handles_checkout_intent_ssp_product_access_exception(self):
        """If accessing checkout_intent.ssp_product raises, we continue to other strategies."""
        subscription_id = 'sub_except_1'
        # Ensure an SspProduct exists and a CheckoutIntent with a stripe_customer_id maps to it
        sp, _ = SspProduct.objects.get_or_create(
            slug='essentials-customer-ex',
            defaults={
                'stripe_price_lookup_key': 'k_ex',
                'catalog_query_uuid': uuid4(),
            },
        )
        user = UserFactory()
        c = CheckoutIntent.create_intent(user=user, slug='e-ex', name='E-Ex', quantity=1)
        c.stripe_customer_id = 'cus_for_exception'
        c.ssp_product = sp
        c.save()

        class BadCheckoutIntent:
            @property
            def ssp_product(self):
                raise Exception('boom')

        event_data = {'customer': 'cus_for_exception', 'id': subscription_id}
        slug = _get_ssp_product_slug_from_stripe_event(event_data, checkout_intent=BadCheckoutIntent())
        self.assertEqual(slug, 'essentials-customer-ex')

    def test_get_ssp_product_slug_resolves_from_invoice_line_metadata(self):
        """Should read ssp_product_slug from invoice line price.metadata."""
        event_data = {
            'lines': {
                'data': [
                    {
                        'price': {
                            'metadata': {
                                'ssp_product_slug': 'essentials-from-line'
                            }
                        }
                    }
                ]
            }
        }
        slug = _get_ssp_product_slug_from_stripe_event(event_data)
        self.assertEqual(slug, 'essentials-from-line')
