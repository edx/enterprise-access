"""Summarize a run_personalized_search_harness JSONL file as a side-by-side table (and JSON)."""
import json

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.personalized_search.core.harness_scoring import report_markdown, summarize


class Command(BaseCommand):
    """``report_personalized_search_harness``."""

    help = 'Per-arm summary of a Personalized Search harness run.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='JSONL written by run_personalized_search_harness.')
        parser.add_argument('--json', action='store_true', help='Print the summary as JSON instead of a table.')

    def handle(self, *args, **options):
        try:
            with open(options['path'], encoding='utf-8') as handle:
                records = [json.loads(line) for line in handle if line.strip()]
        except (OSError, ValueError) as exc:
            raise CommandError(f'Could not read {options["path"]}: {exc}') from exc
        if not records:
            raise CommandError('No records.')
        summary = summarize(records)
        self.stdout.write(json.dumps(summary, indent=1) if options['json'] else report_markdown(summary))
