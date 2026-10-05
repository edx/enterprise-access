"""
Management command: score exported pathways against the reviewer's course judgements.

Offline: reads a ``collect_pathway_variants`` export, a collection checkpoint or a
``replay_shape_review`` checkpoint, and the committed review-judgements fixture, and prints the
rejected rate, clean rate and endorsed share per group. Issues no calls. See ``judgement_scoring``.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval.judgement_scoring import (
    DEFAULT_FIXTURE,
    GROUP_BY,
    GROUP_BY_LABEL,
    ReviewJudgements,
    load_runs,
    pathways_from_runs,
    score_groups
)
from enterprise_access.apps.pathway_eval.shape_review import SHAPE_TIERS


def _rate(value) -> str:
    return '   -  ' if value is None else f'{value:6.3f}'


class Command(BaseCommand):
    """Score pathways in exported runs against the committed course judgements."""

    help = (
        'Score the pathways in collect_pathway_variants exports, collection checkpoints or replay '
        'checkpoints against the reviewer\'s course judgements: rejected rate, clean rate, endorsed '
        'share. Offline.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--input', action='append', required=True, metavar='[NAME=]PATH',
            help='A collect_pathway_variants --output-json export, a collection checkpoint, or a '
                 'replay_shape_review checkpoint. Repeatable; NAME labels its rows (default: the file name).',
        )
        parser.add_argument('--label', action='append', default=[],
                            help='A variant label to score, as a shell pattern (shape_pick_v2:*); the '
                                 'delivered pathway is "default". Repeatable. Default: every label.')
        parser.add_argument('--tier', action='append', default=[], choices=[tier.key for tier in SHAPE_TIERS],
                            help='A shape tier to score, by the levels a pathway landed on. Repeatable. '
                                 'Default: every tier.')
        parser.add_argument('--group-by', choices=GROUP_BY, default=GROUP_BY_LABEL,
                            help='One row per label (default), per shape tier, or one row per input.')
        parser.add_argument('--by-split', action='store_true',
                            help='Add rows for the calibration and held-out careers apart.')
        parser.add_argument('--include-incomplete', action='store_true',
                            help='Also score pathways that fell short of their size or shape.')
        parser.add_argument('--fixture', default=str(DEFAULT_FIXTURE), help='The review-judgements fixture.')
        parser.add_argument('--output-json', help='Write every row to this path.')

    def handle(self, *args, **options):
        try:
            judgements = ReviewJudgements.load(options['fixture'])
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f'Could not read the fixture {options["fixture"]}: {exc}') from exc

        rows = []
        for spec in options['input']:
            name, path = self._parse_input(spec)
            try:
                runs = load_runs(path)
            except OSError as exc:
                raise CommandError(f'Could not read {path}: {exc}') from exc
            pathways = pathways_from_runs(
                runs, labels=options['label'], tiers=options['tier'],
                complete_only=not options['include_incomplete'],
            )
            for row in score_groups(pathways, judgements, group_by=options['group_by'],
                                    by_split=options['by_split']):
                rows.append({'input': name, **row})
        self._render(rows)

        if options.get('output_json'):
            path = Path(options['output_json'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'fixture': str(options['fixture']), 'rows': rows}, indent=1, sort_keys=True))
            self.stdout.write(f'  wrote {len(rows)} row(s) to {path}')

    @staticmethod
    def _parse_input(spec):
        """``NAME=PATH``, or a bare path named after its file."""
        name, sep, path = spec.partition('=')
        if not sep:
            name, path = Path(spec).name, spec
        if not Path(path).is_file():
            raise CommandError(f'No such input: {path}')
        return name, path

    def _render(self, rows):
        """One line per row."""
        write = self.stdout.write
        write(f'{"input":<22}{"group":<24}{"split":<12}{"paths":>6}{"rejected":>9}{"clean":>7}'
              f'{"endorsed":>9}{"known":>7}{"unknown":>8}{"contest":>8}')
        for row in rows:
            write(f'{row["input"][:21]:<22}{row["group"][:23]:<24}{row["split"]:<12}{row["pathways"]:>6}'
                  f'{_rate(row["rejected_rate"]):>9}{_rate(row["clean_rate"]):>7}{_rate(row["endorsed_share"]):>9}'
                  f'{row["courses_with_a_known_standing"]:>7}{row["unknown_courses"]:>8}{row["contested_courses"]:>8}')
        if not rows:
            write('  no pathways matched')
        unseen = sum(row['careers_unseen'] for row in rows if row['split'] == 'all')
        if unseen:
            write(f'  {unseen} pathway(s) belong to careers the fixture has no judgements on; their courses '
                  'are all unknown')
