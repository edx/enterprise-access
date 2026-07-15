"""Tests for the customer_billing Django admin configuration."""

from unittest import mock
from uuid import uuid4

import stripe
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from enterprise_access.apps.customer_billing.admin import CheckoutIntentAdmin, SspProductAdmin
from enterprise_access.apps.customer_billing.models import CheckoutIntent, SspProduct
from enterprise_access.apps.customer_billing.tests.factories import (
    CheckoutIntentFactory,
    SelfServiceSubscriptionRenewalFactory
)


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


class TestBackfillSspProductSlug(TestCase):
    """Tests for the backfill_ssp_product_slug_to_stripe admin action."""

    def setUp(self):
        self.product, _ = SspProduct.objects.get_or_create(
            slug='teams-yearly',
            defaults={
                'stripe_price_lookup_key': 'teams-yearly-key',
                'catalog_query_uuid': uuid4(),
            },
        )
        self.intent = CheckoutIntentFactory(ssp_product=self.product)
        self.renewal = SelfServiceSubscriptionRenewalFactory(
            checkout_intent=self.intent,
            stripe_subscription_id='sub_test123',
        )
        self.admin_instance = CheckoutIntentAdmin(CheckoutIntent, admin.site)

    def _make_request(self):
        request = RequestFactory().get('/')
        request.session = self.client.session
        request._messages = FallbackStorage(request)  # pylint: disable=protected-access
        return request

    @mock.patch('enterprise_access.apps.customer_billing.admin.stripe.Subscription.modify')
    def test_patches_stripe_metadata(self, mock_modify):
        """Action calls stripe.Subscription.modify with ssp_product_slug for each intent."""
        queryset = CheckoutIntent.objects.filter(pk=self.intent.pk)
        self.admin_instance.backfill_ssp_product_slug_to_stripe(self._make_request(), queryset)
        mock_modify.assert_called_once_with('sub_test123', metadata={'ssp_product_slug': 'teams-yearly'})

    @mock.patch('enterprise_access.apps.customer_billing.admin.stripe.Subscription.modify')
    def test_skips_intent_with_no_renewal(self, mock_modify):
        """Action skips intents that have no renewal with a subscription ID."""
        intent_no_renewal = CheckoutIntentFactory(ssp_product=self.product)
        queryset = CheckoutIntent.objects.filter(pk=intent_no_renewal.pk)
        self.admin_instance.backfill_ssp_product_slug_to_stripe(self._make_request(), queryset)
        mock_modify.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.admin.stripe.Subscription.modify')
    def test_continues_on_stripe_error(self, mock_modify):
        """Action logs the error and continues when Stripe raises, without re-raising."""
        mock_modify.side_effect = stripe.StripeError('boom')
        queryset = CheckoutIntent.objects.filter(pk=self.intent.pk)
        # Should not raise
        self.admin_instance.backfill_ssp_product_slug_to_stripe(self._make_request(), queryset)
        mock_modify.assert_called_once()
