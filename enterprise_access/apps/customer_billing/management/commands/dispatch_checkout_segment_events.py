"""
Management command that publishes recorded checkout Segment events whose send task is not with Celery.
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand

from enterprise_access.apps.customer_billing.segment_outbox import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_DISPATCH_ATTEMPTS,
    DEFAULT_STALE_AFTER,
    dispatch_pending_segment_events
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Dispatcher for the checkout Segment event outbox (``SegmentEventEmission``).

    Webhook handlers record each event before handing its send task to Celery. This command publishes the events
    whose task never reached the broker (PENDING, e.g. during a broker outage), and the events handed to the broker
    that never finished (Celery gave up, or the broker lost the message), up to a bounded number of attempts. Meant
    to run on a schedule, e.g. every 10 minutes. It is safe to run concurrently with itself and with the webhooks.

    Usage:
        ./manage.py dispatch_checkout_segment_events
        ./manage.py dispatch_checkout_segment_events --stale-after-minutes=120 --max-dispatch-attempts=5
    """

    help = 'Publish recorded checkout Segment events that are not queued in Celery.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--stale-after-minutes',
            type=int,
            default=int(DEFAULT_STALE_AFTER.total_seconds() // 60),
            help=(
                'Publish a dispatched event again if it has not finished this many minutes after it was dispatched. '
                'Keep it longer than the send task\'s own Celery retries take.'
            ),
        )
        parser.add_argument(
            '--max-dispatch-attempts',
            type=int,
            default=DEFAULT_MAX_DISPATCH_ATTEMPTS,
            help='Mark an event failed once it was dispatched this many times without finishing.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help='Publish at most this many pending events per run.',
        )

    def handle(self, *args, **options):
        counts = dispatch_pending_segment_events(
            stale_after=timedelta(minutes=options['stale_after_minutes']),
            max_dispatch_attempts=options['max_dispatch_attempts'],
            batch_size=options['batch_size'],
        )
        summary = ', '.join(f'{key}={value}' for key, value in counts.items())
        self.stdout.write(self.style.SUCCESS(f'dispatch_checkout_segment_events completed: {summary}'))
