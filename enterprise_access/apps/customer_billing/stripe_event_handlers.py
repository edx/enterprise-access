"""
Stripe event handlers
"""
import logging
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from uuid import UUID

import stripe
from django.conf import settings
from django.utils import timezone
from simple_history.utils import bulk_update_with_history

from enterprise_access.apps.api_client.license_manager_client import LicenseManagerApiClient
from enterprise_access.apps.customer_billing.constants import (
    SUBSCRIPTION_ITEM_TYPE,
    CheckoutIntentState,
    StripeSegmentEvents,
    StripeSubscriptionStatus
)
from enterprise_access.apps.customer_billing.models import (
    CheckoutIntent,
    SelfServiceSubscriptionRenewal,
    StripeEventData,
    StripeEventSummary
)
from enterprise_access.apps.customer_billing.stripe_event_types import StripeEventType
from enterprise_access.apps.customer_billing.tasks import (
    send_billing_error_email_task,
    send_finalized_cancelation_email_task,
    send_paid_cancellation_email_task,
    send_payment_receipt_email,
    send_reinstatement_email_task,
    send_trial_cancellation_email_task,
    send_trial_end_and_subscription_started_email_task,
    send_trial_ending_reminder_email_task
)
from enterprise_access.apps.customer_billing.utils import datetime_from_timestamp
from enterprise_access.apps.provisioning.models import ProvisionNewCustomerWorkflow
from enterprise_access.apps.track.segment import track_event

logger = logging.getLogger(__name__)


class CheckoutIntentLookupError(Exception):
    """Raised when CheckoutIntent cannot be found by UUID or ID."""


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
    metadata = stripe_subscription.metadata
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
    if event.type in ('invoice.paid', 'invoice.created'):
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
    stripe_customer_id = event.data.object.get('customer')

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
            'data': dict(event),
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
            return CheckoutIntent.objects.get(uuid=uuid_value)
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
            return CheckoutIntent.objects.get(id=id_int)
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


def cancel_all_future_plans(checkout_intent):
    """
    Deactivate (cancel) all future renewal plans descending from the
    anchor plan for this enterprise, regardless of whether
    the renewal has already been processed.
    """
    unprocessed_renewals = checkout_intent.renewals.all()
    if not unprocessed_renewals.exists():
        logger.warning('No renewals to cancel for Checkout Intent %s', checkout_intent.uuid)
        return []

    client = LicenseManagerApiClient()
    deactivated: list[UUID] = []

    for renewal in unprocessed_renewals:
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


def _extract_invoice_trial_window(invoice) -> tuple:
    """
    Attempt to infer trial period boundaries from invoice line item periods.
    """
    try:
        line_items = invoice.get('lines', {}).get('data', [])
        if not line_items:
            return None, None
        period = line_items[0].get('period', {})
        start_ts = period.get('start')
        end_ts = period.get('end')
        start_dt = datetime_from_timestamp(start_ts) if start_ts else None
        end_dt = datetime_from_timestamp(end_ts) if end_ts else None
        return start_dt, end_dt
    except Exception:  # pylint: disable=broad-except
        return None, None


def _maybe_auto_provision_paid_checkout_intent(
    checkout_intent: CheckoutIntent,
    trial_start=None,
    trial_end=None,
) -> None:
    """
    Create and execute provisioning workflow for a paid checkout intent when missing.
    """
    if checkout_intent.workflow:
        logger.info(
            'Skipping auto-provisioning for CheckoutIntent %s because workflow %s already exists',
            checkout_intent.id,
            checkout_intent.workflow_id,
        )
        return

    if not checkout_intent.user or not checkout_intent.user.email:
        logger.warning(
            'Skipping auto-provisioning for CheckoutIntent %s because admin email is unavailable',
            checkout_intent.id,
        )
        return

    if not trial_start or not trial_end:
        trial_start = timezone.now()
        trial_end = timezone.now() + timedelta(days=getattr(settings, 'SSP_TRIAL_PERIOD_DAYS', 14))

    try:
        workflow = ProvisionNewCustomerWorkflow.create_and_execute_for_checkout_intent(
            checkout_intent=checkout_intent,
            trial_start=trial_start,
            trial_end=trial_end,
        )
        logger.info(
            'Auto-provisioning workflow %s executed for CheckoutIntent %s',
            workflow.uuid,
            checkout_intent.id,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception(
            'Auto-provisioning failed for CheckoutIntent %s: %s',
            checkout_intent.id,
            exc,
        )


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
            cancel_all_future_plans(checkout_intent)
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
                if event.type in ('invoice.paid', 'invoice.created') and not _valid_invoice_event_type(event):
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
            # Attempt to send the receipt FIRST before triggering renewal.
            # Renewal might want to force a retry by raising, risking duplicate receipt emails. We'll
            # mitigate this by configuring the Braze campaign to avoid sending more than 1 in a 3
            # day period.
            send_payment_receipt_email.delay(
                invoice_id=invoice.id,
                invoice_data=invoice,
                enterprise_customer_name=checkout_intent.enterprise_name,
                enterprise_slug=checkout_intent.enterprise_slug,
            )
            # only update status for non-trial invoice.paid events
            _handle_invoice_paid_status_updated(event, checkout_intent)
            return

        trial_start, trial_end = _extract_invoice_trial_window(invoice)

        transitioned_to_paid = False
        try:
            checkout_intent.mark_as_paid(stripe_customer_id=stripe_customer_id)
            transitioned_to_paid = True
            logger.info(
                'Marked checkout_intent uuid=%s as paid via invoice=%s',
                checkout_intent.uuid, invoice.id,
            )
        except ValueError as exc:
            logger.warning(
                'Could not mark checkout intent %s as paid via invoice %s, because %s',
                checkout_intent.uuid, invoice.id, exc,
            )

        if transitioned_to_paid and checkout_intent.state == CheckoutIntentState.PAID:
            _maybe_auto_provision_paid_checkout_intent(
                checkout_intent=checkout_intent,
                trial_start=trial_start,
                trial_end=trial_end,
            )

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
        stripe_subscription_id = subscription_details.get('subscription')

        uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription_details)
        try:
            checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
        except CheckoutIntentLookupError:
            logger.error(
                '[StripeEventHandler] invoice.created event %s could not find Checkout Intent uuid=%s id=%s',
                event.id, uuid_str, id_int,
            )
            return

        link_event_data_to_checkout_intent(event, checkout_intent)

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

        checkout_intent.stripe_customer_id = subscription.get('customer', None)
        checkout_intent.save()

        _try_enable_pending_updates(subscription.id)

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

        # Pending update
        pending_update = getattr(subscription, "pending_update", None)
        if pending_update:
            handle_pending_update(subscription.id, checkout_intent.id, pending_update)

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
                subscription.id, event.id,
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
                subscription.id, prior_default_payment_method, new_default_payment_method,
            )
            _try_enable_pending_updates(subscription.id)

        prior_status = previous_summary.subscription_status

        # Handle subscription cancellation scheduling (when user clicks cancel in Stripe)
        # This triggers before the subscription status actually changes
        prior_cancel_at = previous_summary.subscription_cancel_at

        # Detect when cancellation is newly scheduled (was None, now has value)
        if prior_cancel_at is None and current_cancel_at_datetime is not None:
            logger.info(
                f"Subscription {subscription.id} was scheduled for cancellation at {current_cancel_at_datetime}. "
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
                f"Subscription {subscription.id} was reinstated (cancellation reversed). "
                f"Processing reinstatement notification for checkout_intent uuid={checkout_intent.uuid}"
            )
            send_reinstatement_email_task.delay(checkout_intent_id=checkout_intent.id)

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

        logger.info(
            "Subscription %s status was deleted via event %s", subscription.id, event.id,
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
                subscription.id,
                checkout_intent.id,
            )
        _update_renewal_cancellation_state(checkout_intent, is_canceled=True, subscription_cancel_at=None)

        previous_summary = checkout_intent.previous_summary(event, stripe_object_type='subscription')
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
