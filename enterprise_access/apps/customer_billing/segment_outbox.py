"""
Transactional outbox for the server-side checkout Segment events.

Webhook handlers record each event as a ``SegmentEventEmission`` row (PENDING) and only then hand its send task to
Celery. A broker outage therefore delays an event instead of losing it: the row stays PENDING, and the
``dispatch_checkout_segment_events`` management command publishes it later. The command also re-publishes rows that
were handed to the broker but never finished, a bounded number of times.
"""
import logging
from datetime import timedelta
from functools import partial

from django.db import DatabaseError, transaction
from django.db.models import F, Value
from django.db.models.functions import Coalesce, Concat
from django.utils import timezone

from enterprise_access.apps.customer_billing.constants import CheckoutSegmentEvents, SegmentEventEmissionStatus
from enterprise_access.apps.customer_billing.models import SegmentEventEmission
from enterprise_access.apps.customer_billing.tasks import send_checkout_segment_event_task
from enterprise_access.toggles import checkout_segment_events_v2_enabled

logger = logging.getLogger(__name__)

# Longer than the send task's own Celery retries take (5 retries, at most ~31 minutes), so a row is only re-published
# once Celery has given up on it.
DEFAULT_STALE_AFTER = timedelta(hours=2)
DEFAULT_MAX_DISPATCH_ATTEMPTS = 5
DEFAULT_BATCH_SIZE = 500


def record_order_completed_segment_event(checkout_intent, invoice_id, subscription_id):
    """Record ``order_completed`` for the order's first paid invoice, then dispatch it."""
    return _record(CheckoutSegmentEvents.ORDER_COMPLETED, checkout_intent, subscription_id, invoice_id=invoice_id)


def record_order_cancelled_segment_event(checkout_intent, subscription_id):
    """
    Record ``order_cancelled`` for a deleted subscription, then dispatch it.

    Whether the subscription ever paid, and which invoice was the order's, is decided later by the send task from
    Stripe's payment history, so this does not depend on the invoice webhooks arriving before the deletion.
    """
    return _record(CheckoutSegmentEvents.ORDER_CANCELLED, checkout_intent, subscription_id)


def _record(event_name, checkout_intent, subscription_id, invoice_id=None):
    """
    Save the event's outbox row, once per (event, order), and dispatch it after the current transaction commits.

    The ``order_id`` is the purchase's Stripe Checkout Session ID, which cannot change once the CheckoutIntent is
    paid, so a redelivered webhook finds the same row.

    A database error is raised, never swallowed: the webhook must fail, so that Stripe redelivers it, rather than be
    acknowledged without a row that anything could send the event from. Handlers call this after their billing work,
    so the outbox never holds that work up, and the redelivery repeats it (handlers are idempotent) and then records
    the event.
    """
    if not checkout_segment_events_v2_enabled():
        return None
    order_id = checkout_intent.stripe_checkout_session_id
    if not order_id:
        logger.error(
            'Not recording Segment event %s for CheckoutIntent %s (subscription %s): it has no Stripe Checkout '
            'Session ID to use as the order_id',
            event_name, checkout_intent.id, subscription_id,
        )
        return None
    try:
        emission, created = SegmentEventEmission.objects.get_or_create(
            event_name=event_name,
            order_id=order_id,
            defaults={
                'checkout_intent': checkout_intent,
                'stripe_subscription_id': subscription_id,
                'stripe_invoice_id': invoice_id,
            },
        )
    except DatabaseError:
        # Logged here for the context, then raised so the webhook fails and Stripe redelivers it.
        logger.exception(
            'Could not record Segment event %s for CheckoutIntent %s (subscription %s, invoice %s); failing the '
            'webhook so Stripe redelivers it',
            event_name, checkout_intent.id, subscription_id, invoice_id,
        )
        raise
    logger.info(
        '%s Segment event %s for order %s (status %s)',
        'Recorded' if created else 'Already recorded', event_name, order_id, emission.status,
    )
    # After commit, so the worker can never pick up the task before the row is visible to it.
    transaction.on_commit(partial(_dispatch_after_commit, emission.pk))
    return emission


def _dispatch_after_commit(emission_id):
    """Dispatch a just-recorded row; never raises, because the row is saved and the command dispatches it later."""
    try:
        dispatch_segment_event(emission_id)
    except DatabaseError:
        logger.exception('Could not dispatch SegmentEventEmission %s; it stays pending', emission_id)


def dispatch_segment_event(emission_id) -> bool:
    """
    Hand a PENDING row's send task to the broker. Returns whether it was published; never raises for broker errors.

    The row is locked with ``skip_locked``, so a concurrent dispatcher skips it instead of publishing it twice. It
    is marked DISPATCHED before publishing, in the same transaction: a worker that picks the task up immediately
    waits for the lock and then sees it DISPATCHED, and a task run eagerly (inside ``delay()``) can finish the row
    without this overwriting it. If publishing fails, the transaction is rolled back, so the row stays PENDING and
    the failed attempt is not counted.
    """
    publish_error = None
    with transaction.atomic():
        emission = SegmentEventEmission.objects.select_for_update(skip_locked=True).filter(
            pk=emission_id, status=SegmentEventEmissionStatus.PENDING,
        ).first()
        if emission is None:
            # Already dispatched or finished, or another dispatcher holds it.
            return False
        now = timezone.now()
        SegmentEventEmission.objects.filter(pk=emission.pk).update(
            status=SegmentEventEmissionStatus.DISPATCHED,
            dispatch_attempts=F('dispatch_attempts') + 1,
            last_dispatched_at=now,
            modified=now,
        )
        try:
            send_checkout_segment_event_task.delay(emission.pk)
        except Exception as exc:  # pylint: disable=broad-except
            # Broker errors depend on the transport (kombu, redis, amqp, socket), so they can't be listed. Nothing is
            # lost: the rollback leaves the row PENDING for the next dispatch.
            publish_error = exc
            transaction.set_rollback(True)

    if publish_error is not None:
        SegmentEventEmission.objects.filter(
            pk=emission_id, status=SegmentEventEmissionStatus.PENDING,
        ).update(last_error=f'Could not publish the send task: {publish_error!r}'[:1000], modified=timezone.now())
        logger.warning(
            'Could not publish the Segment send task for SegmentEventEmission %s; it stays pending: %r',
            emission_id, publish_error,
        )
        return False
    return True


def dispatch_pending_segment_events(
    stale_after=DEFAULT_STALE_AFTER,
    max_dispatch_attempts=DEFAULT_MAX_DISPATCH_ATTEMPTS,
    batch_size=DEFAULT_BATCH_SIZE,
) -> dict:
    """
    Publish the send task of every PENDING row (up to ``batch_size``), and return counts of what was done.

    First, rows DISPATCHED more than ``stale_after`` ago that never finished (Celery gave up retrying, or the broker
    lost the message) go back to PENDING, or to FAILED once they were dispatched ``max_dispatch_attempts`` times.
    A failed publish is not counted as an attempt: it says nothing about the event, and the row is tried again on the
    next run. Safe to run concurrently: every status change is a conditional update, and each row is published
    under a row lock. Does nothing while the feature setting is off, so recorded events wait instead of failing.
    """
    counts = {'failed': 0, 'requeued': 0, 'dispatched': 0, 'not_dispatched': 0}
    if not checkout_segment_events_v2_enabled():
        logger.info('Checkout Segment events are disabled; not dispatching')
        return counts

    now = timezone.now()
    stale = SegmentEventEmission.objects.filter(
        status=SegmentEventEmissionStatus.DISPATCHED,
        last_dispatched_at__lt=now - stale_after,
    )
    counts['failed'] = stale.filter(dispatch_attempts__gte=max_dispatch_attempts).update(
        status=SegmentEventEmissionStatus.FAILED,
        last_error=Concat(
            Value(f'Not sent after {max_dispatch_attempts} dispatches. Last error: '),
            Coalesce('last_error', Value('none recorded')),
        ),
        modified=now,
    )
    counts['requeued'] = stale.filter(dispatch_attempts__lt=max_dispatch_attempts).update(
        status=SegmentEventEmissionStatus.PENDING,
        modified=now,
    )

    pending_ids = list(
        SegmentEventEmission.objects.filter(
            status=SegmentEventEmissionStatus.PENDING,
        ).order_by('pk').values_list('pk', flat=True)[:batch_size]
    )
    for emission_id in pending_ids:
        try:
            published = dispatch_segment_event(emission_id)
        except DatabaseError:
            logger.exception('Could not dispatch SegmentEventEmission %s; it stays pending', emission_id)
            published = False
        counts['dispatched' if published else 'not_dispatched'] += 1

    if counts['failed']:
        logger.error('%s checkout Segment event(s) were never sent and are now failed', counts['failed'])
    logger.info('Dispatched checkout Segment events: %s', counts)
    return counts
