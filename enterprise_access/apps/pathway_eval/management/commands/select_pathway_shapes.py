"""
Management command: pick the judge's best pathway per shape tier from a variant collection.

Reads what ``collect_pathway_variants`` exported and issues no calls, so it can be re-run
against the same collection as often as the selection rule changes. See ``shape_review``.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval.shape_review import SHAPE_TIERS, select_all, write_selection_csv
from enterprise_access.apps.pathway_eval.variant_collection import load_checkpoint
from enterprise_access.apps.pathways.judging import JUDGE_RUBRICS, RUBRIC_V1


class Command(BaseCommand):
    """
    Pick, per career, the best judged pathway in each of the four review shapes.

    Offline: reads a collection's JSON export or checkpoint, writes the picks.
    """

    help = (
        'Pick the judge\'s best pathway per shape tier (intro 2, intermediate 2, full ladder, '
        'another shape) from a collect_pathway_variants export. Issues no calls.'
    )

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument('--input-json', help='A collect_pathway_variants --output-json export.')
        source.add_argument('--checkpoint', help='A collect_pathway_variants --checkpoint file.')
        parser.add_argument(
            '--rubric', choices=JUDGE_RUBRICS, default=RUBRIC_V1,
            help='The judge rubric to rank by: v1 (default) or v2, which the collection must '
                 'have judged with --judge-rubric v2. Picks record the rubric that chose them.',
        )
        parser.add_argument('--output-json', help='Write every career\'s picks in full to this path.')
        parser.add_argument('--output-csv', help='Write one row per career and tier to this path.')

    def handle(self, *args, **options):
        runs = self._load_runs(options)
        if not runs:
            raise CommandError('The collection holds no runs.')
        rubric = options.get('rubric') or RUBRIC_V1
        selections = select_all(runs, rubric)
        self._render(selections, rubric)

        if options.get('output_json'):
            path = Path(options['output_json'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'rubric': rubric, 'selections': selections}, indent=2, sort_keys=True))
            self.stdout.write(f'  wrote {len(selections)} career(s) to {path}')
        if options.get('output_csv'):
            path = Path(options['output_csv'])
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = write_selection_csv(selections, path)
            self.stdout.write(f'  wrote {rows} row(s) to {path}')

    @staticmethod
    def _load_runs(options) -> list[dict]:
        """Exported runs as dicts, from whichever source was given."""
        if options.get('checkpoint'):
            return [run.to_dict() for run in load_checkpoint(options['checkpoint']).values()]
        try:
            data = json.loads(Path(options['input_json']).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise CommandError(f'Could not read --input-json: {exc}') from exc
        return list(data.get('runs') or [])

    def _render(self, selections, rubric):
        """One line per career: each tier's verdict and level mix, or a dash."""
        write = self.stdout.write
        write('')
        write('PATHWAY SHAPE PICKS  (best judged candidate per tier; a pick is best-of-N, not a rate)')
        write(f'  ranked by judge rubric {rubric}')
        write('  ' + f'{"career":<30}' + ''.join(f'{tier.key:>18}' for tier in SHAPE_TIERS))
        write('=' * (32 + 18 * len(SHAPE_TIERS)))
        counts = {tier.key: {} for tier in SHAPE_TIERS}
        for selection in selections:
            cells = []
            for tier in selection['tiers']:
                pick = tier['pick']
                verdict = pick['verdict'] if pick else 'none'
                counts[tier['tier']][verdict] = counts[tier['tier']].get(verdict, 0) + 1
                cells.append(f'{(verdict + " " + pick["level_mix"]) if pick else "-":>18}')
            write(f'  {selection["career"][:30]:<30}' + ''.join(cells))
        write('=' * (32 + 18 * len(SHAPE_TIERS)))
        for tier in SHAPE_TIERS:
            tally = ', '.join(f'{verdict} {n}' for verdict, n in sorted(counts[tier.key].items()))
            write(f'  {tier.label:<26} {tally}')
