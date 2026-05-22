"""Tests for customer_billing app configuration."""

import importlib
from unittest import mock

from django.test import SimpleTestCase

from enterprise_access.apps.customer_billing.apps import CustomerBillingConfig


class TestCustomerBillingConfig(SimpleTestCase):
    """Verify app startup wiring behavior."""

    def test_ready_imports_signals(self):
        """Calling ready should import customer_billing signal handlers."""
        app_module = importlib.import_module('enterprise_access.apps.customer_billing')
        config = CustomerBillingConfig('customer_billing', app_module)

        with mock.patch('builtins.__import__', wraps=__import__) as mock_import:
            config.ready()

        signal_import_calls = [
            call
            for call in mock_import.call_args_list
            if call.args and call.args[0] == 'enterprise_access.apps.customer_billing.signals'
        ]

        self.assertEqual(len(signal_import_calls), 1)
