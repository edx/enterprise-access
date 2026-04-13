"""
Management command to backfill LearnerCreditRequestActions status values
to align with the action-based pattern used in content assignments.
"""

import logging

from django.core.management.base import BaseCommand

from enterprise_access.apps.subsidy_request.constants import LearnerCreditRequestUserMessages
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequestActions

logger = logging.getLogger(__name__)

# Mapping from recent_action values to the correct user-facing status values.
ACTION_TO_STATUS_MAPPING = dict(LearnerCreditRequestUserMessages.CHOICES)


class Command(BaseCommand):
    """
    Backfill the ``status`` field on LearnerCreditRequestActions records
    to ensure each record's status matches the expected user-facing message
    for its ``recent_action`` value.

    Usage:
        # Preview changes without modifying any data:
        python manage.py backfill_learner_credit_request_action_statuses --dry-run

        # Apply the backfill:
        python manage.py backfill_learner_credit_request_action_statuses
    """
    help = (
        'Backfill LearnerCreditRequestActions status values to align with '
        'the action-based pattern from content assignments.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry_run',
            default=False,
            help='Preview changes without modifying any data.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no records will be modified.\n'))

        total_updated = 0

        for action_key, expected_status in ACTION_TO_STATUS_MAPPING.items():
            # Find records whose recent_action matches but whose status
            # does NOT already have the expected value.
            mismatched_qs = LearnerCreditRequestActions.objects.filter(
                recent_action=action_key,
            ).exclude(
                status=expected_status,
            )
            count = mismatched_qs.count()

            if count == 0:
                self.stdout.write(
                    f'  {action_key} → "{expected_status}": 0 records to update (all correct)'
                )
                continue

            if dry_run:
                self.stdout.write(
                    self.style.NOTICE(
                        f'  {action_key} → "{expected_status}": {count} record(s) would be updated'
                    )
                )
            else:
                updated = mismatched_qs.update(status=expected_status)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'  {action_key} → "{expected_status}": {updated} record(s) updated'
                    )
                )
                count = updated

            total_updated += count

        # Handle any records whose recent_action doesn't have a direct mapping
        # (e.g. 'pending', 'error') — these don't have entries in
        # LearnerCreditRequestUserMessages, so flag them for review.
        mapped_actions = set(ACTION_TO_STATUS_MAPPING.keys())
        unmapped_qs = LearnerCreditRequestActions.objects.exclude(
            recent_action__in=mapped_actions,
        )
        unmapped_count = unmapped_qs.count()
        if unmapped_count:
            self.stdout.write(
                self.style.WARNING(
                    f'\n  {unmapped_count} record(s) have a recent_action with no status mapping '
                    f'and were skipped. Review these manually.'
                )
            )

        summary_style = self.style.WARNING if dry_run else self.style.SUCCESS
        verb = 'would be' if dry_run else 'were'
        self.stdout.write(
            summary_style(f'\nDone. {total_updated} record(s) {verb} updated.')
        )
