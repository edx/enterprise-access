"""
Utility functions for customer billing app
"""
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Union

from django.conf import settings

if TYPE_CHECKING:
    from enterprise_access.apps.customer_billing.models import SspProduct


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
            'trial_cancellation', 'payment_receipt',
            'trial_end_subscription_started', 'billing_error',
            'paid_cancellation'.
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
