"""
Tests for automated paid-to-paid annual renewals of Teams and Essentials subscriptions.
"""
import uuid
from datetime import timedelta
from random import randint
from typing import Type, cast
from unittest import mock

import ddt
import stripe
from dateutil.relativedelta import relativedelta
from django.contrib.auth.models import AbstractUser
from django.test import TestCase
from django.utils import timezone
from edx_django_utils.cache import RequestCache
from edx_toggles.toggles.testutils import override_waffle_switch

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import SUBSCRIPTION_ITEM_TYPE
from enterprise_access.apps.customer_billing.models import (
    CheckoutIntent,
    SelfServiceSubscriptionRenewal,
    SspProduct,
    StripeEventSummary
)
from enterprise_access.apps.customer_billing.stripe_event_handlers import (
    AnnualRenewalError,
    StripeEventHandler,
    resolve_renewal_product
)
from enterprise_access.apps.customer_billing.tests.factories import (
    SelfServiceSubscriptionRenewalFactory,
    StripeEventDataFactory
)
from enterprise_access.apps.customer_billing.tests.utils import AttrDict
from enterprise_access.apps.customer_billing.utils import datetime_from_timestamp
from enterprise_access.toggles import SSP_PAID_RENEWAL

HANDLERS_MODULE = 'enterprise_access.apps.customer_billing.stripe_event_handlers'

TEAMS_SLUG = 'teams-yearly'
TEAMS_LOOKUP_KEY = 'teams_subscription_license_yearly'
TEAMS_PRICE_ID = 'price_teams_yearly'

AI_ACADEMY_UUID = uuid.uuid4()
DATA_ACADEMY_UUID = uuid.uuid4()
LEGACY_ACADEMY_UUID = uuid.uuid4()
ACADEMY_TITLES = {
    str(AI_ACADEMY_UUID): 'AI Academy',
    str(DATA_ACADEMY_UUID): 'Data Science Academy',
    str(LEGACY_ACADEMY_UUID): 'Legacy Leadership Academy',
}

# Essentials products, one per academy setup: (slug, lookup key, academy uuid, price id, is_active)
ESSENTIALS_PRODUCTS = [
    ('ai-academy-yearly', 'ai_academy_yearly', AI_ACADEMY_UUID, 'price_ai_academy', True),
    ('data-academy-yearly', 'data_academy_yearly', DATA_ACADEMY_UUID, 'price_data_academy', True),
    # A retired academy that existing subscribers are grandfathered onto.
    ('legacy-academy-yearly', 'legacy_academy_yearly', LEGACY_ACADEMY_UUID, 'price_legacy_academy', False),
]

STRIPE_SUBSCRIPTION_ID = 'sub_test_annual'
STRIPE_CUSTOMER_ID = 'cus_test_annual'
RENEWAL_INVOICE_ID = 'in_test_year_two'
FIRST_PAID_INVOICE_ID = 'in_test_first_paid'


def _academy_data(academy_uuid, **_kwargs):
    return {'title': ACADEMY_TITLES[str(academy_uuid)]}


@ddt.ddt
@override_waffle_switch(SSP_PAID_RENEWAL, True)
class TestAnnualRenewal(TestCase):
    """
    Tests for ``invoice.paid``, ``invoice.payment_failed`` and ``invoice.created`` handling of annual renewal
    invoices (``billing_reason == 'subscription_cycle'`` on an already paid subscription).

    The ``ssp_paid_renewal`` switch is on unless a test turns it off.
    """

    def setUp(self):
        super().setUp()
        # override_waffle_switch used as a context manager leaves its value in the request cache.
        self.addCleanup(RequestCache.clear_all_namespaces)
        self.checkout_intent = None
        self.essentials_products = {}
        for slug, lookup_key, academy_uuid, _, is_active in ESSENTIALS_PRODUCTS:
            self.essentials_products[slug] = SspProduct.objects.create(
                slug=slug,
                stripe_price_lookup_key=lookup_key,
                academy_uuid=academy_uuid,
                catalog_query_uuid=uuid.uuid4(),
                catalog_query_id=randint(100, 1000),
                license_manager_product_id_trial=3,
                license_manager_product_id_paid=4,
                is_active=is_active,
            )
        self.stripe_prices = {
            TEAMS_PRICE_ID: {'id': TEAMS_PRICE_ID, 'lookup_key': TEAMS_LOOKUP_KEY,
                             'metadata': {'ssp_product_slug': TEAMS_SLUG}},
        }
        for slug, lookup_key, _, price_id, _ in ESSENTIALS_PRODUCTS:
            self.stripe_prices[price_id] = {
                'id': price_id, 'lookup_key': lookup_key, 'metadata': {'ssp_product_slug': slug},
            }

        self.year_one_plan_uuid = uuid.uuid4()
        self.year_two_plan_uuid = uuid.uuid4()
        # Year 1 ends, and year 2 starts, at the renewal invoice's period start.
        self.period_start = timezone.now().replace(microsecond=0) - timedelta(hours=1)

        self._patch(f'{HANDLERS_MODULE}.stripe.Price.retrieve', side_effect=self._retrieve_price)
        self._patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data', side_effect=_academy_data)
        self.mock_receipt_email = self._patch(f'{HANDLERS_MODULE}.send_payment_receipt_email')
        self.mock_renewal_email = self._patch(f'{HANDLERS_MODULE}.send_paid_subscription_renewal_notice_email_task')
        self.mock_lm_client = self._patch(f'{HANDLERS_MODULE}.LicenseManagerApiClient', autospec=True).return_value
        self.mock_lm_client.create_subscription_plan_renewal.return_value = {'id': 777}
        self.mock_lm_client.process_subscription_plan_renewal.return_value = {
            'id': 777, 'renewed_subscription_plan': str(self.year_two_plan_uuid),
        }

    def _patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _retrieve_price(self, price_id, **_kwargs):
        if price_id not in self.stripe_prices:
            raise stripe.InvalidRequestError(f'No such price: {price_id}', 'id')
        return mock.Mock(to_dict=mock.Mock(return_value=self.stripe_prices[price_id]))

    def _set_up_paid_subscriber(self, product_slug=TEAMS_SLUG, with_paid_plan=True):
        """
        Create a checkout intent for ``product_slug`` that went through trial->paid: its first paid invoice
        was paid, and (unless ``with_paid_plan`` is False) its trial->paid renewal was processed into the
        year 1 plan.
        """
        self.checkout_intent = CheckoutIntent.create_intent(
            user=cast(Type[AbstractUser], UserFactory()),
            slug='renewing-enterprise',
            name='Renewing Enterprise',
            quantity=25,
            country='US',
            terms_metadata={'version': '1.0'},
            ssp_product=SspProduct.objects.get(slug=product_slug),
        )
        self.checkout_intent.stripe_customer_id = STRIPE_CUSTOMER_ID
        self.checkout_intent.enterprise_uuid = uuid.uuid4()
        self.checkout_intent.save()

        first_paid_event = StripeEventDataFactory(checkout_intent=self.checkout_intent, event_type='invoice.paid')
        summary = first_paid_event.summary
        summary.stripe_subscription_id = STRIPE_SUBSCRIPTION_ID
        summary.stripe_invoice_id = FIRST_PAID_INVOICE_ID
        summary.invoice_amount_paid = 50000
        summary.stripe_event_created_at = self.period_start - relativedelta(years=1)
        summary.save()

        if with_paid_plan:
            SelfServiceSubscriptionRenewalFactory(
                checkout_intent=self.checkout_intent,
                prior_subscription_plan_uuid=uuid.uuid4(),
                renewed_subscription_plan_uuid=self.year_one_plan_uuid,
                stripe_subscription_id=STRIPE_SUBSCRIPTION_ID,
                stripe_invoice_id=FIRST_PAID_INVOICE_ID,
                effective_date=self.period_start - relativedelta(years=1),
                processed_at=self.period_start - relativedelta(years=1),
            )

    def _renewal_invoice_event(
        self,
        event_type='invoice.paid',
        price_ids=(TEAMS_PRICE_ID,),
        quantity=25,
        billing_reason='subscription_cycle',
        invoice_id=RENEWAL_INVOICE_ID,
        lines=None,
        unknown_checkout_intent=False,
    ):
        """
        Build a renewal invoice event shaped like Stripe API 2025-03-31.basil and later, where invoice lines
        only carry the Price ID under ``pricing.price_details``. ``lines`` replaces the default invoice lines.
        """
        if lines is None:
            lines = [self._invoice_line(price_id, quantity, self.period_start) for price_id in price_ids or (None,)]
        amount = 0 if event_type == 'invoice.payment_failed' else 500000
        invoice_data = {
            'id': invoice_id,
            'object': 'invoice',
            'customer': STRIPE_CUSTOMER_ID,
            'billing_reason': billing_reason,
            'total': 500000,
            'amount_paid': amount,
            'currency': 'usd',
            'parent': {
                'subscription_details': {
                    'subscription': STRIPE_SUBSCRIPTION_ID,
                    'metadata': {
                        'checkout_intent_id': '999999' if unknown_checkout_intent else str(self.checkout_intent.id),
                        'checkout_intent_uuid': (
                            str(uuid.uuid4()) if unknown_checkout_intent else str(self.checkout_intent.uuid)
                        ),
                        'enterprise_customer_slug': self.checkout_intent.enterprise_slug,
                    },
                },
            },
            'lines': {'data': lines},
        }
        event = stripe.Event()
        event.id = f'evt_test_{event_type.replace(".", "_")}_{randint(1, 10 ** 9)}'
        event.created = int(timezone.now().timestamp())
        event.type = event_type
        event.data = stripe.StripeObject()
        event.data.object = AttrDict.wrap(invoice_data)
        return event

    def _invoice_line(self, price_id, quantity, start, proration=False, parent_type=SUBSCRIPTION_ITEM_TYPE):
        """
        An invoice line billing ``price_id`` (no Price if None) for one year from ``start``. ``parent_type``
        'invoice_item_details' is a one-off item.
        """
        parent = {'type': parent_type}
        if parent_type == SUBSCRIPTION_ITEM_TYPE:
            parent['subscription_item_details'] = {'proration': proration, 'subscription': STRIPE_SUBSCRIPTION_ID}
        else:
            parent['invoice_item_details'] = {'invoice_item': 'ii_test_adjustment'}
        pricing = {'unit_amount_decimal': '20000'}
        if price_id:
            pricing.update(type='price_details', price_details={'price': price_id, 'product': 'prod_test'})
        return {
            'object': 'line_item',
            'parent': parent,
            'quantity': quantity,
            'pricing': pricing,
            'period': {
                'start': int(start.timestamp()),
                'end': int((start + relativedelta(years=1)).timestamp()),
            },
        }

    def _assert_nothing_created(self):
        """Assert that no renewal was created or processed, locally or in License Manager."""
        self.mock_lm_client.create_subscription_plan_renewal.assert_not_called()
        self.mock_lm_client.process_subscription_plan_renewal.assert_not_called()
        self.mock_lm_client.update_subscription_plan.assert_not_called()
        self.assertFalse(SelfServiceSubscriptionRenewal.objects.filter(stripe_invoice_id=RENEWAL_INVOICE_ID).exists())
        self.mock_renewal_email.delay.assert_not_called()
        self.mock_receipt_email.delay.assert_not_called()

    @ddt.data(
        {'product_slug': TEAMS_SLUG, 'price_id': TEAMS_PRICE_ID,
         'expected_title': 'Renewing Enterprise 25 SUBS {month_year} - Teams PAID'},
        {'product_slug': 'ai-academy-yearly', 'price_id': 'price_ai_academy',
         'expected_title': 'Renewing Enterprise 25 SUBS {month_year} - Essentials AI Academy PAID'},
        {'product_slug': 'data-academy-yearly', 'price_id': 'price_data_academy',
         'expected_title': 'Renewing Enterprise 25 SUBS {month_year} - Essentials Data Science Academy PAID'},
        {'product_slug': 'legacy-academy-yearly', 'price_id': 'price_legacy_academy',
         'expected_title': 'Renewing Enterprise 25 SUBS {month_year} - Essentials Legacy Leadership Academy PAID'},
    )
    @ddt.unpack
    def test_invoice_paid_renews_onto_year_two(self, product_slug, price_id, expected_title):
        """
        A paid renewal invoice renews year 1 into a year 2 that License Manager creates from year 1 (so the
        same product, catalog and catalog query), with the same seats and dates anchored on year 1's end date.
        Both products are named with the ENT-12202 convention, with the month and year year 2 ends.
        """
        self._set_up_paid_subscriber(product_slug)
        year_two_end = self.period_start + relativedelta(years=1)
        expected_title = expected_title.format(month_year=year_two_end.strftime('%B %Y'))

        StripeEventHandler.dispatch(self._renewal_invoice_event(price_ids=(price_id,), quantity=25))

        self.mock_lm_client.create_subscription_plan_renewal.assert_called_once_with(
            prior_subscription_plan_uuid=str(self.year_one_plan_uuid),
            salesforce_opportunity_line_item_id=None,
            effective_date=self.period_start.isoformat(),
            renewed_expiration_date=(self.period_start + relativedelta(years=1)).isoformat(),
            number_of_licenses=25,
            renewed_plan_title=expected_title,
            exempt_from_batch_processing=True,
        )
        self.mock_lm_client.process_subscription_plan_renewal.assert_called_once_with(777)
        self.mock_lm_client.update_subscription_plan.assert_has_calls([
            mock.call(str(self.year_two_plan_uuid), is_active=True),
            mock.call(str(self.year_one_plan_uuid), expiration_date=self.period_start.isoformat()),
        ])

        renewal = SelfServiceSubscriptionRenewal.objects.get(stripe_invoice_id=RENEWAL_INVOICE_ID)
        self.assertEqual(renewal.checkout_intent, self.checkout_intent)
        self.assertEqual(renewal.subscription_plan_renewal_id, 777)
        self.assertEqual(renewal.prior_subscription_plan_uuid, self.year_one_plan_uuid)
        self.assertEqual(renewal.renewed_subscription_plan_uuid, self.year_two_plan_uuid)
        self.assertEqual(renewal.stripe_subscription_id, STRIPE_SUBSCRIPTION_ID)
        self.assertEqual(renewal.effective_date, self.period_start)
        self.assertIsNotNone(renewal.processed_at)
        self.mock_renewal_email.delay.assert_called_once()
        self.mock_receipt_email.delay.assert_called_once()
        self.assertEqual(self.mock_receipt_email.delay.call_args.kwargs['invoice_id'], RENEWAL_INVOICE_ID)

    def test_resolve_renewal_product_falls_back_to_price_lookup_key(self):
        """A Price without ssp_product_slug metadata resolves through its lookup key, never to a default."""
        self._set_up_paid_subscriber('ai-academy-yearly')
        self.stripe_prices['price_ai_academy']['metadata'] = {}
        invoice = self._renewal_invoice_event(price_ids=('price_ai_academy',)).data.object

        product = resolve_renewal_product(self.checkout_intent, invoice)

        self.assertEqual(product.slug, 'ai-academy-yearly')

    @ddt.data(
        # The invoice carries no Price at all.
        {'case': 'no_price', 'purchased': 'ai-academy-yearly', 'price_ids': ()},
        # The Price has no slug and its lookup key matches no product: no fallback to Teams.
        {'case': 'unknown_price', 'purchased': TEAMS_SLUG, 'price_ids': ('price_unmapped',)},
        # The slug is not a known SspProduct.
        {'case': 'unknown_slug', 'purchased': 'ai-academy-yearly', 'price_ids': ('price_mystery',)},
        # The Essentials customer is being charged the Teams price.
        {'case': 'teams_price_for_essentials', 'purchased': 'ai-academy-yearly', 'price_ids': (TEAMS_PRICE_ID,)},
        # The customer is being charged another academy's price.
        {'case': 'other_academy', 'purchased': 'ai-academy-yearly', 'price_ids': ('price_data_academy',)},
        # The Price metadata and its lookup key point at different products.
        {'case': 'conflicting_price', 'purchased': 'ai-academy-yearly', 'price_ids': ('price_conflicting',)},
        # Two prices for two products on the same invoice.
        {'case': 'two_products', 'purchased': 'ai-academy-yearly',
         'price_ids': ('price_ai_academy', 'price_data_academy')},
    )
    @ddt.unpack
    def test_invoice_paid_halts_on_missing_or_mismatched_product(self, case, purchased, price_ids):
        """A missing, unknown or mismatched product creates nothing, logs an error and raises for a retry."""
        self._set_up_paid_subscriber(purchased)
        self.stripe_prices['price_unmapped'] = {'id': 'price_unmapped', 'lookup_key': 'nope', 'metadata': {}}
        self.stripe_prices['price_mystery'] = {
            'id': 'price_mystery', 'lookup_key': None, 'metadata': {'ssp_product_slug': 'mystery-academy'},
        }
        self.stripe_prices['price_conflicting'] = {
            'id': 'price_conflicting', 'lookup_key': 'data_academy_yearly',
            'metadata': {'ssp_product_slug': 'ai-academy-yearly'},
        }

        with self.assertLogs(HANDLERS_MODULE, level='ERROR') as logs, self.assertRaises(AnnualRenewalError):
            StripeEventHandler.dispatch(self._renewal_invoice_event(price_ids=price_ids))

        halt_logs = [line for line in logs.output if '[SSPAnnualRenewal] outcome=halted' in line]
        self.assertEqual(len(halt_logs), 1, case)
        self.assertIn(str(self.checkout_intent.uuid), halt_logs[0])
        self.assertIn(RENEWAL_INVOICE_ID, halt_logs[0])
        self._assert_nothing_created()

    def test_invoice_paid_halts_when_academy_title_is_unavailable(self):
        """An Essentials renewal is not created without the academy name for the plan title."""
        self._set_up_paid_subscriber('ai-academy-yearly')

        with mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data', return_value={}):
            with self.assertRaises(AnnualRenewalError):
                StripeEventHandler.dispatch(self._renewal_invoice_event(price_ids=('price_ai_academy',)))

        self._assert_nothing_created()

    def test_invoice_paid_halts_without_a_processed_paid_plan(self):
        """Without a processed paid plan to renew there is nothing to renew from, so nothing is created."""
        self._set_up_paid_subscriber(with_paid_plan=False)

        with self.assertRaises(AnnualRenewalError):
            StripeEventHandler.dispatch(self._renewal_invoice_event())

        self._assert_nothing_created()

    def test_invoice_paid_replay_is_skipped(self):
        """
        Replaying a processed renewal invoice does not touch License Manager or email admins again. The renewal
        leaves a created and a processed audit log line, and the replay an already_processed one.
        """
        self._set_up_paid_subscriber()
        with self.assertLogs(HANDLERS_MODULE, level='INFO') as logs:
            StripeEventHandler.dispatch(self._renewal_invoice_event())
            self.mock_lm_client.reset_mock()
            self.mock_renewal_email.reset_mock()
            self.mock_receipt_email.reset_mock()

            StripeEventHandler.dispatch(self._renewal_invoice_event())

        self.mock_lm_client.create_subscription_plan_renewal.assert_not_called()
        self.mock_lm_client.process_subscription_plan_renewal.assert_not_called()
        self.mock_lm_client.update_subscription_plan.assert_not_called()
        self.mock_renewal_email.delay.assert_not_called()
        self.mock_receipt_email.delay.assert_not_called()
        self.assertEqual(SelfServiceSubscriptionRenewal.objects.filter(stripe_invoice_id=RENEWAL_INVOICE_ID).count(), 1)

        audit_logs = [line for line in logs.output if '[SSPAnnualRenewal]' in line]
        self.assertEqual(
            [line.split('outcome=')[1].split()[0] for line in audit_logs],
            ['created', 'processed', 'already_processed'],
        )
        for line in audit_logs:
            self.assertIn(f'checkout_intent_uuid={self.checkout_intent.uuid}', line)
            self.assertIn(f'enterprise_slug={self.checkout_intent.enterprise_slug}', line)
            self.assertIn(f'invoice_id={RENEWAL_INVOICE_ID}', line)
        self.assertIn(f'renewed_plan_uuid={self.year_two_plan_uuid}', audit_logs[1])

    @ddt.data('receipt', 'notice')
    def test_broker_error_queuing_an_email_is_retried_with_the_emails(self, failing_email):
        """
        A broker error while queuing a renewal email fails the webhook before the renewal is marked processed,
        so Stripe's retry finishes the renewal and sends both emails instead of skipping it as already processed.
        """
        self._set_up_paid_subscriber()
        failing_mock = self.mock_receipt_email if failing_email == 'receipt' else self.mock_renewal_email
        failing_mock.delay.side_effect = [Exception('broker is down'), None]

        with self.assertRaises(Exception):
            StripeEventHandler.dispatch(self._renewal_invoice_event())
        renewal = SelfServiceSubscriptionRenewal.objects.get(stripe_invoice_id=RENEWAL_INVOICE_ID)
        self.assertIsNone(renewal.processed_at)

        self.mock_receipt_email.reset_mock(side_effect=True)
        self.mock_renewal_email.reset_mock(side_effect=True)
        StripeEventHandler.dispatch(self._renewal_invoice_event())

        renewal.refresh_from_db()
        self.assertIsNotNone(renewal.processed_at)
        self.mock_receipt_email.delay.assert_called_once()
        self.mock_renewal_email.delay.assert_called_once()

    def test_renewal_term_comes_from_the_subscription_item_line(self):
        """
        Prorations and one-off invoice items (adjustments) on the renewal invoice don't set the renewed seats,
        dates or product: only the regular subscription item line does.
        """
        self._set_up_paid_subscriber()
        lines = [
            self._invoice_line(TEAMS_PRICE_ID, 5, self.period_start - timedelta(days=30), proration=True),
            # Not a Price we sell: it would halt the renewal if it were read as the renewed product.
            self._invoice_line('price_adjustment', 99, self.period_start - timedelta(days=60),
                               parent_type='invoice_item_details'),
            self._invoice_line(TEAMS_PRICE_ID, 25, self.period_start),
        ]

        StripeEventHandler.dispatch(self._renewal_invoice_event(lines=lines))

        kwargs = self.mock_lm_client.create_subscription_plan_renewal.call_args.kwargs
        self.assertEqual(kwargs['number_of_licenses'], 25)
        self.assertEqual(kwargs['effective_date'], self.period_start.isoformat())

    def test_grace_extension_halts_without_a_subscription_term_line(self):
        """
        A renewal invoice with only prorations and one-off items is an error, logged as outcome=halted. The webhook
        doesn't fail, since a retry can't fix it.
        """
        self._set_up_paid_subscriber()
        lines = [
            self._invoice_line(TEAMS_PRICE_ID, 5, self.period_start, proration=True),
            self._invoice_line(TEAMS_PRICE_ID, 25, self.period_start, parent_type='invoice_item_details'),
        ]

        with self.assertLogs(HANDLERS_MODULE, level='ERROR') as logs:
            StripeEventHandler.dispatch(self._renewal_invoice_event(event_type='invoice.payment_failed', lines=lines))

        self.assertTrue(any('outcome=halted' in line and 'no subscription term line' in line for line in logs.output))
        self.mock_lm_client.update_subscription_plan.assert_not_called()

    def test_paid_invoice_after_subscription_deleted_is_not_renewed(self):
        """
        A renewal invoice paid (or delivered) after customer.subscription.deleted doesn't bring the canceled
        subscription back. It is audit logged as halted for CS, and the webhook succeeds because a retry
        can't change the outcome.
        """
        self._set_up_paid_subscriber()
        self.checkout_intent.renewals.update(is_canceled=True)

        with self.assertLogs(HANDLERS_MODULE, level='ERROR') as logs:
            StripeEventHandler.dispatch(self._renewal_invoice_event())

        self._assert_nothing_created()
        halted = [line for line in logs.output if 'outcome=halted' in line]
        self.assertEqual(len(halted), 1)
        self.assertIn('the subscription is canceled', halted[0])

    def test_invoice_paid_halts_when_license_manager_returns_no_renewed_plan(self):
        """A processed renewal without a renewed plan is a halt: nothing is activated and no emails are sent."""
        self._set_up_paid_subscriber()
        self.mock_lm_client.process_subscription_plan_renewal.return_value = {'id': 777}

        with self.assertRaises(AnnualRenewalError):
            StripeEventHandler.dispatch(self._renewal_invoice_event())

        renewal = SelfServiceSubscriptionRenewal.objects.get(stripe_invoice_id=RENEWAL_INVOICE_ID)
        self.assertIsNone(renewal.processed_at)
        self.mock_lm_client.update_subscription_plan.assert_not_called()
        self.mock_receipt_email.delay.assert_not_called()
        self.mock_renewal_email.delay.assert_not_called()

    @ddt.data(
        # License Manager failed to process the renewal.
        ('process_subscription_plan_renewal', 0),
        # License Manager processed the renewal, then activating year 2 failed.
        ('update_subscription_plan', 0),
        # Year 2 was activated, then moving year 1's end date back failed.
        ('update_subscription_plan', 1),
    )
    @ddt.unpack
    def test_retry_resumes_a_failed_renewal(self, failing_call, failing_call_index):
        """
        A renewal that failed part way is finished by Stripe's retry on the same License Manager renewal and year 2
        plan: no second renewal is created (License Manager returns an already processed renewal unchanged). The
        failed attempt sends no emails; the retry clears the grace flag and sends them once.
        """
        self._set_up_paid_subscriber()
        self.checkout_intent.in_renewal_grace_period = True
        self.checkout_intent.save(update_fields=['in_renewal_grace_period'])
        getattr(self.mock_lm_client, failing_call).side_effect = (
            [mock.DEFAULT] * failing_call_index + [Exception('License Manager is down')] + [mock.DEFAULT] * 3
        )

        with self.assertRaises(Exception):
            StripeEventHandler.dispatch(self._renewal_invoice_event())
        renewal = SelfServiceSubscriptionRenewal.objects.get(stripe_invoice_id=RENEWAL_INVOICE_ID)
        self.assertIsNone(renewal.processed_at)
        self.checkout_intent.refresh_from_db()
        self.assertTrue(self.checkout_intent.in_renewal_grace_period)
        self.mock_receipt_email.delay.assert_not_called()
        self.mock_renewal_email.delay.assert_not_called()

        self.mock_lm_client.reset_mock()
        StripeEventHandler.dispatch(self._renewal_invoice_event())

        self.mock_lm_client.create_subscription_plan_renewal.assert_not_called()
        self.mock_lm_client.process_subscription_plan_renewal.assert_called_once_with(777)
        self.mock_lm_client.update_subscription_plan.assert_has_calls([
            mock.call(str(self.year_two_plan_uuid), is_active=True),
            mock.call(str(self.year_one_plan_uuid), expiration_date=self.period_start.isoformat()),
        ])
        renewal.refresh_from_db()
        self.assertIsNotNone(renewal.processed_at)
        self.assertEqual(renewal.renewed_subscription_plan_uuid, self.year_two_plan_uuid)
        self.checkout_intent.refresh_from_db()
        self.assertFalse(self.checkout_intent.in_renewal_grace_period)
        self.mock_receipt_email.delay.assert_called_once()
        self.mock_renewal_email.delay.assert_called_once()

    def test_payment_failed_for_unknown_checkout_intent_is_logged(self):
        """A failed invoice whose checkout intent can't be found is logged and ignored."""
        self._set_up_paid_subscriber()

        with self.assertLogs(HANDLERS_MODULE, level='ERROR') as logs:
            StripeEventHandler.dispatch(self._renewal_invoice_event(
                event_type='invoice.payment_failed', unknown_checkout_intent=True,
            ))

        self.assertTrue(any('could not find Checkout Intent' in line for line in logs.output))
        self.mock_lm_client.update_subscription_plan.assert_not_called()

    @ddt.data(
        {'product_slug': TEAMS_SLUG, 'price_id': TEAMS_PRICE_ID, 'product_name': 'Teams'},
        {'product_slug': 'ai-academy-yearly', 'price_id': 'price_ai_academy',
         'product_name': 'Essentials AI Academy'},
    )
    @ddt.unpack
    def test_renewed_plan_titles_are_unique_each_year(self, product_slug, price_id, product_name):
        """
        License Manager requires plan titles to be unique per customer agreement, so each renewal is named for
        its own term: the month and year it ends, and its seat count.
        """
        self._set_up_paid_subscriber(product_slug)
        year_two_start = self.period_start
        StripeEventHandler.dispatch(self._renewal_invoice_event(price_ids=(price_id,), quantity=25))

        # Year 3: a year later, the next invoice renews year 2, with more seats.
        self.period_start = year_two_start + relativedelta(years=1)
        self.mock_lm_client.process_subscription_plan_renewal.return_value = {
            'id': 777, 'renewed_subscription_plan': str(uuid.uuid4()),
        }
        StripeEventHandler.dispatch(self._renewal_invoice_event(
            price_ids=(price_id,), quantity=30, invoice_id='in_test_year_three',
        ))

        titles = [
            create_call.kwargs['renewed_plan_title']
            for create_call in self.mock_lm_client.create_subscription_plan_renewal.call_args_list
        ]
        self.assertEqual(titles, [
            f'Renewing Enterprise 25 SUBS {year_two_start + relativedelta(years=1):%B %Y} - {product_name} PAID',
            f'Renewing Enterprise 30 SUBS {self.period_start + relativedelta(years=1):%B %Y} - {product_name} PAID',
        ])
        self.assertEqual(
            self.mock_lm_client.create_subscription_plan_renewal.call_args_list[1].kwargs[
                'prior_subscription_plan_uuid'
            ],
            str(self.year_two_plan_uuid),
        )

    @ddt.data(
        {'event_type': 'invoice.paid', 'failing_call': 'process_subscription_plan_renewal', 'step': 'process'},
        {'event_type': 'invoice.payment_failed', 'failing_call': 'update_subscription_plan',
         'step': 'grace_extension'},
    )
    @ddt.unpack
    def test_unexpected_error_is_audit_logged_as_failed(self, event_type, failing_call, step):
        """
        An unexpected error, like a License Manager API error, is logged once as outcome=failed, so it can be
        monitored like a halt, and still fails the webhook so Stripe retries it.
        """
        self._set_up_paid_subscriber()
        getattr(self.mock_lm_client, failing_call).side_effect = Exception('License Manager is down')

        with self.assertLogs(HANDLERS_MODULE, level='ERROR') as logs, self.assertRaises(Exception):
            StripeEventHandler.dispatch(self._renewal_invoice_event(event_type=event_type))

        failed_logs = [line for line in logs.output if '[SSPAnnualRenewal] outcome=failed' in line]
        self.assertEqual(len(failed_logs), 1)
        self.assertIn(f'step={step}', failed_logs[0])
        self.assertIn('License Manager is down', failed_logs[0])
        self.assertIn(f'invoice_id={RENEWAL_INVOICE_ID}', failed_logs[0])

    @ddt.data('invoice.created', 'invoice.payment_failed')
    def test_grace_extension_without_paid_plan_halts_without_failing(self, event_type):
        """
        A created or failed renewal invoice with no paid plan to keep active is audit logged once, as halted, and
        doesn't fail the webhook: a retry can't fix it, and invoice.paid halts on the same problem.
        """
        self._set_up_paid_subscriber(with_paid_plan=False)

        with self.assertLogs(HANDLERS_MODULE, level='ERROR') as logs:
            StripeEventHandler.dispatch(self._renewal_invoice_event(event_type=event_type))

        audit_logs = [line for line in logs.output if '[SSPAnnualRenewal]' in line]
        self.assertEqual(len(audit_logs), 1)
        self.assertIn('outcome=halted', audit_logs[0])
        self.mock_lm_client.update_subscription_plan.assert_not_called()

    @ddt.data(
        ('invoice.payment_failed', True),
        ('invoice.created', True),
        ('invoice.payment_failed', False),
        ('invoice.created', False),
    )
    @ddt.unpack
    def test_year_one_kept_active_until_a_week_after_its_end(self, event_type, switch_on):
        """
        A created (not yet paid) or failed renewal invoice extends year 1 to an absolute date a week after its
        original end date, so repeated events do not keep pushing it out. With the ssp_paid_renewal switch off,
        year 1 is not extended.
        """
        self._set_up_paid_subscriber()

        with override_waffle_switch(SSP_PAID_RENEWAL, switch_on):
            StripeEventHandler.dispatch(self._renewal_invoice_event(event_type=event_type))
            StripeEventHandler.dispatch(self._renewal_invoice_event(event_type=event_type))

        grace_end = (self.period_start + timedelta(days=7)).isoformat()
        self.assertEqual(
            self.mock_lm_client.update_subscription_plan.call_args_list,
            [mock.call(str(self.year_one_plan_uuid), expiration_date=grace_end)] * 2 if switch_on else [],
        )
        self.mock_lm_client.create_subscription_plan_renewal.assert_not_called()
        self.checkout_intent.refresh_from_db()
        self.assertEqual(
            self.checkout_intent.in_renewal_grace_period, switch_on and event_type == 'invoice.payment_failed',
        )

    def test_payment_failed_after_renewal_is_ignored(self):
        """
        A late payment_failed for an invoice that is already renewed leaves year 1's end date alone and the
        grace period flag off.
        """
        self._set_up_paid_subscriber()
        StripeEventHandler.dispatch(self._renewal_invoice_event())
        self.mock_lm_client.reset_mock()

        StripeEventHandler.dispatch(self._renewal_invoice_event(event_type='invoice.payment_failed'))

        self.mock_lm_client.update_subscription_plan.assert_not_called()
        self.checkout_intent.refresh_from_db()
        self.assertFalse(self.checkout_intent.in_renewal_grace_period)

    @ddt.data('invoice.payment_failed', 'invoice.created')
    def test_late_grace_event_after_subscription_deleted_is_ignored(self, event_type):
        """
        A renewal invoice event handled after customer.subscription.deleted neither extends the deactivated
        year 1 plan nor flags the checkout intent as in the grace period.
        """
        self._set_up_paid_subscriber()
        self.checkout_intent.renewals.update(is_canceled=True)

        with self.assertLogs(HANDLERS_MODULE, level='INFO') as logs:
            StripeEventHandler.dispatch(self._renewal_invoice_event(event_type=event_type))

        self.mock_lm_client.update_subscription_plan.assert_not_called()
        self.checkout_intent.refresh_from_db()
        self.assertFalse(self.checkout_intent.in_renewal_grace_period)
        self.assertTrue(any('outcome=grace_skipped_canceled' in line for line in logs.output))

    def test_payment_failed_for_first_paid_invoice_is_ignored(self):
        """A failed trial->paid charge is not an annual renewal, and is left to the past_due handling."""
        self._set_up_paid_subscriber(with_paid_plan=False)
        StripeEventSummary.objects.filter(stripe_invoice_id=FIRST_PAID_INVOICE_ID).delete()

        StripeEventHandler.dispatch(self._renewal_invoice_event(event_type='invoice.payment_failed'))

        self.mock_lm_client.update_subscription_plan.assert_not_called()

    def test_payment_within_grace_period_keeps_original_dates(self):
        """
        After a failed charge, a payment during the grace week creates year 2 on year 1's original end date,
        ending one year later, and moves year 1's extended end date back to it. The failed charge sets
        in_renewal_grace_period (once, however often Stripe retries), and paying the renewal clears it.
        """
        self._set_up_paid_subscriber('ai-academy-yearly')
        failed_event = self._renewal_invoice_event(event_type='invoice.payment_failed', price_ids=('price_ai_academy',))
        with self.assertLogs(HANDLERS_MODULE, level='INFO') as logs:
            StripeEventHandler.dispatch(failed_event)
            StripeEventHandler.dispatch(failed_event)
        self.checkout_intent.refresh_from_db()
        self.assertTrue(self.checkout_intent.in_renewal_grace_period)
        self.assertEqual(sum('outcome=grace_period_started' in line for line in logs.output), 1)

        with mock.patch('django.utils.timezone.now', return_value=self.period_start + timedelta(days=5)):
            StripeEventHandler.dispatch(self._renewal_invoice_event(price_ids=('price_ai_academy',)))

        create_kwargs = self.mock_lm_client.create_subscription_plan_renewal.call_args.kwargs
        self.assertEqual(create_kwargs['effective_date'], self.period_start.isoformat())
        self.assertEqual(
            create_kwargs['renewed_expiration_date'], (self.period_start + relativedelta(years=1)).isoformat(),
        )
        grace_end = (self.period_start + timedelta(days=7)).isoformat()
        self.assertEqual(self.mock_lm_client.update_subscription_plan.call_args_list, [
            mock.call(str(self.year_one_plan_uuid), expiration_date=grace_end),
            mock.call(str(self.year_one_plan_uuid), expiration_date=grace_end),
            mock.call(str(self.year_two_plan_uuid), is_active=True),
            mock.call(str(self.year_one_plan_uuid), expiration_date=self.period_start.isoformat()),
        ])
        renewal = SelfServiceSubscriptionRenewal.objects.get(stripe_invoice_id=RENEWAL_INVOICE_ID)
        self.assertEqual(renewal.effective_date, datetime_from_timestamp(int(self.period_start.timestamp())))
        self.checkout_intent.refresh_from_db()
        self.assertFalse(self.checkout_intent.in_renewal_grace_period)
        self.assertEqual(self.checkout_intent.history.filter(in_renewal_grace_period=True).count(), 1)

    def test_switch_off_invoice_paid_only_emails_and_logs(self):
        """
        With the ssp_paid_renewal switch off, a paid renewal invoice keeps the behaviour from before automated
        renewals: the receipt and renewal notice go out, nothing is renewed, and an error is logged.
        """
        self._set_up_paid_subscriber()

        with override_waffle_switch(SSP_PAID_RENEWAL, False):
            with self.assertLogs(HANDLERS_MODULE, level='ERROR') as logs:
                StripeEventHandler.dispatch(self._renewal_invoice_event())

        self.mock_lm_client.create_subscription_plan_renewal.assert_not_called()
        self.mock_lm_client.process_subscription_plan_renewal.assert_not_called()
        self.mock_lm_client.update_subscription_plan.assert_not_called()
        self.assertFalse(SelfServiceSubscriptionRenewal.objects.filter(stripe_invoice_id=RENEWAL_INVOICE_ID).exists())
        self.mock_receipt_email.delay.assert_called_once()
        self.mock_renewal_email.delay.assert_called_once()
        self.assertEqual(len(logs.output), 1)
        self.assertIn('ssp_paid_renewal switch is off', logs.output[0])
        self.assertIn(str(self.checkout_intent.uuid), logs.output[0])
        self.assertIn(RENEWAL_INVOICE_ID, logs.output[0])
