"""
Tests for the ``enterprise_access.apps.customer_billing.utils`` module.
"""

import ddt
from django.test import TestCase
from django.utils import timezone

from enterprise_access.apps.customer_billing.utils import datetime_from_timestamp


@ddt.ddt
class TestCustomerBillingUtils(TestCase):
    """
    Tests for customer billing utility functions.
    """

    def test_datetime_from_timestamp_returns_aware_datetime(self):
        """datetime_from_timestamp should return a timezone-aware datetime."""
        ts = 1767285545

        dt = datetime_from_timestamp(ts)

        self.assertTrue(timezone.is_aware(dt))

    def test_datetime_from_timestamp_uses_current_timezone(self):
        """
        datetime_from_timestamp should attach the current Django timezone
        (make_aware default behavior).
        """
        ts = 1767285545

        dt = datetime_from_timestamp(ts)

        self.assertEqual(dt.tzinfo, timezone.get_current_timezone())
