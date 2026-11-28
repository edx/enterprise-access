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
from enterprise_access.apps.customer_billing.models import CheckoutIntent
from enterprise_access.apps.customer_billing.tests.factories import (
    StripeEventDataFactory,
    get_stripe_object_for_event_type
)

NOW = datetime(2026, 11, 17, 14, 0, tzinfo=dt_timezone.utc)
SUBSCRIPTION_ID = 'sub_renewing'
RENEWAL = NOW + timedelta(days=40, hours=1)
TASK_PATH = (
    'enterprise_access.apps.customer_billing.management.commands.'
    'send_renewal_reminder_emails.send_renewal_reminder_email_task'
)


def _timestamp(value):
    return int(value.timestamp()) if value else None


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

    def _add_event(self, event_type, checkout_intent, **stripe_object):
        """
        Record a Stripe event, each one later than the previous. As for a webhook, the StripeEventData post_save
        signal builds its StripeEventSummary from the payload.
        """
        self.event_count += 1
        created = NOW - timedelta(days=100) + timedelta(minutes=self.event_count)
        StripeEventDataFactory(event_type=event_type, checkout_intent=checkout_intent, data={
            'created': _timestamp(created),
            'data': {'object': get_stripe_object_for_event_type(event_type, **stripe_object)},
        })

    def _add_invoice_paid(self, subscription_id=SUBSCRIPTION_ID, amount_paid=500000):
        # The factory gives the invoice's subscription a random ID, unrelated to the subscription's own.
        self._add_event(
            'invoice.paid', self.checkout_intent,
            amount_paid=amount_paid, parent={'subscription_details': {'subscription': subscription_id}},
        )

    def _add_subscription_event(self, period_end, subscription_id=SUBSCRIPTION_ID, **overrides):
        """Record an update to an active subscription whose current term ends at ``period_end``."""
        checkout_intent = overrides.pop('checkout_intent', self.checkout_intent)
        self._add_event('customer.subscription.updated', checkout_intent, **{
            'id': subscription_id, 'status': 'active', 'cancel_at': None,
            'items': {'data': [{'current_period_end': _timestamp(period_end)}]},
            **overrides,
        })

    def _add_paid_subscription(self, period_end, subscription_id=SUBSCRIPTION_ID):
        """Record a paid invoice and an active subscription whose current term ends at ``period_end``."""
        self._add_invoice_paid(subscription_id)
        self._add_subscription_event(period_end, subscription_id)

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

    @ddt.data(
        {'latest': {'status': 'past_due'}},
        {'latest': {'status': 'canceled'}},
        {'latest': {'cancel_at': _timestamp(RENEWAL)}},
        # The renewal date moved since the earlier event.
        {'latest': {'period_end': NOW + timedelta(days=200)}},
        # The latest event isn't linked to a checkout intent, or has no period end.
        {'latest': {'checkout_intent': None}},
        {'latest': {'period_end': None}},
        # Only a $0 (trial) invoice: not a paid-to-paid renewal.
        {'amount_paid': 0},
        # The paid invoice is another subscription's.
        {'paid_subscription_id': 'sub_other'},
    )
    @ddt.unpack
    def test_not_queued(self, mock_task, latest=None, amount_paid=500000, paid_subscription_id=SUBSCRIPTION_ID):
        """
        No reminder for a subscription that was never paid, or whose latest event cancels, ends, moves the renewal
        or lacks what the email needs.
        """
        self._add_invoice_paid(paid_subscription_id, amount_paid)
        self._add_subscription_event(RENEWAL)
        if latest is not None:
            self._add_subscription_event(**{'period_end': RENEWAL, **latest})

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
        self._add_paid_subscription(RENEWAL)

        call_command('send_renewal_reminder_emails', dry_run=True)

        mock_task.delay.assert_not_called()
