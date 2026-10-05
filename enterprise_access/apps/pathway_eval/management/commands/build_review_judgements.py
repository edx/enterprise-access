"""
Management command: turn the bench's votes into the course-judgement fixture pathways are scored on.

Offline and re-runnable: reads the bench database read-only and the shape review's career list and
blind key, and writes two files -- the fixture and the anonymous votes export it was built from,
whose sha256 the fixture records. See ``judgement_scoring``.
"""
import json
import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval.judgement_scoring import (
    BLIND_KEY_FILE,
    CALIBRATION_CAREERS,
    CAREERS_FILE,
    DEFAULT_REVIEWER_LABEL,
    SPLITS,
    annotate_careers,
    build_fixture,
    item_careers,
    load_bench_votes,
    read_careers,
    sha256_bytes,
    votes_export_bytes
)


class Command(BaseCommand):
    """Build ``fixtures/review_judgements/*.json`` from a Pathway Review Bench database. Issues no calls."""

    help = (
        'Build the review-judgements fixture (endorsed, rejected and contested courses per career and '
        'level, with a calibration/held-out split) from a Pathway Review Bench database. Offline.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--db', required=True, help='The bench\'s sqlite database; opened read-only.')
        parser.add_argument('--shape-review-dir', required=True,
                            help=f'The directory holding {CAREERS_FILE} (careers in rank order) and '
                                 f'{BLIND_KEY_FILE} (round-2 item -> career).')
        parser.add_argument('--reviewer', required=True,
                            help='The bench username whose votes to read. Not written to the output.')
        parser.add_argument('--reviewer-label', default=DEFAULT_REVIEWER_LABEL,
                            help='The anonymous name the judgements are recorded under.')
        parser.add_argument('--calibration-careers', type=int, default=CALIBRATION_CAREERS,
                            help='How many top-ranked careers form the calibration split; the rest are held out.')
        parser.add_argument('--output', required=True, help='Where to write the fixture (JSON).')
        parser.add_argument('--votes-export',
                            help='Where to write the anonymous votes export. Default: beside the fixture, '
                                 'as <name>.votes.json.')

    def handle(self, *args, **options):
        db = Path(options['db'])
        if not db.is_file():
            raise CommandError(f'No database at {db}.')
        directory = Path(options['shape_review_dir'])
        try:
            careers_bytes = (directory / CAREERS_FILE).read_bytes()
            key_bytes = (directory / BLIND_KEY_FILE).read_bytes()
            careers = read_careers(directory / CAREERS_FILE)
            blind_key = json.loads(key_bytes)
        except (OSError, ValueError) as exc:
            raise CommandError(f'Could not read the shape review: {exc}') from exc
        try:
            votes = load_bench_votes(db, options['reviewer'])
        except (sqlite3.Error, ValueError) as exc:
            raise CommandError(f'Could not read votes from {db}: {exc}') from exc
        if not votes:
            raise CommandError('The database holds no votes by that reviewer.')

        fixture = build_fixture(
            votes=votes,
            careers_in_order=careers,
            blind_key_items=blind_key.get('items') or {},
            sources={CAREERS_FILE: sha256_bytes(careers_bytes), BLIND_KEY_FILE: sha256_bytes(key_bytes)},
            reviewer_label=options['reviewer_label'],
            calibration_careers=options['calibration_careers'],
        )
        fixture['built_from']['database'] = db.name

        output = Path(options['output'])
        export = Path(options['votes_export']) if options.get('votes_export') else \
            output.with_name(f'{output.stem}.votes.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        export.parent.mkdir(parents=True, exist_ok=True)
        careers_by_item = item_careers(careers, blind_key.get('items') or {})
        export.write_bytes(votes_export_bytes(annotate_careers(votes, careers_by_item)))
        output.write_text(json.dumps(fixture, indent=1, sort_keys=True) + '\n', encoding='utf-8')
        self._render(fixture, output, export)

    def _render(self, fixture, output, export):
        """The headline counts."""
        counts = fixture['built_from']['counts']
        write = self.stdout.write
        write('REVIEW JUDGEMENTS')
        write(f'  votes {counts["votes"]} ({counts["votes_with_a_career"]} with a career, by round '
              f'{counts["votes_by_round"]})  careers {counts["careers"]} ({counts["careers_with_judgements"]} '
              f'with judgements)  slots {counts["slots"]}')
        write(f'  endorsed {counts["endorsed"]}  rejected {counts["rejected"]}  contested {counts["contested"]}  '
              f'unplaced {counts["unplaced_judgements"]}')
        for split in SPLITS:
            part = counts[split]
            write(f'  {split:<12} careers {part["careers"]:>3}  slots {part["slots"]:>3}  endorsed '
                  f'{part["endorsed"]:>4}  rejected {part["rejected"]:>3}  contested {part["contested"]:>3}')
        write(f'  wrote {output} and {export}')
