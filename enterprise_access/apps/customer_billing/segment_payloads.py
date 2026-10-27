"""
Payload builders for the server-side checkout Segment events.

Payloads are built only from the fields set explicitly below and must never contain PII
(email, cardholder name, billing details, card last4, tokens).
"""
import logging

from enterprise_access.apps.customer_billing.constants import ATTRIBUTION_KEYS, SUBSCRIPTION_ITEM_TYPE
from enterprise_access.apps.customer_billing.pricing_api import StripePricingError, get_stripe_price_data
from enterprise_access.apps.customer_billing.utils import get_product_type
from enterprise_access.utils import cents_to_dollars

logger = logging.getLogger(__name__)

PRODUCT_CATEGORY = 'subscription'
PRODUCT_BRAND = 'enterprise'


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


def get_payment_schedule(line) -> str | None:
    """
    Billing interval of the invoice line, read from the (cached) Stripe price's recurring interval.
    None if the price is unknown or cannot be fetched; the interval is never guessed from the line's period,
    which can be a prorated fragment.
    """
    price_id = _get(_get(_get(line, 'pricing'), 'price_details'), 'price')
    if not price_id:
        return None
    try:
        price_data = get_stripe_price_data(price_id)
    except StripePricingError:
        logger.warning('Could not fetch Stripe price %s for the Segment payment_schedule', price_id)
        return None
    return {'year': 'yearly', 'month': 'monthly'}.get(_get(_get(price_data, 'recurring'), 'interval'))


def _product_properties(checkout_intent, invoice) -> dict:
    """Product half of the payload, derived from the SspProduct and the invoice's subscription item line."""
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
        'price': float(cents_to_dollars(unit_amount)) if unit_amount is not None else None,
        'slug': ssp_product.slug,
        'payment_schedule': get_payment_schedule(line),
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
        'revenue': float(cents_to_dollars(total)) if total is not None else None,
        'currency': (_get(invoice, 'currency') or '').upper() or None,
    })
    return {key: value for key, value in properties.items() if value is not None}


def build_order_completed_properties(checkout_intent, invoice) -> dict:
    """
    Order payload plus attribution (UTM/referrer) captured on the CheckoutIntent. The referrer's query string and
    fragment were already stripped when the attribution was saved (``CheckoutIntentCreateRequestSerializer``).
    """
    properties = build_order_properties(checkout_intent, invoice)
    attribution = checkout_intent.attribution or {}
    for key in ATTRIBUTION_KEYS:
        if attribution.get(key):
            properties[key] = attribution[key]
    return properties
