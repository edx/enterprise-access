"""
Queue the paid-to-paid renewal reminder emails, sent 40 days and 72 hours before a paid subscription renews.

Run ONCE per day. Each reminder's day-long window is measured from the start of the hour the run began in, so
daily runs cover back-to-back windows. See "Renewal Reminders" in
docs/customer-billing/subscription-and-renewal-lifecycle.rst.
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from enterprise_access.apps.customer_billing.constants import RENEWAL_REMINDER_LEAD_TIMES, StripeSubscriptionStatus
from enterprise_access.apps.customer_billing.models import StripeEventSummary
from enterprise_access.apps.customer_billing.tasks import send_renewal_reminder_email_task

logger = logging.getLogger(__name__)


def get_renewing_subscription_summaries(window_start, window_end):
    """
    Return the latest subscription event summary of each paid Stripe subscription that is due to renew
    in ``[window_start, window_end)``: active, with no cancellation scheduled.
    """
    paid_subscription_ids = StripeEventSummary.objects.filter(
        event_type='invoice.paid',
        invoice_amount_paid__gt=0,
    ).values('stripe_subscription_id')
    candidate_ids = set(StripeEventSummary.objects.filter(
        stripe_object_type='subscription',
        stripe_subscription_id__in=paid_subscription_ids,
        subscription_period_end__gte=window_start,
        subscription_period_end__lt=window_end,
    ).values_list('stripe_subscription_id', flat=True))

    summaries = []
    for subscription_id in sorted(candidate_ids):
        # A later event may have moved the period end, scheduled a cancellation or ended the subscription. Stripe's
        # event times are to the second, so the later row breaks a tie.
        latest = StripeEventSummary.objects.filter(
            stripe_object_type='subscription',
            stripe_subscription_id=subscription_id,
        ).order_by('-stripe_event_created_at', '-pk').first()
        if (latest.checkout_intent_id and latest.subscription_status == StripeSubscriptionStatus.ACTIVE and
                latest.subscription_cancel_at is None and latest.subscription_period_end is not None and
                window_start <= latest.subscription_period_end < window_end):
            summaries.append(latest)
    return summaries


class Command(BaseCommand):
    """Queue the renewal reminder emails (see the module docstring)."""
    help = 'Queue Braze renewal reminder emails for paid subscriptions renewing in 40 days or 72 hours.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true', help='Log the reminders that would be sent, without queuing them.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        # The scheduled hour, not the moment this run started: a later or earlier start leaves a gap or an overlap.
        now = timezone.now().replace(minute=0, second=0, microsecond=0)

        for email_type, lead_time in RENEWAL_REMINDER_LEAD_TIMES.items():
            window_start = now + lead_time
            window_end = window_start + timedelta(days=1)
            summaries = get_renewing_subscription_summaries(window_start, window_end)
            logger.info(
                '[RENEWAL_REMINDERS] %s: %d subscriptions renew between %s and %s. dry_run=%s',
                email_type, len(summaries), window_start, window_end, dry_run,
            )

            for summary in summaries:
                logger.info(
                    '[RENEWAL_REMINDERS] %s: Stripe subscription %s, checkout intent %s, renews %s. dry_run=%s',
                    email_type, summary.stripe_subscription_id, summary.checkout_intent_id,
                    summary.subscription_period_end, dry_run,
                )
                if not dry_run:
                    send_renewal_reminder_email_task.delay(
                        checkout_intent_id=summary.checkout_intent_id,
                        stripe_subscription_id=summary.stripe_subscription_id,
                        email_type=email_type,
                    )
