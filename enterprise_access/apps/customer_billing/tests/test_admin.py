"""Tests for the customer_billing Django admin configuration."""

from unittest import mock
from uuid import uuid4

import ddt
import stripe
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.urls import reverse

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.admin import CheckoutIntentAdmin, SspProductAdmin
from enterprise_access.apps.customer_billing.constants import LearnerCreditTopUpState
from enterprise_access.apps.customer_billing.models import (
    CheckoutIntent,
    EnterpriseStripeCustomer,
    LearnerCreditTopUp,
    SspProduct
)
from enterprise_access.apps.customer_billing.tests.factories import (
    CheckoutIntentFactory,
    EnterpriseStripeCustomerFactory,
    LearnerCreditTopUpFactory,
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

    def test_renewal_grace_period_is_shown_read_only_and_filterable(self):
        """
        Support can see and filter by in_renewal_grace_period, but only the Stripe webhooks change it (ENT-11531).
        """
        admin_instance = CheckoutIntentAdmin(CheckoutIntent, admin.site)

        status_fields = next(
            fieldset['fields']
            for name, fieldset in admin_instance.fieldsets
            if name == 'Status'
        )

        self.assertIn('in_renewal_grace_period', status_fields)
        self.assertIn('in_renewal_grace_period', admin_instance.readonly_fields)
        self.assertIn('in_renewal_grace_period', admin_instance.list_filter)
        self.assertIn('in_renewal_grace_period', admin_instance.list_display)

    def test_renewal_grace_period_on_change_page(self):
        """The change page renders the flag as read-only text, not a checkbox."""
        self.client.force_login(UserFactory(is_staff=True, is_superuser=True))
        intent = CheckoutIntentFactory(in_renewal_grace_period=True)

        response = self.client.get(
            reverse('admin:customer_billing_checkoutintent_change', args=[intent.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'In renewal grace period')
        self.assertNotContains(response, 'name="in_renewal_grace_period"')


class TestSspProductAdmin(TestCase):
    """Tests for the SspProduct admin configuration."""

    def test_ssp_product_admin_list_display_includes_marketing_url(self):
        """marketing_url is displayed in the SspProduct list view."""
        self.assertIn('marketing_url', SspProductAdmin.list_display)


class TestEnterpriseStripeCustomerAdmin(TestCase):
    """
    The EnterpriseStripeCustomer admin shows and searches mappings but can't add, change, delete or revert them
    (ENT-12509).
    """

    def test_mappings_can_be_viewed_and_searched_but_not_edited(self):
        self.client.force_login(UserFactory(is_staff=True, is_superuser=True))
        mapping = EnterpriseStripeCustomerFactory(stripe_customer_id='cus_mapped')
        EnterpriseStripeCustomerFactory(stripe_customer_id='cus_other')
        changelist_url = reverse('admin:customer_billing_enterprisestripecustomer_changelist')
        change_url = reverse('admin:customer_billing_enterprisestripecustomer_change', args=[mapping.pk])

        search_response = self.client.get(changelist_url, {'q': str(mapping.enterprise_customer_uuid)})
        detail_response = self.client.get(change_url)
        save_response = self.client.post(change_url, {'stripe_customer_id': 'cus_changed'})

        self.assertEqual(list(search_response.context['cl'].result_list), [mapping])
        self.assertContains(detail_response, 'cus_mapped')
        self.assertNotContains(detail_response, 'name="_save"')
        history_url = reverse('admin:customer_billing_enterprisestripecustomer_history', args=[mapping.pk])
        self.assertContains(self.client.get(history_url), 'cus_mapped')
        revert_url = reverse(
            'admin:customer_billing_enterprisestripecustomer_simple_history',
            args=[mapping.pk, mapping.history.get().history_id],
        )
        self.assertEqual(self.client.post(revert_url, {}).status_code, 403)
        self.assertEqual(save_response.status_code, 403)
        self.assertEqual(
            self.client.get(reverse('admin:customer_billing_enterprisestripecustomer_add')).status_code, 403,
        )
        self.assertEqual(
            self.client.get(reverse('admin:customer_billing_enterprisestripecustomer_delete', args=[mapping.pk]))
            .status_code,
            403,
        )
        self.assertEqual(EnterpriseStripeCustomer.objects.get(pk=mapping.pk).stripe_customer_id, 'cus_mapped')


@ddt.ddt
class TestLearnerCreditTopUpAdmin(TestCase):
    """
    The LearnerCreditTopUp admin shows and searches top-ups and their history, but can't add, change, delete or
    revert one: each row is a payment audit record whose state changes only through transition_to().
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(UserFactory(is_staff=True, is_superuser=True))
        self.top_up = LearnerCreditTopUpFactory(
            state=LearnerCreditTopUpState.DEPOSITED,
            stripe_payment_intent_id='pi_admin_lookup',
            deposit_uuid=uuid4(),
        )
        LearnerCreditTopUpFactory()
        self.changelist_url = reverse('admin:customer_billing_learnercredittopup_changelist')

    @ddt.data(
        'uuid',
        'enterprise_customer_uuid',
        'subsidy_access_policy_id',
        'subsidy_uuid',
        'stripe_customer_id',
        'stripe_payment_intent_id',
        'deposit_uuid',
    )
    def test_search_finds_the_top_up_by(self, attname):
        response = self.client.get(self.changelist_url, {'q': str(getattr(self.top_up, attname))})

        self.assertEqual(list(response.context['cl'].result_list), [self.top_up])

    def test_top_up_and_its_history_can_be_viewed_but_not_changed(self):
        pk = self.top_up.pk
        version = self.top_up.history.get()
        change_url = reverse('admin:customer_billing_learnercredittopup_change', args=[pk])
        history_url = reverse('admin:customer_billing_learnercredittopup_history', args=[pk])
        revert_url = reverse('admin:customer_billing_learnercredittopup_simple_history', args=[pk, version.history_id])

        detail_response = self.client.get(change_url)
        self.assertContains(detail_response, 'pi_admin_lookup')
        self.assertNotContains(detail_response, 'name="_save"')
        self.assertContains(self.client.get(history_url), revert_url)
        version_response = self.client.get(revert_url)
        self.assertContains(version_response, 'pi_admin_lookup')
        self.assertNotContains(version_response, 'name="initiated_by"')

        self.assertEqual(self.client.post(change_url, {'state': LearnerCreditTopUpState.FULFILLED}).status_code, 403)
        self.assertEqual(self.client.post(revert_url, {}).status_code, 403)
        self.assertEqual(self.client.get(reverse('admin:customer_billing_learnercredittopup_add')).status_code, 403)
        self.assertEqual(
            self.client.get(reverse('admin:customer_billing_learnercredittopup_delete', args=[pk])).status_code, 403,
        )
        stored = LearnerCreditTopUp.objects.get(pk=pk)
        self.assertEqual(stored.state, LearnerCreditTopUpState.DEPOSITED)
        self.assertEqual(stored.history.count(), 1)


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
