"""Tests for the customer_billing Django admin configuration."""

from django.contrib import admin
from django.test import TestCase

from enterprise_access.apps.customer_billing.admin import CheckoutIntentAdmin, SspProductAdmin
from enterprise_access.apps.customer_billing.models import CheckoutIntent


class TestCheckoutIntentAdmin(TestCase):
    """Tests for the CheckoutIntent admin detail view."""

    def test_ssp_product_is_displayed_on_detail_view(self):
        admin_instance = CheckoutIntentAdmin(CheckoutIntent, admin.site)

        integration_fields = next(
            fieldset['fields']
            for name, fieldset in admin_instance.fieldsets
            if name == 'Integration Details'
        )

        self.assertIn('ssp_product', integration_fields)


class TestSspProductAdmin(TestCase):
    """Tests for the SspProduct admin configuration."""

    def test_ssp_product_admin_list_display_includes_marketing_url(self):
        """marketing_url is displayed in the SspProduct list view."""
        self.assertIn('marketing_url', SspProductAdmin.list_display)
