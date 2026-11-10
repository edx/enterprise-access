"""
Payload builders for the server-side checkout Segment events.

Payloads are built only from the fields set explicitly below and must never contain PII
(email, cardholder name, billing details, card last4, tokens).

Also holds the attribution sanitizers, which the CheckoutIntent create API uses before storing attribution and the
order_completed payload uses again before sending it.
"""
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import stripe

from enterprise_access.apps.customer_billing.constants import (
    ATTRIBUTION_KEYS,
    ATTRIBUTION_VALUE_MAX_LENGTH,
    SUBSCRIPTION_ITEM_TYPE
)
from enterprise_access.apps.customer_billing.stripe_api import get_stripe_price
from enterprise_access.apps.customer_billing.utils import get_product_type
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


REFERRER_SCHEMES = ('http', 'https')

# The format a UTM value must have: a campaign label of letters, digits, spaces and ``_ - . + | ,``. That leaves out
# every character that marks an email address, a URL or encoded data (``@ % / : = & ? #``), markup and quotes.
UTM_VALUE_PATTERN = re.compile(r'[\w .+|,-]+')
# Identifiers that fit that format: 7 or more digits, together or split by spaces, dots, plus signs or hyphens
# (phone, account and order numbers, and dates written as one number); 20 or more letters and digits in a row
# (tokens, hashes); a UUID. Only ever run on values of at most ATTRIBUTION_VALUE_MAX_LENGTH characters.
UTM_IDENTIFIER_PATTERN = re.compile(
    r'\d(?:[ .+-]*\d){6,}'
    r'|[A-Za-z0-9]{20,}'
    r'|[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}'
)


def sanitize_utm_value(value: str) -> str | None:
    """
    Return the UTM value if it is a campaign label in the agreed format, or None.

    Kept: at most ``ATTRIBUTION_VALUE_MAX_LENGTH`` characters matching ``UTM_VALUE_PATTERN``, with nothing that looks
    like an identifier (``UTM_IDENTIFIER_PATTERN``). This drops email addresses (also percent-encoded), URLs, query
    strings, phone and account numbers, tokens and UUIDs. The length is checked first, so a long client-supplied value
    is never scanned. Longer values are dropped, not truncated.

    No format can tell a person's name from a campaign label (``jane_doe`` and ``spring_sale`` look alike), so this
    narrows what can get through; it does not prove that a value holds no personal data.
    """
    if not value or len(value) > ATTRIBUTION_VALUE_MAX_LENGTH:
        return None
    if not UTM_VALUE_PATTERN.fullmatch(value) or UTM_IDENTIFIER_PATTERN.search(value):
        return None
    return value


def sanitize_referrer(url: str) -> str | None:
    """
    Reduce a referrer URL to its origin, ``scheme://host[:port]/``, or return None if it is not an http(s) URL with a
    host, or the origin is longer than ``ATTRIBUTION_VALUE_MAX_LENGTH``.

    Only the origin is kept. The username and password, query string and fragment are dropped, and so is the path:
    path segments can identify a person (``/u/jane-doe``, ``/reset/<token>``), and no path pattern is safe in
    general. Browsers already send only the origin for cross-site referrers by default
    (``strict-origin-when-cross-origin``), so this mostly drops paths of same-site referrers.
    """
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname  # lowercased, without the username and password
        port = parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in REFERRER_SCHEMES or not hostname:
        return None
    host = f'[{hostname}]' if ':' in hostname else hostname  # IPv6
    netloc = f'{host}:{port}' if port else host
    origin = urlunsplit((scheme, netloc, '/', '', ''))
    return origin if len(origin) <= ATTRIBUTION_VALUE_MAX_LENGTH else None


def sanitize_attribution(attribution) -> dict | None:
    """
    Return the parts of ``attribution`` that may be stored and sent to Segment, or None if nothing is left.

    Keeps only ``ATTRIBUTION_KEYS`` with string values: UTM values that are campaign labels in the agreed format
    (``sanitize_utm_value``), and the referrer reduced to its origin (``sanitize_referrer``). Anything else is dropped
    rather than rejected, so this never raises. A key allowlist alone does not make values safe, which is why each
    value is checked too.
    """
    if not isinstance(attribution, dict):
        return None
    cleaned = {}
    for key in ATTRIBUTION_KEYS:
        value = attribution.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        value = sanitize_referrer(value) if key == 'referrer' else sanitize_utm_value(value)
        if value:
            cleaned[key] = value
    return cleaned or None


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
    Billing interval of the invoice line, read from its (cached) Stripe price's recurring interval.

    The price is read even if it has been archived since: that doesn't change the subscriptions and invoices that use
    it, and an order that was paid must still be reported. Returns None if the line has no price, or Stripe has no
    such price or it isn't recurring; the interval is never guessed from the line's period, which can be a prorated
    fragment. Transient Stripe errors (``TRANSIENT_STRIPE_ERRORS``) are raised, so the caller retries instead of
    treating a passing outage as missing data.
    """
    price_id = _get(_get(_get(line, 'pricing'), 'price_details'), 'price')
    if not price_id:
        return None
    try:
        price = get_stripe_price(price_id)
    except TRANSIENT_STRIPE_ERRORS:
        raise
    except stripe.StripeError as exc:
        logger.warning('Stripe price %s gives no payment_schedule for the Segment event: %s', price_id, exc)
        return None
    return PAYMENT_SCHEDULES.get(_get(_get(price, 'recurring'), 'interval'))


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
