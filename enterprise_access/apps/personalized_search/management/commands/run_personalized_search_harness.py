"""
Run Personalized Search turns through two or more model arms and write one JSONL row per turn.

Default arms are the decided OpenAI best fit (the PERSONALIZED_SEARCH_OPENAI_* settings) and the
Xpert platform. Example:

    ./manage.py run_personalized_search_harness --case-set demo --runs 1 --max-calls 400 \\
        --output /tmp/ps-harness.jsonl
    ./manage.py report_personalized_search_harness /tmp/ps-harness.jsonl

Catalog reads have no request to vend a secured key from, so this needs
PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE (a dev-only explicit catalog filter) and
ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH, like the pathway harness's --unscoped.
"""
import functools
import json
import logging

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.personalized_search import harness, wiring
from enterprise_access.apps.personalized_search.adapters.xpert_backend import XPERT_MODEL
from enterprise_access.apps.personalized_search.core.errors import ModelConfigurationError
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice


def _default_arms(registry) -> list[harness.Arm]:
    """The decided OpenAI best fit, plus the Xpert platform when it's configured."""
    intent, compose = wiring.default_models()
    arms = [harness.Arm('openai-best-fit', intent, compose)]
    if registry.is_configured(XPERT_MODEL):
        arms.append(harness.Arm('xpert', ModelChoice(XPERT_MODEL, None), ModelChoice(XPERT_MODEL, None)))
    return arms


def _load_personas():
    from enterprise_access.apps.pathway_eval.personas import load_personas  # pylint: disable=import-outside-toplevel
    return load_personas()


class Command(BaseCommand):
    """``run_personalized_search_harness``."""

    help = 'Compare model arms (default: OpenAI best fit vs Xpert) on the same Personalized Search turns.'

    def add_arguments(self, parser):
        parser.add_argument('--case-set', choices=('demo', 'personas', 'all'), default='all',
                            help='Demo-script prompts, pathway_eval personas, or both.')
        parser.add_argument('--case-id', action='append', dest='case_ids',
                            help='Only these case or persona ids (repeatable).')
        parser.add_argument('--arm', action='append', dest='arms',
                            help='NAME=INTENT[@EFFORT],COMPOSE[@EFFORT] (repeatable). Default: best fit and xpert.')
        parser.add_argument('--runs', type=int, default=1, help='Repeat every case this many times.')
        parser.add_argument('--max-calls', type=int, required=True,
                            help='Hard cap on paid model calls, checked before each call.')
        parser.add_argument('--judge', action='store_true', help=f'Blind-judge each page with {harness.JUDGE_MODEL}.')
        parser.add_argument('--dry-run', action='store_true', help='Print the plan and make no calls.')
        parser.add_argument('--output', help='JSONL path (default: stdout).')

    def handle(self, *args, **options):
        # Devstack logs HTTP clients at DEBUG. Under edx-django-utils' UserIdFilter every such line sends a
        # Django signal; a finalizer logging while that signal's lock was held deadlocked a run on
        # 2026-11-03. The harness doesn't need the noise.
        for name in ('urllib3', 'httpcore', 'httpcore2', 'httpx', 'openai'):
            logging.getLogger(name).setLevel(logging.WARNING)
        registry = wiring.model_registry()
        try:
            arms = ([harness.Arm.parse(spec, check_model=registry.check_model) for spec in options['arms']]
                    if options['arms'] else _default_arms(registry))
        except (ValueError, ModelConfigurationError) as exc:
            raise CommandError(str(exc)) from exc
        cases = []
        if options['case_set'] in ('demo', 'all'):
            cases += harness.demo_cases()
        if options['case_set'] in ('personas', 'all'):
            cases += harness.persona_cases(_load_personas)
        if options['case_ids']:
            cases = [c for c in cases if c.id in options['case_ids']]
        if not cases:
            raise CommandError('No cases selected.')

        self.stdout.write(json.dumps(harness.plan(cases, arms, options['runs'], options['judge'])))
        if options['dry_run']:
            return

        scope = wiring.dev_catalog_scope()
        if not scope:
            raise CommandError('Set PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE: there is no request to vend a secured key.')

        # pylint: disable=consider-using-with
        out = open(options['output'], 'w', encoding='utf-8') if options['output'] else self.stdout
        count = 0
        try:
            for record in harness.run(cases=cases, arms=arms, scope=scope,
                                      steps=wiring.steps(registry=registry, persist=False),
                                      complete_json=registry.complete_json,
                                      turn_context=functools.partial(wiring.turn_context, registry=registry),
                                      runs=options['runs'], max_calls=options['max_calls'], judge=options['judge'],
                                      log=self.stderr.write):
                out.write(json.dumps(record) + '\n')
                out.flush()
                count += 1
        finally:
            if options['output']:
                out.close()
        self.stderr.write(f'Wrote {count} turns.')
