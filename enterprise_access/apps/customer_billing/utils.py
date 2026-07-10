"""
Utility functions for customer billing app
"""
import logging
from datetime import datetime, timezone
from typing import Union

from django.conf import settings


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


# Maps (email_type, product_type) → Django settings attribute name
CAMPAIGN_SETTINGS_MAP = {
    # Teams campaigns (existing)
    ('signup_confirmation', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_SIGNUP_CONFIRMATION_CAMPAIGN',
    ('trial_ending_soon', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_TRIAL_ENDING_SOON_CAMPAIGN',
    ('trial_cancellation', 'teams'): 'BRAZE_TRIAL_CANCELLATION_CAMPAIGN',
    ('payment_receipt', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN',
    ('trial_end_subscription_started', 'teams'): 'BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN',
    ('billing_error', 'teams'): 'BRAZE_BILLING_ERROR_CAMPAIGN',
    ('paid_cancellation', 'teams'): 'BRAZE_PAID_CANCELLATION_CAMPAIGN',

    # Essentials campaigns (new)
    ('signup_confirmation', 'essentials'): 'BRAZE_ESSENTIALS_SIGNUP_CONFIRMATION_CAMPAIGN',
    ('trial_ending_soon', 'essentials'): 'BRAZE_ESSENTIALS_TRIAL_ENDING_SOON_CAMPAIGN',
    ('trial_cancellation', 'essentials'): 'BRAZE_ESSENTIALS_TRIAL_CANCELLATION_CAMPAIGN',
    ('payment_receipt', 'essentials'): 'BRAZE_ESSENTIALS_PAYMENT_RECEIPT_CAMPAIGN',
    ('trial_end_subscription_started', 'essentials'): 'BRAZE_ESSENTIALS_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN',
    ('billing_error', 'essentials'): 'BRAZE_ESSENTIALS_BILLING_ERROR_CAMPAIGN',
    ('paid_cancellation', 'essentials'): 'BRAZE_ESSENTIALS_PAID_CANCELLATION_CAMPAIGN',
}


def get_product_type_from_slug(ssp_product_slug):
    """
    Determine product type ('teams' or 'essentials') from the ssp_product_slug.

    Behaviour:
      - If settings.ENABLE_SSP_ESSENTIALS_CAMPAIGNS is False (default), always
        return 'teams' to preserve historical campaign routing.
      - If the flag is True, classify slugs starting with 'teams' as 'teams',
        and everything else as 'essentials'.

    Returns:
        str: 'teams' or 'essentials'
    """
    if not getattr(settings, 'ENABLE_SSP_ESSENTIALS_CAMPAIGNS', False):
        return 'teams'

    if not ssp_product_slug:
        return 'teams'

    slug = str(ssp_product_slug).lower()
    if slug.startswith('teams'):
        return 'teams'
    return 'essentials'


def get_campaign_id(email_type, ssp_product_slug=None):
    """
    Resolve the Braze campaign UUID for a given email type and product slug.

    Args:
        email_type (str): One of 'signup_confirmation', 'trial_ending_soon',
            'trial_cancellation', 'payment_receipt',
            'trial_end_subscription_started', 'billing_error',
            'paid_cancellation'.
        ssp_product_slug (str|None): The SspProduct slug from CheckoutIntent.

    Returns:
        str: The Braze campaign UUID from Django settings.

    Raises:
        ValueError: If no campaign is configured for the resolved (email_type, product_type).
    """
    product_type = get_product_type_from_slug(ssp_product_slug)
    settings_key = CAMPAIGN_SETTINGS_MAP.get((email_type, product_type))

    if not settings_key:
        raise ValueError(
            f"No campaign mapping found for email_type='{email_type}', "
            f"product_type='{product_type}' (slug='{ssp_product_slug}')"
        )

    campaign_id = getattr(settings, settings_key, None)
    if not campaign_id:
        raise ValueError(
            f"Campaign setting '{settings_key}' is not configured "
            f"(email_type='{email_type}', product_type='{product_type}')"
        )

    logger.info(
        "Resolved campaign: email_type=%s, slug=%s, product_type=%s, campaign_id=%s",
        email_type, ssp_product_slug, product_type, campaign_id,
    )
    return campaign_id


def get_academy_name_from_slug(ssp_product_slug):
    """
    Resolve the academy name for trigger_properties from the SspProduct.

    Uses `get_product_type_from_slug` to determine whether the slug should be
    treated as `teams` or `essentials`, and avoids lookup for Teams products.

    Args:
        ssp_product_slug (str|None): The SspProduct slug.

    Returns:
        str|None: The academy title, or None if not applicable (e.g. Teams).
    """
    if not ssp_product_slug:
        return None

    try:
        product_type = get_product_type_from_slug(ssp_product_slug)
        if product_type == 'teams':
            return None

        from django.apps import apps  # pylint: disable=import-outside-toplevel
        ssp_product_model = apps.get_model('customer_billing', 'SspProduct')
        ssp_product = ssp_product_model.objects.get(slug=ssp_product_slug)
        return getattr(ssp_product, 'academy_title', None)
    except Exception:  # pylint: disable=broad-except
        logger.exception(
            "Failed to resolve academy_name for slug=%s", ssp_product_slug
        )
        return None
