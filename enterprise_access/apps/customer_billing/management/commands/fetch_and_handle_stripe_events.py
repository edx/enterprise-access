"""
Management command to fetch and handle Stripe events.
"""
import logging
from datetime import datetime

import stripe
from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.customer_billing.stripe_event_handlers import StripeEventHandler

logging.getLogger('stripe').setLevel(logging.INFO)


class Command(BaseCommand):
    """
    Command to fetch recent Stripe events and process them with StripeEventHandler.

    This command can be used to:
    - Fetch and process recent events of a specific type
    - Process a specific number of events
    - Handle missed events or reprocess existing ones
    """
    help = 'Fetch and handle recent Stripe events'

    def add_arguments(self, parser):
        parser.add_argument(
            '--event-type',
            type=str,
            help='Specific event type to fetch (e.g., "invoice.paid")',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=10,
            help='Number of events to fetch (default: 10, max: 100)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show events that would be processed without actually processing them',
        )

    def handle(self, *args, **options):
        event_type = options.get('event_type')
        limit = options['limit']
        dry_run = options['dry_run']

        # Validate limit
        if limit < 1 or limit > 100:
            raise CommandError('Limit must be between 1 and 100')

        # Build parameters for Stripe API call
        stripe_params = {'limit': limit}
        if event_type:
            stripe_params['type'] = event_type

        try:
            # Fetch events from Stripe
            self.stdout.write(f'Fetching {limit} events from Stripe...')
            if event_type:
                self.stdout.write(f'Filtering by event type: {event_type}')

            events = stripe.Event.list(**stripe_params)

            if not events.data:
                self.stdout.write(self.style.WARNING('No events found'))
                return

            self.stdout.write(f'Found {len(events.data)} events')

            if dry_run:
                self.stdout.write(self.style.WARNING('DRY RUN - Events would be processed:'))
                for event in events.data:
                    self.stdout.write(
                        f"  - ID: {event.id}, Type: {event.type}, "
                        f"Created: {datetime.fromtimestamp(event.created).isoformat()}, "
                        f"Customer id: {event.data.object.get('customer')}, "
                        f"Customer email: {event.data.object.get('customer_email')}"
                    )
                return

            # Process each event
            processed_count = 0
            error_count = 0

            for event in events.data:
                try:
                    self.stdout.write(f'Processing event {event.id} ({event.type})...')
                    StripeEventHandler.dispatch(event)
                    processed_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(f'Successfully processed event {event.id}')
                    )
                except Exception as e:  # pylint: disable=broad-exception-caught
                    error_count += 1
                    self.stdout.write(
                        self.style.ERROR(f'Error processing event {event.id}: {str(e)}')
                    )

            # Summary
            self.stdout.write('\nProcessing complete:')
            self.stdout.write(f'  Successfully processed: {processed_count}')
            if error_count > 0:
                self.stdout.write(self.style.WARNING(f'  Errors: {error_count}'))

        except stripe.error.StripeError as e:
            raise CommandError(f'Stripe API error: {str(e)}') from e
        except Exception as e:
            raise CommandError(f'Unexpected error: {str(e)}') from e
