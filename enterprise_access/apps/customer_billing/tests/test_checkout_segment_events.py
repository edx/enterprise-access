"""
Tests for the server-side checkout Segment events (order_completed / order_cancelled): payloads, the send task and
the transactional outbox that dispatches it.
"""
from datetime import timedelta
from unittest import mock

import ddt
import requests
import stripe
from analytics.request import APIError as SegmentAPIError
from django.db import IntegrityError, OperationalError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from edx_django_utils.cache import TieredCache

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import (
    ATTRIBUTION_KEYS,
    ORDER_COMPLETED_BILLING_REASONS,
    CheckoutSegmentEvents,
    SegmentEventEmissionStatus
)
from enterprise_access.apps.customer_billing.models import CheckoutIntent, SegmentEventEmission
from enterprise_access.apps.customer_billing.pricing_api import StripePricingError
from enterprise_access.apps.customer_billing.segment_outbox import (
    _dispatch_after_commit,
    dispatch_pending_segment_events,
    dispatch_segment_event,
    record_order_cancelled_segment_event,
    record_order_completed_segment_event
)
from enterprise_access.apps.customer_billing.segment_payloads import (
    IncompleteOrderPayloadError,
    StripePriceTemporarilyUnavailable,
    build_order_cancelled_properties,
    build_order_completed_properties,
    find_order_invoice,
    get_payment_schedule
)
from enterprise_access.apps.customer_billing.tasks import SegmentSendError, send_checkout_segment_event_task
from enterprise_access.apps.customer_billing.tests.utils import AttrDict

TASKS = 'enterprise_access.apps.customer_billing.tasks'
PAYLOADS = 'enterprise_access.apps.customer_billing.segment_payloads'
OUTBOX = 'enterprise_access.apps.customer_billing.segment_outbox'
ORDER_COMPLETED = CheckoutSegmentEvents.ORDER_COMPLETED
ORDER_CANCELLED = CheckoutSegmentEvents.ORDER_CANCELLED
PENDING = SegmentEventEmissionStatus.PENDING
DISPATCHED = SegmentEventEmissionStatus.DISPATCHED
SENT = SegmentEventEmissionStatus.SENT
SKIPPED = SegmentEventEmissionStatus.SKIPPED
FAILED = SegmentEventEmissionStatus.FAILED
UTM_KEYS = tuple(key for key in ATTRIBUTION_KEYS if key != 'referrer')
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
YEARLY_PRICE = {'recurring': {'interval': 'year'}}


def make_line(quantity=10, unit_amount='1250', product='prod_abc', proration=False, price='price_yearly'):
    """Build an invoice line shaped like Stripe's: prorations share the subscription item parent type."""
    return {
        'quantity': quantity,
        'parent': {'type': 'subscription_item_details', 'subscription_item_details': {'proration': proration}},
        'pricing': {'unit_amount_decimal': unit_amount, 'price_details': {'product': product, 'price': price}},
    }


def make_invoice(
    total=10000, quantity=10, unit_amount='1250', invoice_id='in_123', billing_reason='subscription_cycle',
    created=1000, lines=None,
):
    """Build an invoice dict shaped like a Stripe invoice retrieved without ``expand``."""
    return AttrDict.wrap({
        'id': invoice_id,
        'total': total,
        'currency': 'usd',
        'customer_email': 'pii@example.com',
        'billing_reason': billing_reason,
        'created': created,
        'lines': {'data': lines if lines is not None else [make_line(quantity=quantity, unit_amount=unit_amount)]},
    })


def make_subscription_item(quantity=10, unit_amount_decimal='1250', product='prod_abc', interval='year'):
    return {
        'object': 'subscription_item',
        'quantity': quantity,
        'price': {
            'object': 'price',
            'id': f'price_{interval}',
            'product': product,
            'unit_amount': int(float(unit_amount_decimal)),
            'unit_amount_decimal': unit_amount_decimal,
            'recurring': {'interval': interval, 'interval_count': 1},
        },
    }


def make_subscription(items=None, **item_kwargs):
    """A real StripeObject, where ``items`` is a key that collides with ``dict.items``, as it does in production."""
    return stripe.Subscription.construct_from({
        'id': 'sub_123',
        'object': 'subscription',
        'status': 'canceled',
        'items': {'object': 'list', 'data': items if items is not None else [make_subscription_item(**item_kwargs)]},
    }, 'sk_test')


def pricing_error(cause):
    """A StripePricingError chained to ``cause``, as get_stripe_price_data raises it."""
    try:
        raise StripePricingError(f'Failed to fetch price: {cause}') from cause
    except StripePricingError as exc:
        return exc


TRANSIENT_STRIPE_ERRORS = (
    stripe.APIConnectionError('Network error communicating with Stripe'),
    stripe.APIConnectionError('Request to Stripe timed out'),  # stripe-python raises this for timeouts
    stripe.RateLimitError('Too many requests', http_status=429),
    stripe.APIError('Stripe server error', http_status=500),
)


@override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=True)
class CheckoutSegmentTestCase(TestCase):
    """Shared setup: a paid-looking CheckoutIntent with attribution and a Checkout Session ID."""

    def setUp(self):
        super().setUp()
        TieredCache.dangerous_clear_all_tiers()  # get_stripe_invoice and get_stripe_price_data cache
        self.user = UserFactory()
        self.intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme',
            attribution={**{key: f'{key}-value' for key in UTM_KEYS}, 'referrer': 'https://www.google.com/'},
        )
        self.intent.update_stripe_identifiers(session_id='cs_123')
        price_patcher = mock.patch(f'{PAYLOADS}.get_stripe_price_data', return_value=YEARLY_PRICE)
        self.mock_price = price_patcher.start()
        self.addCleanup(price_patcher.stop)

    def make_emission(self, event_name=ORDER_COMPLETED, invoice_id='in_123', status=DISPATCHED, **kwargs):
        """Create an outbox row for this CheckoutIntent's order (``cs_123``), dispatched unless told otherwise."""
        defaults = {
            'order_id': 'cs_123',
            'checkout_intent': self.intent,
            'stripe_subscription_id': 'sub_123',
            'stripe_invoice_id': invoice_id,
            'status': status,
        }
        return SegmentEventEmission.objects.create(event_name=event_name, **{**defaults, **kwargs})


@ddt.ddt
class TestOrderCompletedPayload(CheckoutSegmentTestCase):
    """build_order_completed_properties."""

    def test_revenue_is_invoice_total_not_price_times_quantity(self):
        # 10 licenses * $12.50 = $125.00, but a discount/proration brought the total to $87.35.
        invoice = make_invoice(total=8735, quantity=10, unit_amount='1250')
        props = build_order_completed_properties(self.intent, invoice, 'cs_123')
        self.assertEqual(props['revenue'], 87.35)
        self.assertEqual(props['price'], 12.5)
        self.assertEqual(props['total_quantity'], 10)
        self.assertEqual(props['order_id'], 'cs_123')
        self.assertEqual(props['product_id'], 'prod_abc')
        self.assertEqual(props['payment_schedule'], 'yearly')
        self.assertEqual(props['currency'], 'USD')
        self.assertEqual(props['category'], 'subscription')
        self.assertEqual(props['brand'], 'enterprise')
        self.assertEqual(props['slug'], self.intent.ssp_product.slug)

    def test_no_pii_keys_and_variant_omitted_for_teams(self):
        props = build_order_completed_properties(self.intent, make_invoice(), 'cs_123')
        self.assertFalse(PII_KEYS & set(props))
        self.assertNotIn('pii@example.com', str(props))
        self.assertEqual(props['name'], 'teams')
        self.assertNotIn('variant', props)

    def test_variant_present_for_essentials(self):
        product = self.intent.ssp_product
        with mock.patch.object(type(product), 'academy_uuid', new_callable=mock.PropertyMock, create=True), \
                mock.patch.object(type(product), 'academy_title', new_callable=mock.PropertyMock) as title, \
                mock.patch(f'{PAYLOADS}.get_product_type', return_value='essentials'):
            title.return_value = 'AI Academy'
            props = build_order_completed_properties(self.intent, make_invoice(), 'cs_123')
        self.assertEqual(props['name'], 'essentials')
        self.assertEqual(props['variant'], 'AI Academy')

    def test_attribution_sent_on_order_completed(self):
        props = build_order_completed_properties(self.intent, make_invoice(), 'cs_123')
        for key in UTM_KEYS:
            self.assertEqual(props[key], f'{key}-value')
        self.assertEqual(props['referrer'], 'https://www.google.com/')

    def test_stored_attribution_is_sanitized_again_before_sending(self):
        """A value stored before the current rules (or edited in admin) is never forwarded unchecked."""
        self.intent.attribution = {
            'utm_source': 'google',
            'utm_content': {'customer_email': 'jane@example.com'},
            'utm_term': 'jane@example.com',
            'utm_medium': ['cpc'],
            'referrer': 'https://jane:secret@example.com/u/jane-doe?email=jane@example.com#top',
        }
        props = build_order_completed_properties(self.intent, make_invoice(), 'cs_123')
        self.assertEqual(props['utm_source'], 'google')
        self.assertEqual(props['referrer'], 'https://example.com/')
        for key in ('utm_content', 'utm_term', 'utm_medium'):
            self.assertNotIn(key, props)
        self.assertNotIn('jane', str(props))

    def test_payload_only_contains_approved_keys(self):
        props = build_order_completed_properties(self.intent, make_invoice(), 'cs_123')
        self.assertLessEqual(set(props), APPROVED_PAYLOAD_KEYS)

    def test_subscription_item_line_is_used_over_proration_line(self):
        """Stripe proration lines share the subscription item parent type; only the proration flag tells them apart."""
        invoice = make_invoice(quantity=10)
        invoice['lines']['data'].insert(0, make_line(quantity=1, unit_amount='99', product='prod_proration',
                                                     proration=True))
        props = build_order_completed_properties(self.intent, invoice, 'cs_123')
        self.assertEqual(props['product_id'], 'prod_abc')
        self.assertEqual(props['price'], 12.5)
        self.assertEqual(props['total_quantity'], 10)

    def test_invoice_of_only_prorations_is_incomplete_rather_than_sent_partially(self):
        """No subscription item line: product, price, schedule and quantity are unknown, so nothing is sent."""
        invoice = make_invoice(total=2500, lines=[
            make_line(quantity=10, unit_amount='1250', proration=True),  # unused time on the old quantity
            make_line(quantity=12, unit_amount='1250', proration=True),  # remaining time on the new quantity
        ])
        with self.assertRaises(IncompleteOrderPayloadError) as ctx:
            build_order_completed_properties(self.intent, invoice, 'cs_123')
        for key in ('product_id', 'price', 'payment_schedule', 'total_quantity'):
            self.assertIn(key, str(ctx.exception))
        self.mock_price.assert_not_called()

    def test_missing_invoice_fields_are_incomplete(self):
        invoice = AttrDict.wrap({'id': 'in_sparse', 'total': None, 'lines': {'data': [make_line()]}})
        with self.assertRaises(IncompleteOrderPayloadError) as ctx:
            build_order_completed_properties(self.intent, invoice, 'cs_123')
        self.assertIn('revenue', str(ctx.exception))
        self.assertIn('currency', str(ctx.exception))

    def test_genuinely_unavailable_payment_schedule_is_incomplete(self):
        self.mock_price.return_value = {'recurring': {'interval': 'week'}}
        with self.assertRaisesRegex(IncompleteOrderPayloadError, 'payment_schedule'):
            build_order_completed_properties(self.intent, make_invoice(), 'cs_123')


@ddt.ddt
class TestPaymentSchedule(TestCase):
    """get_payment_schedule, with the real (cached) get_stripe_price_data and only Stripe's API mocked."""

    def setUp(self):
        super().setUp()
        TieredCache.dangerous_clear_all_tiers()
        self.line = make_line(price='price_123')

    @staticmethod
    def _stripe_price(interval='year', active=True):
        return stripe.Price.construct_from({
            'id': 'price_123', 'object': 'price', 'currency': 'usd', 'unit_amount': 1250, 'type': 'recurring',
            'active': active, 'billing_scheme': 'per_unit', 'lookup_key': None,
            'recurring': {'interval': interval, 'interval_count': 1, 'usage_type': 'licensed'},
            'product': {
                'id': 'prod_abc', 'object': 'product', 'name': 'Teams', 'active': True, 'description': None,
                'metadata': {'ssp_product_slug': 'teams-yearly'},
            },
        }, 'sk_test')

    @ddt.data(('year', 'yearly'), ('month', 'monthly'), ('week', None))
    @ddt.unpack
    def test_payment_schedule_from_stripe_price(self, interval, expected):
        with mock.patch(f'{PAYLOADS}.get_stripe_price_data', return_value={'recurring': {'interval': interval}}):
            self.assertEqual(get_payment_schedule(self.line), expected)

    def test_no_price_on_the_line(self):
        self.assertIsNone(get_payment_schedule({}))
        self.assertIsNone(get_payment_schedule(None))

    @ddt.data(*TRANSIENT_STRIPE_ERRORS)
    def test_transient_stripe_failure_is_raised_for_retry_and_not_cached(self, error):
        """Connection errors, timeouts, 429 and 5xx: a passing outage must not look like missing pricing data."""
        with mock.patch('stripe.Price.retrieve', side_effect=error):
            with self.assertRaises(StripePriceTemporarilyUnavailable) as ctx:
                get_payment_schedule(self.line)
        self.assertIs(ctx.exception.__cause__.__cause__, error)
        with mock.patch('stripe.Price.retrieve', return_value=self._stripe_price()):
            self.assertEqual(get_payment_schedule(self.line), 'yearly')

    @ddt.data(
        stripe.InvalidRequestError('No such price: price_123', 'price', http_status=404),
        stripe.AuthenticationError('Invalid API key', http_status=401),
    )
    def test_stripe_error_a_retry_cannot_fix_means_no_schedule(self, error):
        with mock.patch('stripe.Price.retrieve', side_effect=error):
            self.assertIsNone(get_payment_schedule(self.line))

    def test_inactive_price_means_no_schedule(self):
        """get_stripe_price_data rejects inactive prices without a Stripe error behind it: not transient."""
        with mock.patch('stripe.Price.retrieve', return_value=self._stripe_price(active=False)):
            self.assertIsNone(get_payment_schedule(self.line))


@ddt.ddt
class TestOrderCancelledPayload(CheckoutSegmentTestCase):
    """
    build_order_cancelled_properties: product, price, schedule and quantity from the cancelled subscription; order_id
    and revenue from the order (the Checkout Session and its first paid invoice).
    """

    @ddt.data(
        # (description, subscription quantity at cancellation)
        ('original purchase', 10),
        ('license quantity increased', 15),
        ('license quantity decreased', 5),
    )
    @ddt.unpack
    def test_quantity_is_the_subscription_quantity_at_cancellation(self, _description, quantity):
        order_invoice = make_invoice(total=12500, quantity=10, invoice_id='in_order')
        props = build_order_cancelled_properties(
            self.intent, order_invoice, make_subscription(quantity=quantity), 'cs_123',
        )
        self.assertEqual(props['total_quantity'], quantity)
        self.assertEqual(props['order_id'], 'cs_123')
        self.assertEqual(props['revenue'], 125.0)  # the order's invoice total, whatever changed later
        self.assertEqual(props['currency'], 'USD')
        self.assertEqual(props['product_id'], 'prod_abc')
        self.assertEqual(props['price'], 12.5)
        self.assertEqual(props['payment_schedule'], 'yearly')
        self.assertLessEqual(set(props), APPROVED_PAYLOAD_KEYS)
        self.mock_price.assert_not_called()  # the interval comes with the subscription's price

    def test_after_multiple_changes_the_final_subscription_state_is_used(self):
        """Quantity and price changed (e.g. moved to a monthly price): the cancelled subscription has the last state."""
        subscription = make_subscription(quantity=7, unit_amount_decimal='999', product='prod_new', interval='month')
        props = build_order_cancelled_properties(self.intent, make_invoice(total=12500), subscription, 'cs_123')
        self.assertEqual(props['total_quantity'], 7)
        self.assertEqual(props['product_id'], 'prod_new')
        self.assertEqual(props['price'], 9.99)
        self.assertEqual(props['payment_schedule'], 'monthly')
        self.assertEqual(props['revenue'], 125.0)

    def test_price_falls_back_to_the_integer_unit_amount(self):
        item = make_subscription_item()
        item['price']['unit_amount_decimal'] = None
        item['price']['unit_amount'] = 1500
        props = build_order_cancelled_properties(self.intent, make_invoice(), make_subscription(items=[item]), 'cs_1')
        self.assertEqual(props['price'], 15.0)

    def test_expanded_product_is_reduced_to_its_id(self):
        item = make_subscription_item()
        item['price']['product'] = {'id': 'prod_abc', 'object': 'product', 'name': 'Teams'}
        props = build_order_cancelled_properties(self.intent, make_invoice(), make_subscription(items=[item]), 'cs_1')
        self.assertEqual(props['product_id'], 'prod_abc')

    def test_no_attribution_on_order_cancelled(self):
        props = build_order_cancelled_properties(self.intent, make_invoice(), make_subscription(), 'cs_123')
        self.assertFalse(set(ATTRIBUTION_KEYS) & set(props))

    @ddt.data(0, 2)
    def test_subscription_without_exactly_one_item_is_incomplete(self, item_count):
        items = [make_subscription_item() for _ in range(item_count)]
        with self.assertRaisesRegex(IncompleteOrderPayloadError, f'{item_count} items'):
            build_order_cancelled_properties(self.intent, make_invoice(), make_subscription(items=items), 'cs_123')

    def test_unknown_billing_interval_is_incomplete(self):
        with self.assertRaisesRegex(IncompleteOrderPayloadError, 'payment_schedule'):
            build_order_cancelled_properties(
                self.intent, make_invoice(), make_subscription(interval='week'), 'cs_123',
            )


@ddt.ddt
class TestFindOrderInvoice(TestCase):
    """find_order_invoice picks the order's first paid invoice from Stripe's payment history."""

    @ddt.data(
        # (description, invoices as (id, billing_reason, total, created), expected order invoice)
        ('trial, then license change and a renewal', [
            ('in_renewal', 'subscription_cycle', 12000, 4000),
            ('in_license_change', 'subscription_update', 2500, 3000),  # proration-only, the latest change
            ('in_trial_end', 'subscription_cycle', 10000, 2000),
            ('in_trial', 'subscription_create', 0, 1000),
        ], 'in_trial_end'),
        ('paid at creation, no trial', [
            ('in_license_change', 'subscription_update', 2500, 3000),
            ('in_create', 'subscription_create', 10000, 1000),
        ], 'in_create'),
        ('cancelled during the trial', [('in_trial', 'subscription_create', 0, 1000)], None),
        ('only license changes and manual invoices', [
            ('in_license_change', 'subscription_update', 2500, 3000),
            ('in_manual', 'manual', 5000, 2000),
        ], None),
        ('no paid invoice', [], None),
    )
    @ddt.unpack
    def test_find_order_invoice(self, _description, invoices, expected):
        paid = [
            make_invoice(invoice_id=invoice_id, billing_reason=reason, total=total, created=created)
            for invoice_id, reason, total, created in invoices
        ]
        order_invoice = find_order_invoice(paid, ORDER_COMPLETED_BILLING_REASONS)
        self.assertEqual(order_invoice['id'] if order_invoice else None, expected)


@ddt.ddt
class TestSendCheckoutSegmentEventTask(CheckoutSegmentTestCase):
    """send_checkout_segment_event_task: sending, retries and status transitions of the outbox row."""

    def setUp(self):
        super().setUp()
        invoice_patcher = mock.patch(f'{TASKS}.get_stripe_invoice', return_value=make_invoice())
        subscription_patcher = mock.patch(f'{TASKS}.get_stripe_subscription', return_value=make_subscription())
        self.mock_get_stripe_invoice = invoice_patcher.start()
        self.mock_get_stripe_subscription = subscription_patcher.start()
        self.addCleanup(invoice_patcher.stop)
        self.addCleanup(subscription_patcher.stop)

    def run_task_expecting_retry(self, emission, retry_exc_type):
        """Run the task as Celery does, with ``retry`` stubbed to raise; returns the retry mock."""
        with mock.patch.object(
            send_checkout_segment_event_task, 'retry', side_effect=retry_exc_type('retry'),
        ) as mock_retry, self.assertRaises(retry_exc_type):
            send_checkout_segment_event_task.apply(args=(emission.pk,), throw=True).get()
        return mock_retry

    @ddt.data(ORDER_COMPLETED, ORDER_CANCELLED)
    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_task_sends_once_per_order_and_event(self, event_name, mock_send):
        emission = self.make_emission(event_name)
        for _ in range(3):  # webhook redelivery / dispatcher re-publish
            send_checkout_segment_event_task(emission.pk)
        self.mock_get_stripe_invoice.assert_called_once()  # finished rows skip the Stripe fetch
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs['event_name'], event_name)
        self.assertEqual(mock_send.call_args.kwargs['properties']['order_id'], 'cs_123')
        # Deterministic, so Segment drops a resend after a success that failed to be recorded.
        self.assertEqual(mock_send.call_args.kwargs['message_id'], f'{event_name}:cs_123')
        emission.refresh_from_db()
        self.assertEqual(emission.status, SENT)
        self.assertIsNotNone(emission.emitted_at)
        self.assertIsNone(emission.last_error)

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_same_order_different_events_both_sent(self, mock_send):
        for event_name in (ORDER_COMPLETED, ORDER_CANCELLED):
            send_checkout_segment_event_task(self.make_emission(event_name).pk)
        self.assertEqual(mock_send.call_count, 2)

    @override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=False)
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_flag_off_sends_nothing_and_leaves_the_row_for_later(self, mock_send):
        emission = self.make_emission()
        send_checkout_segment_event_task(emission.pk)
        self.mock_get_stripe_invoice.assert_not_called()
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, DISPATCHED)

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_stripe_failure_retries_and_leaves_the_row_unfinished(self, mock_send):
        emission = self.make_emission()
        self.mock_get_stripe_invoice.side_effect = stripe.APIConnectionError('boom')
        mock_retry = self.run_task_expecting_retry(emission, stripe.APIConnectionError)
        self.assertTrue(mock_retry.called)
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, DISPATCHED)

        self.mock_get_stripe_invoice.side_effect = None
        send_checkout_segment_event_task(emission.pk)
        mock_send.assert_called_once()
        emission.refresh_from_db()
        self.assertEqual(emission.status, SENT)

    @override_settings(SEGMENT_KEY='test-key')
    @mock.patch('analytics.request._session')
    def test_segment_outage_is_retryable_and_marked_sent_only_after_success(self, mock_session):
        """
        Runs the real analytics-python client with only its HTTP session mocked: a Segment outage reaches the task
        (it is not swallowed on a background thread), and the row is set to SENT only once Segment returns 200.
        """
        emission = self.make_emission()
        mock_session.post.return_value = mock.Mock(status_code=503, **{'json.return_value': {
            'code': 'unavailable', 'message': 'Segment is down',
        }})

        with self.assertRaises(SegmentSendError):
            send_checkout_segment_event_task(emission.pk)
        emission.refresh_from_db()
        self.assertEqual(emission.status, DISPATCHED)  # a failed send is never recorded as sent
        self.assertIsNone(emission.emitted_at)

        mock_session.post.return_value = mock.Mock(status_code=200)
        send_checkout_segment_event_task(emission.pk)
        emission.refresh_from_db()
        self.assertEqual(emission.status, SENT)
        send_checkout_segment_event_task(emission.pk)
        self.assertEqual(mock_session.post.call_count, 2)  # the failed attempt and one success, no duplicate

    @ddt.data(
        requests.ConnectionError('Segment unreachable'),
        requests.Timeout('Segment timed out'),
        SegmentAPIError(503, 'unavailable', 'Segment is down'),
        SegmentAPIError(429, 'rate_limited', 'slow down'),
    )
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_segment_failure_a_later_attempt_can_fix_is_retried(self, error, mock_send):
        emission = self.make_emission()
        mock_send.side_effect = error
        mock_retry = self.run_task_expecting_retry(emission, SegmentSendError)
        self.assertIsInstance(mock_retry.call_args.kwargs['exc'], SegmentSendError)
        emission.refresh_from_db()
        self.assertEqual(emission.status, DISPATCHED)

    @ddt.data(
        SegmentAPIError(400, 'invalid_request', 'bad payload'),
        AssertionError('user_id must be a string or number'),
    )
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_segment_failure_that_would_repeat_fails_the_row_without_retry(self, error, mock_send):
        """A 4xx other than 429, or invalid arguments, would fail the same way on every retry."""
        emission = self.make_emission()
        mock_send.side_effect = error
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry, \
                self.assertRaises(type(error)):
            send_checkout_segment_event_task.apply(args=(emission.pk,), throw=True).get()
        mock_retry.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, FAILED)
        self.assertIn('Segment rejected the event', emission.last_error)

    @mock.patch('analytics.request._session')
    def test_not_marked_sent_when_segment_key_unset(self, mock_session):
        emission = self.make_emission()
        with override_settings(SEGMENT_KEY=None):
            send_checkout_segment_event_task(emission.pk)
        mock_session.post.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, DISPATCHED)  # the dispatcher publishes it again later

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_invalid_request_error_fails_the_row_and_is_not_silenced(self, mock_send):
        emission = self.make_emission()
        self.mock_get_stripe_invoice.side_effect = stripe.InvalidRequestError('No such invoice', 'id')
        with self.assertRaises(stripe.InvalidRequestError):
            send_checkout_segment_event_task(emission.pk)
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, FAILED)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_no_lms_user_id_fails_the_row_before_any_stripe_call(self, mock_send):
        """The local DB user id is not an LMS user id, so it must never be used as a fallback."""
        emission = self.make_emission()
        self.user.lms_user_id = None
        self.user.save()

        with self.assertLogs(TASKS, level='WARNING') as logs:
            send_checkout_segment_event_task(emission.pk)

        mock_send.assert_not_called()
        self.mock_get_stripe_invoice.assert_not_called()
        self.assertIn(str(self.intent.id), '\n'.join(logs.output))
        emission.refresh_from_db()
        self.assertEqual(emission.status, FAILED)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_deleted_checkout_intent_fails_the_row(self, mock_send):
        emission = self.make_emission()
        self.intent.delete()
        send_checkout_segment_event_task(emission.pk)
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertIsNone(emission.checkout_intent)
        self.assertEqual(emission.status, FAILED)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_missing_row_is_logged_and_not_retried(self, mock_send):
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry, \
                self.assertLogs(TASKS, level='ERROR'):
            send_checkout_segment_event_task.apply(args=(999999,), throw=True).get()
        mock_retry.assert_not_called()
        self.mock_get_stripe_invoice.assert_not_called()
        mock_send.assert_not_called()

    @ddt.data(SENT, SKIPPED, FAILED)
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_finished_row_is_never_sent_again(self, status, mock_send):
        emission = self.make_emission(status=status)
        send_checkout_segment_event_task(emission.pk)
        self.mock_get_stripe_invoice.assert_not_called()
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, status)

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_stale_copy_never_moves_a_finished_row_back(self, _mock_send):
        """Two copies loaded before either writes: the one that loses cannot overwrite the SENT row."""
        emission = self.make_emission()
        stale_copy = SegmentEventEmission.objects.get(pk=emission.pk)
        send_checkout_segment_event_task(emission.pk)
        self.assertFalse(stale_copy.finish(FAILED, 'late failure'))
        emission.refresh_from_db()
        self.assertEqual(emission.status, SENT)
        self.assertIsNone(emission.last_error)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_incomplete_payload_fails_the_row_and_is_never_sent(self, mock_send):
        emission = self.make_emission()
        self.mock_get_stripe_invoice.return_value = make_invoice(lines=[make_line(proration=True)])
        with self.assertLogs(TASKS, level='ERROR'):
            send_checkout_segment_event_task(emission.pk)
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, FAILED)
        self.assertIn('product_id', emission.last_error)

    @ddt.data(*TRANSIENT_STRIPE_ERRORS)
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_transient_pricing_failure_is_retried_not_sent_incomplete(self, error, mock_send):
        emission = self.make_emission()
        self.mock_price.side_effect = pricing_error(error)
        mock_retry = self.run_task_expecting_retry(emission, StripePriceTemporarilyUnavailable)
        self.assertIsInstance(mock_retry.call_args.kwargs['exc'], StripePriceTemporarilyUnavailable)
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, DISPATCHED)

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_successful_retry_after_a_transient_pricing_failure(self, mock_send):
        """Celery's own (eager) retry: the first attempt can't reach Stripe, the retry sends the complete event."""
        emission = self.make_emission()
        self.mock_price.side_effect = [pricing_error(stripe.APIConnectionError('timed out')), YEARLY_PRICE]

        send_checkout_segment_event_task.apply(args=(emission.pk,))

        self.assertEqual(self.mock_price.call_count, 2)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs['properties']['payment_schedule'], 'yearly')
        emission.refresh_from_db()
        self.assertEqual(emission.status, SENT)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_genuinely_unavailable_pricing_fails_the_row_without_retry(self, mock_send):
        emission = self.make_emission()
        self.mock_price.side_effect = pricing_error(stripe.InvalidRequestError('No such price', 'price'))
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry:
            send_checkout_segment_event_task.apply(args=(emission.pk,), throw=True).get()
        mock_retry.assert_not_called()
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, FAILED)
        self.assertIn('payment_schedule', emission.last_error)

    @ddt.data(
        # (event_name, stripe_invoice_id, expected error)
        (ORDER_COMPLETED, None, 'without its paid invoice'),
        ('edx.ui.enterprise.checkout.unknown', 'in_123', 'Unknown checkout Segment event'),
    )
    @ddt.unpack
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_row_that_cannot_be_built_fails(self, event_name, invoice_id, expected_error, mock_send):
        emission = self.make_emission(event_name, invoice_id=invoice_id)
        with self.assertLogs(TASKS, level='ERROR'):
            send_checkout_segment_event_task(emission.pk)
        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, FAILED)
        self.assertIn(expected_error, emission.last_error)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_row_finished_by_a_concurrent_worker_while_building_is_not_sent_again(self, mock_send):
        """The authoritative check happens under the row lock, after the payload was built."""
        emission = self.make_emission()

        def build_while_another_worker_sends(row):
            SegmentEventEmission.objects.filter(pk=row.pk).update(status=SENT, emitted_at=timezone.now())
            return self.user.lms_user_id, {'order_id': 'cs_123'}

        with mock.patch(f'{TASKS}._build_checkout_segment_event', side_effect=build_while_another_worker_sends):
            send_checkout_segment_event_task(emission.pk)

        mock_send.assert_not_called()
        emission.refresh_from_db()
        self.assertEqual(emission.status, SENT)

    @mock.patch(f'{TASKS}.list_paid_subscription_invoices')
    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_cancellation_uses_the_invoice_recorded_for_order_completed(self, mock_send, mock_list):
        self.make_emission(ORDER_COMPLETED, invoice_id='in_order', status=SENT)
        cancellation = self.make_emission(ORDER_CANCELLED, invoice_id=None)
        self.mock_get_stripe_invoice.return_value = make_invoice(total=12500, invoice_id='in_order')
        self.mock_get_stripe_subscription.return_value = make_subscription(quantity=15)

        send_checkout_segment_event_task(cancellation.pk)

        mock_list.assert_not_called()
        self.mock_get_stripe_invoice.assert_called_once_with('in_order')
        properties = mock_send.call_args.kwargs['properties']
        self.assertEqual(properties['order_id'], 'cs_123')
        self.assertEqual(properties['revenue'], 125.0)
        self.assertEqual(properties['total_quantity'], 15)
        cancellation.refresh_from_db()
        self.assertEqual(cancellation.stripe_invoice_id, 'in_order')
        self.assertEqual(cancellation.status, SENT)

    @mock.patch(f'{TASKS}.list_paid_subscription_invoices')
    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_cancellation_before_any_invoice_webhook_resolves_the_order_from_stripe(self, mock_send, mock_list):
        """
        No order_completed row and no local invoice history (the deletion arrived first). Stripe's history has a
        trial, the trial-end payment and a later proration-only license change; the order is the trial-end payment,
        and product/quantity come from the subscription, not from the proration-only invoice.
        """
        mock_list.return_value = [
            make_invoice(invoice_id='in_license_change', billing_reason='subscription_update', total=2500,
                         created=3000, lines=[make_line(quantity=12, proration=True)]),
            make_invoice(invoice_id='in_trial_end', billing_reason='subscription_cycle', total=12500, created=2000),
            make_invoice(invoice_id='in_trial', billing_reason='subscription_create', total=0, created=1000),
        ]
        cancellation = self.make_emission(ORDER_CANCELLED, invoice_id=None)
        self.mock_get_stripe_invoice.return_value = make_invoice(total=12500, invoice_id='in_trial_end')
        self.mock_get_stripe_subscription.return_value = make_subscription(quantity=12)

        for _ in range(2):  # redelivery: the resolved invoice is reused, not looked up again
            SegmentEventEmission.objects.filter(pk=cancellation.pk).update(status=DISPATCHED)
            send_checkout_segment_event_task(cancellation.pk)

        mock_list.assert_called_once_with('sub_123')
        self.mock_get_stripe_invoice.assert_called_with('in_trial_end')
        self.mock_get_stripe_subscription.assert_called_with('sub_123')
        for call in mock_send.call_args_list:
            properties = call.kwargs['properties']
            self.assertEqual(call.kwargs['message_id'], f'{ORDER_CANCELLED}:cs_123')
            self.assertEqual(properties['revenue'], 125.0)
            self.assertEqual(properties['total_quantity'], 12)
            self.assertEqual(properties['product_id'], 'prod_abc')
            self.assertEqual(properties['price'], 12.5)
            self.assertEqual(properties['payment_schedule'], 'yearly')
        cancellation.refresh_from_db()
        self.assertEqual(cancellation.stripe_invoice_id, 'in_trial_end')

    @mock.patch(f'{TASKS}.list_paid_subscription_invoices')
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_cancellation_of_a_subscription_that_never_paid_is_skipped(self, mock_send, mock_list):
        """Stripe, not the local history, shows no payment (cancelled during the trial): no order to cancel."""
        mock_list.return_value = [make_invoice(invoice_id='in_trial', billing_reason='subscription_create', total=0)]
        cancellation = self.make_emission(ORDER_CANCELLED, invoice_id=None)

        send_checkout_segment_event_task(cancellation.pk)

        mock_send.assert_not_called()
        self.mock_get_stripe_invoice.assert_not_called()
        cancellation.refresh_from_db()
        self.assertEqual(cancellation.status, SKIPPED)
        self.assertIn('never collected a payment', cancellation.last_error)

    @mock.patch(f'{TASKS}.list_paid_subscription_invoices')
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_transient_failure_listing_invoices_is_retried(self, mock_send, mock_list):
        mock_list.side_effect = stripe.RateLimitError('Too many requests', http_status=429)
        cancellation = self.make_emission(ORDER_CANCELLED, invoice_id=None)
        self.run_task_expecting_retry(cancellation, stripe.RateLimitError)
        mock_send.assert_not_called()
        cancellation.refresh_from_db()
        self.assertEqual(cancellation.status, DISPATCHED)
        self.assertIsNone(cancellation.stripe_invoice_id)

    def test_unique_constraint_on_event_and_order(self):
        self.make_emission()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_emission(invoice_id='in_other')

    def test_emission_str(self):
        emission = SegmentEventEmission(event_name='e', order_id='o', status=PENDING)
        self.assertEqual(str(emission), 'e o (pending)')


@ddt.ddt
class TestSegmentOutbox(CheckoutSegmentTestCase):
    """Recording events before publishing, and the dispatcher that publishes what the broker never got."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch(f'{OUTBOX}.send_checkout_segment_event_task')
        self.mock_task = patcher.start()
        self.addCleanup(patcher.stop)

    def test_row_is_saved_as_pending_before_the_task_is_published(self):
        with self.captureOnCommitCallbacks() as callbacks:
            emission = record_order_completed_segment_event(self.intent, 'in_123', 'sub_123')
            emission.refresh_from_db()
            self.assertEqual(emission.status, PENDING)
            self.mock_task.delay.assert_not_called()  # only after the row is committed
        for callback in callbacks:
            callback()

        self.mock_task.delay.assert_called_once_with(emission.pk)
        emission.refresh_from_db()
        self.assertEqual(emission.status, DISPATCHED)
        self.assertEqual(emission.dispatch_attempts, 1)
        self.assertIsNotNone(emission.last_dispatched_at)
        self.assertEqual(
            (emission.order_id, emission.stripe_invoice_id, emission.stripe_subscription_id),
            ('cs_123', 'in_123', 'sub_123'),
        )

    def test_recording_is_idempotent_per_order(self):
        """Redelivered webhooks find the same row; a dispatched row is not published again."""
        with self.captureOnCommitCallbacks(execute=True):
            first = record_order_cancelled_segment_event(self.intent, 'sub_123')
            second = record_order_cancelled_segment_event(self.intent, 'sub_123')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SegmentEventEmission.objects.count(), 1)
        self.mock_task.delay.assert_called_once_with(first.pk)

    @override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=False)
    def test_nothing_is_recorded_when_the_flag_is_off(self):
        self.assertIsNone(record_order_completed_segment_event(self.intent, 'in_123', 'sub_123'))
        self.assertFalse(SegmentEventEmission.objects.exists())

    def test_nothing_is_recorded_without_a_checkout_session(self):
        """Without the Checkout Session there is no stable order_id; that is logged, never replaced by another ID."""
        self.intent.stripe_checkout_session_id = None
        self.intent.save()
        with self.assertLogs(OUTBOX, level='ERROR'):
            self.assertIsNone(record_order_cancelled_segment_event(self.intent, 'sub_123'))
        self.assertFalse(SegmentEventEmission.objects.exists())

    def test_database_error_while_recording_is_logged_not_raised(self):
        with mock.patch.object(SegmentEventEmission.objects, 'get_or_create', side_effect=OperationalError('down')), \
                self.assertLogs(OUTBOX, level='ERROR'):
            self.assertIsNone(record_order_completed_segment_event(self.intent, 'in_123', 'sub_123'))

    def test_broker_failure_keeps_the_row_pending_until_the_dispatcher_publishes_it(self):
        self.mock_task.delay.side_effect = ConnectionError('broker down')
        # assertLogs outermost: the publish runs when captureOnCommitCallbacks exits.
        with self.assertLogs(OUTBOX, level='WARNING'), self.captureOnCommitCallbacks(execute=True):
            emission = record_order_completed_segment_event(self.intent, 'in_123', 'sub_123')
        emission.refresh_from_db()
        self.assertEqual(emission.status, PENDING)
        self.assertEqual(emission.dispatch_attempts, 0)  # a failed publish is not counted as an attempt
        self.assertIn('broker down', emission.last_error)

        # Still down on the next run: nothing lost, nothing counted.
        self.assertEqual(dispatch_pending_segment_events()['not_dispatched'], 1)
        emission.refresh_from_db()
        self.assertEqual((emission.status, emission.dispatch_attempts), (PENDING, 0))

        self.mock_task.delay.side_effect = None
        counts = dispatch_pending_segment_events()
        self.assertEqual(counts['dispatched'], 1)
        emission.refresh_from_db()
        self.assertEqual((emission.status, emission.dispatch_attempts), (DISPATCHED, 1))
        self.assertEqual(self.mock_task.delay.call_count, 3)

    @ddt.data(DISPATCHED, SENT, SKIPPED, FAILED)
    def test_only_pending_rows_are_dispatched(self, status):
        emission = self.make_emission(status=status)
        self.assertFalse(dispatch_segment_event(emission.pk))
        self.mock_task.delay.assert_not_called()

    def test_concurrent_dispatchers_publish_a_row_once(self):
        """The second dispatcher finds the row no longer PENDING (in MySQL it would also skip the locked row)."""
        emission = self.make_emission(status=PENDING)
        self.assertTrue(dispatch_segment_event(emission.pk))
        self.assertFalse(dispatch_segment_event(emission.pk))
        self.mock_task.delay.assert_called_once_with(emission.pk)

    @mock.patch(f'{TASKS}.get_stripe_invoice', return_value=make_invoice())
    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_eager_task_result_is_not_overwritten_by_the_dispatcher(self, mock_send, _mock_invoice):
        """With Celery eager (tests, some devstacks) the task finishes inside delay(); the row must stay SENT."""
        self.mock_task.delay.side_effect = send_checkout_segment_event_task.delay
        with self.captureOnCommitCallbacks(execute=True):
            emission = record_order_completed_segment_event(self.intent, 'in_123', 'sub_123')
        mock_send.assert_called_once()
        emission.refresh_from_db()
        self.assertEqual(emission.status, SENT)
        self.assertEqual(emission.dispatch_attempts, 1)

    def test_dispatcher_requeues_unfinished_rows_and_fails_exhausted_ones(self):
        long_ago = timezone.now() - timedelta(hours=3)
        recently = timezone.now() - timedelta(minutes=10)
        stale = self.make_emission(order_id='cs_stale', dispatch_attempts=1, last_dispatched_at=long_ago)
        in_flight = self.make_emission(order_id='cs_in_flight', dispatch_attempts=1, last_dispatched_at=recently)
        exhausted = self.make_emission(
            order_id='cs_exhausted', dispatch_attempts=5, last_dispatched_at=long_ago, last_error='Segment 503',
        )
        pending = self.make_emission(order_id='cs_pending', status=PENDING)
        sent = self.make_emission(order_id='cs_sent', status=SENT, last_dispatched_at=long_ago)

        with self.assertLogs(OUTBOX, level='ERROR'):
            counts = dispatch_pending_segment_events(stale_after=timedelta(hours=2), max_dispatch_attempts=5)

        self.assertEqual(counts, {'failed': 1, 'requeued': 1, 'dispatched': 2, 'not_dispatched': 0})
        for emission, status, attempts in (
            (stale, DISPATCHED, 2), (in_flight, DISPATCHED, 1), (exhausted, FAILED, 5), (pending, DISPATCHED, 1),
            (sent, SENT, 0),
        ):
            emission.refresh_from_db()
            self.assertEqual((emission.order_id, emission.status, emission.dispatch_attempts),
                             (emission.order_id, status, attempts))
        self.assertEqual(exhausted.last_error, 'Not sent after 5 dispatches. Last error: Segment 503')
        self.assertCountEqual(
            [call.args[0] for call in self.mock_task.delay.call_args_list], [stale.pk, pending.pk],
        )

    def test_dispatcher_respects_the_batch_size(self):
        for index in range(3):
            self.make_emission(order_id=f'cs_{index}', status=PENDING)
        self.assertEqual(dispatch_pending_segment_events(batch_size=2)['dispatched'], 2)
        self.assertEqual(SegmentEventEmission.objects.filter(status=PENDING).count(), 1)

    def test_dispatcher_does_nothing_while_the_flag_is_off(self):
        """Recorded events wait, instead of being failed, while the feature is off."""
        pending = self.make_emission(status=PENDING)
        stale = self.make_emission(
            order_id='cs_stale', dispatch_attempts=5, last_dispatched_at=timezone.now() - timedelta(days=1),
        )
        with override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=False):
            dispatch_pending_segment_events()
        self.mock_task.delay.assert_not_called()
        for emission, status in ((pending, PENDING), (stale, DISPATCHED)):
            emission.refresh_from_db()
            self.assertEqual(emission.status, status)

    def test_dispatcher_continues_after_a_database_error_on_one_row(self):
        self.make_emission(order_id='cs_1', status=PENDING)
        self.make_emission(order_id='cs_2', status=PENDING)
        with mock.patch(f'{OUTBOX}.dispatch_segment_event', side_effect=[OperationalError('lock wait'), True]), \
                self.assertLogs(OUTBOX, level='ERROR'):
            counts = dispatch_pending_segment_events()
        self.assertEqual((counts['dispatched'], counts['not_dispatched']), (1, 1))

    def test_dispatch_after_commit_never_raises(self):
        with mock.patch(f'{OUTBOX}.dispatch_segment_event', side_effect=OperationalError('down')), \
                self.assertLogs(OUTBOX, level='ERROR'):
            _dispatch_after_commit(123)


@override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=True)
class TestCheckoutIntentAttribution(TestCase):
    """How create_intent stores attribution."""

    def setUp(self):
        super().setUp()
        self.user = UserFactory()
        self.intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme',
            attribution={key: f'{key}-value' for key in UTM_KEYS},
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
