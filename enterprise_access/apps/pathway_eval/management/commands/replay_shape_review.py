"""
Management command: replay shape selection on a collection's stored candidate windows.

Re-runs the app's variant arms and judge on the windows ``collect_pathway_variants
--include-candidates`` recorded, so a change to selection is measured on exactly the
candidates the reviewer saw, without re-retrieving or re-ranking. Issues paid calls, so
``--dry-run`` and ``--max-calls`` behave as they do for the collection: the limit is checked
before each career against an upper bound of what it can cost. See
``review_feedback.replay_career``.
"""
import hashlib
import json
from functools import partial
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from enterprise_access.apps.pathway_eval import review_feedback, variant_collection
from enterprise_access.apps.pathway_eval.shape_review import REVIEW_SHAPES, REVIEW_STRATEGIES
from enterprise_access.apps.pathways import judging, pathway_variants


class Command(BaseCommand):
    """Replay variant selection and judging, career by career, on stored windows."""

    help = (
        'Replay the shape arms and the judge on a collection\'s stored candidate windows, with an optional '
        'editorial policy snapshot, and append each career to a JSON-lines checkpoint.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--checkpoint', required=True,
                            help='The collect_pathway_variants checkpoint, recorded with --include-candidates.')
        parser.add_argument('--careers-file', help='Careers to replay, in order. Defaults to every run.')
        parser.add_argument('--career', action='append', dest='careers', default=[],
                            help='Replay only this career (exact, case-insensitive). Repeatable.')
        parser.add_argument('--variant-strategy', action='append', dest='variant_strategies',
                            help=f'Arm to replay. Repeatable; defaults to {list(REVIEW_STRATEGIES)}.')
        parser.add_argument('--variant-shape', action='append', dest='variant_shapes',
                            help='Shape to build, Introductory/Intermediate/Advanced. Repeatable; defaults to '
                                 'the review shapes.')
        parser.add_argument('--editorial-snapshot',
                            help='An editorial policy snapshot (JSON), loaded with EditorialPolicy.from_dict.')
        parser.add_argument('--single-ecosystem', action='store_true',
                            help="Refuse a course that would leave a pathway spanning two vendors' "
                                 'products (see pathways.ecosystems).')
        parser.add_argument('--career-context',
                            help='The review queue (queue.json): each career\'s description and family titles, '
                                 'passed to the arms and the judge.')
        parser.add_argument('--judge-rubric', action='append', dest='judge_rubrics',
                            choices=review_feedback.RUBRICS, help='Rubric to judge with. Repeatable; defaults to v1.')
        parser.add_argument('--max-calls', type=int,
                            help='Stop before exceeding this many paid calls (an upper bound per career, '
                                 'checked before it starts).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the plan and its cost bound; no lookups, no calls.')
        parser.add_argument('--output-checkpoint',
                            help='Append each replayed career to this JSON-lines file as it finishes.')
        parser.add_argument('--resume', action='store_true',
                            help='Skip careers already replayed in --output-checkpoint.')

    def handle(self, *args, **options):
        if not options['dry_run'] and not options.get('output_checkpoint'):
            raise CommandError('--output-checkpoint is required unless --dry-run.')
        if options['resume'] and not options.get('output_checkpoint'):
            raise CommandError('--resume needs --output-checkpoint.')
        if options.get('max_calls') is not None and options['max_calls'] < 0:
            raise CommandError('--max-calls must not be negative.')

        plan = self._plan(options)
        runs = self._runs(options)
        done = set()
        if options['resume']:
            done = {
                (run.get('requested_name') or run.get('career_name') or '').lower()
                for run in review_feedback.load_replays(options['output_checkpoint'])
            }
        on_run = None
        if options.get('output_checkpoint') and not options['dry_run']:
            path = Path(options['output_checkpoint'])
            path.parent.mkdir(parents=True, exist_ok=True)
            on_run = partial(review_feedback.append_replay, path)

        lines, charged, issued, exhausted = [], 0, 0, False
        for run in runs:
            name = run.get('career_name') or run.get('requested_name')
            if name.lower() in done or (run.get('requested_name') or '').lower() in done:
                lines.append((name, 'RESUMED', ''))
                continue
            if options['dry_run']:
                lines.append((name, 'DRY RUN', f'window {len(run.get("candidates") or [])}'))
                continue
            if not run.get('candidates'):
                lines.append((name, 'SKIPPED', 'no stored candidate window (collected without --include-candidates)'))
                continue
            if options.get('max_calls') is not None and charged + plan['bound'] > options['max_calls']:
                exhausted = True
                lines.append((name, 'SKIPPED', 'max calls reached'))
                continue
            status, detail, replay = self._replay_one(run, name, plan)
            if status != 'SKIPPED':
                # Charged once the replay started, whether or not it finished: its calls may be spent.
                charged += plan['bound']
            if replay is not None:
                issued += replay['replay']['model_calls']['total']
                if on_run is not None:
                    on_run(replay)
            lines.append((name, status, detail))

        self._render(lines, plan, len(runs), charged, issued, exhausted, options)

    def _plan(self, options) -> dict:
        """Validate the request up front, so a bad flag fails before any career is paid for."""
        strategies = options.get('variant_strategies') or list(REVIEW_STRATEGIES)
        try:
            strategies = pathway_variants.normalise_strategies(strategies)
            shapes = pathway_variants.normalise_shapes(options.get('variant_shapes') or list(REVIEW_SHAPES))
            rubrics = review_feedback.normalise_rubrics(options.get('judge_rubrics'))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        size_only = [s for s in strategies if s in review_feedback.SIZE_ONLY_STRATEGIES]
        if size_only:
            raise CommandError(f'{size_only} build by size, and a replay builds shapes only; drop them.')

        policy, snapshot = None, None
        if options.get('editorial_snapshot'):
            try:
                raw = Path(options['editorial_snapshot']).read_bytes()
                data = json.loads(raw)
            except (OSError, ValueError) as exc:
                raise CommandError(f'Could not read --editorial-snapshot: {exc}') from exc
            if not isinstance(data, dict):
                raise CommandError('--editorial-snapshot must hold a JSON object (an EditorialPolicy.to_dict()).')
            try:
                # The app's own loader: EditorialPolicy.from_dict, imported lazily from pathway_editorial.
                policy = pathway_variants.resolve_editorial_policy(snapshot=data)
            except (ImportError, ValueError, TypeError, KeyError) as exc:
                raise CommandError(f'Could not load --editorial-snapshot: {exc}') from exc
            snapshot = {'path': str(options['editorial_snapshot']), 'sha256': hashlib.sha256(raw).hexdigest(),
                        'policy': pathway_variants.policy_record(policy)}

        contexts = {}
        if options.get('career_context'):
            try:
                queue = json.loads(Path(options['career_context']).read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise CommandError(f'Could not read --career-context: {exc}') from exc
            contexts = {name.lower(): value for name, value in review_feedback.career_context_from_queue(queue).items()}

        # Fail now, not after the first career's arms have been paid for, if the app does not
        # yet take the policy, the rubric or the career context.
        sample_context = {'career_description': 'x', 'family_titles': ['x'], 'family_size': 1} if contexts else {}
        try:
            review_feedback.contract_kwargs(pathway_variants.build_variants, {'policy': policy, **sample_context})
            for rubric in rubrics:
                review_feedback.contract_kwargs(judging.judge_pathway, {'rubric': rubric, **sample_context})
        except review_feedback.ReplayContractError as exc:
            raise CommandError(str(exc)) from exc

        return {
            'strategies': strategies, 'shapes': shapes, 'rubrics': rubrics, 'policy': policy,
            'single_ecosystem': options['single_ecosystem'],
            'snapshot': snapshot, 'contexts': contexts,
            'bound': review_feedback.replay_call_bound(strategies=strategies, shapes=shapes, rubrics=rubrics),
        }

    @staticmethod
    def _runs(options) -> list[dict]:
        """The collection's runs to replay, in careers-file order, filtered by --career."""
        by_name = {
            name.lower(): run.to_dict()
            for name, run in variant_collection.load_checkpoint(options['checkpoint']).items()
        }
        if not by_name:
            raise CommandError(f'No finished runs in {options["checkpoint"]}.')
        if options.get('careers_file'):
            try:
                names = variant_collection.load_career_names(options['careers_file'])
            except OSError as exc:
                raise CommandError(f'Could not read --careers-file: {exc}') from exc
        else:
            names = [run['requested_name'] for run in by_name.values()]
        wanted = {name.strip().lower() for name in options.get('careers') or [] if name.strip()}
        if wanted:
            names = [name for name in names if name.lower() in wanted]
        missing = [name for name in names if name.lower() not in by_name]
        if missing:
            raise CommandError(f'Not in the checkpoint: {missing}.')
        if not names:
            raise CommandError('No careers to replay.')
        return [by_name[name.lower()] for name in names]

    @staticmethod
    def _replay_one(run, name, plan):
        """
        Look the career's skills up and replay it; ``(status, detail, replay or None)``.

        ``SKIPPED`` issued nothing. ``ERROR`` may have: the replay started and then failed, which
        costs that career and not the batch.
        """
        try:
            career = variant_collection.lookup_career(name)
        except Exception as exc:  # pylint: disable=broad-except
            return 'SKIPPED', f'career lookup failed ({type(exc).__name__}): {exc}', None
        if career is None:
            return 'SKIPPED', variant_collection.NO_CAREER, None
        if not career.get('skills'):
            return 'SKIPPED', variant_collection.NO_SKILLS, None
        context = plan['contexts'].get(name.lower()) or {}
        try:
            replay = review_feedback.replay_career(
                run, strategies=plan['strategies'], shapes=plan['shapes'], career_skills=career['skills'],
                career_description=context.get('career_description', ''),
                family_titles=context.get('family_titles', ()), family_size=context.get('family_size', 0),
                policy=plan['policy'], rubrics=plan['rubrics'],
                single_ecosystem=plan['single_ecosystem'],
            )
        except Exception as exc:  # pylint: disable=broad-except
            return 'ERROR', f'replay failed ({type(exc).__name__}): {exc}', None
        replay['replay']['editorial_snapshot'] = plan['snapshot']
        replay['replay']['call_bound'] = plan['bound']
        judged = sum(1 for variant in replay['variants'] if variant.get(
            review_feedback.judgement_field(plan['rubrics'][0])))
        return 'REPLAYED', (f'variants {len(replay["variants"])}  judged {judged}  '
                            f'calls {replay["replay"]["model_calls"]["total"]}'), replay

    def _render(self, lines, plan, n_runs, charged, issued, exhausted, options):
        """The plan, one line per career, and what was charged."""
        write = self.stdout.write
        write('')
        write('SHAPE REVIEW REPLAY' + ('  (DRY RUN -- no lookups, no calls)' if options['dry_run'] else ''))
        write(f'  careers: {n_runs}  strategies: {plan["strategies"]}  rubrics: {plan["rubrics"]}  '
              f'editorial policy: {"yes" if plan["policy"] is not None else "no"}  '
              f'single ecosystem: {"yes" if plan["single_ecosystem"] else "no"}  '
              f'career context: {"yes" if plan["contexts"] else "no"}')
        write(f'  shapes: {plan["shapes"]}')
        write(f'  up to {plan["bound"]} paid call(s) per career; {plan["bound"] * n_runs} for the whole list')
        write('=' * 78)
        for name, status, detail in lines:
            write(f'  {name:<32} {status:<9} {detail}')
        write('=' * 78)
        write(f'  calls charged (upper bound): {charged}   calls issued: {issued}')
        if exhausted:
            write(self.style.WARNING('  --max-calls was reached; the remaining careers were not started.'))
