"""
Tests for the fetch_and_handle_stripe_events management command.
"""
from io import StringIO
from unittest import mock

import ddt
import stripe
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase


def _make_mock_event(event_id='evt_test_123', event_type='invoice.paid', created=1700000000,
                     customer='cus_test', customer_email='test@example.com'):
    """Build a minimal mock Stripe event suitable for the dry-run output path."""
    event = mock.Mock()
    event.id = event_id
    event.type = event_type
    event.created = created
    event.data.object.to_dict.return_value = {
        'customer': customer,
        'customer_email': customer_email,
    }
    return event


@ddt.ddt
class FetchAndHandleStripeEventsCommandTests(TestCase):
    """Tests for the fetch_and_handle_stripe_events management command."""

    # -------------------------------------------------------------------------
    # Argument / limit validation
    # -------------------------------------------------------------------------

    @ddt.data((0,), (101,))
    @ddt.unpack
    def test_invalid_limit_raises_command_error(self, limit):
        """limit < 1 or > 100 should raise CommandError before hitting Stripe."""
        with self.assertRaises(CommandError) as ctx:
            call_command('fetch_and_handle_stripe_events', limit=limit)
        self.assertIn('Limit must be between 1 and 100', str(ctx.exception))

    # -------------------------------------------------------------------------
    # Stripe param construction
    # -------------------------------------------------------------------------

    @ddt.data(
        # (cmd_kwargs, expected_type, check_created_gte, exact_params)
        ({'event_type': 'invoice.paid'}, 'invoice.paid', False, None),
        ({'created_since': '2024-01-15T10:00:00'}, None, True, None),
        ({'created_since_hours_ago': 24}, None, True, None),
        ({'limit': 5}, None, False, {'limit': 5}),
    )
    @ddt.unpack
    @mock.patch('stripe.Event.list')
    def test_stripe_params_construction(
        self, cmd_kwargs, expected_type, check_created_gte, exact_params, mock_event_list
    ):
        """Correct parameters are forwarded to stripe.Event.list for each filtering option."""
        mock_event_list.return_value = mock.Mock(data=[])
        call_command('fetch_and_handle_stripe_events', **cmd_kwargs)
        call_kwargs = mock_event_list.call_args[1]
        if exact_params is not None:
            self.assertEqual(call_kwargs, exact_params)
        if expected_type is not None:
            self.assertEqual(call_kwargs.get('type'), expected_type)
        if check_created_gte:
            self.assertIsInstance(call_kwargs['created']['gte'], int)

    # -------------------------------------------------------------------------
    # No events found
    # -------------------------------------------------------------------------

    @mock.patch('stripe.Event.list')
    def test_no_events_found_prints_warning(self, mock_event_list):
        """When Stripe returns an empty list the command should print a warning and exit cleanly."""
        mock_event_list.return_value = mock.Mock(data=[])

        out = StringIO()
        call_command('fetch_and_handle_stripe_events', stdout=out)

        self.assertIn('No events found', out.getvalue())

    # -------------------------------------------------------------------------
    # Dry run (lines 114-124)
    # -------------------------------------------------------------------------

    @mock.patch('stripe.Event.list')
    def test_dry_run_prints_events_without_dispatching(self, mock_event_list):
        """
        Dry run should print event details for each found event and return without
        calling StripeEventHandler.dispatch.
        """
        mock_event = _make_mock_event(
            event_id='evt_dry_run_001',
            event_type='invoice.paid',
            created=1700000000,
            customer='cus_abc123',
            customer_email='billing@example.com',
        )
        mock_event_list.return_value = mock.Mock(data=[mock_event])

        out = StringIO()
        with mock.patch(
            'enterprise_access.apps.customer_billing.stripe_event_handlers.StripeEventHandler.dispatch'
        ) as mock_dispatch:
            call_command('fetch_and_handle_stripe_events', dry_run=True, stdout=out)
            mock_dispatch.assert_not_called()

        output = out.getvalue()
        self.assertIn('DRY RUN', output)
        self.assertIn('evt_dry_run_001', output)
        self.assertIn('invoice.paid', output)
        self.assertIn('cus_abc123', output)
        self.assertIn('billing@example.com', output)

    @mock.patch('stripe.Event.list')
    def test_dry_run_with_multiple_events(self, mock_event_list):
        """Dry run with multiple events should print a line for each event."""
        events = [
            _make_mock_event('evt_001', 'checkout.session.completed', 1700000001, 'cus_1', 'a@example.com'),
            _make_mock_event('evt_002', 'customer.subscription.updated', 1700000002, 'cus_2', 'b@example.com'),
        ]
        mock_event_list.return_value = mock.Mock(data=events)

        out = StringIO()
        call_command('fetch_and_handle_stripe_events', dry_run=True, stdout=out)

        output = out.getvalue()
        self.assertIn('evt_001', output)
        self.assertIn('evt_002', output)
        self.assertIn('checkout.session.completed', output)
        self.assertIn('customer.subscription.updated', output)

    # -------------------------------------------------------------------------
    # Normal processing loop
    # -------------------------------------------------------------------------

    @mock.patch('enterprise_access.apps.customer_billing.management.commands.'
                'fetch_and_handle_stripe_events.StripeEventHandler')
    @mock.patch('stripe.Event.list')
    def test_successful_processing_dispatches_all_events(self, mock_event_list, mock_handler):
        """All events should be dispatched and a success summary printed."""
        events = [
            _make_mock_event('evt_s1', 'invoice.paid'),
            _make_mock_event('evt_s2', 'checkout.session.completed'),
        ]
        mock_event_list.return_value = mock.Mock(data=events)

        out = StringIO()
        call_command('fetch_and_handle_stripe_events', stdout=out)

        self.assertEqual(mock_handler.dispatch.call_count, 2)
        output = out.getvalue()
        self.assertIn('Successfully processed: 2', output)
        self.assertIn('Skipped: 0', output)

    @mock.patch('enterprise_access.apps.customer_billing.management.commands.'
                'fetch_and_handle_stripe_events.StripeEventHandler')
    @mock.patch('stripe.Event.list')
    def test_keyerror_skips_event(self, mock_event_list, mock_handler):
        """A KeyError from dispatch (no handler registered) should skip the event."""
        mock_handler.dispatch.side_effect = KeyError('unknown.event.type')
        mock_event_list.return_value = mock.Mock(data=[_make_mock_event('evt_skip')])

        out = StringIO()
        call_command('fetch_and_handle_stripe_events', stdout=out)

        output = out.getvalue()
        self.assertIn('Skipped: 1', output)
        self.assertIn('Successfully processed: 0', output)

    @mock.patch('enterprise_access.apps.customer_billing.management.commands.'
                'fetch_and_handle_stripe_events.StripeEventHandler')
    @mock.patch('stripe.Event.list')
    def test_generic_exception_counts_as_error(self, mock_event_list, mock_handler):
        """A non-KeyError exception from dispatch should increment the error counter."""
        mock_handler.dispatch.side_effect = Exception('something went wrong')
        mock_event_list.return_value = mock.Mock(data=[_make_mock_event('evt_err')])

        out = StringIO()
        call_command('fetch_and_handle_stripe_events', stdout=out)

        output = out.getvalue()
        self.assertIn('Errors: 1', output)
        self.assertIn('Successfully processed: 0', output)

    @mock.patch('enterprise_access.apps.customer_billing.management.commands.'
                'fetch_and_handle_stripe_events.StripeEventHandler')
    @mock.patch('stripe.Event.list')
    def test_mixed_results_reflected_in_summary(self, mock_event_list, mock_handler):
        """Success, skip, and error counts should all appear correctly in the summary."""
        events = [
            _make_mock_event('evt_ok'),
            _make_mock_event('evt_skip'),
            _make_mock_event('evt_fail'),
        ]
        mock_event_list.return_value = mock.Mock(data=events)

        mock_handler.dispatch.side_effect = [
            None,                       # evt_ok  → success
            KeyError('no handler'),     # evt_skip → skipped
            Exception('boom'),          # evt_fail → error
        ]

        out = StringIO()
        call_command('fetch_and_handle_stripe_events', stdout=out)

        output = out.getvalue()
        self.assertIn('Successfully processed: 1', output)
        self.assertIn('Skipped: 1', output)
        self.assertIn('Errors: 1', output)

    @mock.patch('enterprise_access.apps.customer_billing.management.commands.'
                'fetch_and_handle_stripe_events.StripeEventHandler')
    @mock.patch('stripe.Event.list')
    def test_no_errors_omits_error_line_from_summary(self, mock_event_list, mock_handler):
        """When error_count is 0 the Errors line should not appear in the summary."""
        mock_event_list.return_value = mock.Mock(data=[_make_mock_event()])

        out = StringIO()
        call_command('fetch_and_handle_stripe_events', stdout=out)

        self.assertNotIn('Errors:', out.getvalue())

    # -------------------------------------------------------------------------
    # Stripe / unexpected errors from Event.list
    # -------------------------------------------------------------------------

    @ddt.data(
        (stripe.StripeError('API unavailable'), 'Stripe API error'),
        (RuntimeError('network failure'), 'Unexpected error'),
    )
    @ddt.unpack
    @mock.patch('stripe.Event.list')
    def test_event_list_exception_raises_command_error(self, exc, expected_msg, mock_event_list):
        """StripeError and unexpected exceptions from Event.list are both re-raised as CommandError."""
        mock_event_list.side_effect = exc

        with self.assertRaises(CommandError) as ctx:
            call_command('fetch_and_handle_stripe_events')

        self.assertIn(expected_msg, str(ctx.exception))
