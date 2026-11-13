"""
Tests for the server-side checkout Segment events (order_completed / order_cancelled): payloads and the send task.
"""
from unittest import mock

import ddt
import requests
import stripe
from analytics.request import APIError as SegmentAPIError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from edx_django_utils.cache import TieredCache

from enterprise_access.apps.api.serializers import (
    CheckoutIntentCreateRequestSerializer,
    CheckoutIntentReadOnlySerializer,
    CheckoutIntentUpdateRequestSerializer
)
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import (
    ATTRIBUTION_KEYS,
    CheckoutSegmentEvents,
    SegmentEventEmissionStatus
)
from enterprise_access.apps.customer_billing.models import CheckoutIntent, SegmentEventEmission
from enterprise_access.apps.customer_billing.segment_payloads import (
    IncompleteOrderPayloadError,
    build_order_cancelled_properties,
    build_order_completed_properties,
    get_line_price
)
from enterprise_access.apps.customer_billing.tasks import SegmentSendError, send_checkout_segment_event_task
from enterprise_access.apps.customer_billing.tests.utils import AttrDict

TASKS = 'enterprise_access.apps.customer_billing.tasks'
PAYLOADS = 'enterprise_access.apps.customer_billing.segment_payloads'
ORDER_COMPLETED = CheckoutSegmentEvents.ORDER_COMPLETED
ORDER_CANCELLED = CheckoutSegmentEvents.ORDER_CANCELLED
PENDING = SegmentEventEmissionStatus.PENDING
SENT = SegmentEventEmissionStatus.SENT
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


def make_price(unit_amount_decimal='1250', product='prod_abc', interval='year'):
    """A Stripe Price as ``get_stripe_price`` returns it (product not expanded)."""
    return {
        'id': f'price_{interval}',
        'object': 'price',
        'product': product,
        'unit_amount': int(float(unit_amount_decimal)),
        'unit_amount_decimal': unit_amount_decimal,
        'recurring': {'interval': interval, 'interval_count': 1},
    }


def make_line(quantity=10, unit_amount='1250', product='prod_abc', proration=False, price='price_year'):
    """Build an invoice line shaped like Stripe's: prorations share the subscription item parent type."""
    return {
        'quantity': quantity,
        'parent': {'type': 'subscription_item_details', 'subscription_item_details': {'proration': proration}},
        'pricing': {'unit_amount_decimal': unit_amount, 'price_details': {'product': product, 'price': price}},
    }


def make_invoice(total=10000, quantity=10, unit_amount='1250', invoice_id='in_123', lines=None):
    """Build the order's first invoice, shaped like a Stripe invoice retrieved without ``expand``."""
    return AttrDict.wrap({
        'id': invoice_id,
        'total': total,
        'currency': 'usd',
        'customer_email': 'pii@example.com',
        'billing_reason': 'subscription_create',
        'lines': {'data': lines if lines is not None else [make_line(quantity=quantity, unit_amount=unit_amount)]},
    })


def make_trial_invoice(invoice_id='in_123'):
    """The $0 invoice Stripe issues at checkout for a free trial; its line amounts are the trial's, not the price."""
    return make_invoice(total=0, invoice_id=invoice_id, lines=[make_line(unit_amount='0')])


def make_subscription_item(quantity=10, unit_amount_decimal='1250', product='prod_abc', interval='year'):
    return {
        'object': 'subscription_item',
        'quantity': quantity,
        'price': make_price(unit_amount_decimal=unit_amount_decimal, product=product, interval=interval),
    }


def make_subscription_data(items=None, **item_kwargs):
    """The cancelled subscription as the webhook handler passes it to the task: its id and items, as plain JSON."""
    return {
        'id': 'sub_123',
        'items': {'object': 'list', 'data': items if items is not None else [make_subscription_item(**item_kwargs)]},
    }


def make_checkout_session(invoice='in_123'):
    return stripe.checkout.Session.construct_from(
        {'id': 'cs_123', 'object': 'checkout.session', 'invoice': invoice}, 'sk_test',
    )


TRANSIENT_STRIPE_ERRORS = (
    stripe.APIConnectionError('Network error communicating with Stripe'),
    stripe.APIConnectionError('Request to Stripe timed out'),  # stripe-python raises this for timeouts
    stripe.RateLimitError('Too many requests', http_status=429),
    stripe.APIError('Stripe server error', http_status=500),
)


@override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=True)
class CheckoutSegmentTestCase(TestCase):
    """Shared setup: a CheckoutIntent with attribution and a Checkout Session ID."""

    def setUp(self):
        super().setUp()
        TieredCache.dangerous_clear_all_tiers()  # get_stripe_invoice and get_stripe_price cache
        self.user = UserFactory()
        self.intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme',
            attribution={**{key: f'{key}-value' for key in UTM_KEYS}, 'referrer': 'https://www.google.com/'},
        )
        self.intent.update_stripe_identifiers(session_id='cs_123')
        price_patcher = mock.patch(f'{PAYLOADS}.get_stripe_price', return_value=make_price())
        self.mock_price = price_patcher.start()
        self.addCleanup(price_patcher.stop)


@ddt.ddt
class TestOrderCompletedPayload(CheckoutSegmentTestCase):
    """build_order_completed_properties."""

    @ddt.data(
        # (description, invoice, expected revenue)
        # 10 licenses * $12.50 = $125.00, but a discount brought the total to $87.35.
        ('discounted purchase', make_invoice(total=8735, quantity=10), 87.35),
        # The free-trial checkout charges $0, and its line amounts are the trial's, not the price.
        ('free-trial checkout', make_trial_invoice(), 0.0),
    )
    @ddt.unpack
    def test_revenue_is_the_invoice_total_and_price_the_stripe_unit_price(self, _description, invoice, revenue):
        props = build_order_completed_properties(self.intent, invoice, 'cs_123')
        self.assertEqual(props['revenue'], revenue)
        self.assertEqual(props['price'], 12.5)  # from the Stripe Price, never price * quantity or the trial line
        self.mock_price.assert_called_once_with('price_year')
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
        """A value edited in admin is never forwarded unchecked."""
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
        invoice['lines']['data'].insert(0, make_line(quantity=1, product='prod_proration', proration=True,
                                                     price='price_proration'))
        props = build_order_completed_properties(self.intent, invoice, 'cs_123')
        self.mock_price.assert_called_once_with('price_year')
        self.assertEqual(props['total_quantity'], 10)

    def test_invoice_without_a_subscription_item_line_is_incomplete_rather_than_sent_partially(self):
        """Product, price, schedule and quantity are unknown, so nothing is sent."""
        invoice = make_invoice(total=2500, lines=[make_line(proration=True), make_line(quantity=12, proration=True)])
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

    def test_unknown_billing_interval_is_incomplete(self):
        self.mock_price.return_value = make_price(interval='week')
        with self.assertRaisesRegex(IncompleteOrderPayloadError, 'payment_schedule'):
            build_order_completed_properties(self.intent, make_invoice(), 'cs_123')


@ddt.ddt
class TestLinePrice(TestCase):
    """get_line_price, with the real (cached) get_stripe_price and only Stripe's API mocked."""

    def setUp(self):
        super().setUp()
        TieredCache.dangerous_clear_all_tiers()
        self.line = make_line(price='price_123')

    @staticmethod
    def _stripe_price(active=True):
        return stripe.Price.construct_from({
            'id': 'price_123', 'object': 'price', 'currency': 'usd', 'unit_amount': 1250, 'type': 'recurring',
            'active': active, 'product': 'prod_abc',
            'recurring': {'interval': 'year', 'interval_count': 1, 'usage_type': 'licensed'},
        }, 'sk_test')

    @ddt.data(True, False)
    def test_price_is_read_from_stripe_even_if_archived(self, active):
        """Archiving a price doesn't change the subscriptions and invoices that use it, so the order is reported."""
        with mock.patch('stripe.Price.retrieve', return_value=self._stripe_price(active=active)) as mock_retrieve:
            price = get_line_price(self.line)
        self.assertEqual((price['id'], price['active']), ('price_123', active))
        mock_retrieve.assert_called_once_with('price_123')

    def test_no_price_on_the_line(self):
        self.assertIsNone(get_line_price({}))
        self.assertIsNone(get_line_price(None))

    @ddt.data(*TRANSIENT_STRIPE_ERRORS)
    def test_transient_stripe_failure_is_raised_for_retry_and_not_cached(self, error):
        """Connection errors, timeouts, 429 and 5xx: a passing outage must not look like missing pricing data."""
        with mock.patch('stripe.Price.retrieve', side_effect=error):
            with self.assertRaises(type(error)) as ctx:
                get_line_price(self.line)
        self.assertIs(ctx.exception, error)
        with mock.patch('stripe.Price.retrieve', return_value=self._stripe_price()):
            self.assertEqual(get_line_price(self.line)['id'], 'price_123')

    @ddt.data(
        stripe.InvalidRequestError('No such price: price_123', 'price', http_status=404),
        stripe.AuthenticationError('Invalid API key', http_status=401),
    )
    def test_stripe_error_a_retry_cannot_fix_means_no_price(self, error):
        with mock.patch('stripe.Price.retrieve', side_effect=error):
            self.assertIsNone(get_line_price(self.line))


@ddt.ddt
class TestOrderCancelledPayload(CheckoutSegmentTestCase):
    """
    build_order_cancelled_properties: product, price, schedule and quantity from the cancelled subscription; order_id
    and revenue from the order (the Checkout Session and its first invoice).
    """

    @ddt.data(
        # (description, subscription quantity at cancellation, order invoice total, expected revenue)
        ('original purchase', 10, 12500, 125.0),
        ('license quantity increased', 15, 12500, 125.0),
        ('license quantity decreased', 5, 12500, 125.0),
        ('cancelled during the free trial', 10, 0, 0.0),
    )
    @ddt.unpack
    def test_quantity_is_the_subscription_quantity_at_cancellation(self, _description, quantity, total, revenue):
        order_invoice = make_invoice(total=total, quantity=10, invoice_id='in_order')
        props = build_order_cancelled_properties(
            self.intent, order_invoice, make_subscription_data(quantity=quantity), 'cs_123',
        )
        self.assertEqual(props['total_quantity'], quantity)
        self.assertEqual(props['order_id'], 'cs_123')
        self.assertEqual(props['revenue'], revenue)  # the order's invoice total, whatever changed later
        self.assertEqual(props['currency'], 'USD')
        self.assertEqual(props['product_id'], 'prod_abc')
        self.assertEqual(props['price'], 12.5)
        self.assertEqual(props['payment_schedule'], 'yearly')
        self.assertLessEqual(set(props), APPROVED_PAYLOAD_KEYS)
        self.mock_price.assert_not_called()  # the price comes with the subscription item

    def test_after_multiple_changes_the_final_subscription_state_is_used(self):
        """Quantity and price changed (e.g. moved to a monthly price): the cancelled subscription has the last state."""
        subscription = make_subscription_data(
            quantity=7, unit_amount_decimal='999', product='prod_new', interval='month',
        )
        props = build_order_cancelled_properties(self.intent, make_invoice(total=12500), subscription, 'cs_123')
        self.assertEqual(props['total_quantity'], 7)
        self.assertEqual(props['product_id'], 'prod_new')
        self.assertEqual(props['price'], 9.99)
        self.assertEqual(props['payment_schedule'], 'monthly')
        self.assertEqual(props['revenue'], 125.0)

    @ddt.data(
        # (description, changes to the item's price, property, expected value)
        ('price falls back to the integer unit_amount', {'unit_amount_decimal': None, 'unit_amount': 1500},
         'price', 15.0),
        ('expanded product is reduced to its id', {'product': {'id': 'prod_abc', 'object': 'product'}},
         'product_id', 'prod_abc'),
    )
    @ddt.unpack
    def test_price_fields_of_the_subscription_item(self, _description, price_changes, key, expected):
        item = make_subscription_item()
        item['price'].update(price_changes)
        props = build_order_cancelled_properties(
            self.intent, make_invoice(), make_subscription_data(items=[item]), 'cs_1',
        )
        self.assertEqual(props[key], expected)

    def test_stripe_object_subscription_is_read_like_json(self):
        """``items`` collides with ``dict.items`` on a StripeObject; the payload reads it by key either way."""
        subscription = stripe.Subscription.construct_from(
            {'object': 'subscription', **make_subscription_data(quantity=3)}, 'sk_test',
        )
        props = build_order_cancelled_properties(self.intent, make_invoice(), subscription, 'cs_1')
        self.assertEqual(props['total_quantity'], 3)

    def test_no_attribution_on_order_cancelled(self):
        props = build_order_cancelled_properties(self.intent, make_invoice(), make_subscription_data(), 'cs_123')
        self.assertFalse(set(ATTRIBUTION_KEYS) & set(props))

    @ddt.data(0, 2)
    def test_subscription_without_exactly_one_item_is_incomplete(self, item_count):
        items = [make_subscription_item() for _ in range(item_count)]
        with self.assertRaisesRegex(IncompleteOrderPayloadError, f'{item_count} items'):
            build_order_cancelled_properties(
                self.intent, make_invoice(), make_subscription_data(items=items), 'cs_123',
            )

    def test_unknown_billing_interval_is_incomplete(self):
        with self.assertRaisesRegex(IncompleteOrderPayloadError, 'payment_schedule'):
            build_order_cancelled_properties(
                self.intent, make_invoice(), make_subscription_data(interval='week'), 'cs_123',
            )


@ddt.ddt
class TestSendCheckoutSegmentEventTask(CheckoutSegmentTestCase):
    """send_checkout_segment_event_task: recording, sending, retries and status transitions."""

    def setUp(self):
        super().setUp()
        invoice_patcher = mock.patch(f'{TASKS}.get_stripe_invoice', return_value=make_trial_invoice())
        session_patcher = mock.patch(f'{TASKS}.get_stripe_checkout_session', return_value=make_checkout_session())
        self.mock_get_stripe_invoice = invoice_patcher.start()
        self.mock_get_checkout_session = session_patcher.start()
        self.addCleanup(invoice_patcher.stop)
        self.addCleanup(session_patcher.stop)

    def task_args(self, event_name=ORDER_COMPLETED, **kwargs):
        """The task's arguments, as the webhook handlers queue it."""
        if event_name == ORDER_COMPLETED:
            defaults = {'invoice_id': 'in_123'}
        else:
            defaults = {'subscription_data': make_subscription_data()}
        return (event_name, self.intent.id, 'sub_123'), {**defaults, **kwargs}

    def run_task(self, event_name=ORDER_COMPLETED, **kwargs):
        args, task_kwargs = self.task_args(event_name, **kwargs)
        return send_checkout_segment_event_task(*args, **task_kwargs)

    def apply_task(self, event_name=ORDER_COMPLETED, **kwargs):
        """Run the task as Celery does, with its automatic retries."""
        args, task_kwargs = self.task_args(event_name, **kwargs)
        return send_checkout_segment_event_task.apply(args=args, kwargs=task_kwargs, throw=True).get()

    def run_task_expecting_retry(self, retry_exc_type, event_name=ORDER_COMPLETED, **kwargs):
        """Run the task as Celery does, with ``retry`` stubbed to raise; returns the retry mock."""
        with mock.patch.object(
            send_checkout_segment_event_task, 'retry', side_effect=retry_exc_type('retry'),
        ) as mock_retry, self.assertRaises(retry_exc_type):
            self.apply_task(event_name, **kwargs)
        return mock_retry

    def emission(self, event_name=ORDER_COMPLETED):
        return SegmentEventEmission.objects.get(event_name=event_name, order_id='cs_123')

    def make_emission(self, event_name=ORDER_COMPLETED, invoice_id='in_123', status=PENDING):
        return SegmentEventEmission.objects.create(
            event_name=event_name, order_id='cs_123', checkout_intent=self.intent, stripe_subscription_id='sub_123',
            stripe_invoice_id=invoice_id, status=status,
        )

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_trial_checkout_sends_order_completed_with_zero_revenue(self, mock_send):
        self.run_task(ORDER_COMPLETED)

        self.mock_get_stripe_invoice.assert_called_once_with('in_123')
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs['event_name'], ORDER_COMPLETED)
        self.assertEqual(mock_send.call_args.kwargs['lms_user_id'], str(self.user.lms_user_id))
        properties = mock_send.call_args.kwargs['properties']
        self.assertEqual(properties['order_id'], 'cs_123')
        self.assertEqual(properties['revenue'], 0.0)
        self.assertEqual(properties['price'], 12.5)
        self.assertEqual(properties['utm_source'], 'utm_source-value')
        emission = self.emission()
        self.assertEqual(
            (emission.checkout_intent, emission.stripe_subscription_id, emission.stripe_invoice_id, emission.status),
            (self.intent, 'sub_123', 'in_123', SENT),
        )

    @ddt.data(ORDER_COMPLETED, ORDER_CANCELLED)
    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_task_sends_once_per_order_and_event(self, event_name, mock_send):
        for _ in range(3):  # each webhook redelivery queues the task again
            self.run_task(event_name)

        self.mock_get_stripe_invoice.assert_called_once()  # finished rows skip the Stripe fetch
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs['event_name'], event_name)
        self.assertEqual(mock_send.call_args.kwargs['properties']['order_id'], 'cs_123')
        # Deterministic, so Segment drops a resend after a success that failed to be recorded.
        self.assertEqual(mock_send.call_args.kwargs['message_id'], f'{event_name}:cs_123')
        self.assertEqual(SegmentEventEmission.objects.filter(event_name=event_name).count(), 1)
        emission = self.emission(event_name)
        self.assertEqual(emission.status, SENT)
        self.assertIsNotNone(emission.emitted_at)
        self.assertIsNone(emission.last_error)

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_same_order_different_events_both_sent(self, mock_send):
        self.run_task(ORDER_COMPLETED)
        self.run_task(ORDER_CANCELLED)
        self.assertEqual(mock_send.call_count, 2)

    @override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=False)
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_flag_off_sends_and_records_nothing(self, mock_send):
        self.run_task(ORDER_COMPLETED)
        self.mock_get_stripe_invoice.assert_not_called()
        mock_send.assert_not_called()
        self.assertFalse(SegmentEventEmission.objects.exists())

    @ddt.data(
        # (event, the Stripe call that fails, error)
        *[(ORDER_COMPLETED, 'price', error) for error in TRANSIENT_STRIPE_ERRORS],
        (ORDER_COMPLETED, 'invoice', stripe.APIConnectionError('Network error communicating with Stripe')),
        (ORDER_CANCELLED, 'checkout_session', stripe.RateLimitError('Too many requests', http_status=429)),
    )
    @ddt.unpack
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_transient_stripe_failure_is_retried_and_leaves_the_row_pending(
        self, event_name, failing_call, error, mock_send,
    ):
        """Connection errors, timeouts, 429 and 5xx: retried, never sent incomplete, and the row is left as it was."""
        failing_mock = {
            'price': self.mock_price,
            'invoice': self.mock_get_stripe_invoice,
            'checkout_session': self.mock_get_checkout_session,
        }[failing_call]
        failing_mock.side_effect = error

        mock_retry = self.run_task_expecting_retry(type(error), event_name)

        self.assertIs(mock_retry.call_args.kwargs['exc'], error)
        mock_send.assert_not_called()
        emission = self.emission(event_name)
        self.assertEqual(emission.status, PENDING)
        self.assertEqual(emission.stripe_invoice_id, 'in_123' if event_name == ORDER_COMPLETED else None)

    @override_settings(SEGMENT_KEY='test-key')
    @mock.patch('analytics.request._session')
    def test_segment_outage_is_retryable_and_marked_sent_only_after_success(self, mock_session):
        """
        Runs the real analytics-python client with only its HTTP session mocked: a Segment outage reaches the task
        (it is not swallowed on a background thread), and the row is set to SENT only once Segment returns 200.
        """
        mock_session.post.return_value = mock.Mock(status_code=503, **{'json.return_value': {
            'code': 'unavailable', 'message': 'Segment is down',
        }})

        with self.assertRaises(SegmentSendError):
            self.run_task()
        emission = self.emission()
        self.assertEqual(emission.status, PENDING)  # a failed send is never recorded as sent
        self.assertIsNone(emission.emitted_at)

        mock_session.post.return_value = mock.Mock(status_code=200)
        self.run_task()
        self.assertEqual(self.emission().status, SENT)
        self.run_task()
        self.assertEqual(mock_session.post.call_count, 2)  # the failed attempt and one success, no duplicate

    @ddt.data(
        requests.ConnectionError('Segment unreachable'),
        requests.Timeout('Segment timed out'),
        SegmentAPIError(503, 'unavailable', 'Segment is down'),
        SegmentAPIError(429, 'rate_limited', 'slow down'),
    )
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_segment_failure_a_later_attempt_can_fix_is_retried(self, error, mock_send):
        mock_send.side_effect = error
        mock_retry = self.run_task_expecting_retry(SegmentSendError)
        self.assertIsInstance(mock_retry.call_args.kwargs['exc'], SegmentSendError)
        self.assertEqual(self.emission().status, PENDING)

    @ddt.data(
        SegmentAPIError(400, 'invalid_request', 'bad payload'),
        AssertionError('user_id must be a string or number'),
    )
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_segment_failure_that_would_repeat_fails_the_row_without_retry(self, error, mock_send):
        """A 4xx other than 429, or invalid arguments, would fail the same way on every retry."""
        mock_send.side_effect = error
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry, \
                self.assertRaises(type(error)):
            self.apply_task()
        mock_retry.assert_not_called()
        emission = self.emission()
        self.assertEqual(emission.status, FAILED)
        self.assertIn('Segment rejected the event', emission.last_error)

    @mock.patch('analytics.request._session')
    def test_not_marked_sent_when_segment_key_unset(self, mock_session):
        with override_settings(SEGMENT_KEY=None):
            self.run_task()
        mock_session.post.assert_not_called()
        self.assertEqual(self.emission().status, PENDING)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_invalid_request_error_fails_the_row_and_is_not_silenced(self, mock_send):
        self.mock_get_stripe_invoice.side_effect = stripe.InvalidRequestError('No such invoice', 'id')
        with self.assertRaises(stripe.InvalidRequestError):
            self.run_task()
        mock_send.assert_not_called()
        self.assertEqual(self.emission().status, FAILED)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_no_lms_user_id_fails_the_row_before_any_stripe_call(self, mock_send):
        """The local DB user id is not an LMS user id, so it must never be used as a fallback."""
        self.user.lms_user_id = None
        self.user.save()

        with self.assertLogs(TASKS, level='WARNING') as logs:
            self.run_task()

        mock_send.assert_not_called()
        self.mock_get_stripe_invoice.assert_not_called()
        self.assertIn(str(self.intent.id), '\n'.join(logs.output))
        self.assertEqual(self.emission().status, FAILED)

    @ddt.data(
        # (event, whether the CheckoutIntent still exists)
        (ORDER_COMPLETED, False),
        (ORDER_CANCELLED, True),  # but without a Checkout Session ID
    )
    @ddt.unpack
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_nothing_is_recorded_without_an_order_id(self, event_name, intent_exists, mock_send):
        """
        Logged and not retried: without the CheckoutIntent, or its Checkout Session ID, there is no stable order_id,
        and it is never replaced by another ID.
        """
        args, kwargs = self.task_args(event_name)
        if intent_exists:
            self.intent.stripe_checkout_session_id = None
            self.intent.save()
        else:
            args = (event_name, 999999, 'sub_123')
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry, \
                self.assertLogs(TASKS, level='ERROR'):
            send_checkout_segment_event_task.apply(args=args, kwargs=kwargs, throw=True).get()
        mock_retry.assert_not_called()
        self.mock_get_stripe_invoice.assert_not_called()
        mock_send.assert_not_called()
        self.assertFalse(SegmentEventEmission.objects.exists())

    @ddt.data(SENT, FAILED)
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_finished_row_is_never_sent_again(self, status, mock_send):
        self.make_emission(status=status)
        self.run_task()
        self.mock_get_stripe_invoice.assert_not_called()
        mock_send.assert_not_called()
        self.assertEqual(self.emission().status, status)

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_stale_copy_never_moves_a_finished_row_back(self, _mock_send):
        """Two copies loaded before either writes: the one that loses cannot overwrite the SENT row."""
        emission = self.make_emission()
        stale_copy = SegmentEventEmission.objects.get(pk=emission.pk)
        self.run_task()
        self.assertFalse(stale_copy.finish(FAILED, 'late failure'))
        emission.refresh_from_db()
        self.assertEqual(emission.status, SENT)
        self.assertIsNone(emission.last_error)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_incomplete_payload_fails_the_row_and_is_never_sent(self, mock_send):
        self.mock_get_stripe_invoice.return_value = make_invoice(lines=[make_line(proration=True)])
        with self.assertLogs(TASKS, level='ERROR'):
            self.run_task()
        mock_send.assert_not_called()
        emission = self.emission()
        self.assertEqual(emission.status, FAILED)
        self.assertIn('product_id', emission.last_error)

    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_successful_retry_after_a_transient_pricing_failure(self, mock_send):
        """Celery's own (eager) retry: the first attempt can't reach Stripe, the retry sends the complete event."""
        self.mock_price.side_effect = [stripe.APIConnectionError('timed out'), make_price()]
        args, kwargs = self.task_args()

        send_checkout_segment_event_task.apply(args=args, kwargs=kwargs)

        self.assertEqual(self.mock_price.call_count, 2)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs['properties']['payment_schedule'], 'yearly')
        self.assertEqual(self.emission().status, SENT)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_genuinely_unavailable_pricing_fails_the_row_without_retry(self, mock_send):
        self.mock_price.side_effect = stripe.InvalidRequestError('No such price', 'price')
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry:
            self.apply_task()
        mock_retry.assert_not_called()
        mock_send.assert_not_called()
        emission = self.emission()
        self.assertEqual(emission.status, FAILED)
        self.assertIn('payment_schedule', emission.last_error)

    @ddt.data(
        # (event_name, task kwargs, expected error)
        (ORDER_COMPLETED, {'invoice_id': None}, 'without its invoice'),
        (ORDER_CANCELLED, {'subscription_data': None}, 'without the cancelled subscription'),
        ('edx.ui.enterprise.checkout.unknown', {}, 'Unknown checkout Segment event'),
    )
    @ddt.unpack
    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_event_that_cannot_be_built_fails(self, event_name, task_kwargs, expected_error, mock_send):
        with self.assertLogs(TASKS, level='ERROR'):
            self.run_task(event_name, **task_kwargs)
        mock_send.assert_not_called()
        emission = self.emission(event_name)
        self.assertEqual(emission.status, FAILED)
        self.assertIn(expected_error, emission.last_error)

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_row_finished_by_a_concurrent_worker_while_building_is_not_sent_again(self, mock_send):
        """The authoritative check happens under the row lock, after the payload was built."""
        def build_while_another_worker_sends(emission, _subscription_data):
            SegmentEventEmission.objects.filter(pk=emission.pk).update(status=SENT, emitted_at=timezone.now())
            return self.user.lms_user_id, {'order_id': 'cs_123'}

        with mock.patch(f'{TASKS}._build_checkout_segment_event', side_effect=build_while_another_worker_sends):
            self.run_task()

        mock_send.assert_not_called()
        self.assertEqual(self.emission().status, SENT)

    @ddt.data(
        # (description, order invoice total, expected revenue, the CheckoutIntent's Checkout Session ID by now)
        ('paid order', 12500, 125.0, 'cs_123'),
        ('cancelled during the free trial: still an order', 0, 0.0, 'cs_123'),
        # The customer started another checkout after paying, which overwrote the CheckoutIntent's session ID.
        ('Checkout Session ID overwritten since the order', 12500, 125.0, 'cs_later'),
    )
    @ddt.unpack
    @mock.patch(f'{TASKS}.send_event_synchronously', return_value=True)
    def test_cancellation_reuses_the_order_id_and_invoice_of_order_completed(
        self, _description, total, revenue, session_id, mock_send,
    ):
        self.make_emission(ORDER_COMPLETED, invoice_id='in_order', status=SENT)
        self.intent.update_stripe_identifiers(session_id=session_id)
        self.mock_get_stripe_invoice.return_value = make_invoice(total=total, invoice_id='in_order')

        self.run_task(ORDER_CANCELLED, subscription_data=make_subscription_data(quantity=15))

        self.mock_get_checkout_session.assert_not_called()
        self.mock_get_stripe_invoice.assert_called_once_with('in_order')
        self.assertEqual(mock_send.call_args.kwargs['message_id'], f'{ORDER_CANCELLED}:cs_123')
        properties = mock_send.call_args.kwargs['properties']
        self.assertEqual(properties['order_id'], 'cs_123')
        self.assertEqual(properties['revenue'], revenue)
        self.assertEqual(properties['total_quantity'], 15)
        self.assertEqual(properties['price'], 12.5)
        self.assertEqual(properties['payment_schedule'], 'yearly')
        cancellation = self.emission(ORDER_CANCELLED)
        self.assertEqual((cancellation.stripe_invoice_id, cancellation.status), ('in_order', SENT))

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_cancellation_of_an_order_placed_before_the_feature_reads_the_checkout_session(self, mock_send):
        """With no order_completed row, the order's invoice is the one its Checkout Session created."""
        self.mock_get_checkout_session.return_value = make_checkout_session(invoice='in_checkout')
        mock_send.side_effect = SegmentAPIError(503, 'unavailable', 'Segment is down')

        with self.assertRaises(SegmentSendError):
            self.run_task(ORDER_CANCELLED)
        mock_send.side_effect = None
        mock_send.return_value = True
        self.run_task(ORDER_CANCELLED)  # the retry reuses the invoice saved on the row

        self.mock_get_checkout_session.assert_called_once_with('cs_123')
        self.mock_get_stripe_invoice.assert_called_with('in_checkout')
        cancellation = self.emission(ORDER_CANCELLED)
        self.assertEqual((cancellation.stripe_invoice_id, cancellation.status), ('in_checkout', SENT))

    @mock.patch(f'{TASKS}.send_event_synchronously')
    def test_cancellation_without_any_order_invoice_fails(self, mock_send):
        self.mock_get_checkout_session.return_value = make_checkout_session(invoice=None)
        with self.assertLogs(TASKS, level='ERROR'):
            self.run_task(ORDER_CANCELLED)
        mock_send.assert_not_called()
        cancellation = self.emission(ORDER_CANCELLED)
        self.assertEqual(cancellation.status, FAILED)
        self.assertIn('Found no invoice for order cs_123', cancellation.last_error)

    def test_unique_constraint_on_event_and_order(self):
        self.make_emission()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_emission(invoice_id='in_other')

    def test_emission_str(self):
        emission = SegmentEventEmission(event_name='e', order_id='o', status=PENDING)
        self.assertEqual(str(emission), 'e o (pending)')


@ddt.ddt
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

    @ddt.data(
        # (description, existing intent has no attribution, sent attribution, expected attribution)
        ('a later campaign replaces the earlier one whole, never merged', False, {'utm_source': 'bing'},
         {'utm_source': 'bing'}),
        ('set on an existing intent without any', True, {'utm_source': 'bing'}, {'utm_source': 'bing'}),
        ('a call without attribution keeps the existing one', False, None, {key: f'{key}-value' for key in UTM_KEYS}),
    )
    @ddt.unpack
    def test_create_intent_on_an_existing_intent(self, _description, existing_has_none, sent, expected):
        if existing_has_none:
            self.intent.attribution = None
            self.intent.save()
        intent = CheckoutIntent.create_intent(
            user=self.user, quantity=10, slug='acme', name='Acme', attribution=sent,
        )
        intent.refresh_from_db()
        self.assertEqual(intent.id, self.intent.id)
        self.assertEqual(intent.attribution, expected)

    @mock.patch('enterprise_access.apps.customer_billing.signals.track_event')
    def test_api_responses_and_lifecycle_event_never_carry_attribution(self, mock_track_event):
        """
        Attribution is only sent on order_completed. The read, create and update serializers leave it out of their
        output, so it is in no API response, nor in the lifecycle Segment event that the post_save signal builds
        from the read serializer.
        """
        intent = CheckoutIntent.create_intent(
            user=UserFactory(), slug='globex', name='Globex', quantity=10,
            attribution={'utm_source': 'newsletter', 'referrer': 'https://www.google.com/'},
        )

        for serializer_class in (
            CheckoutIntentReadOnlySerializer, CheckoutIntentCreateRequestSerializer,
            CheckoutIntentUpdateRequestSerializer,
        ):
            with self.subTest(serializer=serializer_class.__name__):
                self.assertNotIn('attribution', serializer_class(intent).data)
        mock_track_event.assert_called_once()
        properties = mock_track_event.call_args.kwargs['properties']
        self.assertNotIn('attribution', properties)
        self.assertNotIn('newsletter', str(properties))
        self.assertEqual(properties['new_state'], 'created')
