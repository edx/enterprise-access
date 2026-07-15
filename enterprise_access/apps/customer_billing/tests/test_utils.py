"""
Tests for the ``enterprise_access.apps.customer_billing.utils`` module.
"""

import datetime
from uuid import uuid4

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from enterprise_access.apps.customer_billing.models import SspProduct
from enterprise_access.apps.customer_billing.utils import datetime_from_timestamp, get_campaign_id, get_product_type


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


class TestGetProductType(TestCase):
    """Tests for get_product_type."""

    def test_returns_teams_for_none_product(self):
        self.assertEqual(get_product_type(None), 'teams')

    def test_returns_teams_for_product_without_academy_uuid(self):
        ssp_product = SspProduct(
            slug='teams-yearly',
            stripe_price_lookup_key='teams-yearly-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=None,
        )
        self.assertEqual(get_product_type(ssp_product), 'teams')

    def test_returns_essentials_for_product_with_academy_uuid(self):
        ssp_product = SspProduct(
            slug='essentials-monthly',
            stripe_price_lookup_key='essentials-monthly-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=uuid4(),
        )
        self.assertEqual(get_product_type(ssp_product), 'essentials')


class TestGetCampaignId(TestCase):
    """Tests for get_campaign_id."""

    def test_teams_campaign_routing_for_all_email_types(self):
        """Products without academy UUID route to teams campaigns."""
        teams_product = SspProduct(
            slug='teams-yearly',
            stripe_price_lookup_key='teams-yearly-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=None,
        )
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
            result = get_campaign_id(email_type, teams_product)
            self.assertEqual(result, expected, f'Mismatch for email_type={email_type}')

    def test_essentials_campaign_routing(self):
        """Products with academy UUID route to essentials campaigns."""
        essentials_product = SspProduct(
            slug='essentials-monthly',
            stripe_price_lookup_key='essentials-monthly-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=uuid4(),
        )
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
            result = get_campaign_id(email_type, essentials_product)
            self.assertEqual(result, expected, f'Mismatch for email_type={email_type}')

    def test_teams_product_still_routes_to_teams_campaigns(self):
        """Teams products always route to teams campaigns."""
        teams_product = SspProduct(
            slug='teams-yearly',
            stripe_price_lookup_key='teams-yearly-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=None,
        )
        result = get_campaign_id('signup_confirmation', teams_product)
        self.assertEqual(result, settings.BRAZE_ENTERPRISE_PROVISION_SIGNUP_CONFIRMATION_CAMPAIGN)

    def test_raises_value_error_for_unknown_email_type(self):
        """ValueError should be raised for unrecognised email types."""
        with self.assertRaises(ValueError):
            get_campaign_id('unknown_email_type', None)

    @override_settings(BRAZE_BILLING_ERROR_CAMPAIGN='')
    def test_raises_value_error_when_setting_not_configured(self):
        """ValueError should be raised when the resolved campaign setting is empty."""
        with self.assertRaises(ValueError):
            get_campaign_id('billing_error', None)
