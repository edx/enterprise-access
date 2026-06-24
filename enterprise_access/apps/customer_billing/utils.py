"""
Utility functions for customer billing app
"""

from datetime import datetime, timezone
from typing import Union

from django.apps import apps
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


def get_product_type_from_checkout_intent(checkout_intent) -> str:
    """
    Determine if a CheckoutIntent is for 'essentials' or 'teams'.

    Heuristic:
    - If the related SspProduct has an academy_uuid or an academy_title, treat as 'essentials'.
    - Otherwise default to 'teams'.
    """
    ssp_product = getattr(checkout_intent, 'ssp_product', None)
    if ssp_product:
        academy_uuid = getattr(ssp_product, 'academy_uuid', None)
        if academy_uuid:
            return 'essentials'
        academy_title = getattr(ssp_product, 'academy_title', None)
        if academy_title:
            return 'essentials'
        slug = getattr(ssp_product, 'slug', '') or ''
        if 'essentials' in slug.lower() or 'academy' in slug.lower():
            return 'essentials'
    return 'teams'


def get_product_type_from_stripe_subscription(stripe_subscription_data) -> str:
    """
    Determine product type from Stripe subscription metadata.
    """
    if not stripe_subscription_data:
        return 'teams'
    metadata = stripe_subscription_data.get('metadata') or {}
    if metadata.get('academy_name'):
        return 'essentials'
    if metadata.get('product_type') == 'essentials':
        return 'essentials'
    return 'teams'


def get_academy_name_from_checkout_intent(checkout_intent):
    """
    Extract the academy display name from a CheckoutIntent's associated SspProduct
    or from persisted StripeEventData metadata (best-effort). Returns None if not found.
    """
    ssp_product = getattr(checkout_intent, 'ssp_product', None)
    if ssp_product:
        academy_title = getattr(ssp_product, 'academy_title', None)
        if academy_title:
            return academy_title
        slug = getattr(ssp_product, 'slug', None)
        if slug:
            return _humanize_academy_name(slug)

    try:
        StripeEventData = apps.get_model('customer_billing', 'StripeEventData')
        latest_event = (
            StripeEventData.objects.filter(checkout_intent=checkout_intent)
            .order_by('-created')
            .first()
        )
        if latest_event:
            event_json = latest_event.data or {}
            subscription = (event_json.get('data') or {}).get('object', {})
            metadata = subscription.get('metadata', {}) or {}
            academy_name = metadata.get('academy_name')
            if academy_name:
                return _humanize_academy_name(academy_name)
    except LookupError:
        # Model not available or apps not loaded yet; best-effort only
        pass
    except AttributeError:
        # Unexpected structure in persisted event data
        pass

    return None


def _humanize_academy_name(academy_slug: str) -> str:
    """
    Convert academy slug or snake_case to a human-readable title.
    e.g. 'artificial_intelligence' -> 'Artificial Intelligence'
    """
    if not academy_slug:
        return academy_slug
    return str(academy_slug).replace('_', ' ').replace('-', ' ').title()


def get_campaign_for_product_type(product_type: str, campaign_type: str) -> str:
    """
    Return the correct Braze campaign UUID based on product_type and campaign_type.
    """
    CAMPAIGN_MAP = {
        'essentials': {
            'signup_confirmation': settings.BRAZE_SSP_ESSENTIALS_SIGNUP_CONFIRMATION_CAMPAIGN,
            'trial_ending': settings.BRAZE_SSP_ESSENTIALS_TRIAL_ENDING_CAMPAIGN,
            'trial_cancellation': settings.BRAZE_SSP_ESSENTIALS_TRIAL_CANCELLATION_CAMPAIGN,
            'subscription_started': settings.BRAZE_SSP_ESSENTIALS_SUBSCRIPTION_STARTED_CAMPAIGN,
            'payment_receipt': settings.BRAZE_SSP_ESSENTIALS_PAYMENT_RECEIPT_CAMPAIGN,
            'billing_error': settings.BRAZE_SSP_ESSENTIALS_BILLING_ERROR_CAMPAIGN,
            'paid_cancellation': getattr(settings, 'BRAZE_SSP_ESSENTIALS_SUBSCRIPTION_CANCELLATION_CAMPAIGN', ''),
        },
        'teams': {
            'signup_confirmation': getattr(settings, 'BRAZE_SSP_SIGNUP_CONFIRMATION_CAMPAIGN', ''),
            'trial_ending': getattr(settings, 'BRAZE_SSP_TRIAL_ENDING_CAMPAIGN', ''),
            'trial_cancellation': getattr(settings, 'BRAZE_TRIAL_CANCELLATION_CAMPAIGN', ''),
            'subscription_started': getattr(settings, 'BRAZE_SSP_SUBSCRIPTION_REINSTATED_CAMPAIGN', ''),
            'payment_receipt': getattr(settings, 'BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN', ''),
            'billing_error': getattr(settings, 'BRAZE_BILLING_ERROR_CAMPAIGN', ''),
            'paid_cancellation': getattr(settings, 'BRAZE_PAID_CANCELLATION_CAMPAIGN', ''),
        },
    }

    return CAMPAIGN_MAP.get(product_type, CAMPAIGN_MAP['teams']).get(campaign_type, '')


def get_cancellation_campaign_id(product_type, cancellation_type='trial'):
    """
    Returns the correct Braze campaign ID for cancellation emails.
    Args:
        product_type: 'essentials' or 'teams'
        cancellation_type: 'trial' or 'paid'
    """
    if cancellation_type == 'trial':
        if product_type == 'essentials':
            return settings.BRAZE_SSP_ESSENTIALS_TRIAL_CANCELLATION_CAMPAIGN
        return settings.BRAZE_TRIAL_CANCELLATION_CAMPAIGN
    # paid subscription cancellation
    if product_type == 'essentials':
        return settings.BRAZE_SSP_ESSENTIALS_SUBSCRIPTION_CANCELLATION_CAMPAIGN
    return settings.BRAZE_PAID_CANCELLATION_CAMPAIGN
