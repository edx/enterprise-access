"""
Test data migrations using django_test_migrations.

Uses the ``Migrator`` class to properly roll back to the pre-migration state,
create test data with historical (frozen) model classes, then apply the
migration under test and verify results.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import ddt
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


@ddt.ddt
class TestBackfillSubscriptionRenewalCancellations(TransactionTestCase):
    """
    Tests for the backfill data migration 0031.

    Each test:
    1. Rolls the DB back to migration 0029 (is_canceled and subscription_cancel_at fields exist but no backfill yet)
    2. Creates test data using historical (frozen) model classes
    3. Applies migration 0031 (the backfill)
    4. Verifies the backfilled cancellation state
    """

    migrate_from = [('customer_billing', '0029_historicalselfservicesubscriptionrenewal_is_canceled_and_more')]
    migrate_to = [('customer_billing', '0030_backfill_subscription_renewal_cancellations')]

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

    # Each entry describes a simple scenario: create one renewal, create N summary
    # events, apply the migration, and assert the final renewal state.
    #
    # Fields:
    #   initial_is_canceled       - bool: the renewal's starting is_canceled value
    #   initial_cancel_at_offset  - timedelta (future) or None: renewal's starting
    #                               subscription_cancel_at, computed as now + offset
    #   events                    - list of (event_type, offset_ago, subscription_status,
    #                               subscription_cancel_at), where offset_ago is a timedelta
    #                               and times are computed as now - offset_ago
    #   expected_is_canceled      - bool: asserted on renewal after migration
    #   expected_cancel_at        - datetime or None: asserted on renewal after migration
    @ddt.data(
        dict(
            initial_is_canceled=False,
            initial_cancel_at_offset=None,
            events=[
                ('customer.subscription.deleted', timedelta(days=1), None, None),
            ],
            expected_is_canceled=True,
            expected_cancel_at=None,
        ),
        dict(
            initial_is_canceled=True,
            initial_cancel_at_offset=None,
            events=[
                ('customer.subscription.deleted', timedelta(days=2), 'canceled', None),
                ('customer.subscription.updated', timedelta(days=1), 'active', None),
            ],
            expected_is_canceled=False,
            expected_cancel_at=None,
        ),
        dict(
            initial_is_canceled=True,
            initial_cancel_at_offset=None,
            events=[
                ('customer.subscription.deleted', timedelta(hours=1), None, None),
            ],
            expected_is_canceled=True,
            expected_cancel_at=None,
        ),
        dict(
            initial_is_canceled=True,
            initial_cancel_at_offset=None,
            events=[
                ('customer.subscription.deleted', timedelta(days=2), None, None),
                ('customer.subscription.created', timedelta(days=1), None, None),
            ],
            expected_is_canceled=False,
            expected_cancel_at=None,
        ),
        dict(
            initial_is_canceled=False,
            initial_cancel_at_offset=None,
            events=[
                ('customer.subscription.updated', timedelta(days=3), 'active', None),
                ('customer.subscription.deleted', timedelta(days=1), None, None),
            ],
            expected_is_canceled=True,
            expected_cancel_at=None,
        ),
        dict(
            initial_is_canceled=False,
            initial_cancel_at_offset=timedelta(days=30),
            events=[
                ('customer.subscription.deleted', timedelta(hours=1), None, None),
            ],
            expected_is_canceled=True,
            expected_cancel_at=None,
        ),
        dict(
            initial_is_canceled=True,
            initial_cancel_at_offset=None,
            events=[
                ('customer.subscription.deleted', timedelta(days=2), None, None),
                ('customer.subscription.updated', timedelta(days=1), 'active',
                 datetime(2026, 6, 1, tzinfo=timezone.utc)),
            ],
            expected_is_canceled=False,
            expected_cancel_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ),
        dict(
            initial_is_canceled=True,
            initial_cancel_at_offset=timedelta(days=10),
            events=[
                ('customer.subscription.deleted', timedelta(days=2), None, None),
                ('customer.subscription.updated', timedelta(days=1), 'active', None),
            ],
            expected_is_canceled=False,
            expected_cancel_at=None,
        ),
    )
    @ddt.unpack
    def test_cancellation_scenario(
        self, initial_is_canceled, initial_cancel_at_offset, events,
        expected_is_canceled, expected_cancel_at,
    ):
        """
        Parameterized test covering the common cancellation backfill scenarios:
        - deletion sets is_canceled=True and clears subscription_cancel_at
        - a restore event after deletion sets is_canceled=False and updates subscription_cancel_at
        - a restore event before deletion is ignored
        - already-correct state is left unchanged
        """
        now = django_tz.now()
        checkout_intent = self._create_checkout_intent()
        initial_cancel_at = now + initial_cancel_at_offset if initial_cancel_at_offset else None
        renewal = self._create_renewal(
            checkout_intent,
            is_canceled=initial_is_canceled,
            subscription_cancel_at=initial_cancel_at,
        )
        for event_type, offset_ago, status, cancel_at in events:
            self._create_summary(checkout_intent, event_type, now - offset_ago, status, cancel_at)
        renewal = self._apply_migration_and_get_renewal(renewal.id)
        self.assertEqual(renewal.is_canceled, expected_is_canceled)
        self.assertEqual(renewal.subscription_cancel_at, expected_cancel_at)

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

    def test_no_renewals_for_checkout_intent_is_unchanged(self):
        """A deletion event whose checkout intent has no renewals is a no-op."""
        checkout_intent = self._create_checkout_intent()
        self._create_summary(checkout_intent, 'customer.subscription.deleted', django_tz.now() - timedelta(hours=1))

        new_state = self.migrator.apply_tested_migration(self.migrate_to)
        Renewal = new_state.apps.get_model('customer_billing', 'SelfServiceSubscriptionRenewal')
        self.assertEqual(Renewal.objects.count(), 0)

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
