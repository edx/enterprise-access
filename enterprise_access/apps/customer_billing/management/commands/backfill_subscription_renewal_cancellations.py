"""
Backfill SelfServiceSubscriptionRenewal.is_canceled from historical Stripe events.
"""
from django.core.management.base import BaseCommand

from enterprise_access.apps.customer_billing.constants import StripeSubscriptionStatus
from enterprise_access.apps.customer_billing.models import StripeEventSummary


class Command(BaseCommand):
    """Backfill cancellation status on self-service subscription renewals."""

    help = 'Backfill SelfServiceSubscriptionRenewal.is_canceled from Stripe deletion/restore events'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            help='Number of deletion events to process in each batch (default: 100).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without writing to the database.',
        )

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - no records will be updated'))

        deleted_events = StripeEventSummary.objects.filter(
            event_type='customer.subscription.deleted',
            checkout_intent__isnull=False,
        ).select_related('checkout_intent').order_by('stripe_event_created_at', 'created')

        total = deleted_events.count()
        self.stdout.write(f'Found {total} customer.subscription.deleted events with checkout intents')

        updated_count = 0
        unchanged_count = 0
        processed_count = 0

        for deleted_event in deleted_events.iterator(chunk_size=batch_size):
            has_restore_event = self._has_later_restore_event(deleted_event)
            target_is_canceled = not has_restore_event
            renewals_qs = deleted_event.checkout_intent.renewals.all()

            if not renewals_qs.exists():
                unchanged_count += 1
            else:
                needs_update_qs = renewals_qs.exclude(is_canceled=target_is_canceled)
                needs_update_count = needs_update_qs.count()

                if needs_update_count == 0:
                    unchanged_count += 1
                elif dry_run:
                    self.stdout.write(
                        f'Would update {needs_update_count} renewal(s) for checkout_intent '
                        f'{deleted_event.checkout_intent.id} to is_canceled={target_is_canceled}'
                    )
                    updated_count += needs_update_count
                else:
                    updated_count += needs_update_qs.update(is_canceled=target_is_canceled)

            processed_count += 1
            if processed_count % batch_size == 0:
                self.stdout.write(f'Processed {processed_count}/{total} deletion events...')

        self.stdout.write(f'Processed {processed_count}/{total} deletion events...')

        self.stdout.write(
            self.style.SUCCESS(
                f'Backfill complete. Updated renewals: {updated_count}. Unchanged events: {unchanged_count}.'
            )
        )

    def _has_later_restore_event(self, deleted_event: StripeEventSummary) -> bool:
        """Return True if a later restore event exists for the same checkout intent."""
        restore_events = StripeEventSummary.objects.filter(
            checkout_intent=deleted_event.checkout_intent,
            event_type__in=['customer.subscription.created', 'customer.subscription.updated'],
        )

        if deleted_event.stripe_event_created_at:
            restore_events = restore_events.filter(
                stripe_event_created_at__gt=deleted_event.stripe_event_created_at,
            )
        else:
            restore_events = restore_events.filter(created__gt=deleted_event.created)

        return restore_events.filter(
            event_type='customer.subscription.created',
        ).exists() or restore_events.filter(
            event_type='customer.subscription.updated',
            subscription_status__in=[StripeSubscriptionStatus.ACTIVE, StripeSubscriptionStatus.TRIALING],
        ).exists()
