"""Tests for the Personalized Search harness, its scoring and its management commands."""
import json
from io import StringIO
from tempfile import NamedTemporaryFile

import ddt
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from enterprise_access.apps.pathway_eval.personas import load_personas
from enterprise_access.apps.personalized_search import harness
from enterprise_access.apps.personalized_search.adapters.openai_backend import OpenAIBackend
from enterprise_access.apps.personalized_search.adapters.xpert_backend import XpertBackend
from enterprise_access.apps.personalized_search.core import harness_scoring
from enterprise_access.apps.personalized_search.core.model_registry import ModelRegistry
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice
from enterprise_access.apps.personalized_search.core.ports import Usage
from enterprise_access.apps.personalized_search.tests.test_orchestrator import fake_steps

MODELS = {'gpt-4.1-mini': (0.4, 0.1, 1.6), 'gpt-5.4-mini': (0.75, 0.075, 4.5)}
XPERT = {'XPERT_API_BASE_URL': 'https://xpert.test', 'XPERT_API_CLIENT_ID': 'client-123'}
ARMS = [harness.Arm('a', ModelChoice('gpt-4.1-mini', None), ModelChoice('gpt-5.4-mini', 'low')),
        harness.Arm('b', ModelChoice('xpert', None), ModelChoice('xpert', None))]
CASE = harness.Case(id='O1', message='I want to get better at leading a team', kind='demo',
                    expect={'on_topic': True, 'redirect_reason': None})
REGISTRY = ModelRegistry([OpenAIBackend(api_key='k', models=MODELS),
                          XpertBackend(base_url='https://xpert.test', client_id='client-123')])


def no_judge(**_):
    raise AssertionError('the judge was not asked for')


def run(**kwargs):
    """``harness.run`` with this module's defaults."""
    kwargs.setdefault('complete_json', no_judge)
    return harness.run(**kwargs)


@ddt.ddt
class HarnessRunTests(TestCase):
    """``harness.run`` drives the real orchestrator through each arm."""

    def setUp(self):
        super().setUp()
        self.addCleanup(cache.clear)

    def test_arm_parse(self):
        arm = harness.Arm.parse('best=gpt-4.1-mini,gpt-5.4-mini@low', check_model=REGISTRY.check_model)
        self.assertEqual(arm.intent, ModelChoice('gpt-4.1-mini', None))
        self.assertEqual(arm.compose, ModelChoice('gpt-5.4-mini', 'low'))
        self.assertEqual(harness.Arm.parse('x=xpert,xpert', check_model=REGISTRY.check_model).compose.model, 'xpert')

    @ddt.data('no-equals', 'x=gpt-4.1-mini', 'x=gpt-9,gpt-4.1-mini')
    def test_arm_parse_rejects(self, spec):
        with self.assertRaises(Exception):
            harness.Arm.parse(spec, check_model=REGISTRY.check_model)

    def test_runs_every_arm_and_records_scores(self):
        records = list(run(cases=[CASE, CASE], arms=ARMS, scope=None, steps=fake_steps(), max_calls=None))
        self.assertEqual([r['arm'] for r in records], ['a', 'b', 'b', 'a'])  # order alternates per case
        first = records[0]
        self.assertEqual(first['status'], 'done')
        self.assertEqual(first['item_ids'], ['edX+C1'])
        self.assertEqual(first['models'], ['gpt-4.1-mini', 'gpt-5.4-mini@low'])
        self.assertEqual(first['intent_score'], {'pass': True, 'why': ''})
        self.assertIn('complete_ms', first['timing'])
        self.assertFalse(first['error'])
        self.assertEqual(len(first['candidate_ids']), 8)

    def test_retrieval_is_shared_when_intents_match(self):
        calls = []
        steps = fake_steps()
        base_retrieve = steps.retrieve

        def counting(*args, **kwargs):
            calls.append((args, kwargs))
            return base_retrieve(*args, **kwargs)

        steps.retrieve = counting
        list(run(cases=[CASE], arms=ARMS, scope=None, steps=steps))
        self.assertEqual(len(calls), 1)
        self.assertIn('timeout_s', calls[0][1])  # the turn's capped timeout reaches the real retrieve

    def test_budget_stops_before_the_call_that_would_exceed_it(self):
        records = list(run(cases=[CASE, CASE], arms=ARMS, scope=None, steps=fake_steps(), max_calls=3))
        self.assertEqual(len(records), 1)  # intent+compose, then the next turn's intent fits, its compose doesn't

    def test_judge(self):
        def fake_judge(**kwargs):
            self.assertEqual(kwargs['model'], harness.JUDGE_MODEL)
            return json.dumps({'relevance': 4, 'why_quality': 4, 'editorial': 3, 'followup': 5, 'overall': 4}), Usage(
                model=harness.JUDGE_MODEL, effort='low')

        record = next(run(cases=[CASE], arms=ARMS[:1], scope=None, steps=fake_steps(), judge=True,
                          complete_json=fake_judge))
        self.assertEqual(record['judge']['overall'], 4)

    def test_redirect_case(self):
        case = harness.Case(id='A1', message='Weather?', kind='demo', expect={'redirect_reason': 'off_topic'})
        steps = fake_steps(intent_text='{"on_topic": false, "redirect_reason": "off_topic"}')
        record = next(run(cases=[case], arms=ARMS[:1], scope=None, steps=steps))
        self.assertTrue(record['redirected'])
        self.assertTrue(record['intent_score']['pass'])
        self.assertEqual(record['item_ids'], [])

    def test_cases(self):
        demo = harness.demo_cases()
        self.assertEqual(len(demo), 42)
        refine = [c for c in demo if c.id == 'R1'][0]
        self.assertEqual(refine.history, ('I want to get better at leading a team',))
        personas = harness.persona_cases(load_personas)
        self.assertGreaterEqual(len(personas), 10)
        self.assertTrue(all(c.message and len(c.message) <= 500 for c in personas))
        self.assertTrue(any(c.expected_course_keys for c in personas))


class HarnessScoringTests(TestCase):
    """Per-turn scoring and the per-arm summary."""

    def test_score_intent_refine_checks_the_merged_intent(self):
        expect = {'on_topic': True, 'redirect_reason': None, 'merge': 'refine', 'max_weeks_le': 4}
        debug = {'intent_result': {'on_topic': True, 'redirect_reason': None, 'merge': 'refine'},
                 'merged_intent': {'constraints': {'max_weeks': 6}}}
        self.assertEqual(harness_scoring.score_intent(expect, debug), {'pass': False, 'why': 'missed max_weeks'})
        debug['merged_intent']['constraints']['max_weeks'] = 4
        self.assertTrue(harness_scoring.score_intent(expect, debug)['pass'])
        self.assertIsNone(harness_scoring.score_intent(None, debug))

    def test_recall(self):
        self.assertEqual(harness_scoring.recall(['a', 'b'], ['b', 'c']), 0.5)
        self.assertIsNone(harness_scoring.recall([], ['b']))

    def test_summary_and_report(self):
        rows = [
            {'arm': 'a', 'case_id': 'O1', 'models': ['m1', 'm2'], 'status': 'done', 'redirected': False,
             'timing': {'intent_ms': 1000, 'first_text_ms': 2000, 'first_section_ms': 4000, 'complete_ms': 6000,
                        'retrieval_ms': 200, 'dropped_lines': 1},
             'item_ids': ['x', 'y'], 'intent_score': {'pass': True}, 'cost_usd': 0.004, 'page_recall': None,
             'candidate_recall': None, 'judge': {'overall': 4, 'relevance': 4, 'why_quality': 4, 'editorial': 4,
                                                 'followup': 4}},
            {'arm': 'b', 'case_id': 'A1', 'models': ['xpert', 'xpert'], 'status': 'error', 'error': 'model_failed',
             'redirected': False, 'timing': None, 'item_ids': [], 'intent_score': {'pass': False}, 'cost_usd': None},
        ]
        summary = harness_scoring.summarize(rows)
        self.assertEqual(summary['a']['latency_p50']['first_section_ms'], 4000)
        self.assertEqual(summary['a']['turns_with_dropped_lines'], 1)
        self.assertEqual(summary['b']['errors'], {'model_failed': 1})
        self.assertEqual(summary['b']['adversarial_pass'], '0/1')
        table = harness_scoring.report_markdown(summary)
        self.assertIn('| Metric | a | b |', table)
        self.assertIn('unknown', table)  # xpert cost


@override_settings(PERSONALIZED_SEARCH_OPENAI_MODELS=MODELS, **XPERT)
class HarnessCommandTests(TestCase):
    """The run and report commands."""

    def test_dry_run_plans_default_arms(self):
        out = StringIO()
        call_command('run_personalized_search_harness', '--case-set', 'demo', '--max-calls', '10', '--dry-run',
                     stdout=out)
        plan = json.loads(out.getvalue())
        self.assertEqual(plan['arms'], ['openai-best-fit', 'xpert'])
        self.assertEqual(plan['turns'], 84)

    @override_settings(PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE=None)
    def test_requires_dev_scope(self):
        with self.assertRaises(CommandError):
            call_command('run_personalized_search_harness', '--case-id', 'O1', '--max-calls', '2',
                         stdout=StringIO())

    def test_report(self):
        row = {'arm': 'a', 'case_id': 'O1', 'models': ['m1', 'm2'], 'status': 'done', 'redirected': False,
               'timing': {'intent_ms': 1, 'first_text_ms': 2, 'first_section_ms': 3, 'complete_ms': 4,
                          'retrieval_ms': 1, 'dropped_lines': 0}, 'item_ids': ['x'], 'cost_usd': 0.001}
        with NamedTemporaryFile('w', suffix='.jsonl', delete=False) as handle:
            handle.write(json.dumps(row) + '\n')
        out = StringIO()
        call_command('report_personalized_search_harness', handle.name, stdout=out)
        self.assertIn('| Metric | a |', out.getvalue())
