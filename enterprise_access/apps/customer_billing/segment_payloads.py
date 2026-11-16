"""
Payload builders for the server-side checkout Segment events.

Payloads are built only from the fields set explicitly below and must never contain PII
(email, cardholder name, billing details, card last4, tokens).
"""
import stripe

from enterprise_access.apps.customer_billing.utils import get_product_type
from enterprise_access.utils import cents_to_dollars

PAYMENT_SCHEDULES = {'year': 'yearly', 'month': 'monthly'}

# If one of these can't be determined the event is not sent: a payload is never corrected after it was sent.
REQUIRED_ORDER_PROPERTIES = (
    'order_id', 'product_id', 'price', 'payment_schedule', 'total_quantity', 'revenue', 'currency',
)

# Stripe failures a later attempt can fix: network errors and timeouts, 429 and 5xx.
TRANSIENT_STRIPE_ERRORS = (stripe.APIConnectionError, stripe.RateLimitError, stripe.APIError)


class IncompleteOrderPayloadError(Exception):
    """A required order property could not be determined from Stripe's data. Retrying gives the same result."""


def _get(obj, *keys):
    """
    Read a nested value from dicts or Stripe objects (which don't support ``dict.get``), or None if any key along
    the way is missing.
    """
    for key in keys:
        try:
            obj = obj[key]
        except (KeyError, TypeError, AttributeError):
            return None
    return obj


def _dollars(cents) -> float | None:
    return float(cents_to_dollars(cents)) if cents is not None else None


def _subscription_item(subscription):
    """
    The subscription's only item. SSP subscriptions have exactly one; with any other count the quantity and price
    would be ambiguous.
    """
    items = _get(subscription, 'items', 'data') or []
    if len(items) != 1:
        raise IncompleteOrderPayloadError(
            f'Subscription {_get(subscription, "id")} has {len(items)} items; expected exactly 1'
        )
    return items[0]


def _properties(checkout_intent, order_id, price, quantity, invoice_summary) -> dict:
    """
    The payload shared by both events. Product, unit price and billing interval come from the subscription item's
    Stripe Price (complete even if the price was archived since), never from invoice line amounts, which reflect a
    trial or proration. Revenue is the amount paid on the order's first invoice, so discounts are included, never
    price * quantity. ``invoice_summary`` is that invoice's ``invoice.paid`` StripeEventSummary.
    """
    ssp_product = checkout_intent.ssp_product
    product_type = get_product_type(ssp_product)
    product = _get(price, 'product')
    unit_amount = _get(price, 'unit_amount_decimal')
    currency = invoice_summary.invoice_currency
    properties = {
        'sku': ssp_product.catalog_query_id,
        'category': 'subscription',
        'name': product_type,
        'brand': 'enterprise',
        'slug': ssp_product.slug,
        'variant': ssp_product.academy_title if product_type == 'essentials' else None,
        # ``product`` is an ID unless the price was retrieved with it expanded.
        'product_id': product if isinstance(product, str) else _get(product, 'id'),
        'price': _dollars(unit_amount if unit_amount is not None else _get(price, 'unit_amount')),
        'payment_schedule': PAYMENT_SCHEDULES.get(_get(price, 'recurring', 'interval')),
        'order_id': order_id,
        'total_quantity': quantity,
        'revenue': _dollars(invoice_summary.invoice_amount_paid),
        'currency': currency.upper() if currency else None,
    }
    properties = {key: value for key, value in properties.items() if value is not None}
    missing = [key for key in REQUIRED_ORDER_PROPERTIES if key not in properties]
    if missing:
        raise IncompleteOrderPayloadError(f'Missing required properties: {", ".join(missing)}')
    return properties


def build_order_completed_properties(checkout_intent, order_id, subscription, invoice_summary) -> dict:
    """
    Payload for ``order_completed``. ``invoice_summary`` is of the order's first invoice (paid at checkout; $0 for
    a free trial), which supplies the quantity ordered and the revenue; ``subscription`` supplies the price.
    """
    item = _subscription_item(subscription)
    return _properties(checkout_intent, order_id, _get(item, 'price'), invoice_summary.invoice_quantity,
                       invoice_summary)


def build_order_cancelled_properties(checkout_intent, order_id, subscription, invoice_summary) -> dict:
    """
    Payload for ``order_cancelled``. Product, price and quantity come from the cancelled subscription in the
    ``customer.subscription.deleted`` webhook (its state after any license changes). Revenue is the order's first
    invoice's, the same as on its ``order_completed``.
    """
    item = _subscription_item(subscription)
    return _properties(checkout_intent, order_id, _get(item, 'price'), _get(item, 'quantity'), invoice_summary)
