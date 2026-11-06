"""
Utility functions for customer billing app
"""
import logging
import re
from datetime import datetime, timezone
from typing import Union
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from django.apps import apps
from django.conf import settings

from enterprise_access.apps.customer_billing.constants import ATTRIBUTION_KEYS, ATTRIBUTION_VALUE_MAX_LENGTH


def datetime_from_timestamp(timestamp: Union[int, float]) -> datetime:
    """
    Convert a Unix timestamp (seconds since epoch) into a timezone-aware UTC datetime.

    Args:
        timestamp (Union[int, float]): Unix timestamp in seconds.

    Returns:
        datetime.datetime: A timezone-aware datetime object with tzinfo set to UTC.
    """
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


# An email address, the PII that most often ends up in marketing parameters (e.g. ``utm_content=jane@example.com``).
EMAIL_ADDRESS_PATTERN = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
REFERRER_SCHEMES = ('http', 'https')


def contains_email_address(value: str) -> bool:
    """
    Whether ``value`` contains an email address, also when percent-encoded (``jane%40example.com``, or encoded twice).
    """
    for _ in range(3):
        if EMAIL_ADDRESS_PATTERN.search(value):
            return True
        decoded = unquote_plus(value)
        if decoded == value:
            return False
        value = decoded
    return bool(EMAIL_ADDRESS_PATTERN.search(value))


def sanitize_referrer(url: str) -> str | None:
    """
    Reduce a referrer URL to its origin, ``scheme://host[:port]/``, or return None if it is not an http(s) URL with a
    host.

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
    return urlunsplit((scheme, netloc, '/', '', ''))


def sanitize_attribution(attribution) -> dict | None:
    """
    Return the parts of ``attribution`` that may be stored and sent to Segment, or None if nothing is left.

    Keeps only ``ATTRIBUTION_KEYS`` whose value is a non-empty string without an email address, capped at
    ``ATTRIBUTION_VALUE_MAX_LENGTH`` characters, with the referrer reduced to its origin (``sanitize_referrer``).
    Anything else is dropped rather than rejected, so this never raises. A key allowlist alone does not make values
    safe, which is why each value is checked too. The checks catch the obvious cases (emails, URL credentials and
    paths, nested objects); they cannot recognize every kind of personal data in free text.
    """
    if not isinstance(attribution, dict):
        return None
    cleaned = {}
    for key in ATTRIBUTION_KEYS:
        value = attribution.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if key == 'referrer':
            value = sanitize_referrer(value)
        # Checked before truncating, so a cut-off email address is still found.
        if not value or contains_email_address(value):
            continue
        cleaned[key] = value[:ATTRIBUTION_VALUE_MAX_LENGTH]
    return cleaned or None


logger = logging.getLogger(__name__)


def get_default_ssp_price_lookup_key() -> str | None:
    """
    Return the `stripe_price_lookup_key` for the default SSP product as
    configured by `SSP_DEFAULT_PRODUCT_SLUG` in settings.

    Falls back to the lookup key of the first active `SspProduct` (ordered by
    slug) if `SSP_DEFAULT_PRODUCT_SLUG` is unset or does not match any active
    product. Returns None only if there are no active SspProducts at all, or
    on error.
    """
    slug = getattr(settings, 'SSP_DEFAULT_PRODUCT_SLUG', None)
    try:
        SspProduct = apps.get_model('customer_billing', 'SspProduct')
        product = None
        if slug:
            product = SspProduct.objects.filter(slug=slug, is_active=True).only('stripe_price_lookup_key').first()
        if product is None:
            product = SspProduct.objects.filter(is_active=True).order_by('slug').only('stripe_price_lookup_key').first()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Error resolving default SSP price lookup key for slug=%s", slug)
        return None
    return getattr(product, 'stripe_price_lookup_key', None)


# Maps (email_type, product_type) → Django settings attribute name
CAMPAIGN_SETTINGS_MAP = {
    # Teams campaigns (existing)
    ('signup_confirmation', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_SIGNUP_CONFIRMATION_CAMPAIGN',
    ('trial_ending_soon', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_TRIAL_ENDING_SOON_CAMPAIGN',
    ('trial_cancellation', 'teams'): 'BRAZE_TRIAL_CANCELLATION_CAMPAIGN',
    ('trial_ended_cancellation', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_ENDED_CAMPAIGN',
    ('payment_receipt', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN',
    ('trial_end_subscription_started', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN',
    ('billing_error', 'teams'): 'BRAZE_BILLING_ERROR_CAMPAIGN',
    ('paid_cancellation', 'teams'): 'BRAZE_PAID_CANCELLATION_CAMPAIGN',
    ('paid_subscription_ended', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_SUBSCRIPTION_ENDED_AND_CANCELLED_CAMPAIGN',
    ('paid_subscription_renewal_notice', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_SUBSCRIPTION_RENEWAL_NOTICE_CAMPAIGN',

    # Essentials campaigns (new)
    ('signup_confirmation', 'essentials'): 'BRAZE_ESSENTIALS_SIGNUP_CONFIRMATION_CAMPAIGN',
    ('trial_ending_soon', 'essentials'): 'BRAZE_ESSENTIALS_TRIAL_ENDING_SOON_CAMPAIGN',
    ('trial_cancellation', 'essentials'): 'BRAZE_ESSENTIALS_TRIAL_CANCELLATION_CAMPAIGN',
    ('trial_ended_cancellation', 'essentials'): 'BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_ENDED_CAMPAIGN',
    ('payment_receipt', 'essentials'): 'BRAZE_ESSENTIALS_PAYMENT_RECEIPT_CAMPAIGN',
    ('trial_end_subscription_started', 'essentials'): 'BRAZE_ESSENTIALS_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN',
    ('billing_error', 'essentials'): 'BRAZE_ESSENTIALS_BILLING_ERROR_CAMPAIGN',
    ('paid_cancellation', 'essentials'): 'BRAZE_ESSENTIALS_PAID_CANCELLATION_CAMPAIGN',
    ('paid_subscription_ended', 'essentials'): 'BRAZE_ENTERPRISE_PROVISION_SUBSCRIPTION_ENDED_AND_CANCELLED_CAMPAIGN',
    ('paid_subscription_renewal_notice', 'essentials'): (
        'BRAZE_ENTERPRISE_PROVISION_SUBSCRIPTION_RENEWAL_NOTICE_CAMPAIGN'
    ),
}


def get_product_type(ssp_product: 'SspProduct | None'):
    """Determine product type from the canonical SspProduct fields."""
    if ssp_product and ssp_product.academy_uuid is not None:
        return 'essentials'
    return 'teams'


def get_campaign_id(email_type, ssp_product: 'SspProduct | None' = None):
    """
    Resolve the Braze campaign UUID for a given email type and product.

    Args:
        email_type (str): One of 'signup_confirmation', 'trial_ending_soon',
            'trial_cancellation', 'trial_ended_cancellation', 'payment_receipt',
            'trial_end_subscription_started', 'billing_error',
            'paid_cancellation', 'paid_subscription_ended', 'paid_subscription_renewal_notice'.
        ssp_product (SspProduct|None): The CheckoutIntent's product.

    Returns:
        str: The Braze campaign UUID from Django settings.

    Raises:
        ValueError: If no campaign is configured for the resolved (email_type, product_type).
    """
    product_type = get_product_type(ssp_product)
    settings_key = CAMPAIGN_SETTINGS_MAP.get((email_type, product_type))

    if not settings_key:
        raise ValueError(
            f"No campaign mapping found for email_type='{email_type}', "
            f"product_type='{product_type}'"
        )

    campaign_id = getattr(settings, settings_key, None)
    if not campaign_id:
        raise ValueError(
            f"Campaign setting '{settings_key}' is not configured "
            f"(email_type='{email_type}', product_type='{product_type}')"
        )

    logger.info(
        "Resolved campaign: email_type=%s, product_slug=%s, product_type=%s, campaign_id=%s",
        email_type,
        getattr(ssp_product, 'slug', None),
        product_type,
        campaign_id,
    )
    return campaign_id
