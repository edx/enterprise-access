"""
Management command for the Tier 1/2/3 harness report.

Reads exported traces rather than re-running the pipeline, so a re-score is free. That
matters: a harness run costs money, and the shape of the report has changed several times
while the underlying traces have not.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval.personas import PersonaValidationError, load_personas
from enterprise_access.apps.pathway_eval.scoring import (
    MIN_EXPECTED_COURSES_IN_PATHWAY,
    MIN_PASSING_PERSONAS,
    SPLIT_NON_TECHNOLOGY,
    SPLIT_TECHNOLOGY,
    regression_verdict,
    score_run
)


def _percent(value):
    """Render a ratio, or say it is unavailable rather than printing a misleading zero."""
    return f'{value:.0%}' if value is not None else 'n/a'


class Command(BaseCommand):
    """
    Score exported harness traces into the three tiers Decision 8 defines.

    Tier 1 is pass/fail correctness, Tier 2 is the ship bar, Tier 3 is tracked and never
    gating. A run that fails Tier 1 is not scored on quality at all -- a quality number
    computed over structurally invalid pathways is noise.
    """

    help = 'Score exported pathway-harness traces and report the Tier 1/2/3 verdict.'

    def add_arguments(self, parser):
        parser.add_argument('traces', help='Path to a run_pathway_harness --output-json file.')
        parser.add_argument(
            '--previous',
            help='A prior traces file to compare against, for the regression bar.',
        )
        parser.add_argument(
            '--persona-dir',
            help='Directory of persona YAML files. Must be the set the run used.',
        )
        parser.add_argument(
            '--min-passing', type=int, default=MIN_PASSING_PERSONAS,
            help=f'Tier 2 passing-persona threshold (default: {MIN_PASSING_PERSONAS}).',
        )
        parser.add_argument('--output-json', help='Write the full scored report to this path.')

    def handle(self, *args, **options):
        traces = self._load_traces(Path(options['traces']))

        try:
            personas = load_personas(fixture_dir=options.get('persona_dir'))
        except PersonaValidationError as exc:
            raise CommandError(str(exc)) from exc

        report = score_run(personas, traces['cells'], min_passing=options['min_passing'])
        report['run_config'] = traces.get('run_config') or {}

        previous_report = None
        if options.get('previous'):
            previous_traces = self._load_traces(Path(options['previous']))
            previous_report = score_run(
                personas, previous_traces['cells'], min_passing=options['min_passing'],
            )
        report['regression'] = regression_verdict(report, previous_report)

        self._render(report)

        if options.get('output_json'):
            path = Path(options['output_json'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
            self.stdout.write(f'  wrote report to {path}')

    @staticmethod
    def _load_traces(path):
        """Read one traces file, failing clearly rather than half-way through scoring."""
        if not path.exists():
            raise CommandError(f'{path} does not exist.')
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise CommandError(f'{path} is not valid JSON: {exc}') from exc
        if not isinstance(payload, dict) or 'cells' not in payload:
            raise CommandError(f'{path} is not a harness traces file (no "cells" key).')
        return payload

    def _render(self, report):
        """Print the report."""
        write = self.stdout.write
        config = report.get('run_config') or {}

        write('')
        write('PATHWAY HARNESS REPORT')
        if config.get('customer_uuid'):
            write(f'  enterprise customer: {config["customer_uuid"]}')
        else:
            write(self.style.WARNING(
                '  NOT scoped to any enterprise customer -- every number is an upper bound.'
            ))
        write(f'  model backend: {config.get("model_backend", "unknown")}   '
              f're-rank: {"on" if config.get("rerank_enabled") else "off"}   '
              f'runs: {config.get("runs", "?")}')
        write('=' * 78)

        self._render_tier_one(report['tier_one'])
        self._render_tier_two(report['tier_two'])
        self._render_personas(report['personas'])
        self._render_tier_three(report['tier_three'])
        self._render_regression(report.get('regression'))

        write('')
        write('=' * 78)
        verdict = 'MEETS THE BAR' if report['shippable'] else 'DOES NOT MEET THE BAR'
        style = self.style.SUCCESS if report['shippable'] else self.style.ERROR
        write(style(f'  {verdict}'))
        write('  Tier 3 is tracked, never gating; the verdict is Tier 1 and Tier 2 only.')

    def _render_tier_one(self, tier_one):
        """Print the Tier 1 correctness verdict."""
        write = self.stdout.write
        write('')
        write('TIER 1 -- correctness gates (bugs, not quality)')
        if tier_one['passed']:
            write(self.style.SUCCESS(
                f'  PASS  ({tier_one["cells_checked"]} cells checked)'
            ))
            return
        write(self.style.ERROR('  FAIL -- do not read the quality numbers below as meaningful.'))
        for name, rows in tier_one['failures'].items():
            write(f'    {name}: {len(rows)}')
            for row in rows[:5]:
                write(f'      {row}')

    def _render_tier_two(self, tier_two):
        """Print the Tier 2 ship bar."""
        write = self.stdout.write
        write('')
        write('TIER 2 -- the ship bar')
        write(f'  rule: a persona passes if >= {MIN_EXPECTED_COURSES_IN_PATHWAY} expected '
              'course appears in the delivered pathway')
        style = self.style.SUCCESS if tier_two['passed'] else self.style.ERROR
        write(style(
            f'  {"PASS" if tier_two["passed"] else "FAIL"}  '
            f'{tier_two["passing"]} of {tier_two["scoreable"]} scoreable personas pass '
            f'(bar: {tier_two["min_passing"]})'
        ))
        for split in (SPLIT_TECHNOLOGY, SPLIT_NON_TECHNOLOGY):
            row = tier_two['per_split'][split]
            flag = '  <-- ZERO' if row['zero'] else ''
            write(f'    {split:<18} {row["passing"]}/{row["scoreable"]} passing{flag}')
        if tier_two['zero_splits']:
            write(self.style.ERROR(
                '  A split scored zero. Concentrated failure is not shippable regardless '
                'of the total, which is what an aggregate metric cannot express.'
            ))

    def _render_personas(self, persona_scores):
        """Print the per-persona pass/fail detail."""
        write = self.stdout.write
        write('')
        write('  per persona:')
        for score in persona_scores:
            if not score['scoreable']:
                write(f'    {score["persona_id"]:<22} not scoreable '
                      f'(expected courses: {score["expected_course_count"]})')
                continue
            mark = 'PASS' if score['passed'] else 'FAIL'
            style = self.style.SUCCESS if score['passed'] else self.style.ERROR
            note = ' [expects no coverage]' if score['expect_no_coverage'] else ''
            write(style(
                f'    {score["persona_id"]:<22} {mark}  {score["split"]:<16} '
                f'best recall={_percent(score["best_recall"])}  '
                f'cells ran={score["cells_ran"]}/{score["cells_planned"]}{note}'
            ))

    def _render_tier_three(self, tier_three):
        """Print the tracked-not-gated metrics."""
        write = self.stdout.write
        write('')
        write('TIER 3 -- tracked, not gating')
        write(f'  cells ran: {tier_three["cells_ran"]}   '
              f'pathways completed: {tier_three["cells_complete"]} '
              f'({_percent(tier_three["completion_rate"])})')
        write(f'  zero-hit rate: {_percent(tier_three["zero_hit_rate"])}   '
              f'unexplained pathways: {_percent(tier_three["unexplained_pathway_rate"])}')
        for split in (SPLIT_TECHNOLOGY, SPLIT_NON_TECHNOLOGY):
            row = tier_three['splits'][split]
            write(f'  {split:<18} personas={row["personas"]:<3} '
                  f'mean recall={_percent(row["mean_recall"])}')
        delta = tier_three['technology_delta']
        write(f'  technology delta: {f"{delta:+.0%}" if delta is not None else "n/a"}')
        write('  unfilled rungs: ' + '  '.join(
            f'{level}={_percent(rate)}'
            for level, rate in tier_three['unfilled_rung_rate'].items()
        ))
        modes = tier_three['career_mode_delta']
        write(f'  career mode: auto={modes["auto"]} oracle={modes["oracle"]} '
              f'delta={modes["delta"]:+d}  '
              '(the gap is the cost of automatic career selection, not of retrieval)')
        consistency = tier_three['cross_run_consistency']
        write(f'  cross-run consistency: '
              f'{_percent(consistency["mean_jaccard"])} mean Jaccard over '
              f'{consistency["pairs_compared"]} pair(s)')

    def _render_regression(self, regression):
        """Print the regression verdict against the previous run."""
        write = self.stdout.write
        write('')
        write('REGRESSION BAR -- engineering-owned, no product input')
        if regression is None:
            write('  no previous run supplied; nothing to compare.')
            return
        if regression['passed']:
            write(self.style.SUCCESS('  PASS -- no tracked metric decreased.'))
        else:
            write(self.style.ERROR('  FAIL -- a tracked metric decreased:'))
            for row in regression['regressions']:
                write(f'    {row["metric"]}: {row["from"]} -> {row["to"]}')
        for row in regression['improvements']:
            write(f'    improved  {row["metric"]}: {row["from"]} -> {row["to"]}')
