"""
Management command: score a replay against the review fixture.

Offline unless ``--judge-swaps`` is set: that judges each swap pair's original and swapped
pathway, at most two calls a pair, and needs ``--max-calls``. See
``review_feedback.score_swaps`` and ``review_feedback.judge_prefers_replacement``.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval import review_feedback, variant_collection


class Command(BaseCommand):
    """Score a replay's picks against the reviewer's swaps, and optionally ask the judge."""

    help = (
        'Score replayed shape picks against the review fixture: replacements taken, dropped courses '
        'kept, kept courses retained, by theme. --judge-swaps also asks the judge whether it prefers '
        'each swap (paid; needs --max-calls).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--fixture', required=True, help='The build_review_fixture output.')
        parser.add_argument('--replay-checkpoint', required=True,
                            help='A replay_shape_review checkpoint (or a collection checkpoint, as a baseline).')
        parser.add_argument('--rubric', choices=review_feedback.RUBRICS, default=review_feedback.RUBRIC_V1,
                            help='Which judgement selects the picks, and judges the swaps.')
        parser.add_argument('--judge-swaps', action='store_true',
                            help='Judge each swap pair\'s original and swapped pathway (paid).')
        parser.add_argument('--max-calls', type=int, help='Required with --judge-swaps.')
        parser.add_argument('--dry-run', action='store_true',
                            help='With --judge-swaps: report the call bound and issue nothing.')
        parser.add_argument('--output', help='Write the full scores (JSON) here.')

    def handle(self, *args, **options):
        if options['judge_swaps'] and options.get('max_calls') is None and not options['dry_run']:
            raise CommandError('--judge-swaps needs --max-calls.')
        try:
            fixture = json.loads(Path(options['fixture']).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise CommandError(f'Could not read --fixture: {exc}') from exc
        replays = review_feedback.load_replays(options['replay_checkpoint'])
        if not replays:
            raise CommandError(f'No replayed runs in {options["replay_checkpoint"]}.')

        rubric = options['rubric']
        try:
            scores = review_feedback.score_swaps(fixture, replays, rubric=rubric)
        except review_feedback.ReplayContractError as exc:
            raise CommandError(str(exc)) from exc
        self._render(scores)

        if options['judge_swaps']:
            bound = review_feedback.judge_swap_call_bound(fixture)
            if options['dry_run']:
                self.stdout.write(f'  judge swaps (DRY RUN): up to {bound} call(s), none issued')
            else:
                careers = self._career_inputs(replays)
                try:
                    judged = review_feedback.judge_prefers_replacement(
                        fixture, careers=careers, rubric=rubric, max_calls=options['max_calls'],
                    )
                except review_feedback.ReplayContractError as exc:
                    raise CommandError(str(exc)) from exc
                scores['judge_swaps'] = judged
                self._render_judged(judged)

        if options.get('output'):
            path = Path(options['output'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(scores, indent=2, sort_keys=True))
            self.stdout.write(f'  wrote the scores to {path}')

    @staticmethod
    def _career_inputs(replays) -> dict:
        """Skills and windows per career; skills looked up (read-only) where a run did not record them."""
        careers = review_feedback.career_inputs_from_replays(replays)
        for name, inputs in careers.items():
            if not inputs['career_skills']:
                try:
                    career = variant_collection.lookup_career(name)
                except Exception:  # pylint: disable=broad-except
                    career = None
                inputs['career_skills'] = list((career or {}).get('skills') or [])
        return careers

    def _render(self, scores):
        """Hit and retention rates overall and by theme."""
        write = self.stdout.write
        write('')
        write(f'REVIEW FEEDBACK SCORES  (rubric {scores["rubric"]}; rates over slots whose tier has a pick)')
        write(f'  {"theme":<20}{"slots":>6}{"picked":>7}{"hit best":>10}{"hit any":>9}{"dropped kept":>14}')
        write('  ' + '-' * 64)
        rows = list(scores['by_theme'].items()) + [('ALL', scores['overall'])]
        for theme, agg in rows:
            write(f'  {theme:<20}{agg["slots"]:>6}{agg["with_pick"]:>7}{self._pct(agg["hit_best_rate"]):>10}'
                  f'{self._pct(agg["hit_any_rate"]):>9}{self._pct(agg["dropped_retained_rate"]):>14}')
        kept, none = scores['kept_good'], scores['nothing_would_work']
        write(f'  kept on good items: {kept["retained"]}/{kept["courses"]} retained '
              f'({self._pct(kept["retention_rate"])}); {kept["items_fully_retained"]}/{kept["items_with_pick"]} '
              f'items fully')
        unpicked = scores['dropped_without_pick']
        write(f'  dropped courses still picked: nothing would work {none["dropped_retained"]}/{none["with_pick"]}, '
              f'no pick {unpicked["dropped_retained"]}/{unpicked["with_pick"]}')
        if scores['careers_missing']:
            write(self.style.WARNING(f'  not replayed: {scores["careers_missing"]}'))

    def _render_judged(self, judged):
        """Which side the judge preferred, overall and by theme."""
        write = self.stdout.write
        write(f'  judge on the swaps (rubric {judged["rubric"]}; calls {judged["calls_issued"]} of at most '
              f'{judged["call_bound"]}):')
        write(f'  {"theme":<20}{"swap":>6}{"orig":>6}{"tie":>5}{"err":>5}{"agree":>8}')
        rows = list(judged['by_theme'].items()) + [('ALL', judged['overall'])]
        for theme, counts in rows:
            write(f'  {theme:<20}{counts.get("replacement", 0):>6}{counts.get("original", 0):>6}'
                  f'{counts.get("tie", 0):>5}{counts.get("error", 0):>5}{self._pct(counts["agreement_rate"]):>8}')
        if judged['budget_exhausted']:
            write(self.style.WARNING('  --max-calls was reached; some pairs were not judged.'))

    @staticmethod
    def _pct(rate):
        return '-' if rate is None else f'{rate * 100:.0f}%'
