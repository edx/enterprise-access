"""
Management command for the pathway evaluation harness.

Issues paid model calls in volume, so ``--dry-run`` and ``--max-calls`` are first-class
rather than conveniences, and the default is a *single* run of a *single* career mode --
the expensive shape has to be asked for explicitly.
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval.harness import CAREER_MODES, PathwayHarness
from enterprise_access.apps.pathway_eval.personas import PersonaValidationError, load_personas
from enterprise_access.apps.pathway_eval.retrieval_diagnostic import validate_customer_uuid
from enterprise_access.apps.pathways.course_retrieval import eval_customer_uuid


class Command(BaseCommand):
    """
    Run the persona set through career discovery and pathway assembly.

    Produces traces, not scores. ``report_pathway_harness`` turns the exported JSON into
    the Tier 1/2/3 report, so a re-score never needs a re-run -- which matters when a run
    costs money.
    """

    help = (
        'Run the learner-pathway persona set through the server-side pipeline and export '
        'per-cell traces as JSON for scoring.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--persona-dir',
            help='Directory of persona YAML files. Defaults to the bundled fixtures.',
        )
        parser.add_argument(
            '--persona-id', action='append', dest='persona_ids',
            help='Restrict the run to this persona id. Repeatable.',
        )
        parser.add_argument(
            '--runs', type=int, default=1,
            help='How many times to run each persona (default: 1). More than one is what '
                 'makes cross-run consistency measurable.',
        )
        parser.add_argument(
            '--career-mode', action='append', dest='career_modes', choices=CAREER_MODES,
            help='Career selection mode. Repeatable; defaults to both, which is what '
                 'separates career-selection error from course-retrieval error.',
        )
        parser.add_argument(
            '--customer-uuid',
            help='Enterprise customer to scope catalog searches to. Defaults to '
                 'PATHWAYS_EVAL_CUSTOMER_UUID.',
        )
        parser.add_argument(
            '--unscoped', action='store_true',
            help='Use the plain search key rather than a secured key. Required offline, '
                 'and also requires ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH.',
        )
        parser.add_argument(
            '--no-rerank', action='store_true',
            help='Disable the model re-rank step. This is the baseline arm of the A/B: '
                 'deterministic assembly alone still produces a valid pathway.',
        )
        parser.add_argument(
            '--no-enrich', action='store_true',
            help='Disable the per-course rationale step. Pathways still ship, unexplained.',
        )
        parser.add_argument(
            '--max-calls', type=int,
            help='Stop before exceeding this many workflow executions. Checked before '
                 'each cell, so the limit is never overshot.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report the cells that would run and issue no calls.',
        )
        parser.add_argument(
            '--output-json', help='Write the full per-cell traces to this path.',
        )

    def handle(self, *args, **options):
        try:
            customer_uuid = validate_customer_uuid(
                options.get('customer_uuid') or eval_customer_uuid() or None,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        try:
            personas = load_personas(
                fixture_dir=options.get('persona_dir'),
                persona_ids=options.get('persona_ids'),
            )
        except PersonaValidationError as exc:
            raise CommandError(str(exc)) from exc

        if not personas:
            raise CommandError('No personas found; nothing to run.')

        if options['runs'] < 1:
            raise CommandError('--runs must be at least 1.')

        harness = PathwayHarness(
            runs=options['runs'],
            career_modes=tuple(options.get('career_modes') or CAREER_MODES),
            max_calls=options.get('max_calls'),
            dry_run=options['dry_run'],
            customer_uuid=customer_uuid or '',
            allow_unscoped=options['unscoped'],
            rerank_enabled=not options['no_rerank'],
            enrich_enabled=not options['no_enrich'],
        )

        result = harness.run(personas)
        self._render(result, options, customer_uuid)

        if options.get('output_json'):
            self._write_json(result, options, customer_uuid, Path(options['output_json']))

    def _render(self, result, options, customer_uuid):
        """Print the human-readable summary."""
        write = self.stdout.write
        cells = result['cells']

        write('')
        write('PATHWAY HARNESS' + ('  (DRY RUN -- no calls issued)' if options['dry_run'] else ''))
        write(f'  personas: {result["personas_total"]}  runs: {options["runs"]}  '
              f'modes: {", ".join(options.get("career_modes") or CAREER_MODES)}')
        if customer_uuid:
            write(f'  scoped to enterprise customer {customer_uuid}')
        else:
            write(self.style.WARNING(
                '  NOT scoped to any enterprise customer -- results are an upper bound '
                'on what a real learner sees.'
            ))
        if not options['no_rerank'] and not options['dry_run']:
            write('  model re-rank: enabled (paid calls)')
        write('=' * 78)

        for cell in cells:
            self._render_cell(cell)

        write('')
        write('=' * 78)
        write(f'  cells: {len(cells)}   ran: {len([c for c in cells if c.ran])}   '
              f'skipped: {len([c for c in cells if c.skipped_reason])}   '
              f'errors: {len([c for c in cells if c.error])}')
        write(f'  workflow executions: {result["calls_made"]}')
        write(f'  personas completed: {result["personas_completed"]} of {result["personas_total"]}')
        if result['budget_exhausted']:
            write(self.style.WARNING(
                '  --max-calls was reached; the remaining cells were not started.'
            ))
        write('')
        write('This command produces traces, not a verdict. Run report_pathway_harness '
              'on the exported JSON for the Tier 1/2/3 report.')

    def _render_cell(self, cell):
        """Print one cell's line."""
        write = self.stdout.write
        label = f'{cell.persona_id:<22} {cell.career_mode:<7} run={cell.run_index}'

        if cell.skipped_reason:
            write(f'  {label}  SKIPPED  ({cell.skipped_reason})')
            return
        if cell.error:
            write(self.style.ERROR(f'  {label}  ERROR    {cell.error}'))
            return

        state = 'pathway' if cell.complete else 'no pathway'
        write(f'  {label}  {state:<11} career={cell.career_name!r} '
              f'courses={len(cell.course_keys)}')
        if cell.violations:
            write(self.style.ERROR(f'      TIER 1 VIOLATIONS: {"; ".join(cell.violations)}'))
        if cell.unfilled_rungs:
            write(f'      unfilled rungs: {", ".join(cell.unfilled_rungs)}')

    def _write_json(self, result, options, customer_uuid, path):
        """Export the traces for scoring."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'run_config': {
                'runs': options['runs'],
                'career_modes': list(options.get('career_modes') or CAREER_MODES),
                'customer_uuid': customer_uuid or '',
                'rerank_enabled': not options['no_rerank'],
                'enrich_enabled': not options['no_enrich'],
                'dry_run': options['dry_run'],
                'model_backend': settings.PATHWAYS_MODEL_BACKEND,
            },
            'calls_made': result['calls_made'],
            'budget_exhausted': result['budget_exhausted'],
            'personas_completed': result['personas_completed'],
            'personas_total': result['personas_total'],
            'cells': [cell.to_dict() for cell in result['cells']],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        self.stdout.write(f'  wrote traces to {path}')
