"""
Payload builders for the server-side checkout Segment events.

Payloads are built only from the fields set explicitly below and must never contain PII
(email, cardholder name, billing details, card last4, tokens).
"""
import logging

import stripe

from enterprise_access.apps.customer_billing.constants import SUBSCRIPTION_ITEM_TYPE
from enterprise_access.apps.customer_billing.stripe_api import get_stripe_price
from enterprise_access.apps.customer_billing.utils import get_product_type, sanitize_attribution
from enterprise_access.utils import cents_to_dollars

logger = logging.getLogger(__name__)

PRODUCT_CATEGORY = 'subscription'
PRODUCT_BRAND = 'enterprise'
PAYMENT_SCHEDULES = {'year': 'yearly', 'month': 'monthly'}

# Every order event carries these. If one cannot be determined the event is not sent (IncompleteOrderPayloadError);
# an event without them can't be reported on, and a payload is never corrected after it was sent.
REQUIRED_ORDER_PROPERTIES = (
    'order_id', 'product_id', 'price', 'payment_schedule', 'total_quantity', 'revenue', 'currency',
)

# Stripe failures that a later attempt can fix: network errors and timeouts (stripe-python raises
# APIConnectionError for both), rate limiting (429) and Stripe server errors (5xx).
TRANSIENT_STRIPE_ERRORS = (stripe.APIConnectionError, stripe.RateLimitError, stripe.APIError)


class IncompleteOrderPayloadError(Exception):
    """A required order property could not be determined from Stripe's data. Retrying gives the same result."""


def _get(obj, key, default=None):
    """Read ``key`` from a dict or stripe object, tolerating missing/None values."""
    if obj is None:
        return default
    try:
        value = obj[key]
    except (KeyError, TypeError, AttributeError):
        return default
    return default if value is None else value


def _dollars(cents) -> float | None:
    return float(cents_to_dollars(cents)) if cents is not None else None


def _subscription_item_line(invoice):
    """
    The invoice's non-proration subscription item line, so proration and credit lines never supply the quantity or
    price. Stripe proration lines share the subscription item parent type and are told apart only by
    ``parent.subscription_item_details.proration``. None if there is no such line.
    """
    for line in _get(_get(invoice, 'lines'), 'data') or []:
        parent = _get(line, 'parent')
        is_proration = _get(_get(parent, 'subscription_item_details'), 'proration', False)
        if _get(parent, 'type') == SUBSCRIPTION_ITEM_TYPE and not is_proration:
            return line
    return None


def _subscription_item(subscription):
    """
    The subscription's only item. SSP subscriptions are created with exactly one line item, so with more than one
    the quantity and price would be ambiguous.
    """
    items = _get(_get(subscription, 'items'), 'data') or []
    if len(items) != 1:
        raise IncompleteOrderPayloadError(
            f'Subscription {_get(subscription, "id")} has {len(items)} items; expected exactly 1'
        )
    return items[0]


def get_line_price(line):
    """
    The Stripe Price of the invoice line, read from Stripe (cached), or None.

    The price is read even if it has been archived since: that doesn't change the subscriptions and invoices that use
    it, and an order must still be reported. Returns None if the line has no price or Stripe has no such price.
    Transient Stripe errors (``TRANSIENT_STRIPE_ERRORS``) are raised, so the caller retries instead of treating a
    passing outage as missing data.
    """
    price_id = _get(_get(_get(line, 'pricing'), 'price_details'), 'price')
    if not price_id:
        return None
    try:
        return get_stripe_price(price_id)
    except TRANSIENT_STRIPE_ERRORS:
        raise
    except stripe.StripeError as exc:
        logger.warning('Stripe price %s could not be read for the Segment event: %s', price_id, exc)
        return None


def _price_properties(price) -> dict:
    """
    ``product_id``, ``price`` (the unit price) and ``payment_schedule`` from a Stripe Price. The billing interval is
    never guessed from an invoice line's period, which can be a trial or a prorated fragment.
    """
    product = _get(price, 'product')
    unit_amount = _get(price, 'unit_amount_decimal')
    if unit_amount is None:
        unit_amount = _get(price, 'unit_amount')
    return {
        # ``product`` is an ID unless the price was retrieved with it expanded.
        'product_id': product if isinstance(product, str) else _get(product, 'id'),
        'price': _dollars(unit_amount),
        'payment_schedule': PAYMENT_SCHEDULES.get(_get(_get(price, 'recurring'), 'interval')),
    }


def _ssp_product_properties(checkout_intent) -> dict:
    """Product fields that come from the purchased SspProduct."""
    ssp_product = checkout_intent.ssp_product
    product_type = get_product_type(ssp_product)
    properties = {
        'sku': ssp_product.catalog_query_id,
        'category': PRODUCT_CATEGORY,
        'name': product_type,
        'brand': PRODUCT_BRAND,
        'slug': ssp_product.slug,
    }
    if product_type == 'essentials' and ssp_product.academy_title:
        properties['variant'] = ssp_product.academy_title
    return properties


def _revenue_properties(invoice) -> dict:
    """``revenue`` is the invoice total (so discounts and proration are included), never price * quantity."""
    return {
        'revenue': _dollars(_get(invoice, 'total')),
        'currency': (_get(invoice, 'currency') or '').upper() or None,
    }


def _complete(properties: dict) -> dict:
    """Drop empty optional properties; raise ``IncompleteOrderPayloadError`` if a required one is missing."""
    properties = {key: value for key, value in properties.items() if value is not None}
    missing = [key for key in REQUIRED_ORDER_PROPERTIES if key not in properties]
    if missing:
        raise IncompleteOrderPayloadError(f'Missing required properties: {", ".join(missing)}')
    return properties


def build_order_completed_properties(checkout_intent, invoice, order_id) -> dict:
    """
    Payload for ``order_completed``, from the order's first invoice (paid at checkout).

    Quantity comes from the invoice's subscription item line. Product, unit price and billing interval come from that
    line's Stripe Price, because a trial invoice's line amounts reflect the trial, not the price. Revenue is the
    invoice total, which is $0 for a free trial: the amount actually charged. Attribution saved on the CheckoutIntent
    is sanitized again here, because it can be edited in admin.
    """
    line = _subscription_item_line(invoice)
    properties = {
        **_ssp_product_properties(checkout_intent),
        **_price_properties(get_line_price(line)),
        'order_id': order_id,
        'total_quantity': _get(line, 'quantity'),
        **_revenue_properties(invoice),
        **(sanitize_attribution(checkout_intent.attribution) or {}),
    }
    return _complete(properties)


def build_order_cancelled_properties(checkout_intent, order_invoice, subscription, order_id) -> dict:
    """
    Payload for ``order_cancelled``.

    Product, price, billing interval and quantity come from ``subscription``, the cancelled subscription from the
    ``customer.subscription.deleted`` webhook: what the customer had when it ended, after any license changes.
    ``order_id`` and ``revenue`` are the order's, the same as on its ``order_completed``: the Checkout Session ID and
    the total of the order's first invoice.
    """
    item = _subscription_item(subscription)
    properties = {
        **_ssp_product_properties(checkout_intent),
        **_price_properties(_get(item, 'price')),
        'order_id': order_id,
        'total_quantity': _get(item, 'quantity'),
        **_revenue_properties(order_invoice),
    }
    return _complete(properties)
