"""
Stripe event handlers
"""
import logging
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from uuid import UUID

import stripe
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.utils import timezone
from simple_history.utils import bulk_update_with_history

from enterprise_access.apps.api_client.license_manager_client import LicenseManagerApiClient
from enterprise_access.apps.customer_billing.constants import (
    SUBSCRIPTION_ITEM_TYPE,
    CheckoutSegmentEvents,
    StripeInvoiceBillingReason,
    StripeSegmentEvents,
    StripeSubscriptionStatus
)
from enterprise_access.apps.customer_billing.models import (
    CheckoutIntent,
    SelfServiceSubscriptionRenewal,
    SspProduct,
    StripeEventData,
    StripeEventSummary
)
from enterprise_access.apps.customer_billing.stripe_event_types import StripeEventType
from enterprise_access.apps.customer_billing.tasks import (
    send_billing_error_email_task,
    send_checkout_segment_event_task,
    send_finalized_cancelation_email_task,
    send_paid_cancellation_email_task,
    send_paid_reinstatement_email_task,
    send_paid_subscription_ended_email_task,
    send_paid_subscription_renewal_notice_email_task,
    send_payment_receipt_email,
    send_trial_cancellation_email_task,
    send_trial_end_and_subscription_started_email_task,
    send_trial_ended_cancellation_email_task,
    send_trial_ending_reminder_email_task,
    send_trial_reinstatement_email_task
)
from enterprise_access.apps.customer_billing.utils import datetime_from_timestamp
from enterprise_access.apps.track.segment import track_event
from enterprise_access.toggles import ssp_paid_renewal_enabled

logger = logging.getLogger(__name__)

# Invoice events whose payload carries the subscription details, under ``invoice.parent``.
INVOICE_EVENT_TYPES = ('invoice.paid', 'invoice.created', 'invoice.payment_failed')

# How long a paid plan stays active past its end date while its annual renewal invoice is unpaid.
ANNUAL_RENEWAL_GRACE_PERIOD = timedelta(days=7)
# Length of each renewed paid term.
ANNUAL_RENEWAL_TERM = relativedelta(years=1)


class CheckoutIntentLookupError(Exception):
    """Raised when CheckoutIntent cannot be found by UUID or ID."""


class AnnualRenewalError(Exception):
    """
    Raised when an annual renewal cannot be processed. The webhook fails so Stripe retries it, and the
    ``outcome=halted`` audit log alerts engineering and CS.
    """

    def __init__(self, message: str = '', audit_logged: bool = False):
        super().__init__(message)
        # True once the error is audit logged, so it is not logged again as outcome=failed.
        self.audit_logged = audit_logged


# Central registry for event handlers.
#
# Needs to be in module scope instead of class scope because the decorator
# didn't have access to the class name soon enough during runtime initialization.
_handlers_by_type: dict[StripeEventType, Callable[[stripe.Event], None]] = {}


def get_invoice_and_subscription(event: stripe.Event):
    """
    Given a stripe invoice event, return the invoice and related subscription records.
    """
    invoice = event.data.object
    subscription_details = invoice.parent.subscription_details
    return invoice, subscription_details


def get_checkout_intent_identifier_from_subscription(stripe_subscription) -> tuple[str | None, int | None]:
    """
    Returns the CheckoutIntent identifiers stored in the given
    stripe subscription's metadata.

    Returns:
        tuple: (uuid_str, id_int) - Either may be None. UUID is preferred if both present.
    """
    metadata = stripe_subscription.metadata.to_dict()
    # The stripe subscription object may actually be a SubscriptionDetails
    # record from an invoice.
    stripe_subscription_id = (
        getattr(stripe_subscription, 'id', None) or getattr(stripe_subscription, 'subscription', None)
    )

    uuid_str = metadata.get('checkout_intent_uuid')
    id_str = metadata.get('checkout_intent_id')
    id_int = None

    if id_str:
        try:
            id_int = int(id_str)
        except (ValueError, TypeError):
            logger.warning(
                'Invalid checkout_intent_id format in metadata: %s for subscription=%s',
                id_str, stripe_subscription_id,
            )

    if uuid_str:
        logger.info(
            'Found checkout_intent_uuid=%s from subscription=%s',
            uuid_str, stripe_subscription_id,
        )
    elif id_int is not None:
        logger.info(
            'Found checkout_intent_id=%s from subscription=%s (UUID not present - legacy record)',
            id_int, stripe_subscription_id,
        )

    return uuid_str, id_int


def persist_stripe_event(event: stripe.Event) -> StripeEventData | None:
    """
    Creates and returns a new ``StripeEventData`` object, or ``None`` if no
    related subscription can be found for the given event.
    """
    stripe_subscription = None
    if event.type in INVOICE_EVENT_TYPES:
        _, stripe_subscription = get_invoice_and_subscription(event)
    elif event.type.startswith('customer.subscription'):
        stripe_subscription = event.data.object

    if not stripe_subscription:
        logger.error(
            'Cannot persist StripeEventData, no subscription found for event %s with type %s',
            event.id,
            event.type,
        )
        return None

    uuid_str, id_int = get_checkout_intent_identifier_from_subscription(stripe_subscription)
    checkout_intent = None
    stripe_customer_id = event.data.object.to_dict().get('customer')

    # Prefer UUID lookup, fall back to ID for legacy records
    if uuid_str:
        try:
            uuid_value = UUID(uuid_str)
            checkout_intent = CheckoutIntent.objects.filter(
                uuid=uuid_value,
                stripe_customer_id=stripe_customer_id,
            ).first()
        except (ValueError, TypeError) as exc:
            logger.warning('Invalid UUID format in metadata: %s, error: %s', uuid_str, exc)

    if not checkout_intent and id_int is not None:
        checkout_intent = CheckoutIntent.objects.filter(
            id=id_int,
            stripe_customer_id=stripe_customer_id,
        ).first()

    record, _ = StripeEventData.objects.get_or_create(
        event_id=event.id,
        defaults={
            'event_type': event.type,
            'checkout_intent': checkout_intent,
            'data': event.to_dict(),
        },
    )
    logger.info('Persisted StripeEventData %s', record)
    return record


def get_checkout_intent_or_raise(
    uuid_str: str | None,
    id_int: int | None,
    event_id: str,
) -> CheckoutIntent:
    """
    Returns a CheckoutIntent by UUID (preferred) or ID (fallback).

    Args:
        uuid_str: The UUID string from metadata, may be None
        id_int: The integer ID from metadata, may be None
        event_id: The Stripe event ID for logging

    Returns:
        CheckoutIntent: The found record

    Raises:
        CheckoutIntentLookupError: If no matching record found
    """
    root_cause = None

    # Prefer UUID lookup
    if uuid_str:
        try:
            uuid_value = UUID(uuid_str)
            return CheckoutIntent.objects.select_related('ssp_product').get(uuid=uuid_value)
        except (ValueError, TypeError) as exc:
            logger.warning(
                'Invalid UUID format %s for event %s: %s',
                uuid_str, event_id, exc,
            )
            root_cause = exc
        except CheckoutIntent.DoesNotExist as exc:
            logger.warning(
                'CheckoutIntent with uuid=%s not found for event %s',
                uuid_str, event_id,
            )
            root_cause = exc

    # Fall back to ID lookup
    if id_int is not None:
        try:
            return CheckoutIntent.objects.select_related('ssp_product').get(id=id_int)
        except CheckoutIntent.DoesNotExist as exc:
            logger.warning(
                'CheckoutIntent with id=%s not found for event %s',
                id_int, event_id,
            )
            root_cause = exc

    # Both lookups failed
    raise CheckoutIntentLookupError(
        f'No CheckoutIntent found for uuid={uuid_str} or id={id_int} (event {event_id})'
    ) from root_cause


def handle_pending_update(subscription_id: str, checkout_intent_id: int, pending_update):
    """
    Log pending update information for visibility.
    Assumes a pending_update is present.
    """
    # TODO: take necessary action on the actual SubscriptionPlan and update the CheckoutIntent.
    logger.warning(
        "Subscription %s has pending update: %s. checkout_intent_id: %s",
        subscription_id,
        pending_update,
        checkout_intent_id,
    )


def link_event_data_to_checkout_intent(event, checkout_intent):
    """
    Set the StripeEventData record for the given event to point at the provided CheckoutIntent.
    """
    event_data = StripeEventData.objects.get(event_id=event.id)
    if not event_data.checkout_intent:
        event_data.checkout_intent = checkout_intent
        event_data.save()  # this triggers a post_save signal that updates the related summary record


def cancel_all_future_plans(checkout_intent, include_processed=True):
    """
    Deactivate (cancel) the renewed plans of this checkout intent's renewals.

    By default this includes processed renewals, whose renewed plan is the one currently in effect, which is
    what a canceled subscription needs. Pass ``include_processed=False`` to only deactivate plans that have not
    started yet, keeping the current paid plan active (e.g. through an annual renewal's grace period).
    Renewals without a renewed plan yet are skipped.
    """
    renewals = checkout_intent.renewals.filter(renewed_subscription_plan_uuid__isnull=False)
    if not include_processed:
        renewals = renewals.filter(processed_at__isnull=True)
    if not renewals.exists():
        logger.warning('No renewals to cancel for Checkout Intent %s', checkout_intent.uuid)
        return []

    client = LicenseManagerApiClient()
    deactivated: list[UUID] = []

    for renewal in renewals:
        client.update_subscription_plan(
            str(renewal.renewed_subscription_plan_uuid),
            is_active=False,
        )
        deactivated_plan_uuid = renewal.renewed_subscription_plan_uuid
        deactivated.append(deactivated_plan_uuid)
        logger.info('Future plan %s de-activated for Checkout Intent %s', deactivated_plan_uuid, checkout_intent.uuid)

    return deactivated


def _update_renewal_cancellation_state(
    checkout_intent: CheckoutIntent,
    is_canceled: bool,
    subscription_cancel_at=None,
) -> None:
    """Set cancellation state on all renewals for a checkout intent."""
    renewals = list(checkout_intent.renewals.all())
    for renewal in renewals:
        renewal.is_canceled = is_canceled
        renewal.subscription_cancel_at = subscription_cancel_at
        renewal.modified = timezone.now()

    updated = bulk_update_with_history(
        renewals,
        SelfServiceSubscriptionRenewal,
        ['is_canceled', 'subscription_cancel_at', 'modified'],
        batch_size=100,
    )
    logger.info(
        'Updated %d renewal(s) for CheckoutIntent %s: is_canceled=%s, subscription_cancel_at=%s',
        updated, checkout_intent.id, is_canceled, subscription_cancel_at,
    )


def _try_enable_pending_updates(stripe_subscription_id):
    """
    We rely on Stripe’s Pending Updates feature to help prevent subscriptions from becoming active
    before a payment is *successfully* processed
    See: https://docs.stripe.com/billing/subscriptions/pending-updates
    and https://docs.stripe.com/api/subscriptions/update
    """
    try:
        # Update the subscription to enable pending updates for future modifications,
        # notably, with the proration_behavior set to "always_invoice".
        # This ensures that quantity changes through the billing portal will only
        # be applied if payment succeeds, preventing license count drift
        logger.info(f'Enabling pending updates for created subscription {stripe_subscription_id}')
        stripe.Subscription.modify(
            stripe_subscription_id,
            payment_behavior='pending_if_incomplete',
            proration_behavior='always_invoice',
        )

        logger.info('Successfully enabled pending updates for subscription %s', stripe_subscription_id)
    except stripe.StripeError as e:
        logger.error('Failed to enable pending updates for subscription %s: %s', stripe_subscription_id, e)


def _queue_checkout_segment_event(event, event_name, checkout_intent, subscription_id, **task_kwargs):
    """
    Queue a server-side checkout Segment event, if FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2 is on.

    Each order has one such Stripe event, so the event is queued once per Stripe event: a redelivery of an event that
    was already handled queues nothing. Call it as the handler's last step, after any work that can raise: a
    delivery whose handler failed is not marked handled and Stripe redelivers it for days, longer than Segment
    de-duplicates a messageId. A broker error is not caught, so the webhook fails and Stripe redelivers it; the
    task's fixed messageId lets Segment drop the rare repeat.
    """
    if not settings.FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2:
        return
    if StripeEventData.objects.filter(event_id=event.id, handled_at__isnull=False).exists():
        logger.info('Stripe event %s was already handled; not queuing Segment event %s again', event.id, event_name)
        return
    send_checkout_segment_event_task.delay(event_name, checkout_intent.id, subscription_id, **task_kwargs)


def track_subscription_cancellation(checkout_intent: CheckoutIntent, cancellation_details: dict):
    """
    Track subscription cancellation event to Segment with cancellation details.

    This function extracts relevant cancellation information (reason, comment, feedback)
    from Stripe's cancellation_details and sends it to Segment for analytics.

    Args:
        checkout_intent: The CheckoutIntent associated with the subscription
        cancellation_details: The cancellation_details dict from Stripe subscription object,
                            containing 'reason', 'comment', and 'feedback' fields
    """
    if not cancellation_details:
        logger.info(
            'No cancellation_details provided for CheckoutIntent %s, skipping cancellation tracking',
            checkout_intent.id
        )
        return

    properties = {
        'checkout_intent_id': checkout_intent.id,
        'reason': cancellation_details.get('reason'),
        'comment': cancellation_details.get('comment'),
        'feedback': cancellation_details.get('feedback'),
    }

    logger.info(
        'Tracking subscription cancellation event for CheckoutIntent %s: reason=%s, comment=%s, feedback=%s',
        checkout_intent.id,
        properties['reason'],
        properties['comment'],
        properties['feedback']
    )

    track_event(
        lms_user_id=str(checkout_intent.user.id),
        event_name=StripeSegmentEvents.SUBSCRIPTION_CANCELED,
        properties=properties,
    )


def _valid_invoice_event_type(event: stripe.Event):
    """
    Determine whether an ``invoice.*`` Stripe event belongs to the SSP workflow.

    Stripe emits ``invoice.*`` events for multiple billing workflows. This helper
    acts as a guard to identify only those invoices that were generated from a
    subscription-based SSP checkout flow.

    The check is performed by inspecting the first invoice line item and verifying
    that its parent type matches ``SUBSCRIPTION_ITEM_TYPE`` to confirm this
    invoice was created by a subscription, as opposed to a one-off invoice or
    other non-SSP invoicing scenarios.

    The function is intentionally defensive:
    - If the invoice payload is missing expected fields
      (e.g., no lines, no parent, or no type), the event is treated as invalid.
    - Any structural mismatch results in ``False`` rather than raising, allowing
      webhook handling to safely NOOP while still returning HTTP 200 to Stripe.

    Args:
        event (stripe.Event): A Stripe ``invoice.*`` webhook event.

    Returns:
        bool: ``True`` if the event represents an SSP-related subscription invoice,
        otherwise ``False``.
    """
    invoice = event.data.object
    try:
        return invoice["lines"]["data"][0]["parent"]["type"] == SUBSCRIPTION_ITEM_TYPE
    except (KeyError, IndexError, TypeError):
        return False


def _is_annual_renewal_invoice(invoice, subscription_details) -> bool:
    """
    Determine whether a paid invoice renews an already paid subscription (its second or later paid term).

    Stripe also uses ``billing_reason == 'subscription_cycle'`` for the first paid invoice, issued when
    the trial ends, so the reason alone is not enough. The subscription must also have been paid by an
    earlier invoice, otherwise the trial→paid transition (or its retry) would be skipped.
    """
    if invoice.to_dict().get('billing_reason') != StripeInvoiceBillingReason.SUBSCRIPTION_CYCLE:
        return False
    return StripeEventSummary.objects.filter(
        stripe_subscription_id=subscription_details.to_dict().get('subscription'),
        event_type='invoice.paid',
        invoice_amount_paid__gt=0,
    ).exclude(stripe_invoice_id=invoice['id']).exists()


def _subscription_item_lines(invoice_dict: dict) -> list[dict]:
    """
    Return the invoice lines that bill a subscription item. One-off invoice items (e.g. adjustments) are left out.
    """
    lines = (invoice_dict.get('lines') or {}).get('data') or []
    return [line for line in lines if (line.get('parent') or {}).get('type') == SUBSCRIPTION_ITEM_TYPE]


def _renewal_invoice_line(invoice_dict: dict) -> dict:
    """
    Return the invoice line that bills the renewed subscription term: the first subscription item line that is
    not a proration.
    """
    for line in _subscription_item_lines(invoice_dict):
        if not line['parent'].get('subscription_item_details', {}).get('proration'):
            return line
    raise AnnualRenewalError(f'Invoice {invoice_dict.get("id")} has no subscription term line')


def _ssp_product_slugs_for_price(price_id: str) -> set[str]:
    """
    Return the SspProduct slugs a Stripe Price points at: its ``ssp_product_slug`` metadata, and the product
    whose ``stripe_price_lookup_key`` it carries. Two different slugs means the Price and our product
    table disagree.
    """
    price = stripe.Price.retrieve(price_id).to_dict()
    slugs = set()
    metadata_slug = (price.get('metadata') or {}).get('ssp_product_slug')
    if metadata_slug:
        slugs.add(metadata_slug)
    lookup_key = price.get('lookup_key')
    if lookup_key:
        # No is_active filter: grandfathered products must still renew.
        product = SspProduct.objects.filter(stripe_price_lookup_key=lookup_key).only('slug').first()
        if product:
            slugs.add(product.slug)
    return slugs


def _log_renewal(level: int, outcome: str, checkout_intent: CheckoutIntent, invoice_id: str, detail: str = '',
                 exc_info: bool = False) -> None:
    """
    Write an annual renewal audit log line.

    Every renewal step logs in this one format, so Datadog can monitor on ``[SSPAnnualRenewal]`` and
    ``outcome``: ``halted`` and ``failed`` (errors, the customer may have paid without getting next year's
    plan) and ``switch_off``; the other outcomes are the audit trail.
    """
    logger.log(
        level,
        '[SSPAnnualRenewal] outcome=%s checkout_intent_uuid=%s enterprise_slug=%s invoice_id=%s %s',
        outcome, checkout_intent.uuid, checkout_intent.enterprise_slug, invoice_id, detail,
        exc_info=exc_info,
    )


@contextmanager
def _audit_renewal_failures(step: str, checkout_intent: CheckoutIntent, invoice_id: str):
    """
    Log any unexpected error in an annual renewal step (e.g. a License Manager API error) as ``outcome=failed``,
    then re-raise it. Halts are logged by ``_halt_annual_renewal`` and are not logged twice.
    """
    try:
        yield
    except Exception as exc:
        if not getattr(exc, 'audit_logged', False):
            _log_renewal(logging.ERROR, 'failed', checkout_intent, invoice_id, f'step={step} error={exc!r}',
                         exc_info=True)
        raise


def _halt_annual_renewal(message: str, checkout_intent: CheckoutIntent, invoice_id: str):
    """
    Log an annual renewal that cannot proceed with enough context for engineering and CS, then raise. The caller
    decides whether the webhook fails.
    """
    _log_renewal(logging.ERROR, 'halted', checkout_intent, invoice_id, f'reason={message!r}')
    raise AnnualRenewalError(message, audit_logged=True)


def resolve_renewal_product(checkout_intent: CheckoutIntent, invoice) -> SspProduct:
    """
    Resolve and validate the SspProduct an annual renewal invoice bills.

    The product comes from the Stripe Price on the renewal invoice (its ``ssp_product_slug`` metadata, or
    its lookup key), and must be the product the customer bought at checkout. Unlike first-time
    provisioning, a renewal never falls back to ``teams-yearly``. Inactive products are accepted so
    grandfathered customers can still renew.

    Raises:
        AnnualRenewalError: if a Price resolves to no product, to more than one, or to one other than the
            purchased product, or if an academy product's title cannot be resolved for the renewed plan.
    """
    invoice_dict = invoice.to_dict()
    invoice_id = invoice_dict['id']

    def halt(message):
        _halt_annual_renewal(message, checkout_intent, invoice_id)

    # Lines carry only the Price ID, not a Price object (see "Gotchas" in docs/references/ssp_annual_renewals.md).
    price_ids = {
        price_id
        for line in _subscription_item_lines(invoice_dict)
        if (price_id := ((line.get('pricing') or {}).get('price_details') or {}).get('price'))
    }
    if not price_ids:
        halt('the renewal invoice has no Stripe Price')

    purchased_slug = checkout_intent.ssp_product_id
    for price_id in sorted(price_ids):
        price_slugs = _ssp_product_slugs_for_price(price_id)
        if price_slugs != {purchased_slug}:
            halt(
                f'Stripe Price {price_id} resolves to SSP products {sorted(price_slugs)}, '
                f'not the purchased product {purchased_slug!r}'
            )

    product = checkout_intent.ssp_product
    if product.academy_uuid and not product.academy_title:
        halt(f'could not resolve the academy title of {purchased_slug!r} to name the renewed plan')
    return product


def _renewed_plan_title(
    checkout_intent: CheckoutIntent,
    product: SspProduct,
    effective_date: datetime,
    number_of_licenses: int,
) -> str:
    """
    Return the title of the plan an annual renewal creates.

    Both products follow the ENT-12202 naming convention, with the month and year the renewed term ends (as the
    admin portal shows it): "[Company Name] [# licenses] SUBS [Month YYYY] - Essentials [Academy Name] PAID", and
    "[Company Name] [# licenses] SUBS [Month YYYY] - Teams PAID". License Manager requires plan titles to be unique
    per customer agreement, and the month and year make each term's title unique.
    """
    term_end = effective_date + ANNUAL_RENEWAL_TERM
    product_name = f'Essentials {product.academy_title}' if product.academy_uuid else 'Teams'
    return f'{checkout_intent.enterprise_name} {number_of_licenses} SUBS {term_end:%B %Y} - {product_name} PAID'


def _current_paid_plan_uuid(checkout_intent: CheckoutIntent) -> UUID | None:
    """
    Return the paid plan currently in effect for the checkout intent: the renewed plan of its most recently
    processed renewal (the first paid plan after trial->paid, then each annual renewal's plan).
    """
    renewal = SelfServiceSubscriptionRenewal.objects.filter(
        checkout_intent=checkout_intent,
        processed_at__isnull=False,
        renewed_subscription_plan_uuid__isnull=False,
    ).order_by('-processed_at').first()
    return renewal.renewed_subscription_plan_uuid if renewal else None


def keep_current_plan_active_for_renewal(checkout_intent: CheckoutIntent, invoice) -> bool:
    """
    Keep the paid plan that is ending (year 1) active until a week after its original end date.

    Year 1 expires at the renewal period start, but year 2 is only created once the renewal invoice is paid:
    some time after that, or up to a week later while Stripe retries a failed charge. The expiration is set
    to an absolute date (renewal period start + grace period), so repeated events do not keep pushing it out.
    Once the renewal is processed, ``_process_annual_renewal`` moves it back to the period start.

    Returns:
        bool: True if year 1 was extended, False if the invoice is already renewed.

    Raises:
        AnnualRenewalError: if the checkout intent has no processed paid plan.
    """
    invoice_id = invoice['id']
    already_renewed = SelfServiceSubscriptionRenewal.objects.filter(
        stripe_invoice_id=invoice_id, processed_at__isnull=False,
    ).exists()
    if already_renewed:
        _log_renewal(logging.INFO, 'grace_skipped_already_renewed', checkout_intent, invoice_id)
        return False

    with _audit_renewal_failures('grace_extension', checkout_intent, invoice_id):
        current_plan_uuid = _current_paid_plan_uuid(checkout_intent)
        if current_plan_uuid is None:
            _halt_annual_renewal('no processed paid subscription plan to keep active', checkout_intent, invoice_id)

        period_start = datetime_from_timestamp(_renewal_invoice_line(invoice.to_dict())['period']['start'])
        grace_expiration = period_start + ANNUAL_RENEWAL_GRACE_PERIOD
        LicenseManagerApiClient().update_subscription_plan(
            str(current_plan_uuid),
            expiration_date=grace_expiration.isoformat(),
        )
        _log_renewal(
            logging.INFO, 'grace_extended', checkout_intent, invoice_id,
            f'plan_uuid={current_plan_uuid} expiration_date={grace_expiration.isoformat()}',
        )
    return True


def _set_renewal_grace_period(checkout_intent: CheckoutIntent, invoice_id: str | None, in_grace_period: bool):
    """
    Record whether the customer is in the renewal grace period (a failed renewal charge, year 1 kept active).
    Saves and audit logs only on a change, so replayed events do nothing.
    """
    if checkout_intent.in_renewal_grace_period == in_grace_period:
        return
    checkout_intent.in_renewal_grace_period = in_grace_period
    checkout_intent.save(update_fields=['in_renewal_grace_period', 'modified'])
    outcome = 'grace_period_started' if in_grace_period else 'grace_period_ended'
    _log_renewal(logging.INFO, outcome, checkout_intent, invoice_id)


def _process_annual_renewal(
    event: stripe.Event,
    checkout_intent: CheckoutIntent,
    invoice,
    subscription_details,
) -> bool:
    """
    Move a paid subscription onto next year's paid plan, for a paid ``subscription_cycle`` invoice.

    Creates a License Manager SubscriptionPlanRenewal without a renewed plan (License Manager creates it from
    year 1 when processing: same product, enterprise catalog and catalog query), tracks it in a
    SelfServiceSubscriptionRenewal keyed by the invoice, processes it and activates the new plan.

    Year 2 starts at the invoice period start, which is year 1's original end date, and ends one year later,
    so a payment that clears during the grace period keeps the original dates.

    Idempotent on ``stripe_invoice_id``: a processed renewal is skipped, and an unprocessed one resumes at the
    processing step.

    Returns:
        bool: True if this call processed the renewal, False if it had already been processed.
    """
    invoice_dict = invoice.to_dict()
    stripe_invoice_id = invoice_dict['id']
    client = LicenseManagerApiClient()

    renewal = SelfServiceSubscriptionRenewal.objects.filter(stripe_invoice_id=stripe_invoice_id).first()
    if renewal and renewal.processed_at:
        _log_renewal(logging.INFO, 'already_processed', checkout_intent, stripe_invoice_id, f'renewal_id={renewal.id}')
        return False
    if checkout_intent.renewals.filter(is_canceled=True).exists():
        # customer.subscription.deleted already ended the subscription and deactivated its plans. Don't renew
        # it, and don't fail the webhook: retries can't change this. The customer may have paid, so CS has to
        # refund or reinstate by hand.
        _log_renewal(
            logging.ERROR, 'halted', checkout_intent, stripe_invoice_id,
            "reason='the subscription is canceled, so the paid renewal invoice was not processed'",
        )
        return False

    product = resolve_renewal_product(checkout_intent, invoice)
    line = _renewal_invoice_line(invoice_dict)
    effective_date = datetime_from_timestamp(line['period']['start'])
    if renewal is None:
        prior_plan_uuid = _current_paid_plan_uuid(checkout_intent)
        if prior_plan_uuid is None:
            _halt_annual_renewal('no processed paid subscription plan to renew', checkout_intent, stripe_invoice_id)

        plan_renewal = client.create_subscription_plan_renewal(
            prior_subscription_plan_uuid=str(prior_plan_uuid),
            renewed_subscription_plan_uuid=None,
            salesforce_opportunity_line_item_id=None,
            effective_date=effective_date.isoformat(),
            renewed_expiration_date=(effective_date + ANNUAL_RENEWAL_TERM).isoformat(),
            number_of_licenses=line['quantity'],
            renewed_plan_title=_renewed_plan_title(checkout_intent, product, effective_date, line['quantity']),
            # This handler processes the renewal itself, the License Manager batch job must not.
            exempt_from_batch_processing=True,
        )
        renewal = SelfServiceSubscriptionRenewal.objects.create(
            checkout_intent=checkout_intent,
            subscription_plan_renewal_id=plan_renewal['id'],
            prior_subscription_plan_uuid=prior_plan_uuid,
            stripe_event_data=StripeEventData.objects.get(event_id=event.id),
            stripe_subscription_id=subscription_details.to_dict().get('subscription'),
            stripe_invoice_id=stripe_invoice_id,
            effective_date=effective_date,
        )
        _log_renewal(
            logging.INFO, 'created', checkout_intent, stripe_invoice_id,
            f'renewal_id={renewal.id} license_manager_renewal_id={plan_renewal["id"]} product={product.slug} '
            f'prior_plan_uuid={prior_plan_uuid} licenses={line["quantity"]}',
        )

    result = client.process_subscription_plan_renewal(renewal.subscription_plan_renewal_id)
    renewed_plan_uuid = (result or {}).get('renewed_subscription_plan')
    if not renewed_plan_uuid:
        _halt_annual_renewal(
            f'License Manager returned no renewed plan for renewal {renewal.subscription_plan_renewal_id}',
            checkout_intent, stripe_invoice_id,
        )

    renewal.renewed_subscription_plan_uuid = renewed_plan_uuid
    renewal.save(update_fields=['renewed_subscription_plan_uuid', 'modified'])
    client.update_subscription_plan(str(renewed_plan_uuid), is_active=True)
    # Undo any grace period extension, so year 1 ends exactly where year 2 starts.
    client.update_subscription_plan(
        str(renewal.prior_subscription_plan_uuid),
        expiration_date=renewal.effective_date.isoformat(),
    )
    # Before the processed checkpoint, so a failure in between is retried: a replay of a processed renewal
    # does nothing.
    _set_renewal_grace_period(checkout_intent, stripe_invoice_id, False)
    renewal.mark_as_processed()
    _log_renewal(
        logging.INFO, 'processed', checkout_intent, stripe_invoice_id,
        f'renewal_id={renewal.id} prior_plan_uuid={renewal.prior_subscription_plan_uuid} '
        f'renewed_plan_uuid={renewed_plan_uuid}',
    )
    return True


def _send_payment_receipt(invoice, checkout_intent: CheckoutIntent) -> None:
    send_payment_receipt_email.delay(
        invoice_id=invoice.id,
        invoice_data=invoice.to_dict(),
        enterprise_customer_name=checkout_intent.enterprise_name,
        enterprise_slug=checkout_intent.enterprise_slug,
    )


def _handle_annual_renewal_invoice_paid(
    event: stripe.Event,
    checkout_intent: CheckoutIntent,
    invoice,
    subscription_details,
) -> None:
    """
    Handle a paid annual renewal invoice.

    With the ``ssp_paid_renewal`` switch on, renew onto next year's plan, then email the admins. The emails go
    out only after the new plan exists, and only once per invoice: a failed attempt raises first so Stripe
    retries, and a replay of a processed renewal sends nothing.

    With the switch off, keep the behaviour from before automated renewals: email the admins and log an error,
    so the renewal is noticed and handled by hand.
    """
    if not ssp_paid_renewal_enabled():
        _log_renewal(
            logging.ERROR, 'switch_off', checkout_intent, invoice.id,
            'reason=\'annual renewal not processed, the ssp_paid_renewal switch is off\'',
        )
    else:
        with _audit_renewal_failures('process', checkout_intent, invoice.id):
            renewed = _process_annual_renewal(event, checkout_intent, invoice, subscription_details)
        if not renewed:
            return

    _send_payment_receipt(invoice, checkout_intent)
    send_paid_subscription_renewal_notice_email_task.delay(
        checkout_intent_id=checkout_intent.id,
        invoice_data=invoice.to_dict(),
    )


def _handle_invoice_paid_status_updated(
    event: stripe.Event,
    checkout_intent: CheckoutIntent,
) -> None:
    """
    Handling of subscription status changes coming from an invoice.paid Stripe event.
    This is ONLY called for invoice.total > 0, meaning we're in the paid phase.

    Looks up the ``SelfServiceSubscriptionRenewal`` by ``stripe_invoice_id`` (linked
    during ``invoice.created`` processing). If the renewal is not found — typically
    because ``invoice.created`` was delivered after ``invoice.paid`` — an exception is
    raised so that Stripe retries the webhook.

    When an invoice is paid:
    - If the renewal is already processed, idempotently reactivate the paid plan
    - If the renewal is not yet processed, run the trial→paid transition

    Args:
        event (stripe.Event): A Stripe ``invoice.paid`` webhook event.
        checkout_intent (CheckoutIntent): The CheckoutIntent associated with the subscription.
    """

    invoice, subscription_details = get_invoice_and_subscription(event)
    subscription_details = subscription_details.to_dict()
    stripe_subscription_id = subscription_details.get('subscription')
    stripe_invoice_id = invoice['id']

    # Look up the renewal by stripe_invoice_id (linked during invoice.created).
    renewal = SelfServiceSubscriptionRenewal.objects.filter(
        stripe_invoice_id=stripe_invoice_id,
    ).first()

    if not renewal:
        # If invoice.created hasn't linked the renewal yet (out-of-order delivery),
        # raise an error to force Stripe to retry the webhook.
        error_msg = (
            f"No SelfServiceSubscriptionRenewal found for checkout_intent {checkout_intent.id} "
            f"with stripe_invoice_id {stripe_invoice_id}"
        )
        logger.error(error_msg)
        raise SelfServiceSubscriptionRenewal.DoesNotExist(error_msg)

    client = LicenseManagerApiClient()

    if renewal.processed_at:
        # Already processed — idempotently reactivate the paid plan in case
        # it was deactivated during a past_due episode.
        plan_to_reactivate = renewal.renewed_subscription_plan_uuid
        if plan_to_reactivate:
            logger.info(
                "Activating PAID subscription plan %s for Stripe subscription %s (post-trial)",
                plan_to_reactivate,
                stripe_subscription_id,
            )
            client.update_subscription_plan(str(plan_to_reactivate), is_active=True)
        else:
            logger.error(
                "SelfServiceSubscriptionRenewal %s record does not have renewed_subscription_plan_uuid",
                renewal,
            )
    else:
        # First paid invoice — process the trial→paid transition.
        logger.info(
            "Processing trial→paid transition for Stripe subscription %s",
            stripe_subscription_id,
        )
        _process_trial_to_paid_renewal(renewal, stripe_subscription_id, event)

        # After processing, idempotently make sure the paid plan is active.
        renewal.refresh_from_db()
        if renewal.renewed_subscription_plan_uuid:
            client.update_subscription_plan(str(renewal.renewed_subscription_plan_uuid), is_active=True)

        # Send the trial-end email
        send_trial_end_and_subscription_started_email_task.delay(
            subscription_id=stripe_subscription_id,
            checkout_intent_id=checkout_intent.id,
        )


def _handle_subscription_updated_status_updates(
        event: stripe.Event,
        prior_status: StripeSubscriptionStatus,
        current_status: StripeSubscriptionStatus,
        checkout_intent: CheckoutIntent,
) -> None:
    """
    Handling of a status change coming from a customer.subscription.updated Stripe event.

    Stripe has many different statuses that need to be handled differently, including trialing,
    active, past_due, canceled, unpaid, and paused. When a subscription transitions from one to the other,
    we need to update our system and sources of truth to reflect this status change.

    Args:
        event (stripe.Event): A Stripe ``customer.subscription.updated`` webhook event.
        prior_status (StripeSubscriptionStatus): The subscription's status we have stored before this event
            Must be a ``StripeSubscriptionStatus`` or string matching the enum values.
        current_status (StripeSubscriptionStatus): The subscription's status of this event.
            Must be a ``StripeSubscriptionStatus`` or string matching the enum values.
        checkout_intent (CheckoutIntent): The CheckoutIntent associated with the subscription.
    """
    subscription = event.data.object
    # Past due transition
    if current_status != prior_status and current_status == StripeSubscriptionStatus.PAST_DUE:
        logger.warning(
            'Stripe subscription %s was %s but is now past_due. '
            'Checkout intent: %s',
            subscription.id, prior_status, checkout_intent.id,
        )
        enterprise_uuid = checkout_intent.enterprise_uuid
        if enterprise_uuid:
            # With ssp_paid_renewal on, keep the paid plan in effect: a failed annual renewal charge gets a grace
            # period, and the plan expires on its own if the charge never clears.
            cancel_all_future_plans(checkout_intent, include_processed=not ssp_paid_renewal_enabled())
        else:
            logger.error(
                (
                    "Cannot deactivate future plans for subscription %s: "
                    "missing enterprise_uuid on CheckoutIntent %s"
                ),
                subscription.id,
                checkout_intent.id,
            )
        send_billing_error_email_task.delay(checkout_intent_id=checkout_intent.id)


class StripeEventHandler:
    """
    Container for Stripe event handler logic.
    """
    @classmethod
    def dispatch(cls, event: stripe.Event) -> None:
        """
        Dispatches an event to the appropriate handler.
        """
        if event.type not in _handlers_by_type:
            logger.warning('No stripe event handler configured for event type %s', event.type)
            return
        _handlers_by_type[event.type](event)

    @staticmethod
    def on_stripe_event(event_type: StripeEventType):
        """
        Decorator to register a function as an event handler.
        """
        def decorator(handler_method: Callable[[stripe.Event], None]):

            # Wrap the handler to add helpful logging.
            @wraps(handler_method)
            def wrapper(event: stripe.Event) -> None:
                # The default __repr__ is really long because it just barfs out the entire payload.
                event_short_repr = f'<stripe.Event id={event.id} type={event.type}>'
                logger.info(f'[StripeEventHandler] handling {event_short_repr}.')
                if event.type in INVOICE_EVENT_TYPES \
                        and not _valid_invoice_event_type(event):
                    logger.warning(
                        f'[StripeEventHandler] event {event_short_repr} is not a valid invoice type'
                    )
                    return
                event_record = persist_stripe_event(event)
                handler_method(event)
                # Mark event as handled if we persisted it successfully and no exception was raised
                if event_record is not None:
                    event_record.refresh_from_db()
                    event_record.mark_as_handled()
                logger.info(f'[StripeEventHandler] handler for {event_short_repr} complete.')

            # Register the wrapped handler method.
            _handlers_by_type[event_type] = wrapper

            return wrapper
        return decorator

    ##################
    # BEGIN HANDLERS #
    ##################

    @on_stripe_event('invoice.paid')
    @staticmethod
    def invoice_paid(event: stripe.Event) -> None:
        """
        Handle invoice.paid events.
        If the amount is greater than zero (i.e. no longer in free trial),
        send a receipt email.
        """
        invoice, subscription_details = get_invoice_and_subscription(event)
        stripe_customer_id = invoice['customer']

        uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription_details)
        try:
            checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
        except CheckoutIntentLookupError:
            logger.error(
                '[StripeEventHandler] invoice.paid event %s could not find Checkout Intent '
                'uuid=%s id=%s to mark as paid',
                event.id, uuid_str, id_int,
            )
            return

        link_event_data_to_checkout_intent(event, checkout_intent)
        if invoice.total > 0:
            if _is_annual_renewal_invoice(invoice, subscription_details):
                _handle_annual_renewal_invoice_paid(event, checkout_intent, invoice, subscription_details)
                return
            # Attempt to send the receipt FIRST before triggering renewal.
            # Renewal might want to force a retry by raising, risking duplicate receipt emails. We'll
            # mitigate this by configuring the Braze campaign to avoid sending more than 1 in a 3
            # day period.
            _send_payment_receipt(invoice, checkout_intent)
            # only update status for non-trial invoice.paid events
            _handle_invoice_paid_status_updated(event, checkout_intent)
            return

        try:
            checkout_intent.mark_as_paid(stripe_customer_id=stripe_customer_id)
            logger.info(
                'Marked checkout_intent uuid=%s as paid via invoice=%s',
                checkout_intent.uuid, invoice.id,
            )
        except ValueError as exc:
            logger.warning(
                'Could not mark checkout intent %s as paid via invoice %s, because %s',
                checkout_intent.uuid, invoice.id, exc,
            )
        # order_completed for the subscription's first invoice, paid at checkout. Every SSP checkout starts a trial
        # (SSP_TRIAL_PERIOD_DAYS), so that invoice is always $0 and handled here, not on the paid path above. Last,
        # after mark_as_paid, so a broker outage never holds up the checkout; redelivery repeats it harmlessly.
        if invoice.to_dict().get('billing_reason') == StripeInvoiceBillingReason.SUBSCRIPTION_CREATE:
            _queue_checkout_segment_event(
                event, CheckoutSegmentEvents.ORDER_COMPLETED, checkout_intent,
                subscription_details.to_dict().get('subscription'), invoice_id=invoice.id,
            )

    @on_stripe_event('invoice.payment_failed')
    @staticmethod
    def invoice_payment_failed(event: stripe.Event) -> None:
        """
        Handle invoice.payment_failed events.

        When an annual renewal charge fails, keep the current paid plan (year 1) active for the grace period
        while Stripe retries, and flag the checkout intent as ``in_renewal_grace_period``. Other failed invoices
        are left to the ``customer.subscription.updated`` handling.
        """
        invoice, subscription_details = get_invoice_and_subscription(event)
        uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription_details)
        try:
            checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
        except CheckoutIntentLookupError:
            logger.error(
                '[StripeEventHandler] invoice.payment_failed event %s could not find Checkout Intent uuid=%s id=%s',
                event.id, uuid_str, id_int,
            )
            return

        link_event_data_to_checkout_intent(event, checkout_intent)

        if not ssp_paid_renewal_enabled() or not _is_annual_renewal_invoice(invoice, subscription_details):
            return
        try:
            extended = keep_current_plan_active_for_renewal(checkout_intent, invoice)
        except AnnualRenewalError:
            # Already audit logged. A retry can't fix it, so don't fail the webhook.
            return
        if extended:
            _set_renewal_grace_period(checkout_intent, invoice['id'], True)

    @on_stripe_event('invoice.created')
    @staticmethod
    def invoice_created(event: stripe.Event) -> None:
        """
        Handle invoice.created events.

        Links the Stripe invoice to the corresponding SelfServiceSubscriptionRenewal
        by matching on stripe_subscription_id and effective_date. This linkage enables
        the invoice.paid handler to perform a direct lookup by stripe_invoice_id.
        """
        invoice, subscription_details = get_invoice_and_subscription(event)
        uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription_details)
        subscription_details = subscription_details.to_dict()
        stripe_subscription_id = subscription_details.get('subscription')
        try:
            checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
        except CheckoutIntentLookupError:
            logger.error(
                '[StripeEventHandler] invoice.created event %s could not find Checkout Intent uuid=%s id=%s',
                event.id, uuid_str, id_int,
            )
            return

        link_event_data_to_checkout_intent(event, checkout_intent)

        if ssp_paid_renewal_enabled() and _is_annual_renewal_invoice(invoice, invoice.parent.subscription_details):
            # Year 1 ends at this invoice's period start, before the invoice can be paid. Cover the gap.
            try:
                keep_current_plan_active_for_renewal(checkout_intent, invoice)
            except AnnualRenewalError:
                # Already audit logged. invoice.paid halts on the same problem, so don't fail this event too.
                pass
            return

        # Extract the invoice period start from line items to match against renewal effective_date
        try:
            invoice_period_start = invoice['lines']['data'][0]['period']['start']
        except (KeyError, IndexError, TypeError):
            logger.error(
                '[StripeEventHandler] invoice.created event %s missing period start in line items',
                event.id,
            )
            return

        invoice_period_start_dt = datetime_from_timestamp(invoice_period_start)

        # Find the renewal that matches this invoice's subscription and effective date
        renewal = SelfServiceSubscriptionRenewal.objects.filter(
            stripe_subscription_id=stripe_subscription_id,
            # Safe comparison because both sides use a UTC date conversion.
            # - `effective_date__date` uses UTC because of the django settings USE_TZ=True & TIME_ZONE="UTC".
            # - `invoice_period_start_dt.date()` uses UTC because datetime_from_timestamp returns UTC.
            effective_date__date=invoice_period_start_dt.date(),
        ).first()

        if not renewal:
            logger.warning(
                '[StripeEventHandler] invoice.created event %s: no SelfServiceSubscriptionRenewal found '
                'for subscription %s with effective_date matching %s',
                event.id, stripe_subscription_id, invoice_period_start_dt.date(),
            )
            return

        # Immutability guard: don't overwrite an existing stripe_invoice_id
        if renewal.stripe_invoice_id:
            logger.info(
                '[StripeEventHandler] invoice.created event %s: renewal %s already has '
                'stripe_invoice_id=%s, skipping',
                event.id, renewal.id, renewal.stripe_invoice_id,
            )
            if renewal.stripe_invoice_id != invoice['id']:
                logger.warning(
                    '[StripeEventHandler] invoice.created event %s: blocked attempt to write different '
                    'invoice ID %s to renewal %s with stripe_invoice_id=%s',
                    event.id, invoice['id'], renewal.id, renewal.stripe_invoice_id,
                )
            return

        renewal.stripe_invoice_id = invoice['id']
        renewal.save(update_fields=['stripe_invoice_id', 'modified'])
        logger.info(
            '[StripeEventHandler] invoice.created event %s: linked invoice %s to renewal %s',
            event.id, invoice['id'], renewal.id,
        )

    @on_stripe_event('customer.subscription.trial_will_end')
    @staticmethod
    def trial_will_end(event: stripe.Event) -> None:
        """
        Handle customer.subscription.trial_will_end events.
        Send reminder email 72 hours before trial ends.
        """
        subscription = event.data.object
        uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription)
        try:
            checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
        except CheckoutIntentLookupError:
            logger.error(
                "[StripeEventHandler] trial_will_end event %s could not find CheckoutIntent uuid=%s id=%s",
                event.id,
                uuid_str,
                id_int,
            )
            return

        link_event_data_to_checkout_intent(event, checkout_intent)

        logger.info(
            "Subscription %s trial ending in 72 hours. "
            "Queuing trial ending reminder email for checkout_intent uuid=%s",
            subscription.id,
            checkout_intent.uuid,
        )

        # Queue the trial ending reminder email task
        send_trial_ending_reminder_email_task.delay(
            checkout_intent_id=checkout_intent.id,
        )

    @on_stripe_event('payment_method.attached')
    @staticmethod
    def payment_method_attached(event: stripe.Event) -> None:
        pass

    @on_stripe_event('customer.subscription.created')
    @staticmethod
    def subscription_created(event: stripe.Event) -> None:
        """
        Handle customer.subscription.created events.
        Enable pending updates to prevent license count drift on failed payments.
        """
        subscription = event.data.object
        uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription)
        checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
        link_event_data_to_checkout_intent(event, checkout_intent)
        # Explicitly mark as not canceled on subscription creation rather than relying on the model default.
        # This ensures consistency since cancellations can be triggered by both updates and deletions.
        _update_renewal_cancellation_state(checkout_intent, is_canceled=False)

        subscription = subscription.to_dict()
        checkout_intent.stripe_customer_id = subscription.get('customer', None)
        checkout_intent.save()

        _try_enable_pending_updates(subscription['id'])

        summary = StripeEventSummary.objects.get(event_id=event.id)
        try:
            summary.update_upcoming_invoice_amount_due()
        except stripe.StripeError as exc:
            logger.warning('Error updating upcoming invoice amount due: %s', exc)

    @on_stripe_event('customer.subscription.updated')
    @staticmethod
    def subscription_updated(event: stripe.Event) -> None:
        """
        Handle customer.subscription.updated events.
        Track when subscriptions have pending updates and update related CheckoutIntent state.
        Send cancellation notification email when a subscription cancellation is scheduled.

        See https://docs.stripe.com/api/subscriptions/object#subscription_object-status for
        important information about allowed state transitions.
        """
        subscription = event.data.object
        uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription)
        checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
        link_event_data_to_checkout_intent(event, checkout_intent)

        subscription = subscription.to_dict()

        # Pending update
        pending_update = subscription.get("pending_update")
        if pending_update:
            handle_pending_update(subscription['id'], checkout_intent.id, pending_update)

        current_status = subscription.get("status")
        current_cancel_at = subscription.get('cancel_at')
        current_cancel_at_datetime = datetime_from_timestamp(current_cancel_at) if current_cancel_at else None

        if current_status in [StripeSubscriptionStatus.ACTIVE, StripeSubscriptionStatus.TRIALING]:
            # Proactively mark as not canceled whenever the subscription is active/trialing.
            # This guards against edge cases where cancellation state is not cleared on creation
            # and ensures correctness when a previously-canceled subscription is re-activated.
            # Also keeps subscription_cancel_at in sync: set it when cancel_at is present,
            # clear it when the subscription is active with no scheduled cancellation.
            # Note that this also handles the reinstatement of a stripe subscription (that is,
            # when a user clears the `cancel_at` time of a subscription record.
            # 1. User reinstates (to either active or trialing) ->
            #    Stripe sends customer.subscription.updated with cancel_at=null
            # 2. Handler extracts cancel_at -> gets None
            # 3. We call _update_renewal_cancellation_state(is_canceled=False, subscription_cancel_at=None)
            # 4. Renewal record's subscription_cancel_at is cleared to None
            _update_renewal_cancellation_state(
                checkout_intent,
                is_canceled=False,
                subscription_cancel_at=current_cancel_at_datetime,
            )

        previous_summary = checkout_intent.previous_summary(event, stripe_object_type='subscription')
        if not previous_summary:
            logger.warning(
                'No previous subscription summary for stripe subscription %s, event %s',
                subscription['id'], event.id,
            )
            return

        # Handle changes to the default payment method on the subscription
        # Changing the default payment method of a subscription can cause the
        # payment_behavior to reset to the default. We need to force it to be "pending_if_incomplete"
        # again, as we do on subscription creation (see above).
        prior_default_payment_method = previous_summary.stripe_event_data.object_data.get('default_payment_method')
        new_default_payment_method = subscription.get('default_payment_method')
        if new_default_payment_method != prior_default_payment_method:
            logger.warning(
                'The default_payment_method for subscription %s has changed from %s to %s',
                subscription['id'], prior_default_payment_method, new_default_payment_method,
            )
            _try_enable_pending_updates(subscription['id'])

        prior_status = previous_summary.subscription_status

        # Handle subscription cancellation scheduling (when user clicks cancel in Stripe)
        # This triggers before the subscription status actually changes
        prior_cancel_at = previous_summary.subscription_cancel_at

        # Detect when cancellation is newly scheduled (was None, now has value)
        if prior_cancel_at is None and current_cancel_at_datetime is not None:
            logger.info(
                f"Subscription {subscription['id']} was scheduled for cancellation at {current_cancel_at_datetime}. "
                f"Processing cancellation notification for checkout_intent uuid={checkout_intent.uuid}"
            )
            if current_status == StripeSubscriptionStatus.TRIALING:
                logger.info(f"Queuing trial cancellation email for checkout_intent uuid={checkout_intent.uuid}")
                send_trial_cancellation_email_task.delay(
                    checkout_intent_id=checkout_intent.id,
                    cancel_at_timestamp=current_cancel_at,
                )
            elif current_status == StripeSubscriptionStatus.ACTIVE:
                logger.info(f"Queuing paid cancellation email for checkout_intent.id={checkout_intent.id}")
                send_paid_cancellation_email_task.delay(
                    checkout_intent_id=checkout_intent.id,
                    cancel_at_timestamp=current_cancel_at,
                )

        # Detect when cancellation is reversed/reinstated (had value, now None)
        if prior_cancel_at is not None and current_cancel_at_datetime is None:
            logger.info(
                f"Subscription {subscription['id']} was reinstated (cancellation reversed). "
                f"Processing reinstatement notification for checkout_intent uuid={checkout_intent.uuid}"
            )
            # Other statuses (e.g. past_due, paused, unpaid) are intentionally not notified.
            if current_status == StripeSubscriptionStatus.TRIALING:
                send_trial_reinstatement_email_task.delay(checkout_intent_id=checkout_intent.id)
            elif current_status == StripeSubscriptionStatus.ACTIVE:
                send_paid_reinstatement_email_task.delay(checkout_intent_id=checkout_intent.id)

        # Everything belows handles a subscription state change. If the status
        # hasn't changed, we're all done.
        if prior_status == current_status:
            return
        else:
            _handle_subscription_updated_status_updates(event, prior_status, current_status, checkout_intent)

    @on_stripe_event("customer.subscription.deleted")
    @staticmethod
    def subscription_deleted(event: stripe.Event) -> None:
        """
        Handle customer.subscription.deleted events.
        """
        subscription = event.data.object
        uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription)
        checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
        link_event_data_to_checkout_intent(event, checkout_intent)

        subscription = subscription.to_dict()

        logger.info(
            "Subscription %s status was deleted via event %s", subscription['id'], event.id,
        )

        cancellation_details = subscription.get('cancellation_details')
        # Track cancellation event if cancellation details are present
        if cancellation_details:
            track_subscription_cancellation(checkout_intent, cancellation_details)

        enterprise_uuid = checkout_intent.enterprise_uuid
        if enterprise_uuid:
            cancel_all_future_plans(checkout_intent)
        else:
            logger.error(
                (
                    "Cannot deactivate future plans for subscription %s: "
                    "missing enterprise_uuid on CheckoutIntent %s"
                ),
                subscription['id'],
                checkout_intent.id,
            )
        _update_renewal_cancellation_state(checkout_intent, is_canceled=True, subscription_cancel_at=None)
        _set_renewal_grace_period(checkout_intent, None, False)

        previous_summary = checkout_intent.previous_summary(event, stripe_object_type='subscription')
        if previous_summary:
            if previous_summary.subscription_status == StripeSubscriptionStatus.ACTIVE:
                # https://docs.stripe.com/api/subscriptions/object#subscription_object-ended_at
                ended_at = subscription.get("ended_at") or timezone.now().timestamp()
                logger.info(
                    "Queuing cancelation finalization email for checkout_intent uuid=%s",
                    checkout_intent.uuid,
                )
                send_finalized_cancelation_email_task.delay(
                    checkout_intent_id=checkout_intent.id,
                    ended_at_timestamp=ended_at,
                )
                # Only when the customer cancelled (not e.g. a failed payment).
                if (cancellation_details or {}).get('reason') == 'cancellation_requested':
                    logger.info(
                        "Queuing paid subscription ended email for checkout_intent uuid=%s",
                        checkout_intent.uuid,
                    )
                    send_paid_subscription_ended_email_task.delay(
                        checkout_intent_id=checkout_intent.id,
                        ended_at_timestamp=ended_at,
                    )
            elif previous_summary.subscription_status == StripeSubscriptionStatus.TRIALING:
                logger.info(
                    "Queuing trial ended cancellation email for checkout_intent uuid=%s",
                    checkout_intent.uuid,
                )
                send_trial_ended_cancellation_email_task.delay(
                    checkout_intent_id=checkout_intent.id,
                )

        # Last, after the license-manager call and renewal updates that can raise, so a failed delivery queues
        # nothing. Only the subscription's id and final items are passed on from the payload.
        _queue_checkout_segment_event(
            event,
            CheckoutSegmentEvents.ORDER_CANCELLED,
            checkout_intent,
            subscription['id'],
            subscription_data={'id': subscription['id'], 'items': subscription.get('items')},
        )


def _process_trial_to_paid_renewal(
    renewal: SelfServiceSubscriptionRenewal,
    stripe_subscription_id: str,
    event: stripe.Event,
):
    """
    Process the trial-to-paid renewal for a subscription.

    This function:
    1. Updates the renewal with the Stripe event data and subscription ID
    2. Calls license manager to process the renewal
    3. Marks the renewal as processed

    Args:
        renewal: The SelfServiceSubscriptionRenewal to process (already looked up
            by stripe_invoice_id in the caller).
        stripe_subscription_id: The Stripe subscription ID
        event: The Stripe event that triggered the renewal
    """
    try:
        # Get the StripeEventData record for this event
        event_data = StripeEventData.objects.get(event_id=event.id)

        # Update the renewal record with event data and subscription ID
        renewal.stripe_event_data = event_data
        renewal.stripe_subscription_id = stripe_subscription_id
        renewal.save(update_fields=['stripe_event_data', 'stripe_subscription_id', 'modified'])

        logger.info(
            f"Updated SelfServiceSubscriptionRenewal {renewal.id} with event data {event_data.event_id} "
            f"and subscription {stripe_subscription_id}"
        )

        # Process the renewal via license manager
        license_manager_client = LicenseManagerApiClient()
        result = license_manager_client.process_subscription_plan_renewal(renewal.subscription_plan_renewal_id)

        logger.info(
            f"Successfully processed subscription plan renewal {renewal.subscription_plan_renewal_id} "
            f"via license manager. Result: {result}"
        )

        # Mark the renewal as processed
        renewal.mark_as_processed()

        logger.info(
            f"Marked SelfServiceSubscriptionRenewal {renewal.id} as processed for "
            f"subscription {stripe_subscription_id}"
        )

    except Exception as exc:
        logger.exception(
            f"Failed to process trial-to-paid renewal {renewal.id} for "
            f"subscription {stripe_subscription_id}: {exc}"
        )
        raise
