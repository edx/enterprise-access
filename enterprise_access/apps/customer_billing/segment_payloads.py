"""
Payload builders for the server-side checkout Segment events.

Payloads are built only from the fields set explicitly below and must never contain PII
(email, cardholder name, billing details, card last4, tokens).
"""
import logging

import stripe

from enterprise_access.apps.customer_billing.constants import SUBSCRIPTION_ITEM_TYPE
from enterprise_access.apps.customer_billing.pricing_api import StripePricingError, get_stripe_price_data
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


class StripePriceTemporarilyUnavailable(Exception):
    """Stripe could not be reached for a price's billing interval. A later attempt can succeed."""


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
    The invoice's non-proration subscription item line, so proration and credit lines never supply the quantity,
    price or product. Stripe proration lines share the subscription item parent type and are told apart only by
    ``parent.subscription_item_details.proration``. None if there is no such line (e.g. a license-change invoice
    made only of prorations).
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


def get_payment_schedule(line) -> str | None:
    """
    Billing interval of the invoice line, read from the (cached) Stripe price's recurring interval.

    Returns None if the line has no price, or Stripe has no usable price for it (e.g. it does not exist, or isn't
    an active recurring price); the interval is never guessed from the line's period, which can be a prorated
    fragment. Raises ``StripePriceTemporarilyUnavailable`` if Stripe could not be reached, so the caller retries
    instead of treating a passing outage as missing data.
    """
    price_id = _get(_get(_get(line, 'pricing'), 'price_details'), 'price')
    if not price_id:
        return None
    try:
        price_data = get_stripe_price_data(price_id)
    except StripePricingError as exc:
        # get_stripe_price_data wraps every failure in StripePricingError; the Stripe error it chains says which.
        if isinstance(exc.__cause__, TRANSIENT_STRIPE_ERRORS):
            raise StripePriceTemporarilyUnavailable(f'Could not fetch Stripe price {price_id}: {exc}') from exc
        logger.warning('Stripe price %s gives no payment_schedule for the Segment event: %s', price_id, exc)
        return None
    return PAYMENT_SCHEDULES.get(_get(_get(price_data, 'recurring'), 'interval'))


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
    Payload for ``order_completed``: product, price and quantity from the paid invoice's subscription item line,
    revenue from the invoice total, plus the attribution saved on the CheckoutIntent. Attribution is sanitized again
    here, so a value stored before the current rules (or edited in admin) is never forwarded unchecked.
    """
    line = _subscription_item_line(invoice)
    pricing = _get(line, 'pricing')
    properties = {
        **_ssp_product_properties(checkout_intent),
        'product_id': _get(_get(pricing, 'price_details'), 'product'),
        'price': _dollars(_get(pricing, 'unit_amount_decimal')),
        'payment_schedule': get_payment_schedule(line),
        'order_id': order_id,
        'total_quantity': _get(line, 'quantity'),
        **_revenue_properties(invoice),
        **(sanitize_attribution(checkout_intent.attribution) or {}),
    }
    return _complete(properties)


def build_order_cancelled_properties(checkout_intent, order_invoice, subscription, order_id) -> dict:
    """
    Payload for ``order_cancelled``.

    Product, price, billing interval and quantity come from the cancelled subscription's item, i.e. what the
    customer had when it ended, after any license changes. They never come from an invoice, because a license-change
    invoice may hold only prorations. ``order_id`` and ``revenue`` are the order's, the same as on its
    ``order_completed``: the Checkout Session ID and the total of the order's first paid invoice.
    """
    item = _subscription_item(subscription)
    price = _get(item, 'price')
    product = _get(price, 'product')
    unit_amount = _get(price, 'unit_amount_decimal')
    if unit_amount is None:
        unit_amount = _get(price, 'unit_amount')
    properties = {
        **_ssp_product_properties(checkout_intent),
        # ``product`` is an ID unless the subscription was retrieved with it expanded.
        'product_id': product if isinstance(product, str) else _get(product, 'id'),
        'price': _dollars(unit_amount),
        'payment_schedule': PAYMENT_SCHEDULES.get(_get(_get(price, 'recurring'), 'interval')),
        'order_id': order_id,
        'total_quantity': _get(item, 'quantity'),
        **_revenue_properties(order_invoice),
    }
    return _complete(properties)


def find_order_invoice(paid_invoices, order_billing_reasons):
    """
    The order's first paid invoice among a subscription's paid invoices: the earliest one with a non-zero total that
    was issued when the subscription was created or its trial ended. License changes (``subscription_update``) and
    manual invoices are not the order. Returns None if the subscription never collected money for the order (e.g.
    it was cancelled during the trial).
    """
    candidates = [
        invoice for invoice in paid_invoices
        if (_get(invoice, 'total') or 0) > 0 and _get(invoice, 'billing_reason') in order_billing_reasons
    ]
    return min(candidates, key=lambda invoice: _get(invoice, 'created', 0), default=None)
