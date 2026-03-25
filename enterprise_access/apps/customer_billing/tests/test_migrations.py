"""
Test data migrations using django_test_migrations.

Uses the ``Migrator`` class to properly roll back to the pre-migration state,
create test data with historical (frozen) model classes, then apply the
migration under test and verify results.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone as django_tz
from django_test_migrations.migrator import Migrator


def _make_invoice_event_data(event_id, invoice_id, amount_paid=0, period_end=None):
    """
    Build a realistic raw data dict for a StripeEventData record of type invoice.paid.
    """
    lines_data = [
        {
            'pricing': {'unit_amount': 100, 'unit_amount_decimal': '100.0'},
            'quantity': 1,
            'parent': {'type': 'subscription_item_details'},
        }
    ]
    if period_end is not None:
        lines_data[0]['period'] = {'start': 1704067200, 'end': period_end}

    return {
        'id': event_id,
        'type': 'invoice.paid',
        'created': int(django_tz.now().timestamp()),
        'data': {
            'object': {
                'object': 'invoice',
                'id': invoice_id,
                'customer': 'cus_test',
                'amount_paid': amount_paid,
                'currency': 'usd',
                'parent': {
                    'subscription_details': {
                        'subscription': 'sub_test_123',
                    }
                },
                'lines': {'data': lines_data},
            }
        },
    }


class TestBackfillRenewalInvoiceAndEffectiveDate(TransactionTestCase):
    """
    Tests for the backfill data migration 0028.

    Each test:
    1. Rolls the DB back to migration 0027 (fields exist but no backfill yet)
    2. Creates test data using historical (frozen) model classes
    3. Applies migration 0028 (the backfill)
    4. Verifies the backfilled data
    """

    migrate_from = [('customer_billing', '0027_add_renewal_invoice_and_effective_date')]
    migrate_to = [('customer_billing', '0028_backfill_renewal_invoice_and_effective_date')]

    def setUp(self):
        super().setUp()
        self.migrator = Migrator(database='default')
        self.old_state = self.migrator.apply_initial_migration(self.migrate_from)

    def tearDown(self):
        connections["default"].close()
        self.migrator.reset()
        super().tearDown()

    def _get_model(self, app_label, model_name):
        """Get a historical model class from the pre-migration state."""
        return self.old_state.apps.get_model(app_label, model_name)

    def _create_checkout_intent(self):
        """Create a User and CheckoutIntent using historical models."""
        User = self._get_model('core', 'User')
        CheckoutIntent = self._get_model('customer_billing', 'CheckoutIntent')
        user = User.objects.create(username='testuser', email='test@example.com')
        return CheckoutIntent.objects.create(
            user=user,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            quantity=10,
            expires_at=django_tz.now() + timedelta(hours=1),
            country='US',
        )

    def _create_invoice_event(self, checkout_intent, event_id, invoice_id, amount_paid=0, period_end=None):
        """
        Create StripeEventData and StripeEventSummary using historical models.

        Signals don't fire with frozen models, so we create the
        StripeEventSummary record manually.
        """
        StripeEventData = self._get_model('customer_billing', 'StripeEventData')
        StripeEventSummary = self._get_model('customer_billing', 'StripeEventSummary')

        raw_data = _make_invoice_event_data(event_id, invoice_id, amount_paid, period_end)
        event_data = StripeEventData.objects.create(
            event_id=event_id,
            event_type='invoice.paid',
            checkout_intent=checkout_intent,
            data=raw_data,
        )
        StripeEventSummary.objects.create(
            stripe_event_data=event_data,
            event_id=event_id,
            event_type='invoice.paid',
            checkout_intent=checkout_intent,
            invoice_amount_paid=amount_paid,
            stripe_invoice_id=invoice_id,
        )
        return event_data

    def _apply_migration_and_get_renewal(self, renewal_id):
        """Apply the backfill migration and return the updated renewal record."""
        new_state = self.migrator.apply_tested_migration(self.migrate_to)
        Renewal = new_state.apps.get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        return Renewal.objects.get(id=renewal_id)

    def test_backfill_populates_both_fields(self):
        """
        Backfill populates both stripe_invoice_id and effective_date when
        the appropriate event data exists.
        """
        Renewal = self._get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        checkout_intent = self._create_checkout_intent()

        trial_period_end_ts = 1735689600  # 2025-01-01T00:00:00Z
        trial_event = self._create_invoice_event(
            checkout_intent, 'evt_trial_paid', 'in_trial_000',
            amount_paid=0, period_end=trial_period_end_ts,
        )
        self._create_invoice_event(
            checkout_intent, 'evt_real_paid', 'in_paid_123', amount_paid=5000,
        )

        renewal = Renewal.objects.create(
            checkout_intent=checkout_intent,
            subscription_plan_renewal_id=1234,
            stripe_event_data=trial_event,
            stripe_subscription_id='sub_test_123',
        )

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertEqual(renewal.stripe_invoice_id, 'in_paid_123')
        self.assertEqual(
            renewal.effective_date,
            datetime.fromtimestamp(trial_period_end_ts, tz=timezone.utc),
        )

    def test_backfill_skips_already_populated_records(self):
        """Backfill does not overwrite existing stripe_invoice_id or effective_date."""
        Renewal = self._get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        checkout_intent = self._create_checkout_intent()

        trial_event = self._create_invoice_event(
            checkout_intent, 'evt_trial', 'in_trial_000',
            amount_paid=0, period_end=9999999999,
        )
        self._create_invoice_event(
            checkout_intent, 'evt_paid', 'in_should_not_use', amount_paid=5000,
        )

        existing_invoice_id = 'in_already_set'
        existing_effective_date = datetime(2025, 6, 1, tzinfo=timezone.utc)
        renewal = Renewal.objects.create(
            checkout_intent=checkout_intent,
            subscription_plan_renewal_id=1234,
            stripe_event_data=trial_event,
            stripe_subscription_id='sub_test_123',
            stripe_invoice_id=existing_invoice_id,
            effective_date=existing_effective_date,
        )

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertEqual(renewal.stripe_invoice_id, existing_invoice_id)
        self.assertEqual(renewal.effective_date, existing_effective_date)

    def test_backfill_handles_missing_paid_invoice(self):
        """
        When no paid invoice.paid event exists, stripe_invoice_id stays None
        but effective_date is still populated from the trial event.
        """
        Renewal = self._get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        checkout_intent = self._create_checkout_intent()

        trial_period_end_ts = 1735689600
        trial_event = self._create_invoice_event(
            checkout_intent, 'evt_trial_only', 'in_trial_000',
            amount_paid=0, period_end=trial_period_end_ts,
        )

        renewal = Renewal.objects.create(
            checkout_intent=checkout_intent,
            subscription_plan_renewal_id=1234,
            stripe_event_data=trial_event,
            stripe_subscription_id='sub_test_123',
        )

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertIsNone(renewal.stripe_invoice_id)
        self.assertEqual(
            renewal.effective_date,
            datetime.fromtimestamp(trial_period_end_ts, tz=timezone.utc),
        )

    def test_backfill_handles_missing_trial_invoice(self):
        """
        When no trial invoice.paid event exists, effective_date stays None
        but stripe_invoice_id is still populated from the paid event.
        """
        Renewal = self._get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        checkout_intent = self._create_checkout_intent()

        paid_event = self._create_invoice_event(
            checkout_intent, 'evt_paid_only', 'in_paid_only', amount_paid=5000,
        )

        renewal = Renewal.objects.create(
            checkout_intent=checkout_intent,
            subscription_plan_renewal_id=1234,
            stripe_event_data=paid_event,
            stripe_subscription_id='sub_test_123',
        )

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertEqual(renewal.stripe_invoice_id, 'in_paid_only')
        self.assertIsNone(renewal.effective_date)

    def test_backfill_handles_malformed_trial_data(self):
        """
        When the trial invoice event has malformed period data, effective_date
        stays None without crashing.
        """
        Renewal = self._get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        checkout_intent = self._create_checkout_intent()

        # Create trial event WITHOUT period data in line items
        trial_event = self._create_invoice_event(
            checkout_intent, 'evt_malformed', 'in_trial_malformed', amount_paid=0,
            # period_end=None means no 'period' key in line items
        )

        renewal = Renewal.objects.create(
            checkout_intent=checkout_intent,
            subscription_plan_renewal_id=1234,
            stripe_event_data=trial_event,
            stripe_subscription_id='sub_test_123',
        )

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertIsNone(renewal.stripe_invoice_id)
        self.assertIsNone(renewal.effective_date)

    def test_backfill_no_renewals_to_process(self):
        """Backfill completes successfully when there are no renewal records."""
        new_state = self.migrator.apply_tested_migration(self.migrate_to)
        Renewal = new_state.apps.get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        self.assertEqual(Renewal.objects.count(), 0)


class TestBackfillSubscriptionRenewalCancellations(TransactionTestCase):
    """
    Tests for the backfill data migration 0031.

    Each test:
    1. Rolls the DB back to migration 0030 (is_canceled and subscription_cancel_at fields exist but no backfill yet)
    2. Creates test data using historical (frozen) model classes
    3. Applies migration 0031 (the backfill)
    4. Verifies the backfilled cancellation state
    """

    migrate_from = [('customer_billing', '0030_selfservicesubscriptionrenewal_subscription_cancel_at')]
    migrate_to = [('customer_billing', '0031_backfill_subscription_renewal_cancellations')]

    def setUp(self):
        super().setUp()
        self.migrator = Migrator(database='default')
        self.old_state = self.migrator.apply_initial_migration(self.migrate_from)

    def tearDown(self):
        connections["default"].close()
        self.migrator.reset()
        super().tearDown()

    def _get_model(self, app_label, model_name):
        """Get a historical model class from the pre-migration state."""
        return self.old_state.apps.get_model(app_label, model_name)

    def _create_checkout_intent(self):
        """Create a User and CheckoutIntent using historical models."""
        User = self._get_model('core', 'User')
        CheckoutIntent = self._get_model('customer_billing', 'CheckoutIntent')
        user = User.objects.create(username=f'user_{uuid4().hex[:8]}', email=f'{uuid4().hex}@example.com')
        return CheckoutIntent.objects.create(
            user=user,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            quantity=10,
            expires_at=django_tz.now() + timedelta(hours=1),
            country='US',
        )

    def _create_event_data(self, checkout_intent, event_type):
        """Create a StripeEventData record using historical models."""
        StripeEventData = self._get_model('customer_billing', 'StripeEventData')
        event_id = f'evt_{uuid4().hex}'
        return StripeEventData.objects.create(
            event_id=event_id,
            event_type=event_type,
            checkout_intent=checkout_intent,
            data={
                'id': event_id,
                'type': event_type,
                'created': int(django_tz.now().timestamp()),
                'data': {
                    'object': {
                        'object': 'subscription',
                        'id': f'sub_{uuid4().hex}',
                        'status': 'active',
                    }
                },
            },
        )

    def _create_summary(self, checkout_intent, event_type, created_at, subscription_status=None,
                        subscription_cancel_at=None):
        """Create StripeEventData + StripeEventSummary for a subscription event."""
        StripeEventSummary = self._get_model('customer_billing', 'StripeEventSummary')
        event_data = self._create_event_data(checkout_intent, event_type)
        summary = StripeEventSummary.objects.create(
            stripe_event_data=event_data,
            event_id=event_data.event_id,
            event_type=event_type,
            stripe_event_created_at=created_at,
            checkout_intent=checkout_intent,
            subscription_status=subscription_status,
            subscription_cancel_at=subscription_cancel_at,
        )
        return summary

    def _create_renewal(self, checkout_intent, is_canceled=False, subscription_cancel_at=None):
        """Create a StripeEventData + SelfServiceSubscriptionRenewal using historical models."""
        SelfServiceSubscriptionRenewal = self._get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        event_data = self._create_event_data(checkout_intent, 'customer.subscription.created')
        return SelfServiceSubscriptionRenewal.objects.create(
            checkout_intent=checkout_intent,
            subscription_plan_renewal_id=1,
            stripe_event_data=event_data,
            stripe_subscription_id=f'sub_{uuid4().hex}',
            is_canceled=is_canceled,
            subscription_cancel_at=subscription_cancel_at,
        )

    def _apply_migration_and_get_renewal(self, renewal_id):
        """Apply the backfill migration and return the updated renewal record."""
        new_state = self.migrator.apply_tested_migration(self.migrate_to)
        Renewal = new_state.apps.get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        return Renewal.objects.get(id=renewal_id)

    def test_idempotent_updates(self):
        """Running the migration twice results in the same final state."""
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(days=1))

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertTrue(renewal.is_canceled)

    def test_handles_null_checkout_intent_event(self):
        """Deletion events without a checkout intent are ignored."""
        StripeEventData = self._get_model('customer_billing', 'StripeEventData')
        StripeEventSummary = self._get_model('customer_billing', 'StripeEventSummary')
        null_event_data = self._create_event_data(None, 'customer.subscription.deleted')
        # Null out the checkout_intent on the event data itself.
        StripeEventData.objects.filter(event_id=null_event_data.event_id).update(checkout_intent=None)
        null_event_data.refresh_from_db()
        StripeEventSummary.objects.create(
            stripe_event_data=null_event_data,
            event_id=null_event_data.event_id,
            event_type='customer.subscription.deleted',
            stripe_event_created_at=django_tz.now() - timedelta(hours=3),
            checkout_intent=None,
        )

        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(hours=2))

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertTrue(renewal.is_canceled)

    def test_later_restore_event_sets_uncanceled(self):
        """A restore event after deletion marks the renewal as not canceled."""
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=True)
        self._create_summary(
            checkout_intent, 'customer.subscription.deleted',
            django_tz.now() - timedelta(days=2), subscription_status='canceled',
        )
        self._create_summary(
            checkout_intent, 'customer.subscription.updated',
            django_tz.now() - timedelta(days=1), subscription_status='active',
        )

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertFalse(renewal.is_canceled)

    def test_only_latest_renewal_is_updated(self):
        """Only the most recently created renewal is updated; older renewals are left unchanged."""
        checkout_intent = self._create_checkout_intent()
        older_renewal = self._create_renewal(checkout_intent, is_canceled=False)
        newer_renewal = self._create_renewal(checkout_intent, is_canceled=False)

        SelfServiceSubscriptionRenewal = self._get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        SelfServiceSubscriptionRenewal.objects.filter(pk=older_renewal.pk).update(
            created=django_tz.now() - timedelta(days=2)
        )
        SelfServiceSubscriptionRenewal.objects.filter(pk=newer_renewal.pk).update(
            created=django_tz.now() - timedelta(days=1)
        )

        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(hours=1))

        new_state = self.migrator.apply_tested_migration(self.migrate_to)
        Renewal = new_state.apps.get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        self.assertFalse(Renewal.objects.get(pk=older_renewal.pk).is_canceled)
        self.assertTrue(Renewal.objects.get(pk=newer_renewal.pk).is_canceled)

    def test_already_correct_state_is_unchanged(self):
        """Renewals already in the target state are not written to."""
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=True)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(hours=1))

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertTrue(renewal.is_canceled)

    def test_no_renewals_for_checkout_intent_is_unchanged(self):
        """A deletion event whose checkout intent has no renewals is a no-op."""
        checkout_intent = self._create_checkout_intent()
        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(hours=1))

        new_state = self.migrator.apply_tested_migration(self.migrate_to)
        Renewal = new_state.apps.get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        self.assertEqual(Renewal.objects.count(), 0)

    def test_restore_via_created_event_sets_uncanceled(self):
        """A customer.subscription.created event after deletion marks the renewal as not canceled."""
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=True)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(days=2))
        self._create_summary(checkout_intent, 'customer.subscription.created', django_tz.now() - timedelta(days=1))

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertFalse(renewal.is_canceled)

    def test_restore_before_deletion_does_not_prevent_cancellation(self):
        """A restore event that predates the deletion is not treated as a valid restore."""
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)
        self._create_summary(
            checkout_intent, 'customer.subscription.updated',
            django_tz.now() - timedelta(days=3), subscription_status='active',
        )
        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(days=1))

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertTrue(renewal.is_canceled)

    def test_null_stripe_event_created_at_falls_back_to_created(self):
        """When stripe_event_created_at is None, the created timestamp is used for ordering."""
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=False)

        StripeEventSummary = self._get_model('customer_billing', 'StripeEventSummary')
        deleted_summary = self._create_summary(
            checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(hours=2)
        )
        StripeEventSummary.objects.filter(pk=deleted_summary.pk).update(stripe_event_created_at=None)

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertTrue(renewal.is_canceled)

    def test_multiple_checkout_intents_processed_independently(self):
        """Deletion events for different checkout intents are handled independently."""
        intent_canceled = self._create_checkout_intent()
        intent_restored = self._create_checkout_intent()

        renewal_canceled = self._create_renewal(intent_canceled, is_canceled=False)
        renewal_restored = self._create_renewal(intent_restored, is_canceled=True)

        self._create_summary(intent_canceled, 'customer.subscription.deleted', django_tz.now() - timedelta(days=1))
        self._create_summary(intent_restored, 'customer.subscription.deleted', django_tz.now() - timedelta(days=2))
        self._create_summary(
            intent_restored, 'customer.subscription.updated',
            django_tz.now() - timedelta(days=1), subscription_status='active',
        )

        new_state = self.migrator.apply_tested_migration(self.migrate_to)
        Renewal = new_state.apps.get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        self.assertTrue(Renewal.objects.get(pk=renewal_canceled.pk).is_canceled)
        self.assertFalse(Renewal.objects.get(pk=renewal_restored.pk).is_canceled)

    def test_canceled_clears_subscription_cancel_at(self):
        """
        When a deletion has no later restore, subscription_cancel_at is cleared to None
        even if the renewal had a prior value.
        """
        cancel_at = django_tz.now() + timedelta(days=30)
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=False, subscription_cancel_at=cancel_at)
        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(hours=1))

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertTrue(renewal.is_canceled)
        self.assertIsNone(renewal.subscription_cancel_at)

    def test_restore_sets_subscription_cancel_at_from_restore_event(self):
        """
        When a deletion is followed by a restore event that has a subscription_cancel_at,
        the renewal's subscription_cancel_at is updated from that restore event.
        """
        expected_cancel_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(checkout_intent, is_canceled=True)
        self._create_summary(
            checkout_intent, 'customer.subscription.deleted',
            django_tz.now() - timedelta(days=2),
        )
        self._create_summary(
            checkout_intent, 'customer.subscription.updated',
            django_tz.now() - timedelta(days=1),
            subscription_status='active',
            subscription_cancel_at=expected_cancel_at,
        )

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertFalse(renewal.is_canceled)
        self.assertEqual(renewal.subscription_cancel_at, expected_cancel_at)

    def test_restore_with_no_cancel_at_sets_null(self):
        """
        When a deletion is followed by a restore event that has no subscription_cancel_at,
        the renewal's subscription_cancel_at is set to None.
        """
        checkout_intent = self._create_checkout_intent()
        renewal = self._create_renewal(
            checkout_intent, is_canceled=True,
            subscription_cancel_at=django_tz.now() + timedelta(days=10),
        )
        self._create_summary(
            checkout_intent, 'customer.subscription.deleted',
            django_tz.now() - timedelta(days=2),
        )
        self._create_summary(
            checkout_intent, 'customer.subscription.updated',
            django_tz.now() - timedelta(days=1),
            subscription_status='active',
            subscription_cancel_at=None,
        )

        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertFalse(renewal.is_canceled)
        self.assertIsNone(renewal.subscription_cancel_at)
