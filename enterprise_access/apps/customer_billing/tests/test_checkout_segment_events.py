"""
Tests for the server-side checkout Segment events (order_completed / order_cancelled).
"""
from unittest import mock

import ddt
import requests
import stripe
from analytics.request import APIError as SegmentAPIError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from edx_django_utils.cache import TieredCache

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import ATTRIBUTION_KEYS, CheckoutSegmentEvents
from enterprise_access.apps.customer_billing.models import CheckoutIntent, SegmentEventEmission
from enterprise_access.apps.customer_billing.pricing_api import StripePricingError
from enterprise_access.apps.customer_billing.segment_payloads import (
    build_order_completed_properties,
    build_order_properties,
    get_payment_schedule
)
from enterprise_access.apps.customer_billing.stripe_event_handlers import enqueue_checkout_segment_event
from enterprise_access.apps.customer_billing.tasks import SegmentSendError, send_checkout_segment_event_task
from enterprise_access.apps.customer_billing.tests.utils import AttrDict

TASKS = 'enterprise_access.apps.customer_billing.tasks'
# Every key a checkout payload may carry. A new payload key must be added here deliberately, after checking that
# it carries no PII.
APPROVED_PAYLOAD_KEYS = {
    'product_id', 'sku', 'category', 'name', 'brand', 'variant', 'price', 'slug', 'payment_schedule',
    'order_id', 'total_quantity', 'revenue', 'currency', *ATTRIBUTION_KEYS,
}
PII_KEYS = {
    'email', 'name_on_card', 'cardholder_name', 'billing_details', 'billing_address',
    'last4', 'customer_name', 'token', 'customer_email', 'address',
}


def make_line(quantity=10, unit_amount='1250', product='prod_abc', proration=False):
    """Build an invoice line shaped like Stripe's: prorations share the subscription item parent type."""
    return {
        'quantity': quantity,
        'parent': {'type': 'subscription_item_details', 'subscription_item_details': {'proration': proration}},
        'pricing': {'unit_amount_decimal': unit_amount, 'price_details': {'product': product}},
    }


def make_invoice(total=10000, quantity=10, unit_amount='1250', invoice_id='in_123'):
    """Build an invoice dict shaped like a Stripe invoice retrieved without ``expand``."""
    return AttrDict.wrap({
        'id': invoice_id,
        'total': total,
        'currency': 'usd',
        'customer_email': 'pii@example.com',
        'lines': {'data': [make_line(quantity=quantity, unit_amount=unit_amount)]},
    })


@ddt.ddt
class TestCheckoutSegmentEvents(TestCase):
    """Payload and task behavior."""

    def setUp(self):
        super().setUp()
        self.switch_patcher = mock.patch(
            'enterprise_access.toggles.CHECKOUT_SEGMENT_EVENTS_V2.is_enabled', return_value=True,
        )
        self.switch_patcher.start()
        TieredCache.dangerous_clear_all_tiers()  # get_stripe_invoice caches invoices
        self.user = UserFactory()
        self.intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme',
            attribution={key: f'{key}-value' for key in ATTRIBUTION_KEYS},
        )

    def tearDown(self):
        self.switch_patcher.stop()
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

    def test_attribution_passed_through_on_order_completed_only(self):
        completed = build_order_completed_properties(self.intent, make_invoice())
        for key in ATTRIBUTION_KEYS:
            self.assertEqual(completed[key], f'{key}-value')
        cancelled = build_order_properties(self.intent, make_invoice())
        self.assertFalse(set(ATTRIBUTION_KEYS) & set(cancelled))

    @ddt.data(CheckoutSegmentEvents.ORDER_COMPLETED, CheckoutSegmentEvents.ORDER_CANCELLED)
    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_task_sends_once_per_order_and_event(self, event_name, mock_invoice, mock_send):
        mock_invoice.return_value = make_invoice()
        for _ in range(3):  # webhook redelivery / reload
            send_checkout_segment_event_task(event_name, self.intent.id, 'in_123')
        mock_invoice.assert_called_once()  # redeliveries skip the Stripe fetch
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs['event_name'], event_name)
        self.assertEqual(mock_send.call_args.kwargs['properties']['order_id'], 'in_123')
        # Deterministic, so Segment drops a resend after a success that failed to be recorded.
        self.assertEqual(mock_send.call_args.kwargs['message_id'], f'{event_name}:in_123')

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_same_order_different_events_both_sent(self, mock_invoice, mock_send):
        mock_invoice.return_value = make_invoice()
        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_CANCELLED, self.intent.id, 'in_123')
        self.assertEqual(mock_send.call_count, 2)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_flag_off_sends_nothing(self, mock_invoice, mock_send):
        with mock.patch('enterprise_access.toggles.CHECKOUT_SEGMENT_EVENTS_V2.is_enabled', return_value=False):
            send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        mock_invoice.assert_not_called()
        mock_send.assert_not_called()

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_stripe_failure_retries_and_leaves_no_emission(self, mock_invoice, mock_send):
        mock_invoice.side_effect = stripe.APIConnectionError('boom')
        with mock.patch.object(send_checkout_segment_event_task, 'retry', side_effect=stripe.APIConnectionError('x')) \
                as mock_retry:
            with self.assertRaises(stripe.APIConnectionError):
                send_checkout_segment_event_task.apply(
                    args=(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123'), throw=True,
                ).get()
            self.assertTrue(mock_retry.called)
        mock_send.assert_not_called()
        mock_invoice.side_effect = None
        mock_invoice.return_value = make_invoice()
        send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        mock_send.assert_called_once()

    @override_settings(SEGMENT_KEY='test-key')
    @mock.patch('analytics.request._session')
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_segment_outage_is_retryable_and_marked_emitted_only_after_success(self, mock_invoice, mock_session):
        """
        Runs the real analytics-python client with only its HTTP session mocked: a Segment outage reaches the task
        (it is not swallowed on a background thread), and the emission is recorded only once Segment returns 200.
        """
        mock_invoice.return_value = make_invoice()
        mock_session.post.return_value = mock.Mock(status_code=503, **{'json.return_value': {
            'code': 'unavailable', 'message': 'Segment is down',
        }})
        event = CheckoutSegmentEvents.ORDER_COMPLETED

        with self.assertRaises(SegmentSendError):
            send_checkout_segment_event_task(event, self.intent.id, 'in_123')
        emission = SegmentEventEmission.objects.get(event_name=event, order_id='in_123')
        self.assertIsNone(emission.emitted_at)  # a failed send is never recorded as sent

        mock_session.post.return_value = mock.Mock(status_code=200)
        send_checkout_segment_event_task(event, self.intent.id, 'in_123')
        emission.refresh_from_db()
        self.assertIsNotNone(emission.emitted_at)
        send_checkout_segment_event_task(event, self.intent.id, 'in_123')
        self.assertEqual(mock_session.post.call_count, 2)  # the failed attempt and one success, no duplicate

    @ddt.data(
        requests.ConnectionError('Segment unreachable'),
        requests.Timeout('Segment timed out'),
        SegmentAPIError(503, 'unavailable', 'Segment is down'),
        SegmentAPIError(429, 'rate_limited', 'slow down'),
    )
    @mock.patch(f'{TASKS}.send_event_synchronously')
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_segment_failure_a_later_attempt_can_fix_is_retried(self, error, mock_invoice, mock_send):
        mock_invoice.return_value = make_invoice()
        mock_send.side_effect = error

        with mock.patch.object(
            send_checkout_segment_event_task, 'retry', side_effect=SegmentSendError('retry'),
        ) as mock_retry, self.assertRaises(SegmentSendError):
            send_checkout_segment_event_task.apply(
                args=(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123'), throw=True,
            ).get()

        self.assertIsInstance(mock_retry.call_args.kwargs['exc'], SegmentSendError)
        self.assertFalse(SegmentEventEmission.objects.filter(emitted_at__isnull=False).exists())

    @ddt.data(
        SegmentAPIError(400, 'invalid_request', 'bad payload'),
        AssertionError('user_id must be a string or number'),
    )
    @mock.patch(f'{TASKS}.send_event_synchronously')
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_segment_failure_that_would_repeat_is_not_retried(self, error, mock_invoice, mock_send):
        """A 4xx other than 429, or invalid arguments, would fail the same way on every retry."""
        mock_invoice.return_value = make_invoice()
        mock_send.side_effect = error

        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry, \
                self.assertRaises(type(error)):
            send_checkout_segment_event_task.apply(
                args=(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123'), throw=True,
            ).get()

        mock_retry.assert_not_called()
        self.assertFalse(SegmentEventEmission.objects.filter(emitted_at__isnull=False).exists())

    @mock.patch('analytics.request._session')
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_not_marked_emitted_when_segment_key_unset(self, mock_invoice, mock_session):
        mock_invoice.return_value = make_invoice()
        with override_settings(SEGMENT_KEY=None):
            send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        mock_session.post.assert_not_called()
        self.assertFalse(SegmentEventEmission.objects.filter(emitted_at__isnull=False).exists())

    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.send_checkout_segment_event_task')
    def test_enqueue_never_raises_when_broker_fails(self, mock_task):
        mock_task.delay.side_effect = ConnectionError('broker down')
        enqueue_checkout_segment_event(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent, 'in_123')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.send_checkout_segment_event_task')
    def test_enqueue_flag_off(self, mock_task):
        with mock.patch('enterprise_access.toggles.CHECKOUT_SEGMENT_EVENTS_V2.is_enabled', return_value=False):
            enqueue_checkout_segment_event(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent, 'in_123')
        mock_task.delay.assert_not_called()

    @mock.patch(f'{TASKS}.send_event_synchronously')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Invoice.retrieve')
    def test_invalid_request_error_is_not_silenced(self, mock_retrieve, mock_send):
        mock_retrieve.side_effect = stripe.InvalidRequestError('bad expand', 'expand')

        with self.assertRaises(stripe.InvalidRequestError):
            send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')

        mock_send.assert_not_called()

    @mock.patch(f'{TASKS}.send_event_synchronously')
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_skipped_with_warning_when_user_has_no_lms_user_id(self, mock_invoice, mock_send):
        """The local DB user id is not an LMS user id, so it must never be used as a fallback."""
        self.user.lms_user_id = None
        self.user.save()

        with self.assertLogs(TASKS, level='WARNING') as logs:
            send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')

        mock_send.assert_not_called()
        mock_invoice.assert_not_called()
        self.assertIn(str(self.intent.id), '\n'.join(logs.output))

    @mock.patch(f'{TASKS}.send_event_synchronously')
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_missing_checkout_intent_is_logged_and_not_retried(self, mock_invoice, mock_send):
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry, \
                self.assertLogs(TASKS, level='ERROR'):
            send_checkout_segment_event_task.apply(
                args=(CheckoutSegmentEvents.ORDER_COMPLETED, 999999, 'in_123'), throw=True,
            ).get()
        mock_retry.assert_not_called()
        mock_invoice.assert_not_called()
        mock_send.assert_not_called()

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    @mock.patch(f'{TASKS}.get_stripe_invoice')
    def test_row_created_by_concurrent_delivery_still_emits_once(self, mock_invoice, mock_send):
        """A concurrent delivery already created the row (get_or_create finds it): still a single emission."""
        mock_invoice.return_value = make_invoice()
        SegmentEventEmission.objects.create(event_name=CheckoutSegmentEvents.ORDER_COMPLETED, order_id='in_123')
        for _ in range(2):
            send_checkout_segment_event_task(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent.id, 'in_123')
        mock_send.assert_called_once()

    def test_unique_constraint_on_event_and_order(self):
        SegmentEventEmission.objects.create(event_name='e', order_id='o')
        with self.assertRaises(IntegrityError), transaction.atomic():
            SegmentEventEmission.objects.create(event_name='e', order_id='o')

    @ddt.data(
        ('year', 'yearly'),
        ('month', 'monthly'),
        ('week', None),
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.customer_billing.segment_payloads.get_stripe_price_data')
    def test_payment_schedule_from_stripe_price(self, interval, expected, mock_price):
        mock_price.return_value = {'recurring': {'interval': interval}}
        line = {'pricing': {'price_details': {'price': 'price_123'}}}
        self.assertEqual(get_payment_schedule(line), expected)

    @mock.patch('enterprise_access.apps.customer_billing.segment_payloads.get_stripe_price_data')
    def test_payment_schedule_omitted_when_price_unavailable(self, mock_price):
        self.assertIsNone(get_payment_schedule({}))
        mock_price.side_effect = StripePricingError('boom')
        self.assertIsNone(get_payment_schedule({'pricing': {'price_details': {'price': 'price_123'}}}))

    def test_subscription_item_line_is_used_over_proration_line(self):
        """Stripe proration lines share the subscription item parent type; only the proration flag tells them apart."""
        invoice = make_invoice(quantity=10)
        invoice['lines']['data'].insert(0, make_line(quantity=1, unit_amount='99', product='prod_proration',
                                                     proration=True))
        props = build_order_properties(self.intent, invoice)
        self.assertEqual(props['product_id'], 'prod_abc')
        self.assertEqual(props['price'], 12.5)
        self.assertEqual(props['total_quantity'], 10)

    @mock.patch('enterprise_access.apps.customer_billing.segment_payloads.get_stripe_price_data')
    def test_invoice_of_only_prorations_supplies_no_line_fields(self, mock_price):
        """E.g. a license-change invoice: no line is used, so quantity falls back to the CheckoutIntent's."""
        invoice = make_invoice(total=2500)
        invoice['lines']['data'] = [
            make_line(quantity=10, unit_amount='1250', proration=True),  # unused time on the old quantity
            make_line(quantity=12, unit_amount='1250', proration=True),  # remaining time on the new quantity
        ]
        props = build_order_properties(self.intent, invoice)
        for key in ('product_id', 'price', 'payment_schedule'):
            self.assertNotIn(key, props)
        self.assertEqual(props['total_quantity'], self.intent.quantity)
        self.assertEqual(props['revenue'], 25.0)
        mock_price.assert_not_called()

    def test_payload_only_contains_approved_keys(self):
        props = build_order_completed_properties(self.intent, make_invoice())
        self.assertLessEqual(set(props), APPROVED_PAYLOAD_KEYS)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.send_checkout_segment_event_task')
    def test_enqueue_queues_task_with_expected_kwargs(self, mock_task):
        enqueue_checkout_segment_event(CheckoutSegmentEvents.ORDER_COMPLETED, self.intent, 'in_123')
        mock_task.delay.assert_called_once_with(
            event_name=CheckoutSegmentEvents.ORDER_COMPLETED,
            checkout_intent_id=self.intent.id,
            invoice_id='in_123',
        )

    def test_create_intent_keeps_existing_attribution_as_a_unit(self):
        """A second create_intent does not replace or merge an existing attribution object."""
        intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme',
            attribution={'utm_source': 'bing'},
        )
        intent.refresh_from_db()
        self.assertEqual(intent.id, self.intent.id)
        self.assertEqual(intent.attribution['utm_source'], 'utm_source-value')
        self.assertEqual(intent.attribution['utm_medium'], 'utm_medium-value')

    def test_create_intent_sets_attribution_on_existing_intent_without_one(self):
        self.intent.attribution = None
        self.intent.save()
        intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme', attribution={'utm_source': 'bing'},
        )
        intent.refresh_from_db()
        self.assertEqual(intent.id, self.intent.id)
        self.assertEqual(intent.attribution, {'utm_source': 'bing'})

    def test_emission_str(self):
        emission = SegmentEventEmission(event_name='e', order_id='o')
        self.assertEqual(str(emission), 'e o')

    def test_payload_tolerates_missing_invoice_fields(self):
        """Missing lines/pricing/currency must not raise; None values are dropped from the payload."""
        invoice = AttrDict.wrap({'id': 'in_sparse', 'total': None, 'lines': {'data': []}})
        props = build_order_properties(self.intent, invoice)
        self.assertEqual(props['order_id'], 'in_sparse')
        self.assertEqual(props['total_quantity'], self.intent.quantity)
        self.assertNotIn('revenue', props)
        self.assertNotIn('currency', props)
        self.assertNotIn('price', props)
