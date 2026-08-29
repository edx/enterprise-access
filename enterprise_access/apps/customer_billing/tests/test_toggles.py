"""Tests for customer_billing toggles."""
from unittest import mock

from django.test import TestCase

from enterprise_access.apps.customer_billing.toggles import bypass_salesforce_for_provisioning_enabled


class BypassSalesforceForProvisioningEnabledTests(TestCase):
    """Tests for bypass_salesforce_for_provisioning_enabled."""

    @mock.patch('enterprise_access.apps.customer_billing.toggles.BYPASS_SALESFORCE_FOR_PROVISIONING')
    def test_returns_true_when_flag_enabled(self, mock_flag):
        mock_flag.is_enabled.return_value = True
        self.assertTrue(bypass_salesforce_for_provisioning_enabled())

    @mock.patch('enterprise_access.apps.customer_billing.toggles.BYPASS_SALESFORCE_FOR_PROVISIONING')
    def test_returns_false_when_flag_disabled(self, mock_flag):
        mock_flag.is_enabled.return_value = False
        self.assertFalse(bypass_salesforce_for_provisioning_enabled())
