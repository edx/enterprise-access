"""Tests for the Personalized Search harness, its scoring and its management commands."""
import json
import os
from io import StringIO
from tempfile import NamedTemporaryFile
from unittest import mock

import ddt
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from enterprise_access.apps.pathway_eval.personas import load_personas
from enterprise_access.apps.personalized_search import harness
from enterprise_access.apps.personalized_search.adapters.openai_backend import OpenAIBackend
from enterprise_access.apps.personalized_search.adapters.xpert_backend import XpertBackend
from enterprise_access.apps.personalized_search.core import compose, constants, harness_scoring, retrieval
from enterprise_access.apps.personalized_search.core.errors import ModelError
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
        self.assertEqual(first['item_ids'], ['edX+C1', 'p-1', 'p-2'])  # related programs are offered too
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

    def test_budget_stop_is_logged_once_and_ends_the_run(self):
        messages = []
        records = list(run(cases=[CASE] * 3, arms=ARMS, scope=None, steps=fake_steps(), max_calls=3,
                           log=messages.append))
        self.assertEqual([m for m in messages if m.startswith('Stopped')],
                         ['Stopped: max_calls=3 reached after 3 calls.'])
        self.assertTrue(all(r['status'] == 'done' for r in records))  # no error turn from the refused call

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

    def test_persona_cases_can_be_limited_to_some_ids(self):
        everyone = harness.persona_cases(load_personas)
        wanted = everyone[0].id
        self.assertEqual([c.id for c in harness.persona_cases(load_personas, [wanted])], [wanted])

    def test_prior_turns_seed_the_session(self):
        case = harness.Case(id='R9', message='make it shorter', kind='demo', history=('lead a team',))
        record = next(run(cases=[case], arms=ARMS[:1], scope=None, steps=fake_steps()))
        self.assertEqual(record['status'], 'done')

    def test_an_error_part_marks_the_turn(self):
        # pylint: disable-next=protected-access
        turn = harness._summarize_turn([{'type': 'error', 'errorText': 'model_failed'}])
        self.assertEqual((turn['status'], turn['error']), ('error', 'model_failed'))

    def test_a_page_without_items_is_not_judged(self):
        case = harness.Case(id='A1', message='Weather?', kind='demo', expect={'redirect_reason': 'off_topic'})
        steps = fake_steps(intent_text='{"on_topic": false, "redirect_reason": "off_topic"}')
        record = next(run(cases=[case], arms=ARMS[:1], scope=None, steps=steps, judge=True))
        self.assertEqual(record['judge'], {'skipped': 'no items on the page'})

    def test_a_failed_or_unreadable_judge_is_recorded_not_raised(self):
        def failing(**_):
            raise ModelError('down')

        def unreadable(**_):
            return 'no json at all', Usage(model=harness.JUDGE_MODEL, effort='low')

        for judge_call, name in ((failing, 'ModelError'), (unreadable, 'ValueError')):
            record = next(run(cases=[CASE], arms=ARMS[:1], scope=None, steps=fake_steps(), judge=True,
                              complete_json=judge_call))
            self.assertEqual(record['judge'], {'error': name})

    def test_the_budget_can_run_out_before_the_judge(self):
        messages = []
        records = list(run(cases=[CASE, CASE], arms=ARMS[:1], scope=None, steps=fake_steps(), judge=True,
                           max_calls=2, log=messages.append))
        self.assertEqual(len(records), 1)
        self.assertTrue(any('before judging' in m for m in messages))


@ddt.ddt
class HarnessConversationTests(TestCase):
    """Three-turn conversations and the harness-only arm options."""

    def setUp(self):
        super().setUp()
        self.addCleanup(cache.clear)

    def test_arm_parse_options(self):
        arm = harness.Arm.parse('x=gpt-4.1-mini,gpt-5.4-mini@low+cold:high+cands:40+view:rich',
                                check_model=REGISTRY.check_model)
        self.assertEqual((arm.cold_effort, arm.candidates, arm.view), ('high', 40, 'rich'))
        self.assertEqual(arm.compose_for_turn(0), ModelChoice('gpt-5.4-mini', 'high'))
        self.assertEqual(arm.compose_for_turn(1), ModelChoice('gpt-5.4-mini', 'low'))
        self.assertEqual(arm.options, {'cold': 'high', 'cands': 40, 'view': 'rich'})
        plain = harness.Arm.parse('y=gpt-4.1-mini,gpt-5.4-mini')
        self.assertEqual(plain.options, {'cold': None, 'cands': 20, 'view': 'standard'})
        self.assertEqual(plain.compose_for_turn(0), plain.compose)

    @ddt.data('+cold', '+speed:1', '+view:huge', '+cands:0', '+cands:many')
    def test_arm_parse_rejects_bad_options(self, option):
        with self.assertRaises(ValueError):
            harness.Arm.parse(f'x=gpt-4.1-mini,gpt-5.4-mini{option}')

    @ddt.data('x=gpt-4.1-mini,gpt-6.1-sol@max', 'x=gpt-4.1-mini,gpt-6.1-sol@low+cold:max')
    def test_arm_parse_checks_each_effort(self, spec):
        registry = ModelRegistry([OpenAIBackend(api_key='k', models=dict(MODELS, **{'gpt-6.1-sol': (2, 0.1, 10)}))])
        with self.assertRaises(ModelError):
            harness.Arm.parse(spec, check_model=registry.check_model, check_effort=registry.resolve_effort)

    def test_arm_overrides_scale_the_shortlist_and_restore_it(self):
        arm = harness.Arm('x', ARMS[0].intent, ARMS[0].compose, candidates=40, view='rich')
        intent = {'query': 'leadership', 'skills': ['Leadership']}
        before = retrieval.build_sub_requests(intent, retrieval.CatalogScope(filter='f'), [])
        with harness.arm_overrides(arm):
            self.assertEqual(constants.MAX_CANDIDATES, 40)
            self.assertEqual(constants.CANDIDATE_CAPS, {'course': 20, 'executive_education': 10, 'program': 10})
            self.assertEqual(constants.PREFERRED_TYPE_CAP, 20)
            self.assertIn('outcome', constants.ATTRIBUTES_TO_RETRIEVE)
            self.assertIs(compose.compose_view, harness.rich_compose_view)
            during = retrieval.build_sub_requests(intent, retrieval.CatalogScope(filter='f'), [])
        self.assertEqual([r['hitsPerPage'] * 2 if r['hitsPerPage'] else 0 for r in before],
                         [r['hitsPerPage'] for r in during])
        self.assertEqual(constants.MAX_CANDIDATES, 20)
        self.assertEqual(constants.CANDIDATE_CAPS, {'course': 10, 'executive_education': 5, 'program': 5})
        self.assertNotIn('outcome', constants.ATTRIBUTES_TO_RETRIEVE)
        self.assertIs(compose.compose_view, retrieval.compose_view)

    def test_rich_compose_view(self):
        course = {'hit': {'skill_names': [f's{i}' for i in range(10)], 'subjects': ['Data'],
                          'outcome': '<ul><li>Write SQL</li></ul>', 'prerequisites': '<p>None</p>'},
                  'card': {'item_id': 'edX+C1', 'item_type': 'course', 'title': 'SQL', 'partner_name': 'edX',
                           'facts': {}}}
        view = harness.rich_compose_view(course)
        self.assertEqual(len(view['skills']), 8)
        self.assertEqual((view['you_will_learn'], view['prerequisites'], view['subjects']),
                         ('Write SQL', 'None', ['Data']))
        program = {'hit': {'learning_items': ['<p>Lead teams</p>', {'title': 'Plan work'}],
                           'course_details': [{'title': 'Course A'}, {'key': 'no-title'}]},
                   'card': {'item_id': 'p-1', 'item_type': 'program', 'title': 'Leading', 'partner_name': 'edX',
                            'facts': {}}}
        view = harness.rich_compose_view(program)
        self.assertEqual((view['you_will_learn'], view['courses']), (['Lead teams', 'Plan work'], ['Course A']))

    def test_choose_chip(self):
        followups = {'suggestions': ['Introductory', 'Advanced'], 'ideas': ['Show me advanced MLOps']}
        self.assertEqual(harness.choose_chip(followups, ('advanced', 'mlops')), 'Show me advanced MLOps')
        self.assertEqual(harness.choose_chip(followups, ('advanced',)), 'Advanced')  # ties go to the first
        self.assertIsNone(harness.choose_chip(followups, ('welding',)))
        self.assertIsNone(harness.choose_chip(None, ('advanced',)))

    def test_conversations_open_with_each_persona_message(self):
        openings = {c.id: c.message for c in harness.persona_cases(load_personas)}
        loaded = harness.conversations(load_personas)
        self.assertEqual(len(loaded), 10)
        for conversation in loaded:
            self.assertEqual(len(conversation.messages), 3)
            self.assertEqual(conversation.messages[0], openings[conversation.id])
            self.assertTrue(conversation.goal and conversation.keywords)
        self.assertEqual([c.id for c in harness.conversations(load_personas, ['p010-welder'])], ['p010-welder'])

    def test_a_conversation_carries_one_session_through_its_turns(self):
        seen = []
        steps = fake_steps()
        base_intent = steps.intent

        def intent(**kwargs):
            seen.append(kwargs['user'])
            return base_intent(**kwargs)

        steps.intent = intent
        conversation = harness.Conversation(id='c1', goal='lead', messages=('first ask', 'second ask', 'third ask'))
        arm = harness.Arm('a', ARMS[0].intent, ARMS[0].compose, cold_effort='high')
        records = list(harness.run_conversations(conversation_list=[conversation], arms=[arm], scope=None,
                                                 steps=steps))
        self.assertEqual([r['turn_index'] for r in records], [0, 1, 2])
        self.assertEqual([r['message'] for r in records], ['first ask', 'second ask', 'third ask'])
        self.assertEqual([r['models'][1] for r in records], ['gpt-5.4-mini@high', 'gpt-5.4-mini@low',
                                                             'gpt-5.4-mini@low'])
        self.assertIn('first ask', seen[2])  # the third turn's intent step sees the earlier turns
        first = records[0]
        self.assertEqual(first['status'], 'done')
        self.assertTrue(first['page_item_ids'])
        self.assertEqual(first['block_types'][0], 'page_header')
        self.assertEqual(first['usage']['compose']['output_tokens'], 50)
        self.assertEqual(first['arm_options'], {'cold': 'high', 'cands': 20, 'view': 'standard'})
        self.assertIsNone(first['chosen_chip'])

    def test_an_adaptive_learner_answers_with_a_matching_chip(self):
        conversation = harness.Conversation(id='c1', goal='lead', messages=('first', 'second', 'third'),
                                            keywords=tuple('aeiou'))
        records = list(harness.run_conversations(conversation_list=[conversation], arms=ARMS[:1], scope=None,
                                                 steps=fake_steps(), style='adaptive'))
        chips = records[0]['followups']['suggestions'] + (records[0]['followups'].get('ideas') or [])
        self.assertIn(records[1]['chosen_chip'], chips)
        self.assertEqual(records[1]['message'], records[1]['chosen_chip'])
        unmatched = harness.Conversation(id='c2', goal='lead', messages=('first', 'second'), keywords=('zzz',))
        records = list(harness.run_conversations(conversation_list=[unmatched], arms=ARMS[:1], scope=None,
                                                 steps=fake_steps(), style='adaptive'))
        self.assertEqual(records[1]['message'], 'second')

    @ddt.data('compose', 'intent')
    def test_a_failed_model_call_is_recorded_with_its_reason(self, step):
        error = ModelError('OpenAI stream failed (APITimeoutError) for model x.')
        steps = fake_steps(**{f'{step}_error': error})
        conversation = harness.Conversation(id='c1', goal='lead', messages=('first',))
        record, = harness.run_conversations(conversation_list=[conversation], arms=ARMS[:1], scope=None, steps=steps)
        self.assertEqual((record['status'], record['error']), ('error', 'model_failed'))
        self.assertEqual(record['model_error'], f'{step}: OpenAI stream failed (APITimeoutError) for model x.')
        if step == 'compose':  # the stream got as far as its text before failing
            self.assertEqual(record['usage']['compose']['output_tokens'], 50)

    def test_model_sections_only_drops_server_sections(self):
        original = compose.ComposeParser._server_section  # pylint: disable=protected-access
        conversation = harness.Conversation(id='c1', goal='lead', messages=('first',))
        with_server, = harness.run_conversations(conversation_list=[conversation], arms=ARMS[:1], scope=None,
                                                 steps=fake_steps())
        model_only, = harness.run_conversations(conversation_list=[conversation], arms=ARMS[:1], scope=None,
                                                steps=fake_steps(), server_sections=False)
        server_built = [b for b in with_server['blocks'] if b['type'] == 'item_section' and b['props']['fallback']]
        self.assertTrue(server_built)  # the canned page gets related programs from the server
        self.assertFalse([b for b in model_only['blocks'] if b['type'] == 'item_section' and b['props']['fallback']])
        self.assertEqual((with_server['server_sections'], model_only['server_sections']), (True, False))
        self.assertIs(compose.ComposeParser._server_section, original)  # pylint: disable=protected-access

    def test_turn_deadline_is_restored(self):
        with harness.turn_deadline(60):
            self.assertEqual(constants.TURN_DEADLINE_S, 60)
        self.assertEqual(constants.TURN_DEADLINE_S, 20)
        with harness.turn_deadline(None):
            self.assertEqual(constants.TURN_DEADLINE_S, 20)

    def test_conversation_budget_and_style_checks(self):
        conversation = harness.Conversation(id='c1', goal='lead', messages=('first', 'second'))
        messages = []
        records = list(harness.run_conversations(conversation_list=[conversation], arms=ARMS[:1], scope=None,
                                                 steps=fake_steps(), max_calls=3, log=messages.append))
        self.assertEqual(len(records), 1)
        self.assertTrue(any('max_calls=3' in m for m in messages))
        with self.assertRaises(ValueError):
            list(harness.run_conversations(conversation_list=[conversation], arms=ARMS[:1], scope=None,
                                           steps=fake_steps(), style='chatty'))
        plan = harness.plan_conversations([conversation], ARMS, runs=2)
        self.assertEqual((plan['turns'], plan['max_paid_calls']), (8, 16))


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

    def test_score_intent_redirect_variants(self):
        def seen(on_topic, redirect):
            return {'intent_result': {'on_topic': on_topic, 'redirect_reason': redirect}}

        either = {'redirect_reason': 'injection_or_on_topic'}
        self.assertTrue(harness_scoring.score_intent(either, seen(False, 'injection'))['pass'])
        self.assertTrue(harness_scoring.score_intent(either, seen(True, None))['pass'])
        self.assertFalse(harness_scoring.score_intent(either, seen(False, 'off_topic'))['pass'])
        other = {'redirect_reason': 'injection_or_off_topic'}
        self.assertTrue(harness_scoring.score_intent(other, seen(False, 'off_topic'))['pass'])
        self.assertFalse(harness_scoring.score_intent(other, seen(True, None))['pass'])

    def test_score_intent_flags_a_wrong_merge_mode(self):
        debug = {'intent_result': {'on_topic': True, 'redirect_reason': None, 'merge': 'replace'}}
        score = harness_scoring.score_intent({'on_topic': True, 'merge': 'refine'}, debug)
        self.assertEqual(score, {'pass': False, 'why': 'merge=replace'})

    def test_score_intent_checks_each_expected_field(self):
        debug = {'intent_result': {'on_topic': True, 'redirect_reason': None},
                 'merged_intent': {'level': 'introductory', 'item_types': ['program', 'course'],
                                   'skills': ['Team Leadership'], 'roles': ['manager'],
                                   'constraints': {'max_weeks': 3, 'pacing': 'self_paced'}}}
        good = {'on_topic': True, 'pacing': 'self_paced', 'level': 'introductory', 'level_in': ['introductory'],
                'item_types': ['course', 'program'], 'skills_contain': 'leader', 'career': True}
        self.assertTrue(harness_scoring.score_intent(good, debug)['pass'])
        bad = {'on_topic': True, 'pacing': 'instructor_led', 'level': 'advanced', 'level_in': ['advanced'],
               'item_types': ['course'], 'skills_contain': 'python', 'career': True}
        debug['merged_intent']['roles'] = []
        why = harness_scoring.score_intent(bad, debug)['why']
        for name in ('pacing', 'level', 'item_types', 'skills', 'career'):
            self.assertIn(name, why)

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

    def test_an_unknown_arm_model_is_a_command_error(self):
        with self.assertRaises(CommandError):
            call_command('run_personalized_search_harness', '--arm', 'x=gpt-9,gpt-4.1-mini', '--max-calls', '2',
                         stdout=StringIO())

    def test_no_matching_cases_is_a_command_error(self):
        with self.assertRaises(CommandError):
            call_command('run_personalized_search_harness', '--case-id', 'nope', '--max-calls', '2',
                         stdout=StringIO())

    @override_settings(OPENAI_API_KEY='k', PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE={'filter': 'f', 'label': 'l'})
    def test_a_run_writes_one_row_per_turn_to_the_output_file(self):
        with NamedTemporaryFile(suffix='.jsonl', delete=False) as handle:
            path = handle.name
        self.addCleanup(os.remove, path)
        err = StringIO()
        with mock.patch('enterprise_access.apps.personalized_search.wiring.steps', return_value=fake_steps()):
            call_command('run_personalized_search_harness', '--case-id', 'O1', '--arm',
                         'a=gpt-4.1-mini,gpt-5.4-mini@low', '--max-calls', '10', '--output', path,
                         stdout=StringIO(), stderr=err)
        with open(path, encoding='utf-8') as written:
            rows = [json.loads(line) for line in written]
        self.assertEqual([r['case_id'] for r in rows], ['O1'])
        self.assertIn('Wrote 1 turns.', err.getvalue())

    @override_settings(OPENAI_API_KEY='k', PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE={'filter': 'f', 'label': 'l'})
    def test_a_run_without_an_output_file_prints_the_rows(self):
        out = StringIO()
        with mock.patch('enterprise_access.apps.personalized_search.wiring.steps', return_value=fake_steps()):
            call_command('run_personalized_search_harness', '--case-id', 'O1', '--arm',
                         'a=gpt-4.1-mini,gpt-5.4-mini@low', '--max-calls', '10', stdout=out, stderr=StringIO())
        self.assertIn('"case_id": "O1"', out.getvalue())

    def test_conversations_dry_run(self):
        out = StringIO()
        call_command('run_personalized_search_harness', '--case-set', 'conversations', '--max-calls', '10',
                     '--dry-run', stdout=out)
        plan = json.loads(out.getvalue())
        self.assertEqual((plan['conversations'], plan['turns']), (10, 60))

    @override_settings(OPENAI_API_KEY='k', PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE={'filter': 'f', 'label': 'l'})
    def test_harness_timeouts_override_the_settings(self):
        seen = []
        steps = fake_steps()
        base_compose = steps.compose

        def timed_compose(**kwargs):
            seen.append(kwargs['timeout_s'])
            return base_compose(**kwargs)

        steps.compose = timed_compose
        with mock.patch('enterprise_access.apps.personalized_search.wiring.steps', return_value=steps):
            call_command('run_personalized_search_harness', '--case-set', 'conversations', '--case-id',
                         'p010-welder', '--arm', 'a=gpt-4.1-mini,gpt-5.4-mini@low', '--max-calls', '10',
                         '--compose-timeout-s', '12', '--intent-timeout-s', '6', '--turn-deadline-s', '40',
                         stdout=StringIO(), stderr=StringIO())
        self.assertTrue(seen and all(timeout > 5 for timeout in seen))

    def test_conversations_have_no_judge(self):
        with self.assertRaises(CommandError):
            call_command('run_personalized_search_harness', '--case-set', 'conversations', '--judge',
                         '--max-calls', '10', stdout=StringIO())

    def test_no_matching_conversations_is_a_command_error(self):
        with self.assertRaises(CommandError):
            call_command('run_personalized_search_harness', '--case-set', 'conversations', '--case-id', 'nope',
                         '--max-calls', '10', stdout=StringIO())

    @override_settings(PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE=None)
    def test_conversations_require_dev_scope(self):
        with self.assertRaises(CommandError):
            call_command('run_personalized_search_harness', '--case-set', 'conversations', '--max-calls', '10',
                         stdout=StringIO())

    @override_settings(OPENAI_API_KEY='k', PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE={'filter': 'f', 'label': 'l'})
    def test_a_conversation_run_writes_one_row_per_turn(self):
        out, err = StringIO(), StringIO()
        with mock.patch('enterprise_access.apps.personalized_search.wiring.steps', return_value=fake_steps()):
            call_command('run_personalized_search_harness', '--case-set', 'conversations', '--case-id',
                         'p010-welder', '--conversation-style', 'adaptive', '--arm',
                         'a=gpt-4.1-mini,gpt-5.4-mini@low+cold:high', '--max-calls', '10', stdout=out, stderr=err)
        rows = [json.loads(line) for line in out.getvalue().splitlines()[1:]]
        self.assertEqual([(r['conversation_id'], r['turn_index'], r['style']) for r in rows],
                         [('p010-welder', i, 'adaptive') for i in range(3)])
        self.assertIn('Wrote 3 turns.', err.getvalue())

    @override_settings(OPENAI_API_KEY='k', PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE={'filter': 'f', 'label': 'l'})
    def test_a_conversation_run_can_write_a_file(self):
        with NamedTemporaryFile(suffix='.jsonl', delete=False) as handle:
            path = handle.name
        self.addCleanup(os.remove, path)
        with mock.patch('enterprise_access.apps.personalized_search.wiring.steps', return_value=fake_steps()):
            call_command('run_personalized_search_harness', '--case-set', 'conversations', '--case-id',
                         'p010-welder', '--arm', 'a=gpt-4.1-mini,gpt-5.4-mini@low', '--max-calls', '10',
                         '--no-server-sections', '--output', path, stdout=StringIO(), stderr=StringIO())
        with open(path, encoding='utf-8') as written:
            rows = [json.loads(line) for line in written]
        self.assertEqual([r['server_sections'] for r in rows], [False, False, False])

    def test_report_needs_a_readable_file_with_records(self):
        with self.assertRaises(CommandError):
            call_command('report_personalized_search_harness', '/nonexistent/harness.jsonl', stdout=StringIO())
        with NamedTemporaryFile('w', suffix='.jsonl', delete=False) as handle:
            handle.write('\n')
        self.addCleanup(os.remove, handle.name)
        with self.assertRaises(CommandError):
            call_command('report_personalized_search_harness', handle.name, stdout=StringIO())

    def test_report(self):
        row = {'arm': 'a', 'case_id': 'O1', 'models': ['m1', 'm2'], 'status': 'done', 'redirected': False,
               'timing': {'intent_ms': 1, 'first_text_ms': 2, 'first_section_ms': 3, 'complete_ms': 4,
                          'retrieval_ms': 1, 'dropped_lines': 0}, 'item_ids': ['x'], 'cost_usd': 0.001}
        with NamedTemporaryFile('w', suffix='.jsonl', delete=False) as handle:
            handle.write(json.dumps(row) + '\n')
        out = StringIO()
        call_command('report_personalized_search_harness', handle.name, stdout=out)
        self.assertIn('| Metric | a |', out.getvalue())
