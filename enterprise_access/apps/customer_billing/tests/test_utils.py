"""
Tests for the ``enterprise_access.apps.customer_billing.utils`` module.
"""

import datetime

from django.test import TestCase
from django.utils import timezone

from enterprise_access.apps.customer_billing.utils import datetime_from_timestamp


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
