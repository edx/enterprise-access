"""
Tests for the server-side checkout Segment events (order_completed / order_cancelled): payloads and the send task.
"""
from datetime import timedelta
from unittest import mock

import ddt
import stripe
from analytics.request import APIError
from celery.exceptions import Retry
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import CheckoutSegmentEvents
from enterprise_access.apps.customer_billing.models import CheckoutIntent, StripeEventSummary
from enterprise_access.apps.customer_billing.segment_payloads import (
    AcademyTitleUnavailableError,
    IncompleteOrderPayloadError,
    build_order_cancelled_properties,
    build_order_completed_properties
)
from enterprise_access.apps.customer_billing.tasks import (
    SEGMENT_EVENT_DROPPED_LOG_PREFIX,
    send_checkout_segment_event_task
)
from enterprise_access.apps.customer_billing.tests.factories import StripeEventDataFactory
from enterprise_access.apps.track.segment import SEGMENT_DELIVERY_ERRORS

TASKS = 'enterprise_access.apps.customer_billing.tasks'
PAYLOADS = 'enterprise_access.apps.customer_billing.segment_payloads'
ORDER_COMPLETED = CheckoutSegmentEvents.ORDER_COMPLETED
ORDER_CANCELLED = CheckoutSegmentEvents.ORDER_CANCELLED
# Every key a checkout payload may carry. A new payload key must be added here deliberately, after checking that
# it carries no PII.
APPROVED_PAYLOAD_KEYS = {
    'product_id', 'sku', 'category', 'name', 'brand', 'variant', 'price', 'slug', 'payment_schedule',
    'order_id', 'total_quantity', 'revenue', 'currency',
}
PII_KEYS = {
    'email', 'name_on_card', 'cardholder_name', 'billing_details', 'billing_address',
    'last4', 'customer_name', 'token', 'customer_email', 'address',
}


def make_price(unit_amount_decimal='1250', product='prod_abc', interval='year'):
    """A Stripe Price as a subscription item carries it (product not expanded)."""
    return {
        'id': f'price_{interval}',
        'object': 'price',
        'product': product,
        'unit_amount': int(float(unit_amount_decimal)),
        'unit_amount_decimal': unit_amount_decimal,
        'recurring': {'interval': interval, 'interval_count': 1},
    }


def make_subscription_item(quantity=10, unit_amount_decimal='1250', product='prod_abc', interval='year'):
    return {
        'object': 'subscription_item',
        'quantity': quantity,
        'price': make_price(unit_amount_decimal=unit_amount_decimal, product=product, interval=interval),
    }


def make_subscription_data(items=None, **item_kwargs):
    """The subscription's id and items, as plain JSON (how the webhook handler passes a cancelled one to the task)."""
    return {
        'id': 'sub_123',
        'items': {'object': 'list', 'data': items if items is not None else [make_subscription_item(**item_kwargs)]},
    }


def make_stripe_subscription(**kwargs):
    """The subscription as ``get_stripe_subscription`` returns it."""
    return stripe.Subscription.construct_from(
        {'object': 'subscription', **make_subscription_data(**kwargs)}, 'sk_test',
    )


def make_invoice_summary(amount_paid=10000, quantity=10, currency='usd'):
    """The ``invoice.paid`` StripeEventSummary of the order's first invoice (unsaved: payloads only read it)."""
    return StripeEventSummary(invoice_amount_paid=amount_paid, invoice_quantity=quantity, invoice_currency=currency)


TRANSIENT_STRIPE_ERRORS = (
    stripe.APIConnectionError('Network error or timeout communicating with Stripe'),
    stripe.RateLimitError('Too many requests', http_status=429),
    stripe.APIError('Stripe server error', http_status=500),
)


@override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=True)
class CheckoutSegmentTestCase(TestCase):
    """Shared setup: a CheckoutIntent with a Checkout Session ID."""

    def setUp(self):
        super().setUp()
        self.user = UserFactory()
        self.intent = CheckoutIntent.create_intent(user=self.user, quantity=10, slug='acme', name='Acme')
        self.intent.update_stripe_identifiers(session_id='cs_123')

    def assert_properties(self, props, **expected):
        self.assertEqual({key: props.get(key) for key in expected}, expected)
        self.assertLessEqual(set(props), APPROVED_PAYLOAD_KEYS)
        self.assertFalse(PII_KEYS & set(props))


@ddt.ddt
class TestOrderCompletedPayload(CheckoutSegmentTestCase):
    """build_order_completed_properties."""

    @ddt.data(
        # (amount paid on the first invoice, expected revenue)
        (8735, 87.35),  # a discount: not 10 licenses * $12.50
        (0, 0.0),  # free trial: $0 charged
    )
    @ddt.unpack
    def test_revenue_is_the_amount_paid_and_price_the_stripe_unit_price(self, amount_paid, revenue):
        props = build_order_completed_properties(
            self.intent, 'cs_123', make_stripe_subscription(), make_invoice_summary(amount_paid=amount_paid),
        )
        self.assert_properties(
            props, revenue=revenue, price=12.5, total_quantity=10, order_id='cs_123', product_id='prod_abc',
            payment_schedule='yearly', currency='USD', category='subscription', brand='enterprise',
            slug=self.intent.ssp_product.slug, name='teams', variant=None,
        )

    def test_quantity_is_the_quantity_ordered(self):
        """Licenses changed before the task ran: the event reports the order's quantity, from its invoice."""
        props = build_order_completed_properties(
            self.intent, 'cs_123', make_stripe_subscription(quantity=15), make_invoice_summary(quantity=10),
        )
        self.assertEqual(props['total_quantity'], 10)

    def test_variant_present_for_essentials(self):
        product = self.intent.ssp_product
        with mock.patch.object(type(product), 'academy_title', new_callable=mock.PropertyMock) as title, \
                mock.patch(f'{PAYLOADS}.get_product_type', return_value='essentials'):
            title.return_value = 'AI Academy'
            props = build_order_completed_properties(
                self.intent, 'cs_123', make_stripe_subscription(), make_invoice_summary(),
            )
        self.assert_properties(props, name='essentials', variant='AI Academy')

    def test_essentials_without_academy_title_is_retried_not_sent_without_variant(self):
        """``academy_title`` is None when the catalog call fails; the payload is never sent without it."""
        product = self.intent.ssp_product
        with mock.patch.object(type(product), 'academy_title', new_callable=mock.PropertyMock, return_value=None), \
                mock.patch(f'{PAYLOADS}.get_product_type', return_value='essentials'):
            with self.assertRaises(AcademyTitleUnavailableError):
                build_order_completed_properties(
                    self.intent, 'cs_123', make_stripe_subscription(), make_invoice_summary(),
                )

    @ddt.data(
        # (subscription, invoice summary, missing properties named in the error)
        (make_stripe_subscription(interval='week'), make_invoice_summary(), 'payment_schedule'),
        (make_stripe_subscription(), make_invoice_summary(amount_paid=None, currency=None), 'revenue, currency'),
        (make_stripe_subscription(), make_invoice_summary(quantity=None), 'total_quantity'),
        (make_stripe_subscription(items=[]), make_invoice_summary(), '0 items'),
    )
    @ddt.unpack
    def test_incomplete_payload_is_never_built(self, subscription, invoice_summary, missing):
        with self.assertRaisesRegex(IncompleteOrderPayloadError, missing):
            build_order_completed_properties(self.intent, 'cs_123', subscription, invoice_summary)


@ddt.ddt
class TestOrderCancelledPayload(CheckoutSegmentTestCase):
    """
    build_order_cancelled_properties: product, price and quantity from the cancelled subscription; revenue from the
    order's first invoice.
    """

    @ddt.data(
        # (subscription quantity at cancellation, amount paid on the order's first invoice, expected revenue)
        (15, 12500, 125.0),  # licenses added since: revenue is still the order's
        (5, 12500, 125.0),  # licenses removed since
        (10, 0, 0.0),  # cancelled during the free trial
    )
    @ddt.unpack
    def test_quantity_at_cancellation_and_revenue_of_the_order(self, quantity, amount_paid, revenue):
        props = build_order_cancelled_properties(
            self.intent, 'cs_123', make_subscription_data(quantity=quantity),
            make_invoice_summary(amount_paid=amount_paid),
        )
        self.assert_properties(
            props, total_quantity=quantity, order_id='cs_123', revenue=revenue, currency='USD',
            product_id='prod_abc', price=12.5, payment_schedule='yearly',
        )

    @ddt.data(
        # (changes to the item's price, expected properties)
        ({'product': 'prod_new', 'unit_amount_decimal': '999', 'recurring': {'interval': 'month'}},
         {'product_id': 'prod_new', 'price': 9.99, 'payment_schedule': 'monthly'}),
        ({'unit_amount_decimal': None, 'unit_amount': 1500}, {'price': 15.0}),
        ({'product': {'id': 'prod_abc', 'object': 'product'}}, {'product_id': 'prod_abc'}),  # expanded product
    )
    @ddt.unpack
    def test_price_fields_of_the_final_subscription_item(self, price_changes, expected):
        item = make_subscription_item()
        item['price'].update(price_changes)
        props = build_order_cancelled_properties(
            self.intent, 'cs_123', make_subscription_data(items=[item]), make_invoice_summary(),
        )
        self.assert_properties(props, **expected)

    def test_stripe_object_subscription_is_read_like_json(self):
        """``items`` collides with ``dict.items`` on a StripeObject; the payload reads it by key either way."""
        props = build_order_cancelled_properties(
            self.intent, 'cs_123', make_stripe_subscription(quantity=3), make_invoice_summary(),
        )
        self.assertEqual(props['total_quantity'], 3)

    @ddt.data(
        (make_subscription_data(items=[]), '0 items'),
        (make_subscription_data(items=[make_subscription_item(), make_subscription_item()]), '2 items'),
        (make_subscription_data(interval='week'), 'payment_schedule'),
        # A price without the recurring key at all.
        (make_subscription_data(items=[{'quantity': 1, 'price': {'product': 'prod_abc', 'unit_amount': 1}}]),
         'payment_schedule'),
    )
    @ddt.unpack
    def test_incomplete_payload_is_never_built(self, subscription, error):
        with self.assertRaisesRegex(IncompleteOrderPayloadError, error):
            build_order_cancelled_properties(self.intent, 'cs_123', subscription, make_invoice_summary())


@ddt.ddt
class TestSendCheckoutSegmentEventTask(CheckoutSegmentTestCase):
    """send_checkout_segment_event_task: what is sent, retries, and events that can't be sent."""

    def setUp(self):
        super().setUp()
        self.first_invoice_summary = self.create_invoice_paid_summary('in_123', amount_paid=0)
        patchers = {
            'subscription': mock.patch(f'{TASKS}.get_stripe_subscription', return_value=make_stripe_subscription()),
            'track': mock.patch(f'{TASKS}.track_event_or_raise', return_value=True),
        }
        self.mocks = {}
        for key, patcher in patchers.items():
            self.mocks[key] = patcher.start()
            self.addCleanup(patcher.stop)

    def create_invoice_paid_summary(self, invoice_id, amount_paid, subscription_id='sub_123', hours_ago=1):
        """
        The StripeEventSummary the webhook persists for an ``invoice.paid`` event (via StripeEventData's signal),
        with the fields the task reads.
        """
        event_data = StripeEventDataFactory(event_type='invoice.paid', checkout_intent=self.intent)
        StripeEventSummary.objects.filter(stripe_event_data=event_data).update(
            stripe_invoice_id=invoice_id,
            stripe_subscription_id=subscription_id,
            invoice_amount_paid=amount_paid,
            invoice_quantity=10,
            invoice_currency='usd',
            stripe_event_created_at=timezone.now() - timedelta(hours=hours_ago),
        )
        return StripeEventSummary.objects.get(stripe_event_data=event_data)

    def task_args(self, event_name=ORDER_COMPLETED, **kwargs):
        """The task's arguments, as the webhook handlers queue it."""
        if event_name == ORDER_COMPLETED:
            defaults = {'invoice_id': 'in_123'}
        else:
            defaults = {'subscription_data': make_subscription_data(quantity=12)}
        return (event_name, self.intent.id, 'sub_123'), {**defaults, **kwargs}

    def run_task(self, event_name=ORDER_COMPLETED, **kwargs):
        args, task_kwargs = self.task_args(event_name, **kwargs)
        return send_checkout_segment_event_task(*args, **task_kwargs)

    def sent(self):
        """The kwargs ``track_event_or_raise`` was called with."""
        self.mocks['track'].assert_called_once()
        return self.mocks['track'].call_args.kwargs

    def test_trial_checkout_sends_order_completed_with_zero_revenue(self):
        self.run_task(ORDER_COMPLETED)

        self.mocks['subscription'].assert_called_once_with('sub_123')
        kwargs = self.sent()
        self.assertEqual(kwargs['event_name'], ORDER_COMPLETED)
        self.assertEqual(kwargs['lms_user_id'], str(self.user.lms_user_id))
        # Deterministic, so Segment drops a resend it has already seen.
        self.assertEqual(kwargs['message_id'], f'{ORDER_COMPLETED}:cs_123')
        props = kwargs['properties']
        self.assertEqual(
            (props['order_id'], props['revenue'], props['price'], props['total_quantity']),
            ('cs_123', 0.0, 12.5, 10),
        )

    def test_order_cancelled_reports_the_first_invoice_and_calls_no_stripe_api(self):
        """The trial's $0 invoice is the order's, not the later trial-end invoice."""
        self.create_invoice_paid_summary('in_trial_end', amount_paid=12500, hours_ago=0)
        self.create_invoice_paid_summary('in_other_subscription', amount_paid=999, subscription_id='sub_other',
                                         hours_ago=2)

        self.run_task(ORDER_CANCELLED)

        self.mocks['subscription'].assert_not_called()
        kwargs = self.sent()
        self.assertEqual(kwargs['event_name'], ORDER_CANCELLED)
        self.assertEqual(kwargs['message_id'], f'{ORDER_CANCELLED}:cs_123')
        props = kwargs['properties']
        self.assertEqual((props['order_id'], props['revenue'], props['total_quantity']), ('cs_123', 0.0, 12))

    @override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=False)
    def test_flag_off_sends_nothing(self):
        self.run_task(ORDER_COMPLETED)
        self.mocks['subscription'].assert_not_called()
        self.mocks['track'].assert_not_called()

    @ddt.data(*TRANSIENT_STRIPE_ERRORS)
    def test_transient_stripe_failure_is_retried(self, error):
        self.mocks['subscription'].side_effect = error
        args, kwargs = self.task_args(ORDER_COMPLETED)
        with mock.patch.object(send_checkout_segment_event_task, 'retry', side_effect=Retry('retry')) as mock_retry:
            with self.assertRaises(Retry):
                send_checkout_segment_event_task.apply(args=args, kwargs=kwargs, throw=True).get()
        mock_retry.assert_called_once()
        self.mocks['track'].assert_not_called()

    def assert_retried(self, event_name=ORDER_COMPLETED):
        """Running the task raises Celery's Retry, through autoretry_for."""
        args, kwargs = self.task_args(event_name)
        with mock.patch.object(send_checkout_segment_event_task, 'retry', side_effect=Retry('retry')) as mock_retry:
            with self.assertRaises(Retry):
                send_checkout_segment_event_task.apply(args=args, kwargs=kwargs, throw=True).get()
        mock_retry.assert_called_once()

    @ddt.data(*SEGMENT_DELIVERY_ERRORS)
    def test_segment_not_accepting_the_event_is_retried(self, error_class):
        """Segment unreachable or answering non-200: the event was not delivered, so the task retries it."""
        error = error_class(503, 'unavailable', 'down') if error_class is APIError else error_class('unreachable')
        self.mocks['track'].side_effect = error
        with mock.patch(f'{TASKS}.logger') as mock_logger:
            self.assert_retried()
        mock_logger.info.assert_not_called()  # never logged as sent

    def test_event_whose_retries_run_out_is_logged_as_dropped(self):
        """The last retry's failure gets the prefix too, so the alert and the resend steps find it."""
        self.mocks['track'].side_effect = APIError(503, 'unavailable', 'down')
        args, kwargs = self.task_args(ORDER_COMPLETED)
        with self.assertLogs(TASKS, level='ERROR') as logs:
            result = send_checkout_segment_event_task.apply(
                args=args, kwargs=kwargs, retries=settings.TASK_MAX_RETRIES,
            )
        self.assertTrue(result.failed())
        self.assertIsInstance(result.result, APIError)
        self.mocks['track'].assert_called_once()
        dropped = [line for line in logs.output if SEGMENT_EVENT_DROPPED_LOG_PREFIX in line]
        self.assertEqual(len(dropped), 1)
        self.assertIn(f'{ORDER_COMPLETED} for CheckoutIntent {self.intent.id} (subscription sub_123)', dropped[0])

    @ddt.data(400, 401, 403, 404)
    def test_event_segment_rejects_is_dropped_not_retried(self, status):
        """A 4xx other than 429 (e.g. a wrong SEGMENT_KEY) gets the same answer on every retry."""
        self.mocks['track'].side_effect = APIError(status, 'rejected', 'no')
        with mock.patch.object(send_checkout_segment_event_task, 'retry') as mock_retry, \
                self.assertLogs(TASKS, level='ERROR') as logs:
            self.run_task(ORDER_COMPLETED)
        mock_retry.assert_not_called()
        self.assertIn(SEGMENT_EVENT_DROPPED_LOG_PREFIX, logs.output[0])

    @ddt.data(429, 500, 503)
    def test_segment_rate_limit_and_server_errors_are_retried(self, status):
        self.mocks['track'].side_effect = APIError(status, 'unavailable', 'down')
        self.assert_retried()

    def test_essentials_without_academy_title_is_retried(self):
        with mock.patch(f'{TASKS}.build_order_completed_properties', side_effect=AcademyTitleUnavailableError()):
            self.assert_retried()
        self.mocks['track'].assert_not_called()

    def test_without_segment_key_is_not_logged_as_sent(self):
        self.mocks['track'].return_value = False
        with mock.patch(f'{TASKS}.logger') as mock_logger:
            self.run_task(ORDER_COMPLETED)
        self.mocks['track'].assert_called_once()
        mock_logger.info.assert_not_called()

    @ddt.data(
        # (event, task kwargs)
        (ORDER_COMPLETED, {'invoice_id': 'in_unknown'}),
        (ORDER_CANCELLED, {'subscription_id': 'sub_unknown'}),
    )
    @ddt.unpack
    def test_missing_invoice_summary_is_retried(self, event_name, task_kwargs):
        """
        The summary may not be readable yet (its signal swallows errors, or the webhook's write isn't visible to the
        worker yet), so the event is retried rather than dropped.
        """
        subscription_id = task_kwargs.pop('subscription_id', 'sub_123')
        args, kwargs = self.task_args(event_name, **task_kwargs)
        args = (*args[:2], subscription_id)
        with mock.patch.object(send_checkout_segment_event_task, 'retry', side_effect=Retry('retry')) as mock_retry:
            with self.assertRaises(Retry):
                send_checkout_segment_event_task.apply(args=args, kwargs=kwargs, throw=True).get()
        mock_retry.assert_called_once()
        self.mocks['track'].assert_not_called()

    @ddt.data(
        # (event, what is wrong, task kwargs)
        (ORDER_COMPLETED, 'Stripe rejects the subscription request', {}),
        (ORDER_COMPLETED, 'invalid Stripe key', {}),
        (ORDER_COMPLETED, 'user without an lms_user_id', {}),
        (ORDER_COMPLETED, 'CheckoutIntent deleted since', {}),
        (ORDER_CANCELLED, 'no Checkout Session ID to use as the order_id', {}),
        (ORDER_CANCELLED, 'queued without the cancelled subscription', {'subscription_data': None}),
    )
    @ddt.unpack
    def test_event_that_cannot_be_built_is_not_sent(self, event_name, problem, task_kwargs):
        if problem == 'Stripe rejects the subscription request':
            self.mocks['subscription'].side_effect = stripe.InvalidRequestError('No such subscription', 'id')
        elif problem == 'invalid Stripe key':
            self.mocks['subscription'].side_effect = stripe.AuthenticationError('Invalid API key')
        elif problem == 'user without an lms_user_id':
            type(self.user).objects.filter(id=self.user.id).update(lms_user_id=None)
        elif problem == 'CheckoutIntent deleted since':
            CheckoutIntent.objects.filter(id=self.intent.id).delete()
        elif problem == 'no Checkout Session ID to use as the order_id':
            CheckoutIntent.objects.filter(id=self.intent.id).update(stripe_checkout_session_id=None)

        with self.assertLogs(TASKS, level='ERROR') as logs:
            self.run_task(event_name, **task_kwargs)

        self.mocks['track'].assert_not_called()
        # A stable prefix, so an alert can match a dropped event.
        self.assertIn(SEGMENT_EVENT_DROPPED_LOG_PREFIX, logs.output[0])
