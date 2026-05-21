"""Tests for customer_billing app configuration."""

import importlib
from unittest import mock

from django.test import SimpleTestCase

from enterprise_access.apps.customer_billing.apps import CustomerBillingConfig


class TestCustomerBillingConfig(SimpleTestCase):
    """Verify app startup wiring behavior."""

    def setUp(self):
        super().setUp()
        # Reset the class-level flag so each test starts clean.
        CustomerBillingConfig._signals_wired = False  # pylint: disable=protected-access

    def tearDown(self):
        super().tearDown()
        CustomerBillingConfig._signals_wired = False  # pylint: disable=protected-access

    def test_ready_wires_signals_only_once(self):
        """Calling ready multiple times should import signal handlers only once."""
        app_module = importlib.import_module('enterprise_access.apps.customer_billing')
        config = CustomerBillingConfig('customer_billing', app_module)

        with mock.patch('builtins.__import__', wraps=__import__) as mock_import:
            config.ready()
            config.ready()

        signal_import_calls = [
            call
            for call in mock_import.call_args_list
            if call.args and call.args[0] == 'enterprise_access.apps.customer_billing.signals'
        ]

        self.assertEqual(len(signal_import_calls), 1)

    def test_ready_guard_is_class_level_not_instance_level(self):
        """A second AppConfig instance should not re-wire signals after the first wired them."""
        app_module = importlib.import_module('enterprise_access.apps.customer_billing')
        config1 = CustomerBillingConfig('customer_billing', app_module)
        config2 = CustomerBillingConfig('customer_billing', app_module)

        with mock.patch('builtins.__import__', wraps=__import__) as mock_import:
            config1.ready()  # wires signals, sets class flag
            config2.ready()  # must be a no-op due to class-level guard

        signal_import_calls = [
            call
            for call in mock_import.call_args_list
            if call.args and call.args[0] == 'enterprise_access.apps.customer_billing.signals'
        ]

        self.assertEqual(len(signal_import_calls), 1)
