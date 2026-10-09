"""
Payload builders for the server-side checkout Segment events.

Payloads are built from an explicit allowlist and must never contain PII
(email, cardholder name, billing details, card last4, tokens).
"""
import logging
from urllib.parse import urlsplit, urlunsplit

from enterprise_access.apps.customer_billing.constants import ATTRIBUTION_KEYS
from enterprise_access.apps.customer_billing.utils import get_product_type

logger = logging.getLogger(__name__)

PRODUCT_CATEGORY = 'subscription'
PRODUCT_BRAND = 'enterprise'

WALLET_PAYMENT_METHOD_NAMES = {
    'apple_pay': 'Apple Pay',
    'google_pay': 'Google Pay',
}
PAYMENT_METHOD_TYPE_NAMES = {
    'card': 'Credit Card',
    'us_bank_account': 'ACH',
    'link': 'Link',
}


def strip_query_and_fragment(url: str) -> str:
    """Drop the query string and fragment from a URL, which may carry PII (e.g. ``?email=...``)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def _get(obj, key, default=None):
    """Read ``key`` from a dict or stripe object, tolerating missing/None values."""
    if obj is None:
        return default
    try:
        value = obj[key]
    except (KeyError, TypeError, AttributeError):
        return default
    return default if value is None else value


def _first_line(invoice):
    lines = _get(_get(invoice, 'lines'), 'data') or []
    return lines[0] if lines else None


def get_payment_method_name(invoice) -> str | None:
    """
    Map the paying charge's ``payment_method_details`` to a display name.
    Returns None if the charge was not expanded on the invoice, or the invoice is $0.
    """
    if not _get(invoice, 'total'):
        return None
    for invoice_payment in _get(_get(invoice, 'payments'), 'data') or []:
        payment_intent = _get(_get(invoice_payment, 'payment'), 'payment_intent')
        charge = _get(payment_intent, 'latest_charge')
        if isinstance(charge, str):
            continue  # not expanded (a bare ID string) or absent
        details = _get(charge, 'payment_method_details') or {}
        method_type = _get(details, 'type')
        if method_type == 'card':
            wallet_type = _get(_get(_get(details, 'card'), 'wallet'), 'type')
            if wallet_type in WALLET_PAYMENT_METHOD_NAMES:
                return WALLET_PAYMENT_METHOD_NAMES[wallet_type]
        if method_type in PAYMENT_METHOD_TYPE_NAMES:
            return PAYMENT_METHOD_TYPE_NAMES[method_type]
    return None


def _product_properties(checkout_intent, invoice) -> dict:
    """Product half of the payload, derived from the SspProduct and the invoice's first line item."""
    ssp_product = checkout_intent.ssp_product
    line = _first_line(invoice)
    pricing = _get(line, 'pricing')
    price_details = _get(pricing, 'price_details')
    unit_amount = _get(pricing, 'unit_amount_decimal')

    product_type = get_product_type(ssp_product)
    properties = {
        'product_id': _get(price_details, 'product'),
        'sku': ssp_product.catalog_query_id,
        'category': PRODUCT_CATEGORY,
        'name': product_type,
        'brand': PRODUCT_BRAND,
        'price': float(unit_amount) / 100 if unit_amount is not None else None,
        'slug': ssp_product.slug,
        'payment_schedule': 'yearly',
    }
    if product_type == 'essentials' and ssp_product.academy_title:
        properties['variant'] = ssp_product.academy_title
    return properties


def build_order_properties(checkout_intent, invoice) -> dict:
    """
    Build the shared Product + Order payload for a Stripe invoice.

    ``revenue`` is the invoice total (so discounts/proration are included), never price * quantity.
    """
    properties = _product_properties(checkout_intent, invoice)
    total = _get(invoice, 'total')
    line = _get(_first_line(invoice), 'quantity')
    properties.update({
        'order_id': _get(invoice, 'id'),
        'total_quantity': line if line is not None else checkout_intent.quantity,
        'revenue': total / 100 if total is not None else None,
        'currency': (_get(invoice, 'currency') or '').upper() or None,
    })
    payment_method = get_payment_method_name(invoice)
    if payment_method:
        properties['payment_method'] = payment_method
    return {key: value for key, value in properties.items() if value is not None}


def build_order_completed_properties(checkout_intent, invoice) -> dict:
    """Order payload plus attribution (UTM/referrer) captured on the CheckoutIntent."""
    properties = build_order_properties(checkout_intent, invoice)
    attribution = checkout_intent.attribution or {}
    for key in ATTRIBUTION_KEYS:
        if attribution.get(key):
            value = attribution[key]
            properties[key] = strip_query_and_fragment(value) if key == 'referrer' else value
    return properties
