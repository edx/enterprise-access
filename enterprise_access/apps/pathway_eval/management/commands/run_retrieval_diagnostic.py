"""
Management command for the retrieval diagnostic.

Read-only. Issues Algolia searches and writes no database records, so it is safe to
run against production indexes.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.api_client.algolia_client import AlgoliaClientError, AlgoliaSearchClient
from enterprise_access.apps.pathway_eval.personas import PersonaValidationError, load_personas
from enterprise_access.apps.pathway_eval.retrieval_diagnostic import (
    DEFAULT_TOP_N,
    Outcome,
    RetrievalDiagnostic,
    summarize,
    validate_customer_uuid
)


class Command(BaseCommand):
    """
    Report whether expert-picked courses appear in Algolia's top N for each persona.

    The output of this command is a *decision*, not a number: it fills in the gate table
    that determines whether re-ranking is aimed at the right layer.
    """

    help = (
        'Run the learner-pathway retrieval diagnostic over the persona set and report, '
        'per persona and per technology split, whether expected courses are retrieved.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--persona-dir',
            help='Directory of persona YAML files. Defaults to the bundled fixtures.',
        )
        parser.add_argument(
            '--persona-id',
            action='append',
            dest='persona_ids',
            help='Restrict the run to this persona id. Repeatable.',
        )
        parser.add_argument(
            '--top-n',
            type=int,
            default=DEFAULT_TOP_N,
            help=f'How many hits count as "retrieved" (default: {DEFAULT_TOP_N}).',
        )
        parser.add_argument(
            '--unscoped',
            action='store_true',
            help=(
                'Use the plain search key instead of a secured key. This governs the '
                'CREDENTIAL, not the filter: a secured key is vended per request from a '
                'user token, so a management command cannot obtain one and needs this to '
                'search at all. Also requires ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH. '
                'Combine with --customer-uuid to scope by filter instead.'
            ),
        )
        parser.add_argument(
            '--customer-uuid',
            help=(
                'Scope catalog searches to this enterprise customer UUID. Needs no '
                'secured key -- enterprise_customer_uuids is a facetable attribute. The '
                'scope is verified before the run, because a wrong-but-well-formed UUID '
                'matches nothing and would report 0%% recall rather than an error.'
            ),
        )
        parser.add_argument(
            '--relax-query',
            action='store_true',
            help=(
                'Send removeWordsIfNoResults=allOptional. The catalog index ANDs every '
                'query word, so a verbose query returns zero hits rather than poor ones. '
                'Run the diagnostic both ways: the delta measures how much of the quality '
                'problem is query construction rather than ranking.'
            ),
        )
        parser.add_argument(
            '--output-json',
            help='Write the full per-persona results to this path as JSON.',
        )

    def handle(self, *args, **options):
        # Validated first, and deliberately before any client is built: a bad argument
        # should not be masked by missing credentials.
        try:
            customer_uuid = validate_customer_uuid(options.get('customer_uuid'))
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
            raise CommandError('No personas found; nothing to diagnose.')

        try:
            diagnostic = RetrievalDiagnostic(
                algolia_client=AlgoliaSearchClient(),
                top_n=options['top_n'],
                allow_unscoped=options['unscoped'],
                relax_query=options['relax_query'],
                customer_uuid=customer_uuid,
            )
            scoped_courses = self._verify_scope(diagnostic)
            results = diagnostic.run(personas)
        except AlgoliaClientError as exc:
            raise CommandError(f'Algolia is not usable: {exc}') from exc

        summary = summarize(results)
        self._render(results, summary, options, scoped_courses)

        if options.get('output_json'):
            self._write_json(results, summary, Path(options['output_json']))

    def _verify_scope(self, diagnostic):
        """
        Confirm the scope holds courses, and fail loudly when it does not.

        An empty scope produces a full run of zeroes that reads exactly like a genuine
        retrieval failure, so it has to be an error rather than a caveat in the report.
        """
        if diagnostic.customer_uuid is None:
            return None
        scoped_courses = diagnostic.count_scoped_courses()
        if not scoped_courses:
            raise CommandError(
                f'Enterprise customer {diagnostic.customer_uuid} has no courses in the '
                'catalog index. Every persona would score zero, which is not a result. '
                'Check that this is a customer UUID and not a catalog or catalog-query UUID.'
            )
        return scoped_courses

    def _render(self, results, summary, options, scoped_courses=None):
        """Print the human-readable report."""
        write = self.stdout.write
        top_n = options['top_n']

        write('')
        write(f'RETRIEVAL DIAGNOSTIC  (top_n={top_n}, '
              f'relax_query={"on" if options["relax_query"] else "off"})')
        if options.get('customer_uuid'):
            write(f'Scoped to enterprise customer {options["customer_uuid"]} '
                  f'({scoped_courses} courses in scope).')
        if not options['relax_query']:
            write(
                'Queries use the index default (every word ANDed). Re-run with '
                '--relax-query to measure how much recall is lost to query construction.'
            )
        if options['unscoped'] and not options.get('customer_uuid'):
            write(self.style.WARNING(
                'Catalog searches were UNSCOPED. Results are not restricted to any '
                'enterprise catalog, so they are an upper bound on what a real learner sees.'
            ))
        elif options.get('customer_uuid'):
            write(self.style.WARNING(
                'Scoped by FILTER, not by secured key. Adequate for a read-only '
                'diagnostic; not a substitute for a secured key on request-scoped '
                'production traffic, where a forgotten filter would leak catalog breadth.'
            ))
        write('=' * 78)

        for result in results:
            self._render_persona(result, top_n)

        write('')
        write('=' * 78)
        write('SUMMARY')
        write(f'  personas: {summary["total_personas"]} '
              f'({summary["expert_authored_personas"]} expert-authored, '
              f'{summary["placeholder_personas"]} placeholder)')

        for split_name in ('expert_authored_only', 'technology', 'non_technology'):
            stats = summary[split_name]
            recall = stats['mean_recall_at_top_n']
            recall_text = f'{recall:.0%}' if recall is not None else 'n/a (nothing scoreable)'
            write(f'  {split_name:<22} personas={stats["personas"]:<3} '
                  f'scoreable={stats["scoreable"]:<3} mean recall={recall_text}')
            for outcome, count in stats['outcomes'].items():
                write(f'      {outcome}: {count}')

        if summary['errors']:
            write(self.style.ERROR(f'  {len(summary["errors"])} error(s) occurred:'))
            for error in summary['errors'][:10]:
                write(f'      {error}')

        write('')
        write(self.style.WARNING(
            'GATE: record the decision (proceed / re-aim / re-scope) in project-log.md. '
            'A green run is not the deliverable -- the logged decision is.'
        ))
        if summary['expert_authored_personas'] == 0:
            write(self.style.ERROR(
                'No persona carries expert-authored ground truth, so this run cannot '
                'clear the gate. It only proves the diagnostic works.'
            ))
        write('')

    def _render_persona(self, result, top_n):
        """Print one persona's strategies, per-course verdicts and outcome."""
        write = self.stdout.write

        write('')
        label = f'{result.persona_id}  [{result.domain}/{result.tier}]'
        if result.ground_truth_status != 'expert_authored':
            label += '  (placeholder ground truth)'
        write(label)

        for strategy in result.strategy_results:
            if strategy.error:
                write(f'    {strategy.strategy:<22} ERROR: {strategy.error}')
                continue
            rank = strategy.best_rank
            position = f'first expected at rank {rank}' if rank else 'no expected course retrieved'
            write(f'    {strategy.strategy:<22} {len(strategy.returned_keys):>3} hits, {position}')
            write(f'      query: {strategy.query[:90]!r}')

        if result.expected_course_keys:
            retrieved = result.retrieved_keys
            for key in result.expected_course_keys:
                if key in retrieved:
                    mark = self.style.SUCCESS('RETRIEVED')
                elif result.probe_found.get(key):
                    mark = self.style.WARNING('in index, not retrieved')
                else:
                    mark = self.style.ERROR('not found by probe')
                write(f'      - {key:<34} {mark}')
            recall = result.recall_at_top_n
            if recall is not None:
                write(f'    recall@{top_n}: {recall:.0%}')

        if result.incidental_hits:
            keys = ', '.join(sorted({hit['key'] for hit in result.incidental_hits})[:5])
            write(f'    returned anyway (expected no coverage): {keys}')

        write(f'    OUTCOME: {result.outcome} -- {Outcome.CONSEQUENCES[result.outcome]}')

    def _write_json(self, results, summary, path):
        """Write the machine-readable trace."""
        payload = {
            'summary': summary,
            'personas': [
                {
                    'persona_id': result.persona_id,
                    'domain': result.domain,
                    'tier': result.tier,
                    'is_technology': result.is_technology,
                    'ground_truth_status': result.ground_truth_status,
                    'expected_course_keys': result.expected_course_keys,
                    'outcome': result.outcome,
                    'best_rank': result.best_rank,
                    'recall_at_top_n': result.recall_at_top_n,
                    'probe_found': result.probe_found,
                    'incidental_hits': result.incidental_hits,
                    'errors': result.errors,
                    'strategies': [
                        {
                            'strategy': strategy.strategy,
                            'query': strategy.query,
                            'returned_keys': strategy.returned_keys,
                            'matched_ranks': strategy.matched_ranks,
                            'error': strategy.error,
                        }
                        for strategy in result.strategy_results
                    ],
                }
                for result in results
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        self.stdout.write(f'Wrote {path}')
