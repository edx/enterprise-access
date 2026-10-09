"""
Tests for the server-side checkout Segment events (order_completed / order_cancelled).
"""
from unittest import mock

import ddt
import stripe
from django.core.cache import cache
from django.test import TestCase, override_settings

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import ATTRIBUTION_KEYS, CheckoutSegmentEvents
from enterprise_access.apps.customer_billing.models import CheckoutIntent
from enterprise_access.apps.customer_billing.segment_payloads import (
    build_order_completed_properties,
    build_order_properties,
    get_payment_method_name
)
from enterprise_access.apps.customer_billing.stripe_api import get_stripe_invoice_for_segment
from enterprise_access.apps.customer_billing.stripe_event_handlers import enqueue_checkout_segment_event
from enterprise_access.apps.customer_billing.tasks import send_checkout_segment_event_task
from enterprise_access.apps.customer_billing.tests.utils import AttrDict

TASKS = 'enterprise_access.apps.customer_billing.tasks'
PII_KEYS = {
    'email', 'name_on_card', 'cardholder_name', 'billing_details', 'billing_address',
    'last4', 'customer_name', 'token', 'customer_email', 'address',
}


def make_invoice(total=10000, quantity=10, unit_amount='1250', charge=None, invoice_id='in_123'):
    """Build an invoice dict shaped like a retrieved (expanded) Stripe invoice."""
    payments = []
    if charge is not None:
        payments = [{'payment': {'payment_intent': {'latest_charge': charge}}}]
    return AttrDict.wrap({
        'id': invoice_id,
        'total': total,
        'currency': 'usd',
        'customer_email': 'pii@example.com',
        'lines': {'data': [{
            'quantity': quantity,
            'pricing': {'unit_amount_decimal': unit_amount, 'price_details': {'product': 'prod_abc'}},
        }]},
        'payments': {'data': payments},
    })


@ddt.ddt
@override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=True)
class TestCheckoutSegmentEvents(TestCase):
    """Payload and task behavior."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = UserFactory()
        self.intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme',
            attribution={key: f'{key}-value' for key in ATTRIBUTION_KEYS},
        )

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_revenue_is_invoice_total_not_price_times_quantity(self):
        # 10 licenses * $12.50 = $125.00, but a discount/proration brought the total to $87.35.
        invoice = make_invoice(total=8735, quantity=10, unit_amount='1250')
        props = build_order_properties(self.intent, invoice)
        self.assertEqual(props['revenue'], 87.35)
        self.assertEqual(props['price'], 12.5)
        self.assertEqual(props['total_quantity'], 10)
        self.assertEqual(props['order_id'], 'in_123')
        self.assertEqual(props['product_id'], 'prod_abc')
        self.assertEqual(props['category'], 'subscription')
        self.assertEqual(props['brand'], 'enterprise')
        self.assertEqual(props['slug'], self.intent.ssp_product.slug)

    def test_no_pii_keys_and_variant_omitted_for_teams(self):
        props = build_order_completed_properties(self.intent, make_invoice())
        self.assertFalse(PII_KEYS & set(props))
        self.assertNotIn('pii@example.com', str(props))
        self.assertEqual(props['name'], 'teams')
        self.assertNotIn('variant', props)

    def test_variant_present_for_essentials(self):
        product = self.intent.ssp_product
        with mock.patch.object(type(product), 'academy_uuid', new_callable=mock.PropertyMock, create=True), \
                mock.patch.object(type(product), 'academy_title', new_callable=mock.PropertyMock) as title, \
                mock.patch('enterprise_access.apps.customer_billing.segment_payloads.get_product_type',
                           return_value='essentials'):
            title.return_value = 'AI Academy'
            props = build_order_properties(self.intent, make_invoice())
        self.assertEqual(props['name'], 'essentials')
        self.assertEqual(props['variant'], 'AI Academy')

    @ddt.data(
        ({'type': 'card', 'card': {}}, 'Credit Card'),
        ({'type': 'card', 'card': {'wallet': {'type': 'apple_pay'}}}, 'Apple Pay'),
        ({'type': 'card', 'card': {'wallet': {'type': 'google_pay'}}}, 'Google Pay'),
        ({'type': 'us_bank_account'}, 'ACH'),
        ({'type': 'link'}, 'Link'),
        ({'type': 'sepa_debit'}, None),
    )
    @ddt.unpack
    def test_payment_method_mapping(self, details, expected):
        invoice = make_invoice(charge={'payment_method_details': details})
        self.assertEqual(get_payment_method_name(invoice), expected)

    @ddt.data(
        {'charge': None},  # no payments
        {'charge': 'ch_unexpanded'},  # charge not expanded
        {'charge': {'payment_method_details': {'type': 'card'}}, 'total': 0},  # $0 invoice
    )
    def test_payment_method_omitted_when_unavailable(self, kwargs):
        invoice = make_invoice(**kwargs)
        props = build_order_properties(self.intent, invoice)
        self.assertNotIn('payment_method', props)

    def test_attribution_passed_through_on_order_completed_only(self):
        completed = build_order_completed_properties(self.intent, make_invoice())
        for key in ATTRIBUTION_KEYS:
            self.assertEqual(completed[key], f'{key}-value')
        cancelled = build_order_properties(self.intent, make_invoice())
        self.assertFalse(set(ATTRIBUTION_KEYS) & set(cancelled))

    @ddt.data(CheckoutSegmentEvents.ORDER_COMPLETED, CheckoutSegmentEvents.ORDER_CANCELLED)
    @mock.patch(f'{TASKS}.track_event')
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    def test_task_sends_once_per_order_and_event(self, event_name, mock_invoice, mock_track):
        mock_invoice.return_value = make_invoice()
        for _ in range(3):  # webhook redelivery / reload
            send_checkout_segment_event_task(event_name, self.intent.id, 'in_123')
        mock_track.assert_called_once()
        self.assertEqual(mock_track.call_args.kwargs['event_name'], event_name)
        self.assertEqual(mock_track.call_args.kwargs['properties']['order_id'], 'in_123')

    @mock.patch(f'{TASKS}.track_event')
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    def test_same_order_different_events_both_sent(self, mock_invoice, mock_track):
        mock_invoice.return_value = make_invoice()
        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_CANCELLED, self.intent.id, 'in_123')
        self.assertEqual(mock_track.call_count, 2)

    @override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=False)
    @mock.patch(f'{TASKS}.track_event')
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    def test_flag_off_sends_nothing(self, mock_invoice, mock_track):
        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        mock_invoice.assert_not_called()
        mock_track.assert_not_called()

    @mock.patch(f'{TASKS}.track_event')
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    def test_stripe_failure_retries_and_leaves_no_idempotency_key(self, mock_invoice, mock_track):
        mock_invoice.side_effect = stripe.APIConnectionError('boom')
        with mock.patch.object(send_checkout_segment_event_task, 'retry', side_effect=stripe.APIConnectionError('x')) \
                as mock_retry:
            with self.assertRaises(stripe.APIConnectionError):
                send_checkout_segment_event_task.apply(
                    args=(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123'), throw=True,
                ).get()
            self.assertTrue(mock_retry.called)
        mock_track.assert_not_called()
        mock_invoice.side_effect = None
        mock_invoice.return_value = make_invoice()
        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        mock_track.assert_called_once()

    @mock.patch(f'{TASKS}.track_event', side_effect=RuntimeError('segment down'))
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    def test_segment_failure_releases_key(self, mock_invoice, _mock_track):
        mock_invoice.return_value = make_invoice()
        with self.assertRaises(RuntimeError):
            send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        self.assertFalse(cache.get(f'checkout-segment-event:{CheckoutSegmentEvents.ORDER_COMPLETED}:in_123'))

    @override_settings(SEGMENT_KEY='test-key')
    @mock.patch('enterprise_access.apps.track.segment.analytics')
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    def test_real_segment_error_reaches_task_and_event_is_retried(self, mock_invoice, mock_analytics):
        """track_event used to swallow Segment errors, leaving the dedupe key set so the event was never retried."""
        mock_invoice.return_value = make_invoice()
        mock_analytics.track.side_effect = [RuntimeError('segment down'), None]
        cache_key = f'checkout-segment-event:{CheckoutSegmentEvents.ORDER_COMPLETED}:in_123'

        with self.assertRaises(RuntimeError):
            send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        self.assertIsNone(cache.get(cache_key))

        # The retry / Stripe redelivery is not dropped as a duplicate.
        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        self.assertEqual(mock_analytics.track.call_count, 2)
        self.assertTrue(cache.get(cache_key))

    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.send_checkout_segment_event_task')
    def test_enqueue_never_raises_when_broker_fails(self, mock_task):
        mock_task.delay.side_effect = ConnectionError('broker down')
        enqueue_checkout_segment_event(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent, 'in_123')

    @override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=False)
    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.send_checkout_segment_event_task')
    def test_enqueue_flag_off(self, mock_task):
        enqueue_checkout_segment_event(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent, 'in_123')
        mock_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Invoice.retrieve')
    def test_invoice_fetch_falls_back_without_expand_on_invalid_request(self, mock_retrieve):
        """An invalid expand path makes Stripe reject the request; the unexpanded fetch is used instead."""
        unexpanded = make_invoice()
        mock_retrieve.side_effect = [stripe.InvalidRequestError('bad expand', 'expand'), unexpanded]

        invoice = get_stripe_invoice_for_segment('in_123')

        self.assertIs(invoice, unexpanded)
        self.assertEqual(mock_retrieve.call_count, 2)
        self.assertIn('expand', mock_retrieve.call_args_list[0].kwargs)
        mock_retrieve.assert_called_with('in_123')

    @mock.patch(f'{TASKS}.track_event')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Invoice.retrieve')
    def test_event_sent_without_payment_method_when_expand_fails(self, mock_retrieve, mock_track):
        mock_retrieve.side_effect = [stripe.InvalidRequestError('bad expand', 'expand'), make_invoice()]

        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')

        mock_track.assert_called_once()
        self.assertNotIn('payment_method', mock_track.call_args.kwargs['properties'])

    @mock.patch(f'{TASKS}.track_event')
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    def test_skipped_with_warning_when_user_has_no_lms_user_id(self, mock_invoice, mock_track):
        """The local DB user id is not an LMS user id, so it must never be used as a fallback."""
        self.user.lms_user_id = None
        self.user.save()

        with self.assertLogs(TASKS, level='WARNING') as logs:
            send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')

        mock_track.assert_not_called()
        mock_invoice.assert_not_called()
        self.assertIn(str(self.intent.id), '\n'.join(logs.output))

    @mock.patch(f'{TASKS}.track_event')
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    def test_missing_checkout_intent_is_logged_and_not_retried(self, mock_invoice, mock_track):
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry, \
                self.assertLogs(TASKS, level='ERROR'):
            send_checkout_segment_event_task.apply(
                args=(CheckoutSegmentEvents.ORDER_COMPLETED, 999999, 'in_123'), throw=True,
            ).get()
        mock_retry.assert_not_called()
        mock_invoice.assert_not_called()
        mock_track.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.send_checkout_segment_event_task')
    def test_enqueue_logs_and_skips_without_invoice_id(self, mock_task):
        with self.assertLogs('enterprise_access.apps.customer_billing.stripe_event_handlers', level='WARNING') as logs:
            enqueue_checkout_segment_event(CheckoutSegmentEvents.ORDER_CANCELLED, self.intent, None)
        mock_task.delay.assert_not_called()
        self.assertIn(str(self.intent.id), '\n'.join(logs.output))

    @ddt.data(
        ('https://www.google.com/search?email=foo@bar.com&q=x#frag', 'https://www.google.com/search'),
        ('https://example.com/page?email=foo@bar.com', 'https://example.com/page'),
        ('https://example.com/page#token=abc', 'https://example.com/page'),
        ('https://example.com/page', 'https://example.com/page'),
    )
    @ddt.unpack
    def test_referrer_query_and_fragment_stripped(self, referrer, expected):
        self.intent.attribution = {'referrer': referrer, 'utm_source': 'google'}
        props = build_order_completed_properties(self.intent, make_invoice())
        self.assertEqual(props['referrer'], expected)
        self.assertNotIn('foo@bar.com', str(props))
        self.assertEqual(props['utm_source'], 'google')

    @mock.patch(f'{TASKS}.track_event')
    @mock.patch(f'{TASKS}.get_stripe_invoice_for_segment')
    @mock.patch(f'{TASKS}.cache')
    def test_concurrent_claim_skips_send(self, mock_cache, mock_invoice, mock_track):
        """If another worker claimed the key first, cache.add returns False and nothing is sent."""
        mock_cache.get.return_value = None
        mock_cache.add.return_value = False
        mock_invoice.return_value = make_invoice()

        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')

        mock_track.assert_not_called()
        mock_cache.delete.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.send_checkout_segment_event_task')
    def test_enqueue_queues_task_with_expected_kwargs(self, mock_task):
        enqueue_checkout_segment_event(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent, 'in_123')
        mock_task.delay.assert_called_once_with(
            event_name=CheckoutSegmentEvents.ORDER_COMPLETED,
            checkout_intent_id=self.intent.id,
            invoice_id='in_123',
        )

    def test_create_intent_merges_attribution_on_existing_intent(self):
        """A second create_intent for the same user merges new attribution into the existing intent."""
        intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme',
            attribution={'utm_source': 'bing'},
        )
        intent.refresh_from_db()
        self.assertEqual(intent.id, self.intent.id)
        self.assertEqual(intent.attribution['utm_source'], 'bing')
        self.assertEqual(intent.attribution['utm_medium'], 'utm_medium-value')

    def test_payload_tolerates_missing_invoice_fields(self):
        """Missing lines/pricing/currency must not raise; None values are dropped from the payload."""
        invoice = AttrDict.wrap({'id': 'in_sparse', 'total': None, 'lines': {'data': []}, 'payments': None})
        props = build_order_properties(self.intent, invoice)
        self.assertEqual(props['order_id'], 'in_sparse')
        self.assertEqual(props['total_quantity'], self.intent.quantity)
        self.assertNotIn('revenue', props)
        self.assertNotIn('currency', props)
        self.assertNotIn('price', props)
