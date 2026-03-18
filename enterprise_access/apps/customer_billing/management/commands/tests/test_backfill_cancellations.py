"""Tests for backfill_subscription_renewal_cancellations command."""
from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from enterprise_access.apps.customer_billing.models import SelfServiceSubscriptionRenewal
from enterprise_access.apps.customer_billing.tests.factories import (
    CheckoutIntentFactory,
    SelfServiceSubscriptionRenewalFactory,
    StripeEventDataFactory
)


class TestBackfillSubscriptionRenewalCancellations(TestCase):
    """Command tests for backfilling renewal cancellation state from Stripe events."""

    def _create_summary(self, checkout_intent, event_type, created_at, subscription_status=None):
        event_data = StripeEventDataFactory(
            checkout_intent=checkout_intent,
            event_type=event_type,
            data={
                'id': f'evt_{event_type}_{uuid4()}',
                'type': event_type,
                'created': int(created_at.timestamp()),
                'data': {
                    'object': {
                        'object': 'subscription',
                        'id': f'sub_{uuid4()}',
                        'status': subscription_status or 'trialing',
                    }
                }
            },
        )
        summary = event_data.summary
        summary.event_type = event_type
        summary.stripe_event_created_at = created_at
        if subscription_status:
            summary.subscription_status = subscription_status
        summary.save(update_fields=['event_type', 'stripe_event_created_at', 'subscription_status'])
        return summary

    def _create_renewal(self, checkout_intent, is_canceled=False):
        created_at = timezone.now() - timedelta(days=30)
        event_data = StripeEventDataFactory(
            checkout_intent=checkout_intent,
            event_type='customer.subscription.created',
            data={
                'id': f'evt_created_{uuid4()}',
                'type': 'customer.subscription.created',
                'created': int(created_at.timestamp()),
                'data': {
                    'object': {
                        'object': 'subscription',
                        'id': 'sub_backfill_123',
                        'status': 'active',
                    }
                },
            },
        )
        return SelfServiceSubscriptionRenewal.objects.create(
            checkout_intent=checkout_intent,
            subscription_plan_renewal_id=123,
            stripe_event_data=event_data,
            stripe_subscription_id='sub_backfill_123',
            renewed_subscription_plan_uuid=uuid4(),
            is_canceled=is_canceled,
        )

    def test_dry_run_does_not_update_records(self):
        checkout_intent = CheckoutIntentFactory()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)

        deleted_time = timezone.now() - timedelta(days=1)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', deleted_time)

        call_command('backfill_subscription_renewal_cancellations', '--dry-run', stdout=StringIO())

        renewal.refresh_from_db()
        self.assertFalse(renewal.is_canceled)

    def test_idempotent_updates(self):
        checkout_intent = CheckoutIntentFactory()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)

        deleted_time = timezone.now() - timedelta(days=1)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', deleted_time)

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())
        renewal.refresh_from_db()
        self.assertTrue(renewal.is_canceled)

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())
        renewal.refresh_from_db()
        self.assertTrue(renewal.is_canceled)

    def test_handles_null_checkout_intent_event(self):
        # Null checkout intent should be ignored.
        StripeEventDataFactory(
            checkout_intent=None,
            event_type='customer.subscription.deleted',
            data={
                'id': f'evt_deleted_{uuid4()}',
                'type': 'customer.subscription.deleted',
                'created': int(timezone.now().timestamp()),
                'data': {'object': {'object': 'subscription', 'id': f'sub_{uuid4()}', 'status': 'canceled'}},
            },
        )

        checkout_intent = CheckoutIntentFactory()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', timezone.now() - timedelta(hours=2))

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())

        renewal.refresh_from_db()
        self.assertTrue(renewal.is_canceled)

    def test_later_restore_event_sets_uncanceled(self):
        checkout_intent = CheckoutIntentFactory()
        renewal = self._create_renewal(checkout_intent, is_canceled=True)

        deleted_time = timezone.now() - timedelta(days=2)
        restored_time = timezone.now() - timedelta(days=1)
        self._create_summary(
            checkout_intent, 'customer.subscription.deleted', deleted_time, subscription_status='canceled'
        )
        self._create_summary(
            checkout_intent, 'customer.subscription.updated', restored_time, subscription_status='active'
        )

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())

        renewal.refresh_from_db()
        self.assertFalse(renewal.is_canceled)

    def test_multiple_renewals_all_updated(self):
        """All renewals tied to a checkout intent are updated in a single pass."""
        checkout_intent = CheckoutIntentFactory()
        renewal_a = self._create_renewal(checkout_intent, is_canceled=False)
        renewal_b = self._create_renewal(checkout_intent, is_canceled=False)

        self._create_summary(checkout_intent, 'customer.subscription.deleted', timezone.now() - timedelta(hours=1))

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())

        renewal_a.refresh_from_db()
        renewal_b.refresh_from_db()
        self.assertTrue(renewal_a.is_canceled)
        self.assertTrue(renewal_b.is_canceled)

    def test_already_correct_state_is_unchanged(self):
        """Renewals already in the target state are counted as unchanged, not written."""
        checkout_intent = CheckoutIntentFactory()
        renewal = self._create_renewal(checkout_intent, is_canceled=True)

        self._create_summary(checkout_intent, 'customer.subscription.deleted', timezone.now() - timedelta(hours=1))

        out = StringIO()
        call_command('backfill_subscription_renewal_cancellations', stdout=out)

        renewal.refresh_from_db()
        self.assertTrue(renewal.is_canceled)
        self.assertIn('Unchanged events: 1', out.getvalue())
        self.assertIn('Updated renewals: 0', out.getvalue())

    def test_no_renewals_for_checkout_intent_is_unchanged(self):
        """A deletion event whose checkout intent has no renewals increments unchanged count."""
        checkout_intent = CheckoutIntentFactory()
        # No renewal created intentionally.
        self._create_summary(checkout_intent, 'customer.subscription.deleted', timezone.now() - timedelta(hours=1))

        out = StringIO()
        call_command('backfill_subscription_renewal_cancellations', stdout=out)

        self.assertIn('Unchanged events: 1', out.getvalue())
        self.assertIn('Updated renewals: 0', out.getvalue())

    def test_restore_via_created_event_sets_uncanceled(self):
        """A customer.subscription.created event after deletion marks the renewal as not canceled."""
        checkout_intent = CheckoutIntentFactory()
        renewal = self._create_renewal(checkout_intent, is_canceled=True)

        deleted_time = timezone.now() - timedelta(days=2)
        restored_time = timezone.now() - timedelta(days=1)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', deleted_time)
        self._create_summary(checkout_intent, 'customer.subscription.created', restored_time)

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())

        renewal.refresh_from_db()
        self.assertFalse(renewal.is_canceled)

    def test_restore_before_deletion_does_not_prevent_cancellation(self):
        """A restore event that predates the deletion is not treated as a valid restore."""
        checkout_intent = CheckoutIntentFactory()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)

        # Restore came *before* deletion — should not prevent cancellation.
        restored_time = timezone.now() - timedelta(days=3)
        deleted_time = timezone.now() - timedelta(days=1)
        self._create_summary(checkout_intent, 'customer.subscription.updated', restored_time,
                             subscription_status='active')
        self._create_summary(checkout_intent, 'customer.subscription.deleted', deleted_time)

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())

        renewal.refresh_from_db()
        self.assertTrue(renewal.is_canceled)

    def test_null_stripe_event_created_at_falls_back_to_created(self):
        """When stripe_event_created_at is None, the created timestamp is used for ordering."""
        checkout_intent = CheckoutIntentFactory()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)

        deleted_summary = self._create_summary(
            checkout_intent, 'customer.subscription.deleted', timezone.now() - timedelta(hours=2)
        )
        # Null out stripe_event_created_at to exercise the fallback branch.
        deleted_summary.stripe_event_created_at = None
        deleted_summary.save(update_fields=['stripe_event_created_at'])

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())

        renewal.refresh_from_db()
        self.assertTrue(renewal.is_canceled)

    def test_multiple_checkout_intents_processed_independently(self):
        """Deletion events for different checkout intents are handled independently."""
        intent_canceled = CheckoutIntentFactory()
        intent_restored = CheckoutIntentFactory()

        renewal_canceled = self._create_renewal(intent_canceled, is_canceled=False)
        renewal_restored = self._create_renewal(intent_restored, is_canceled=True)

        # intent_canceled: only a deletion event → should be canceled.
        self._create_summary(intent_canceled, 'customer.subscription.deleted', timezone.now() - timedelta(days=1))

        # intent_restored: deletion then restore → should not be canceled.
        self._create_summary(intent_restored, 'customer.subscription.deleted', timezone.now() - timedelta(days=2))
        self._create_summary(
            intent_restored, 'customer.subscription.updated', timezone.now() - timedelta(days=1),
            subscription_status='active'
        )

        call_command('backfill_subscription_renewal_cancellations', stdout=StringIO())

        renewal_canceled.refresh_from_db()
        renewal_restored.refresh_from_db()
        self.assertTrue(renewal_canceled.is_canceled)
        self.assertFalse(renewal_restored.is_canceled)

    def test_progress_and_summary_output(self):
        """Stdout contains a progress line and the final summary after processing."""
        checkout_intent = CheckoutIntentFactory()
        self._create_renewal(checkout_intent, is_canceled=False)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', timezone.now() - timedelta(hours=1))

        out = StringIO()
        call_command('backfill_subscription_renewal_cancellations', '--batch-size=1', stdout=out)

        output = out.getvalue()
        self.assertIn('Processed 1/1 deletion events', output)
        self.assertIn('Backfill complete.', output)

    def test_dry_run_stdout_messages(self):
        """Dry run emits a warning header and per-renewal 'Would update' lines."""
        checkout_intent = CheckoutIntentFactory()
        self._create_renewal(checkout_intent, is_canceled=False)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', timezone.now() - timedelta(hours=1))

        out = StringIO()
        call_command('backfill_subscription_renewal_cancellations', '--dry-run', stdout=out)

        output = out.getvalue()
        self.assertIn('DRY RUN MODE', output)
        self.assertIn('Would update', output)
        self.assertIn(str(checkout_intent.id), output)
