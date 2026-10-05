"""
Tests for the review-feedback commands: build the fixture, replay on stored windows, score.

The properties that matter: the fixture build is offline and refuses inconsistent inputs; a
replay's dry run issues no lookup and no call, its budget is checked before each career
against the upper bound, each finished career is appended at once, and a resumed replay skips
what is done; scoring is offline unless the judge is asked for, and then needs a budget.
"""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from edx_toggles.toggles.testutils import override_waffle_switch

from enterprise_access.apps.pathway_eval import review_feedback, variant_collection
from enterprise_access.apps.pathway_eval.review_feedback import append_replay, load_replays
from enterprise_access.apps.pathway_eval.tests.test_review_feedback import (
    ALPHA,
    BETA,
    TRACE,
    candidate,
    fake_judgement,
    make_fixture,
    make_judge_key,
    make_queue,
    make_runs,
    make_votes,
    pathway_dict,
    replay_run
)
from enterprise_access.apps.pathways import pathway_variants
from enterprise_access.apps.pathways.pathway_variants import Variant
from enterprise_access.toggles import LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM

PATCH_LOOKUP = 'enterprise_access.apps.pathway_eval.variant_collection.lookup_career'


class InputFiles:
    """The review round's files, written to a temporary directory."""

    def __init__(self, tmp):
        self.dir = Path(tmp)
        self.votes = self.write_json('votes.json', {'votes': make_votes()})
        self.queue = self.write_json('queue.json', make_queue())
        self.judge_key = self.write_json('judge_key.json', make_judge_key())
        self.checkpoint = self.dir / 'checkpoint.jsonl'
        for run in make_runs():
            variant_collection.append_checkpoint(self.checkpoint, variant_collection.CareerRun.from_dict(run))
        self.careers = self.dir / 'careers.txt'
        self.careers.write_text(f'# ranked\n{ALPHA}\n{BETA}\n')

    def write_json(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload))
        return path


def run_command(name, **kwargs):
    stdout = StringIO()
    call_command(name, stdout=stdout, **kwargs)
    return stdout.getvalue()


class TestBuildReviewFixtureCommand(TestCase):
    """
    Scenario: The fixture is built offline from the round's files.
    """

    def test_the_fixture_is_written_with_its_sources_and_headline(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = InputFiles(tmp)
            output = files.dir / 'out' / 'fixture.json'
            text = run_command('build_review_fixture', votes=str(files.votes), queue=str(files.queue),
                               judge_key=str(files.judge_key), checkpoint=str(files.checkpoint),
                               careers_file=str(files.careers), output=str(output))
            fixture = json.loads(output.read_text())
        self.assertEqual(fixture['summary']['swap_pairs'], 3)
        self.assertEqual(fixture['excluded_careers'], [BETA])
        self.assertEqual(set(fixture['meta']['sources']), {'votes', 'queue', 'judge_key', 'checkpoint', 'careers_file'})
        self.assertIn('swap pairs 3 (best 2, unique 2)', text)
        self.assertIn('judge-good precision 0.5 (1/2)', text)
        self.assertIn('dropped without a pick 1 scored (2 in all)', text)

    def test_a_careers_file_that_disagrees_is_a_command_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = InputFiles(tmp)
            files.careers.write_text(f'{BETA}\n{ALPHA}\n')
            with self.assertRaisesRegex(CommandError, 'disagree'):
                run_command('build_review_fixture', votes=str(files.votes), queue=str(files.queue),
                            judge_key=str(files.judge_key), checkpoint=str(files.checkpoint),
                            careers_file=str(files.careers), output=str(files.dir / 'f.json'))


def replay_variants(**kwargs):
    """Stands in for ``build_variants``: one free cut and one model pick, whatever the career."""
    return [
        Variant(strategy='shape_cut', requested_size=2, shape=(2, 0, 0),
                courses=[candidate('AlphaX+I1'), candidate('RuriX+I2')]),
        Variant(strategy='shape_pick', requested_size=2, shape=(2, 0, 0), trace=dict(TRACE),
                courses=[candidate('AlphaX+I1'), candidate('GenX+I3')]),
    ] if kwargs['career_name'] == ALPHA else []


class TestReplayShapeReviewCommand(TestCase):
    """
    Scenario: Careers are replayed one by one, within budget, into a resumable checkpoint.
    """

    def run_replay(self, files, **kwargs):
        options = {'checkpoint': str(files.checkpoint), 'careers_file': str(files.careers),
                   'variant_strategies': ['shape_cut', 'shape_pick'], 'variant_shapes': ['2/0/0'], **kwargs}
        return run_command('replay_shape_review', **options)

    def test_a_dry_run_reports_the_bound_and_looks_nothing_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = InputFiles(tmp)
            output = files.dir / 'replay.jsonl'
            with mock.patch(PATCH_LOOKUP) as lookup:
                text = self.run_replay(files, dry_run=True, output_checkpoint=str(output))
            lookup.assert_not_called()
            self.assertFalse(output.exists())
        # One shape pick, and two variants judged under v1.
        self.assertIn('up to 3 paid call(s) per career; 6 for the whole list', text)
        self.assertIn('DRY RUN', text)

    def test_the_budget_refuses_a_career_it_cannot_afford_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = InputFiles(tmp)
            output = files.dir / 'replay.jsonl'
            with mock.patch(PATCH_LOOKUP) as lookup:
                text = self.run_replay(files, max_calls=2, output_checkpoint=str(output))
            lookup.assert_not_called()
            self.assertEqual(load_replays(output), [])
        self.assertIn('max calls reached', text)
        self.assertIn('--max-calls was reached', text)

    def test_each_career_is_appended_as_it_finishes_and_a_resume_skips_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = InputFiles(tmp)
            output = files.dir / 'replay.jsonl'
            with mock.patch(PATCH_LOOKUP, return_value={'name': ALPHA, 'skills': ['alpha']}) as lookup, \
                    mock.patch.object(pathway_variants, 'build_variants', side_effect=replay_variants), \
                    mock.patch.object(review_feedback.judging, 'judge_pathway', side_effect=fake_judgement):
                text = self.run_replay(files, careers=[ALPHA], output_checkpoint=str(output),
                                       max_calls=10, judge_rubrics=['v1', 'v2'])
                first = load_replays(output)
                again = self.run_replay(files, output_checkpoint=str(output), resume=True, max_calls=10)
                replays = load_replays(output)
            self.assertEqual(lookup.call_count, 2)
        self.assertEqual([run['career_name'] for run in first], [ALPHA])
        self.assertEqual(first[0]['replay']['model_calls'], {'variant_arms': 1, 'judge': 4, 'total': 5})
        self.assertEqual(first[0]['replay']['call_bound'], 5)
        self.assertIn('calls charged (upper bound): 5   calls issued: 5', text)
        self.assertIn('RESUMED', again)
        self.assertEqual([run['career_name'] for run in replays], [ALPHA, BETA])

    def test_the_editorial_snapshot_and_career_context_reach_the_app(self):
        class Policy:
            def to_dict(self):
                return {'excluded_keys': ['RuriX+I2']}
        policy = Policy()
        with tempfile.TemporaryDirectory() as tmp:
            files = InputFiles(tmp)
            snapshot = files.write_json('policy.json', {'excluded_keys': ['RuriX+I2']})
            output = files.dir / 'replay.jsonl'
            with mock.patch(PATCH_LOOKUP, return_value={'name': ALPHA, 'skills': ['alpha']}), \
                    mock.patch.object(pathway_variants, 'resolve_editorial_policy', return_value=policy) as resolve, \
                    mock.patch.object(pathway_variants, 'build_variants', side_effect=replay_variants) as build, \
                    mock.patch.object(review_feedback.judging, 'judge_pathway', side_effect=fake_judgement):
                self.run_replay(files, careers=[ALPHA], output_checkpoint=str(output),
                                editorial_snapshot=str(snapshot), career_context=str(files.queue))
                replays = load_replays(output)
        resolve.assert_called_once_with(snapshot={'excluded_keys': ['RuriX+I2']})
        kwargs = build.call_args.kwargs
        self.assertIs(kwargs['policy'], policy)
        self.assertEqual(kwargs['career_description'], 'Alpha Analysts do alpha work.')
        self.assertEqual(kwargs['family_size'], 2)
        self.assertEqual(replays[0]['replay']['editorial_snapshot']['policy'], {'excluded_keys': ['RuriX+I2']})

    def replay_ecosystem(self, *args, **kwargs):
        """Replay ALPHA with the given flag; ``(what build_variants got, summary text, replay)``."""
        with tempfile.TemporaryDirectory() as tmp:
            files = InputFiles(tmp)
            output = files.dir / 'replay.jsonl'
            stdout = StringIO()
            with mock.patch(PATCH_LOOKUP, return_value={'name': ALPHA, 'skills': ['alpha']}), \
                    mock.patch.object(pathway_variants, 'build_variants', side_effect=replay_variants) as build, \
                    mock.patch.object(review_feedback.judging, 'judge_pathway', side_effect=fake_judgement):
                call_command('replay_shape_review', *args, stdout=stdout, checkpoint=str(files.checkpoint),
                             careers_file=str(files.careers), careers=[ALPHA], variant_strategies=['shape_cut'],
                             variant_shapes=['2/0/0'], output_checkpoint=str(output), **kwargs)
                replays = load_replays(output)
        return build.call_args.kwargs['single_ecosystem'], stdout.getvalue(), replays[0]['replay']

    def test_single_ecosystem_on(self):
        passed, text, replay = self.replay_ecosystem('--single-ecosystem')

        self.assertIs(passed, True)
        self.assertIn('single ecosystem: yes  ', text)
        self.assertIs(replay['single_ecosystem'], True)

    def test_single_ecosystem_off(self):
        passed, text, replay = self.replay_ecosystem('--no-single-ecosystem')

        self.assertIs(passed, False)
        self.assertIn('single ecosystem: no  ', text)
        self.assertIs(replay['single_ecosystem'], False)

    def test_single_ecosystem_omitted_leaves_it_to_the_app_and_says_what_that_resolved_to(self):
        passed, text, replay = self.replay_ecosystem()

        self.assertIsNone(passed)
        self.assertIn('single ecosystem: yes (default)', text)
        self.assertIs(replay['single_ecosystem'], True)

    def test_single_ecosystem_omitted_follows_the_kill_switch(self):
        with override_waffle_switch(LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM, True):
            passed, text, replay = self.replay_ecosystem()

        self.assertIsNone(passed)
        self.assertIn('single ecosystem: no (default)', text)
        self.assertIs(replay['single_ecosystem'], False)

    def test_bad_requests_are_command_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = InputFiles(tmp)
            output = str(files.dir / 'replay.jsonl')
            for kwargs, message in (
                ({}, '--output-checkpoint is required'),
                ({'output_checkpoint': output, 'variant_strategies': ['ranked_cut']}, 'build by size'),
                ({'output_checkpoint': output, 'variant_strategies': ['no_such_arm']}, 'Unknown variant strategies'),
                ({'output_checkpoint': output, 'careers': ['Gamma Lead']}, 'No careers to replay'),
                ({'dry_run': True, 'resume': True}, '--resume needs --output-checkpoint'),
            ):
                with self.assertRaisesRegex(CommandError, message):
                    self.run_replay(files, **kwargs)


class TestScoreReviewFeedbackCommand(TestCase):
    """
    Scenario: Scoring is offline, and the judge-swap check needs a budget.
    """

    def files(self, tmp):
        """A fixture and a one-career replay checkpoint in ``tmp``."""
        fixture = Path(tmp) / 'fixture.json'
        fixture.write_text(json.dumps(make_fixture()))
        replay = Path(tmp) / 'replay.jsonl'
        append_replay(replay, replay_run(ALPHA, [
            pathway_dict('shape_cut:2/0/0', ['AlphaX+I1', 'GenX+I3']),
            pathway_dict('shape_cut:0/2/0', ['AlphaX+M1', 'AlphaX+M2']),
        ]))
        return fixture, replay

    def test_the_scores_are_printed_and_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, replay = self.files(tmp)
            output = Path(tmp) / 'scores.json'
            text = run_command('score_review_feedback', fixture=str(fixture), replay_checkpoint=str(replay),
                               output=str(output))
            scores = json.loads(output.read_text())
        self.assertEqual(scores['overall']['hit_best'], 1)
        self.assertNotIn('judge_swaps', scores)
        self.assertIn('regional', text)
        self.assertIn('kept on good items: 2/2 retained (100%)', text)

    def test_judging_the_swaps_needs_a_budget_and_a_dry_run_issues_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, replay = self.files(tmp)
            with self.assertRaisesRegex(CommandError, 'needs --max-calls'):
                run_command('score_review_feedback', fixture=str(fixture), replay_checkpoint=str(replay),
                            judge_swaps=True)
            with mock.patch.object(review_feedback.judging, 'judge_pathway') as judge:
                text = run_command('score_review_feedback', fixture=str(fixture), replay_checkpoint=str(replay),
                                   judge_swaps=True, dry_run=True)
            judge.assert_not_called()
        self.assertIn('up to 5 call(s), none issued', text)

    def test_the_judge_swaps_are_counted_and_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, replay = self.files(tmp)
            output = Path(tmp) / 'scores.json'
            with mock.patch.object(review_feedback.judging, 'judge_pathway', side_effect=fake_judgement):
                text = run_command('score_review_feedback', fixture=str(fixture), replay_checkpoint=str(replay),
                                   judge_swaps=True, max_calls=4, rubric='v2', output=str(output))
            scores = json.loads(output.read_text())
        self.assertEqual(scores['judge_swaps']['calls_issued'], 4)
        self.assertTrue(scores['judge_swaps']['budget_exhausted'])
        self.assertIn('calls 4 of at most 5', text)
