"""
Tests for the dispatch_checkout_segment_events management command.
"""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import CheckoutSegmentEvents, SegmentEventEmissionStatus
from enterprise_access.apps.customer_billing.models import CheckoutIntent, SegmentEventEmission

COMMAND_MODULE = 'enterprise_access.apps.customer_billing.management.commands.dispatch_checkout_segment_events'


@override_settings(FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2=True)
class DispatchCheckoutSegmentEventsCommandTests(TestCase):
    """Tests for the outbox dispatcher command."""

    @mock.patch('enterprise_access.apps.customer_billing.segment_outbox.send_checkout_segment_event_task')
    def test_publishes_pending_events(self, mock_task):
        intent = CheckoutIntent.create_intent(user=UserFactory(), quantity=10, slug='acme', name='Acme')
        emission = SegmentEventEmission.objects.create(
            event_name=CheckoutSegmentEvents.ORDER_COMPLETED,
            order_id='cs_123',
            checkout_intent=intent,
            stripe_subscription_id='sub_123',
            stripe_invoice_id='in_123',
        )
        out = StringIO()

        call_command('dispatch_checkout_segment_events', stdout=out)

        mock_task.delay.assert_called_once_with(emission.pk)
        emission.refresh_from_db()
        self.assertEqual(emission.status, SegmentEventEmissionStatus.DISPATCHED)
        self.assertIn('dispatched=1', out.getvalue())

    @mock.patch(f'{COMMAND_MODULE}.dispatch_pending_segment_events', return_value={'dispatched': 0})
    def test_passes_options_through(self, mock_dispatch):
        call_command(
            'dispatch_checkout_segment_events',
            '--stale-after-minutes=30', '--max-dispatch-attempts=2', '--batch-size=10',
            stdout=StringIO(),
        )
        mock_dispatch.assert_called_once_with(
            stale_after=timedelta(minutes=30), max_dispatch_attempts=2, batch_size=10,
        )
