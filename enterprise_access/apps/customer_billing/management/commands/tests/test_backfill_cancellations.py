"""Tests for backfill_subscription_renewal_cancellations command."""
from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from enterprise_access.apps.customer_billing.models import SelfServiceSubscriptionRenewal
from enterprise_access.apps.customer_billing.tests.factories import CheckoutIntentFactory, StripeEventDataFactory


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
