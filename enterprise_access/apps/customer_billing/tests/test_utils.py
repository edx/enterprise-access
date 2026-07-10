"""
Tests for the ``enterprise_access.apps.customer_billing.utils`` module.
"""

import datetime
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

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

            result = get_academy_name_from_slug('essentials-monthly')

        self.assertEqual(result, 'Test Academy')
