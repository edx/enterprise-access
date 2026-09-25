"""
Management command: run a list of careers through the pathway experiments and export them.

Issues paid model calls in volume, so ``--dry-run`` and ``--max-calls`` behave exactly as
they do for ``run_pathway_harness``: the limit is checked before each career, against an
upper bound of what that career can cost, so it is never overshot.
"""
import json
from functools import partial
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval.retrieval_diagnostic import validate_customer_uuid
from enterprise_access.apps.pathway_eval.variant_collection import (
    VariantCollector,
    append_checkpoint,
    load_career_names,
    load_checkpoint,
    summarise,
    write_csv
)
from enterprise_access.apps.pathways.course_retrieval import eval_customer_uuid
from enterprise_access.apps.pathways.pathway_variants import MAX_PATHWAY_SIZE, MIN_PATHWAY_SIZE, VARIANT_STRATEGIES


class Command(BaseCommand):
    """
    Collect pathway size variants, and optionally judge scores, for a list of careers.

    Skips intake and career discovery: each career is looked up by exact name in the jobs
    index and run straight through pathway assembly. Produces traces, not a verdict.
    """

    help = (
        'Run careers through the pathway size-variant experiments and export every pathway, '
        'with its judge score when --judge is set, as JSON and/or CSV.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--career', action='append', dest='careers', default=[],
            help='A career name, matched exactly (case-insensitive). Repeatable.',
        )
        parser.add_argument(
            '--careers-file',
            help='Text file of career names, one per line, or a CSV with a career_name, '
                 'career, name, label or "career family" column.',
        )
        parser.add_argument(
            '--limit', type=int, help='Run only the first N careers.',
        )
        parser.add_argument(
            '--variant-size', action='append', dest='variant_sizes', type=int,
            help=f'Pathway size to build ({MIN_PATHWAY_SIZE}-{MAX_PATHWAY_SIZE}). Repeatable; '
                 'defaults to every size when a strategy is given.',
        )
        parser.add_argument(
            '--variant-strategy', action='append', dest='variant_strategies',
            choices=VARIANT_STRATEGIES,
            help='Strategy to build variants with. Repeatable; defaults to ranked_cut when '
                 'only sizes are given. model_pick and model_sized issue paid calls.',
        )
        parser.add_argument(
            '--judge', action='store_true',
            help='Score the delivered pathway and every variant with the model judge.',
        )
        parser.add_argument(
            '--no-rerank', action='store_true',
            help='Disable the model re-rank, so ranked_cut and the delivered pathway use '
                 'retrieval order.',
        )
        parser.add_argument(
            '--enrich', action='store_true',
            help='Also generate per-course rationales for the delivered pathway (an Xpert '
                 'call). Off by default: rationales are not what this collects.',
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
            '--max-calls', type=int,
            help='Stop before exceeding this many paid model calls (an upper bound per '
                 'career, checked before it starts).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report the plan and its cost bound, and issue no calls.',
        )
        parser.add_argument(
            '--checkpoint',
            help='Append each career to this JSON-lines file the moment it finishes, so an '
                 'interrupted collection keeps what it has already paid for.',
        )
        parser.add_argument(
            '--resume', action='store_true',
            help='Carry over the careers already completed in --checkpoint instead of '
                 're-running them. Errors and budget skips are retried.',
        )
        parser.add_argument('--output-json', help='Write every run in full to this path.')
        parser.add_argument('--output-csv', help='Write one row per pathway to this path.')

    def handle(self, *args, **options):
        try:
            customer_uuid = validate_customer_uuid(
                options.get('customer_uuid') or eval_customer_uuid() or None,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        names = list(options['careers'])
        if options.get('careers_file'):
            try:
                names += load_career_names(options['careers_file'])
            except OSError as exc:
                raise CommandError(f'Could not read --careers-file: {exc}') from exc
        names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
        if options.get('limit') is not None:
            if options['limit'] < 1:
                raise CommandError('--limit must be at least 1.')
            names = names[:options['limit']]
        if not names:
            raise CommandError('No careers given; use --career or --careers-file.')
        if options['resume'] and not options.get('checkpoint'):
            raise CommandError('--resume needs --checkpoint.')
        done = load_checkpoint(options['checkpoint']) if options['resume'] else {}
        on_run = None
        if options.get('checkpoint') and not options['dry_run']:
            checkpoint = Path(options['checkpoint'])
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            on_run = partial(append_checkpoint, checkpoint)

        try:
            collector = VariantCollector(
                variant_sizes=options.get('variant_sizes'),
                variant_strategies=options.get('variant_strategies'),
                judge_enabled=options['judge'],
                rerank_enabled=not options['no_rerank'],
                enrich_enabled=options['enrich'],
                customer_uuid=customer_uuid or '',
                allow_unscoped=options['unscoped'],
                max_calls=options.get('max_calls'),
                dry_run=options['dry_run'],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        result = collector.run(names, done=done, on_run=on_run)
        self._render(result, collector, customer_uuid, options)

        if options.get('output_json'):
            path = Path(options['output_json'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._payload(result, collector, customer_uuid, options),
                                       indent=2, sort_keys=True))
            self.stdout.write(f'  wrote {len(result["runs"])} run(s) to {path}')
        if options.get('output_csv'):
            path = Path(options['output_csv'])
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = write_csv(result['runs'], path)
            self.stdout.write(f'  wrote {rows} pathway row(s) to {path}')

    def _render(self, result, collector, customer_uuid, options):
        """Print the plan, one line per career, and counts per pathway label."""
        write = self.stdout.write
        runs = result['runs']
        write('')
        write('PATHWAY VARIANT COLLECTION' + ('  (DRY RUN -- no calls issued)' if options['dry_run'] else ''))
        write(f'  careers: {len(runs)}  strategies: {collector.strategies or "none"}  '
              f'sizes: {collector.sizes or "none"}  judge: {collector.judge_enabled}')
        write(f'  up to {collector.calls_per_career} paid model call(s) per career; '
              f'{collector.calls_per_career * len(runs)} for the whole list')
        if not customer_uuid:
            write(self.style.WARNING(
                '  NOT scoped to any enterprise customer -- results are an upper bound '
                'on what a real learner sees.'
            ))
        write('=' * 78)
        for run in runs:
            if run.skipped_reason:
                write(f'  {run.requested_name:<40} SKIPPED  ({run.skipped_reason})')
            elif run.error:
                write(self.style.ERROR(f'  {run.requested_name:<40} ERROR    {run.error}'))
            else:
                verdict = (run.judgement or {}).get('verdict') or '-'
                write(f'  {run.requested_name:<40} default={verdict:<5} variants={len(run.variants)}')
        write('=' * 78)

        summary = summarise(runs)
        if summary:
            write(f'  {"label":<18} {"pathways":>8} {"complete":>8} {"mean len":>8} '
                  f'{"good":>5} {"weak":>5} {"bad":>5} {"unjudged":>8}')
            for row in summary:
                mean = row['courses'] / row['pathways'] if row['pathways'] else 0
                write(f'  {row["label"]:<18} {row["pathways"]:>8} {row["complete"]:>8} {mean:>8.2f} '
                      f'{row["good"]:>5} {row["weak"]:>5} {row["bad"]:>5} {row["unjudged"]:>8}')
        if result.get('resumed'):
            write(f'  resumed from checkpoint (not re-run, not charged): {result["resumed"]}')
        write(f'  model calls charged (upper bound): {result["calls_charged"]}')
        if result['budget_exhausted']:
            write(self.style.WARNING('  --max-calls was reached; the remaining careers were not started.'))

    @staticmethod
    def _payload(result, collector, customer_uuid, options):
        """The JSON export: configuration, summary, and every run in full."""
        return {
            'run_config': {
                'variant_sizes': collector.sizes,
                'variant_strategies': collector.strategies,
                'judge_enabled': collector.judge_enabled,
                'rerank_enabled': collector.rerank_enabled,
                'enrich_enabled': collector.enrich_enabled,
                'customer_uuid': customer_uuid or '',
                'dry_run': options['dry_run'],
                'model_backend': settings.PATHWAYS_MODEL_BACKEND,
                'variant_backend': settings.PATHWAYS_VARIANT_BACKEND or settings.PATHWAYS_MODEL_BACKEND,
                'judge_model': settings.PATHWAYS_JUDGE_MODEL if collector.judge_enabled else '',
            },
            'calls_charged': result['calls_charged'],
            'budget_exhausted': result['budget_exhausted'],
            'resumed': result.get('resumed', 0),
            'summary': summarise(result['runs']),
            'runs': [run.to_dict() for run in result['runs']],
        }
