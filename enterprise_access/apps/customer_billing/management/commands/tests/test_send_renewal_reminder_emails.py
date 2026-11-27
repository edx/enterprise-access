"""
Tests for the send_renewal_reminder_emails management command.
"""
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

import ddt
from django.core.management import call_command
from django.test import TestCase
from freezegun import freeze_time

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.models import CheckoutIntent, StripeEventData, StripeEventSummary

NOW = datetime(2026, 11, 17, 14, 0, tzinfo=dt_timezone.utc)
SUBSCRIPTION_ID = 'sub_renewing'
TASK_PATH = (
    'enterprise_access.apps.customer_billing.management.commands.'
    'send_renewal_reminder_emails.send_renewal_reminder_email_task'
)


@ddt.ddt
@freeze_time(NOW)
@mock.patch(TASK_PATH)
class SendRenewalReminderEmailsCommandTests(TestCase):
    """Tests for the send_renewal_reminder_emails management command."""

    def setUp(self):
        self.checkout_intent = CheckoutIntent.create_intent(
            user=UserFactory(), slug='test-enterprise', name='Test Enterprise', quantity=10,
        )
        self.event_count = 0

    def _add_summary(self, event_type, subscription_id=SUBSCRIPTION_ID, **fields):
        """Record a Stripe event summary for the subscription, each one later than the previous."""
        self.event_count += 1
        # The summary takes its event data's checkout intent when it has none, so both get the same one.
        fields.setdefault('checkout_intent', self.checkout_intent)
        event_data = StripeEventData.objects.create(
            event_id=f'evt_{self.event_count}', event_type=event_type, checkout_intent=fields['checkout_intent'],
            data={'created': 1700000000},
        )
        StripeEventSummary.objects.update_or_create(stripe_event_data=event_data, defaults={
            'event_id': event_data.event_id, 'event_type': event_type, 'stripe_subscription_id': subscription_id,
            'stripe_event_created_at': NOW - timedelta(days=100) + timedelta(minutes=self.event_count),
            **fields,
        })

    def _add_paid_subscription(self, period_end, subscription_id=SUBSCRIPTION_ID, amount_paid=500000):
        """Record a paid invoice and an active subscription whose current term ends at ``period_end``."""
        self._add_summary(
            'invoice.paid', subscription_id, stripe_object_type='invoice', invoice_amount_paid=amount_paid,
        )
        self._add_summary(
            'customer.subscription.updated', subscription_id,
            stripe_object_type='subscription', subscription_status='active', subscription_period_end=period_end,
        )

    def _assert_queued(self, mock_task, *expected):
        """Assert the command queued exactly these (subscription id, email type) reminders, in order."""
        self.assertEqual(mock_task.delay.call_args_list, [
            mock.call(checkout_intent_id=self.checkout_intent.id, stripe_subscription_id=sub_id, email_type=email_type)
            for sub_id, email_type in expected
        ])

    @ddt.data(
        # 40-day window: [40 days, 41 days)
        (timedelta(days=39, hours=23, minutes=59), None),
        (timedelta(days=40), 'renewal_reminder_40_day'),
        (timedelta(days=40, hours=23, minutes=59), 'renewal_reminder_40_day'),
        (timedelta(days=41), None),
        # 72-hour window: [72 hours, 96 hours)
        (timedelta(hours=71, minutes=59), None),
        (timedelta(hours=72), 'renewal_reminder_72_hour'),
        (timedelta(hours=95, minutes=59), 'renewal_reminder_72_hour'),
        (timedelta(hours=96), None),
    )
    @ddt.unpack
    def test_queues_reminder_only_for_renewal_in_window(self, time_to_renewal, email_type, mock_task):
        """A paid subscription gets a reminder only from the run whose day-long window its renewal falls in."""
        self._add_paid_subscription(NOW + time_to_renewal)

        call_command('send_renewal_reminder_emails')

        expected = [(SUBSCRIPTION_ID, email_type)] if email_type else []
        self._assert_queued(mock_task, *expected)

    def test_skips_subscription_never_paid(self, mock_task):
        """A subscription with only a $0 (trial) invoice is not a paid-to-paid renewal."""
        self._add_paid_subscription(NOW + timedelta(days=40, hours=1), amount_paid=0)

        call_command('send_renewal_reminder_emails')

        mock_task.delay.assert_not_called()

    @ddt.data(
        {'subscription_status': 'past_due'},
        {'subscription_status': 'canceled'},
        {'subscription_cancel_at': NOW + timedelta(days=40, hours=1)},
        # The renewal date moved since the earlier event.
        {'subscription_period_end': NOW + timedelta(days=200)},
        # The latest event isn't linked to a checkout intent, or has no period end.
        {'checkout_intent': None},
        {'subscription_period_end': None},
    )
    def test_latest_subscription_event_wins(self, latest_overrides, mock_task):
        """A later event that cancels, ends, moves the renewal or lacks what the email needs blocks the reminder."""
        renewal = NOW + timedelta(days=40, hours=1)
        self._add_paid_subscription(renewal)
        self._add_summary('customer.subscription.updated', stripe_object_type='subscription', **{
            'subscription_status': 'active', 'subscription_period_end': renewal, **latest_overrides,
        })

        call_command('send_renewal_reminder_emails')

        mock_task.delay.assert_not_called()

    def test_queues_each_subscription_once_per_reminder(self, mock_task):
        """Subscriptions renewing in different windows each get only their own reminder."""
        self._add_paid_subscription(NOW + timedelta(days=40, hours=1), subscription_id='sub_40')
        self._add_paid_subscription(NOW + timedelta(hours=73), subscription_id='sub_72')

        call_command('send_renewal_reminder_emails')

        self._assert_queued(mock_task, ('sub_40', 'renewal_reminder_40_day'), ('sub_72', 'renewal_reminder_72_hour'))

    @ddt.data(
        # (email type, lead time, first run's start delay, next day's start delay, renewal past the boundary), in s.
        # The next day's run starts later than today's: measured from the start time, the windows leave a gap.
        ('renewal_reminder_40_day', timedelta(days=40), 0, 120, 60),
        ('renewal_reminder_72_hour', timedelta(hours=72), 0, 120, 60),
        # The next day's run starts earlier than today's: measured from the start time, the windows overlap.
        ('renewal_reminder_40_day', timedelta(days=40), 30, 5, 10),
        ('renewal_reminder_72_hour', timedelta(hours=72), 30, 5, 10),
    )
    @ddt.unpack
    def test_start_delay_neither_skips_nor_repeats_a_reminder(
        self, email_type, lead_time, first_delay, second_delay, seconds_past_boundary, mock_task,
    ):
        """Two daily runs starting late by different amounts send a renewal at their windows' boundary one reminder."""
        self._add_paid_subscription(NOW + timedelta(days=1) + lead_time + timedelta(seconds=seconds_past_boundary))

        for run_at in (NOW + timedelta(seconds=first_delay), NOW + timedelta(days=1, seconds=second_delay)):
            with freeze_time(run_at):
                call_command('send_renewal_reminder_emails')

        self._assert_queued(mock_task, (SUBSCRIPTION_ID, email_type))

    def test_dry_run_queues_nothing(self, mock_task):
        """--dry-run only logs."""
        self._add_paid_subscription(NOW + timedelta(days=40, hours=1))

        call_command('send_renewal_reminder_emails', dry_run=True)

        mock_task.delay.assert_not_called()
