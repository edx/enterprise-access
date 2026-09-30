"""
Management command: freeze the active editorial policy to a JSON file.

An experiment that records the policy it ran under can be replayed exactly later, after
the admin rows have moved on. Read it back with ``EditorialPolicy.from_dict``.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_editorial.api import load_policy


class Command(BaseCommand):
    """Write ``load_policy().to_dict()`` to ``--output`` as JSON."""

    help = (
        'Write the active pathway editorial policy (exclusions, flagships, promoted topics) '
        'to a JSON file, for a frozen, reproducible experiment snapshot.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True, help='Path of the JSON file to write.')

    def handle(self, *args, **options):
        policy = load_policy()
        path = Path(options['output'])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(policy.to_dict(), indent=2, sort_keys=True) + '\n', encoding='utf-8')
        except OSError as exc:
            raise CommandError(f'Could not write {path}: {exc}') from exc
        self.stdout.write(
            f'Wrote editorial policy to {path}: {len(policy.excluded_keys)} exclusion(s), '
            f'{len(policy.flagships)} flagship(s), {len(policy.promoted)} promoted topic(s).'
        )
