"""
Tests for the ``enterprise_access.apps.customer_billing.utils`` module.
"""

import datetime
import time
from unittest import mock
from uuid import uuid4

import ddt
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from enterprise_access.apps.customer_billing.models import SspProduct
from enterprise_access.apps.customer_billing.utils import (
    datetime_from_timestamp,
    get_campaign_id,
    get_default_ssp_price_lookup_key,
    get_product_type,
    sanitize_attribution,
    sanitize_referrer,
    sanitize_utm_value
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


class TestGetDefaultSspPriceLookupKey(TestCase):
    """
    Tests for ``get_default_ssp_price_lookup_key``.
    """

    @override_settings(SSP_DEFAULT_PRODUCT_SLUG=None)
    def test_falls_back_to_first_active_product_when_slug_not_configured(self):
        """When SSP_DEFAULT_PRODUCT_SLUG is unset, falls back to the first active SspProduct."""
        first_active_product = SspProduct.objects.filter(is_active=True).order_by('slug').first()
        self.assertEqual(
            get_default_ssp_price_lookup_key(),
            first_active_product.stripe_price_lookup_key,
        )

    @override_settings(SSP_DEFAULT_PRODUCT_SLUG='does-not-exist')
    def test_falls_back_to_first_active_product_when_no_matching_product(self):
        """When no active SspProduct matches the configured slug, falls back to the first active SspProduct."""
        first_active_product = SspProduct.objects.filter(is_active=True).order_by('slug').first()
        self.assertEqual(
            get_default_ssp_price_lookup_key(),
            first_active_product.stripe_price_lookup_key,
        )

    def test_returns_none_when_no_active_products_exist(self):
        """When there are no active SspProducts at all, returns None."""
        SspProduct.objects.update(is_active=False)
        self.assertIsNone(get_default_ssp_price_lookup_key())

    @override_settings(SSP_DEFAULT_PRODUCT_SLUG='teams-yearly')
    def test_returns_lookup_key_for_matching_product(self):
        """
        When an active SspProduct matches the configured slug, returns its lookup key.
        Relies on the 'teams-yearly' SspProduct backfilled via SSP_PRODUCT_BACKFILL_DATA.
        """
        self.assertEqual(
            get_default_ssp_price_lookup_key(),
            'teams_subscription_license_yearly',
        )

    @override_settings(SSP_DEFAULT_PRODUCT_SLUG='teams-yearly')
    def test_returns_none_on_lookup_error(self):
        """When resolving the SspProduct raises, returns None instead of propagating."""
        with mock.patch(
            'enterprise_access.apps.customer_billing.utils.apps.get_model',
            side_effect=LookupError('boom'),
        ):
            self.assertIsNone(get_default_ssp_price_lookup_key())


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
            ('trial_ended_cancellation', 'BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_ENDED_CAMPAIGN'),
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
            ('trial_ended_cancellation', 'BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_ENDED_CAMPAIGN'),
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

    def test_campaign_routing_for_trial_ended_cancellation(self):
        """Both teams and essentials products route to the single shared campaign."""
        teams_product = SspProduct(
            slug='teams-yearly',
            stripe_price_lookup_key='teams-yearly-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=None,
        )
        essentials_product = SspProduct(
            slug='essentials-monthly',
            stripe_price_lookup_key='essentials-monthly-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=uuid4(),
        )
        teams_campaign = get_campaign_id('trial_ended_cancellation', teams_product)
        essentials_campaign = get_campaign_id('trial_ended_cancellation', essentials_product)
        self.assertEqual(teams_campaign, settings.BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_ENDED_CAMPAIGN)
        self.assertEqual(essentials_campaign, settings.BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_ENDED_CAMPAIGN)
        self.assertEqual(teams_campaign, essentials_campaign)

    def test_raises_value_error_for_unknown_email_type(self):
        """ValueError should be raised for unrecognised email types."""
        with self.assertRaises(ValueError):
            get_campaign_id('unknown_email_type', None)

    @override_settings(BRAZE_BILLING_ERROR_CAMPAIGN='')
    def test_raises_value_error_when_setting_not_configured(self):
        """ValueError should be raised when the resolved campaign setting is empty."""
        with self.assertRaises(ValueError):
            get_campaign_id('billing_error', None)


@ddt.ddt
class TestAttributionSanitizers(TestCase):
    """
    Tests for ``sanitize_utm_value``, ``sanitize_referrer`` and ``sanitize_attribution``.
    """

    @ddt.data(
        'google',
        'paid_social',
        'Q4 2026 - Teams launch',
        'carousel-ad_v2',
        'enterprise learning',
        'brand|search|us',
        'spring_sale_2026',
        'formación',
        'v1.2',
        # Campaign labels that look like numbers or long identifiers are kept.
        '20261110',
        'launch-20261106',
        'q4_2026_11_06',  # nine digits: fewer than a phone number
        'springpromotion2026enterprise',
        ('abcdefghi_' * 10)[:100],  # exactly the maximum length
    )
    def test_campaign_labels_are_kept(self, value):
        self.assertEqual(sanitize_utm_value(value), value)

    @ddt.data(
        # (description, value)
        ('email address', 'jane.doe@example.com'),
        ('email address inside a label', 'utm content jane.doe@example.com spring'),
        ('percent-encoded email address', 'jane.doe%40example.com'),
        ('double-encoded email address', 'jane.doe%2540example.com'),
        ('brand handle', '@edXOnline'),
        ('URL', 'https://example.com/?email=jane'),
        ('key=value pairs', 'email=jane&id=1'),
        ('markup', '<b>hi</b>'),
        ('phone number', '555-123-4567'),
        ('international phone number', '+44 20 7946 0958'),
        ('phone number inside a label', 'call_5551234567_now'),
        ('longer than 100 characters', 'abcdefghi_' * 10 + 'x'),
        ('empty', ''),
    )
    @ddt.unpack
    def test_values_not_in_the_label_format_are_dropped(self, _description, value):
        self.assertIsNone(sanitize_utm_value(value))

    def test_a_huge_value_is_dropped_without_being_scanned(self):
        """The length is checked before the pattern, so a client-supplied value can't tie up a web worker."""
        started = time.monotonic()
        self.assertIsNone(sanitize_attribution({'utm_campaign': 'a' * 1_000_000, 'referrer': 'a' * 1_000_000}))
        self.assertLess(time.monotonic() - started, 1)

    @ddt.data(
        # (description, referrer, stored referrer)
        ('query and fragment', 'https://www.google.com/search?email=foo@bar.com#frag', 'https://www.google.com/'),
        ('username and password', 'https://jane:hunter2@partner.example.com/page', 'https://partner.example.com/'),
        ('identifying path segments', 'https://community.example.com/u/jane-doe/profile/12345',
         'https://community.example.com/'),
        ('surrounding whitespace', '  https://example.com/page  ', 'https://example.com/'),
        ('plain http', 'http://example.com/page', 'http://example.com/'),
        ('port kept', 'http://localhost:8734/checkout', 'http://localhost:8734/'),
        ('scheme and host normalized', 'HTTPS://WWW.Example.COM/Path', 'https://www.example.com/'),
        ('IPv6 host', 'https://[2001:db8::1]:8080/x?y=z', 'https://[2001:db8::1]:8080/'),
    )
    @ddt.unpack
    def test_sanitize_referrer_keeps_only_the_origin(self, _description, url, expected):
        self.assertEqual(sanitize_referrer(url), expected)

    @ddt.data(
        'ftp://files.example.com/report.pdf',
        'javascript:alert(document.cookie)',
        'mailto:jane@example.com',
        '//example.com/no-scheme',
        'not a url',
        'https://',
        # urlsplit raises ValueError for these; the value is dropped instead of failing the request.
        'http://[',
        'https://[::1/path?email=foo@bar.com',
        'https://example.com:notaport/',
        f'https://{"a" * 95}.com/',  # an origin longer than 100 characters
    )
    def test_sanitize_referrer_drops_what_is_not_an_http_url(self, url):
        self.assertIsNone(sanitize_referrer(url))

    @ddt.data(None, 'utm_source=google', ['utm_source'])
    def test_sanitize_attribution_never_raises_on_a_wrong_shape(self, value):
        self.assertIsNone(sanitize_attribution(value))

    def test_sanitize_attribution_drops_unsafe_values_and_logs_their_keys(self):
        with self.assertLogs('enterprise_access.apps.customer_billing.utils', level='INFO') as logs:
            result = sanitize_attribution({
                'utm_source': ' google ',
                'utm_medium': 42,
                'utm_campaign': {'nested': 'value'},
                'utm_content': 'jane@example.com',
                'utm_term': '',
                'referrer': 'https://jane:pw@example.com/u/jane?x=1',
                'unknown': 'value',
            })
        self.assertEqual(result, {'utm_source': 'google', 'referrer': 'https://example.com/'})
        output = '\n'.join(logs.output)
        self.assertIn('utm_medium, utm_campaign, utm_content', output)
        self.assertNotIn('utm_term', output)  # empty values are absent, not dropped
        self.assertNotIn('jane', output)  # keys only, never values

    def test_nothing_is_logged_when_nothing_is_dropped(self):
        with mock.patch('enterprise_access.apps.customer_billing.utils.logger') as mock_logger:
            self.assertEqual(sanitize_attribution({'utm_source': 'google'}), {'utm_source': 'google'})
        mock_logger.info.assert_not_called()
