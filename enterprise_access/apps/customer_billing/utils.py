"""
Utility functions for customer billing app
"""
import logging
import re
from datetime import datetime, timezone
from typing import Union
from urllib.parse import urlsplit, urlunsplit

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


REFERRER_SCHEMES = ('http', 'https')

# A UTM value must be a campaign label: letters, digits, spaces and ``_ - . + | ,``. That leaves out every character
# that marks an email address, a URL or encoded data (``@ % / : = & ? #``), markup and quotes.
UTM_VALUE_PATTERN = re.compile(r'[\w .+|,-]+')
# A value with this many digits, however they are separated, is a phone number rather than a campaign label. A date
# written as one number has eight.
UTM_PHONE_NUMBER_MIN_DIGITS = 10


def sanitize_utm_value(value: str) -> str | None:
    """
    Return the UTM value if it is a campaign label of at most ``ATTRIBUTION_VALUE_MAX_LENGTH`` characters that
    matches ``UTM_VALUE_PATTERN`` and has fewer than ``UTM_PHONE_NUMBER_MIN_DIGITS`` digits, or None. This drops email
    addresses (also percent-encoded), URLs, query strings and phone numbers. The length is checked first, so a long
    client-supplied value is never scanned. Longer values are dropped, not truncated.
    """
    if not value or len(value) > ATTRIBUTION_VALUE_MAX_LENGTH or not UTM_VALUE_PATTERN.fullmatch(value):
        return None
    if sum(char.isdigit() for char in value) >= UTM_PHONE_NUMBER_MIN_DIGITS:
        return None
    return value


def sanitize_referrer(url: str) -> str | None:
    """
    Reduce a referrer URL to its origin, ``scheme://host[:port]/``, or return None if it is not an http(s) URL with a
    host, or the origin is longer than ``ATTRIBUTION_VALUE_MAX_LENGTH``.

    The username and password, path, query string and fragment are dropped: any of them can identify a person
    (``/u/jane-doe``, ``?email=...``). Browsers already send only the origin for cross-site referrers by default.
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

    Keeps only ``ATTRIBUTION_KEYS`` with string values: UTM values that are campaign labels (``sanitize_utm_value``),
    and the referrer reduced to its origin (``sanitize_referrer``). Anything else is dropped rather than rejected, so
    this never raises. The keys of dropped values are logged (never the values, which may hold personal data), so
    missing attribution in Segment can be explained.
    """
    if not isinstance(attribution, dict):
        return None
    cleaned = {}
    dropped = []
    for key in ATTRIBUTION_KEYS:
        value = attribution.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        cleaned_value = None
        if isinstance(value, str):
            value = value.strip()
            cleaned_value = sanitize_referrer(value) if key == 'referrer' else sanitize_utm_value(value)
        if cleaned_value:
            cleaned[key] = cleaned_value
        else:
            dropped.append(key)
    if dropped:
        logger.info('Dropped attribution values that are not in the accepted format: %s', ', '.join(dropped))
    return cleaned or None
