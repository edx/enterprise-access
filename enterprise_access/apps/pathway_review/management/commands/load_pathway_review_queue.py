"""
Load the human-review queue produced by the offline pathway measurement scripts.

Takes the ladder items from a queue file and upserts one :class:`PathwayReviewItem` per
pathway, splitting each record in two: the ``payload`` a reviewer's browser may see, and the
blinding fields (``pool``, ``control_key``) it may not. Program matches in the same file are
ignored -- they are reviewed separately.

Each item's payload is self-contained, descriptions included, so the bench serves one pathway
per request instead of shipping the whole queue to the browser.

The reading and writing live in :mod:`enterprise_access.apps.pathway_review.queue_loading`,
which the admin's upload page calls as well, so a queue file means the same thing whichever
door it comes through.

    ./manage.py load_pathway_review_queue --path /path/to/queue.json [--dry-run]
"""

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_review.queue_loading import PROBLEMS_SHOWN, QueueError, load_queue_file


class Command(BaseCommand):
    """ Management command to load or refresh the pathway review queue. """

    help = 'Load the pathway review queue from a queue JSON file produced offline.'

    def add_arguments(self, parser):
        parser.add_argument('--path', required=True, help='Path to the queue JSON file')
        parser.add_argument(
            '--deactivate-missing', action='store_true',
            help='Mark items absent from this file inactive rather than leaving them in the queue.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change, then roll it back.',
        )

    def handle(self, *args, **options):
        try:
            with open(options['path'], encoding='utf-8') as file_handle:
                raw = file_handle.read()
        except OSError as exc:
            raise CommandError(f'Could not read queue file: {exc}') from exc

        try:
            report = load_queue_file(
                raw,
                deactivate_missing=options['deactivate_missing'],
                dry_run=options['dry_run'],
            )
        except QueueError as exc:
            for problem in exc.problems[:PROBLEMS_SHOWN]:
                self.stderr.write(f'  {problem}')
            if len(exc.problems) > PROBLEMS_SHOWN:
                self.stderr.write(f'  ... and {len(exc.problems) - PROBLEMS_SHOWN} more')
            raise CommandError(str(exc)) from exc

        prefix = 'Would load: ' if report['dry_run'] else ''
        self.stdout.write(
            f'{prefix}{report["created"]} created, {report["updated"]} updated, '
            f'{report["deactivated"]} deactivated. '
            f'{report["controls"]} seeded controls in the queue.'
        )
        if report['deactivated_with_votes']:
            self.stdout.write(self.style.WARNING(
                f'  {report["deactivated_with_votes"]} deactivated item(s) already carried votes.'
            ))
        if report['would_deactivate']:
            self.stdout.write(
                f'  {report["would_deactivate"]} item(s) are not in this file and were left active; '
                'pass --deactivate-missing to retire them.'
            )
