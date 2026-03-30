"""
Data migration to backfill SelfServiceSubscriptionRenewal.is_canceled and
subscription_cancel_at from historical Stripe events.
"""
from django.db import migrations
from django.db.models import Q


def backfill_subscription_renewal_cancellations(apps, schema_editor):
    """
    Backfill is_canceled and subscription_cancel_at on SelfServiceSubscriptionRenewal
    from Stripe deletion/restore events.
    """
    StripeEventSummary = apps.get_model('customer_billing', 'StripeEventSummary')

    def get_latest_restore_event(deleted_event):
        """
        Return the latest restore event after the deleted event for the same checkout intent,
        or None if no such event exists.
        """
        restore_filter = Q(event_type='customer.subscription.created') | Q(
            event_type='customer.subscription.updated',
            subscription_status__in=['active', 'trialing'],
        )
        deleted_ts = deleted_event.stripe_event_created_at
        qs = StripeEventSummary.objects.filter(
            checkout_intent=deleted_event.checkout_intent,
        ).filter(restore_filter).filter(stripe_event_created_at__gt=deleted_ts)
        return qs.order_by('-stripe_event_created_at').first()

    deleted_events = StripeEventSummary.objects.filter(
        event_type='customer.subscription.deleted',
        checkout_intent__isnull=False,
    ).select_related('checkout_intent').order_by('stripe_event_created_at')

    for deleted_event in deleted_events.iterator(chunk_size=100):
        latest_restore_event = get_latest_restore_event(deleted_event)
        target_is_canceled = latest_restore_event is None
        target_subscription_cancel_at = (
            latest_restore_event.subscription_cancel_at if latest_restore_event else None
        )

        latest_renewal = deleted_event.checkout_intent.renewals.order_by('-created').first()

        if latest_renewal and (
            latest_renewal.is_canceled != target_is_canceled
            or latest_renewal.subscription_cancel_at != target_subscription_cancel_at
        ):
            latest_renewal.is_canceled = target_is_canceled
            latest_renewal.subscription_cancel_at = target_subscription_cancel_at
            latest_renewal.save(update_fields=['is_canceled', 'subscription_cancel_at', 'modified'])


class Migration(migrations.Migration):
    dependencies = [
        ('customer_billing', '0030_selfservicesubscriptionrenewal_subscription_cancel_at'),
    ]

    operations = [
        migrations.RunPython(
            backfill_subscription_renewal_cancellations,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
