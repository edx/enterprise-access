"""
Management command: turn a shape review's votes into the fixture later changes are scored on.

Offline: reads the bench's vote export, the queue it reviewed, the judge key and the
collection's checkpoint, and writes one JSON fixture. See ``review_feedback.build_fixture``.
"""
import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval.review_feedback import THEME_PRIORITY, build_fixture, load_votes
from enterprise_access.apps.pathway_eval.variant_collection import load_career_names, load_checkpoint


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Command(BaseCommand):
    """
    Build the review fixture: swap pairs, "nothing would work" slots, kept courses, the
    judge-versus-reviewer confusion and the coded themes. Issues no calls.
    """

    help = (
        'Build the shape-review fixture (swap pairs with window ranks, nothing-would-work slots, '
        'kept courses, confusion, themes) from a bench vote export. Offline.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--votes', required=True, help='The bench vote export (JSON).')
        parser.add_argument('--queue', required=True, help='The queue the bench loaded (queue.json).')
        parser.add_argument('--judge-key', required=True, help='The judge key for the queue (judge_key.json).')
        parser.add_argument('--checkpoint', required=True,
                            help='The collect_pathway_variants checkpoint, recorded with --include-candidates.')
        parser.add_argument('--careers-file',
                            help='The careers in rank order, so item S01 is the first. Cross-checked against '
                                 'the judge key and the queue.')
        parser.add_argument('--output', required=True, help='Where to write the fixture (JSON).')

    def handle(self, *args, **options):
        try:
            votes = load_votes(options['votes'])
            queue = json.loads(Path(options['queue']).read_text(encoding='utf-8'))
            judge_key = json.loads(Path(options['judge_key']).read_text(encoding='utf-8'))
            career_names = load_career_names(options['careers_file']) if options.get('careers_file') else None
        except (OSError, ValueError) as exc:
            raise CommandError(f'Could not read an input: {exc}') from exc
        runs = load_checkpoint(options['checkpoint'])
        if not runs:
            raise CommandError(f'No finished runs in {options["checkpoint"]}.')

        try:
            fixture = build_fixture(votes=votes, queue=queue, judge_key=judge_key, checkpoint_runs=runs,
                                    career_names=career_names)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        fixture['meta'] = {
            'sources': {
                name: {'path': str(options[name]), 'sha256': _sha256(options[name])}
                for name in ('votes', 'queue', 'judge_key', 'checkpoint', 'careers_file') if options.get(name)
            },
        }

        path = Path(options['output'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fixture, indent=2, sort_keys=True))
        self._render(fixture)
        self.stdout.write(f'  wrote the fixture to {path}')

    def _render(self, fixture):
        """The headline numbers."""
        write = self.stdout.write
        summary, confusion, ranks = fixture['summary'], fixture['confusion'], fixture['rank_stats']
        write('')
        write('SHAPE REVIEW FIXTURE')
        write(f'  votes {summary["votes"]} (scored {summary["scored_votes"]}, skipped {summary["skipped_votes"]}, '
              f'superseded {summary["superseded_votes"]})  careers {summary["careers"]}  '
              f'excluded {fixture["excluded_careers"] or "none"}')
        write(f'  swap pairs {summary["swap_pairs"]} (best {summary["best_swap_pairs"]}, unique '
              f'{ranks["unique_pairs"]})  nothing would work {summary["nothing_would_work"]}  '
              f'dropped without a pick {summary["dropped_without_pick_scored"]} scored '
              f'({summary["dropped_without_pick"]} in all)  '
              f'kept on good items {summary["kept_good_courses"]} course(s) in {summary["kept_good_items"]} item(s)')
        write('  confusion (judge v1 rows x reviewer columns; skips apart):')
        write(f'    {"":<8}{"good":>6}{"needs_work":>12}{"bad":>6}{"skip":>6}')
        for verdict, row in confusion['matrix'].items():
            write(f'    {verdict:<8}{row.get("good", 0):>6}{row.get("needs_work", 0):>12}{row.get("bad", 0):>6}'
                  f'{confusion["skips"].get(verdict, 0):>6}')
        precision = confusion['judge_good_precision']
        write(f'    judge-good precision {precision["value"]} ({precision["numerator"]}/{precision["denominator"]})')
        themes = fixture['themes']
        write('  themes    ' + '  '.join(
            f'{theme} {themes["vote_counts"].get(theme, 0)}/{themes["swap_counts"].get(theme, 0)}'
            for theme in THEME_PRIORITY if theme in themes['vote_counts'] or theme in themes['swap_counts']
        ) + '   (votes/best swaps)')
        dropped, replacement = ranks['dropped_rung_rank'], ranks['replacement_rung_rank']
        write(f'  rung rank (0-based) median dropped {dropped["median"]} vs replacement {replacement["median"]}; '
              f'replacement ranked below the dropped course in {ranks["replacement_below_dropped_on_rung"]} '
              f'of {ranks["pairs"]}; not in window {ranks["replacement_not_in_window"]}')
