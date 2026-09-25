"""
Tests for the pathway judge.

Two properties carry the weight. The instrument must be the one the analysis calibrated --
same rubric, same schema, same rendering, temperature 0 -- or its verdicts mean nothing.
And its output is untrusted: a verdict outside the rubric is no verdict, and a key the
judge invents must be counted rather than trusted.
"""
import json

from django.test import TestCase, override_settings

from enterprise_access.apps.pathways.judging import (
    CAREER_SKILLS_SHOWN,
    COURSE_SKILLS_SHOWN,
    DESCRIPTION_CHARS_SHOWN,
    JUDGE_SYSTEM_PROMPT,
    build_user_content,
    get_judge_backend,
    judge_pathway,
    parse_judgement
)
from enterprise_access.apps.pathways.model_backends import (
    ModelBackendConfigurationError,
    ModelBackendRequestError,
    OpenAIBackend
)
from enterprise_access.apps.pathways.prompts import (
    PATHWAY_JUDGE_OUTPUT_SCHEMA,
    PATHWAY_JUDGE_SYSTEM_PROMPT,
    PATHWAY_JUDGE_VERDICTS
)
from enterprise_access.apps.pathways.tests.test_reranking import FakeBackend

COURSES = [
    {
        'key': 'A+1', 'title': 'Intro to Welding', 'level_type': 'Introductory',
        'short_description': 'Learn to weld. ' * 40,
        'skill_names': [f'Skill {n}' for n in range(12)],
    },
    {'key': 'B+2', 'title': 'Metallurgy', 'level_type': '', 'short_description': ''},
]


def verdict_payload(verdict='good', courses=(('A+1', True), ('B+2', False)), reason='Fits.'):
    return {
        'verdict': verdict,
        'reason': reason,
        'courses': [{'key': key, 'on_topic': flag} for key, flag in courses],
    }


class TestTheInstrument(TestCase):
    """
    Scenario: The judge is the instrument the analysis calibrated.
    """

    def test_the_verdicts_are_the_rubric_three(self):
        self.assertEqual(PATHWAY_JUDGE_VERDICTS, ('good', 'weak', 'bad'))
        self.assertEqual(
            PATHWAY_JUDGE_OUTPUT_SCHEMA['properties']['verdict']['enum'], list(PATHWAY_JUDGE_VERDICTS),
        )

    def test_the_system_prompt_carries_the_rubric_and_names_its_fields(self):
        self.assertTrue(JUDGE_SYSTEM_PROMPT.startswith(PATHWAY_JUDGE_SYSTEM_PROMPT))
        for phrase in ('Being introductory is NOT a reason to mark false', 'Be willing to say bad',
                       '"on_topic"', '"verdict"'):
            self.assertIn(phrase, JUDGE_SYSTEM_PROMPT)

    @override_settings(PATHWAYS_JUDGE_BACKEND='openai', PATHWAYS_JUDGE_MODEL='gpt-5.4-mini',
                       PATHWAYS_MODEL_BACKEND='claude')
    def test_the_judge_backend_ignores_the_pipeline_backend_and_pins_temperature(self):
        """A judge that followed the builder's model could not tell better from more lenient."""
        backend = get_judge_backend()

        self.assertIsInstance(backend, OpenAIBackend)
        self.assertEqual(backend.model, 'gpt-5.4-mini')
        self.assertEqual(backend.temperature, 0)

    @override_settings(PATHWAYS_JUDGE_BACKEND='xpert')
    def test_an_xpert_judge_is_refused(self):
        with self.assertRaises(ModelBackendConfigurationError):
            get_judge_backend()


class TestBuildUserContent(TestCase):
    """
    Scenario: A pathway is rendered as the analysis rendered it.
    """

    def test_the_layout_matches_the_analysis(self):
        content = build_user_content(
            career_name='Welder', career_skills=['Welding', 'Blueprints'], courses=COURSES,
        )
        lines = content.split('\n')

        self.assertEqual(lines[0], 'CAREER FAMILY: Welder')
        self.assertEqual(lines[1], 'job titles in this family: Welder')
        self.assertEqual(lines[2], 'skills this career needs (Lightcast): Welding, Blueprints')
        self.assertEqual(lines[3], '')
        self.assertEqual(lines[4], 'RECOMMENDED PATHWAY (2 courses):')
        self.assertEqual(lines[5], '1. [A+1] Intro to Welding (Introductory)')
        self.assertIn('2. [B+2] Metallurgy (level unknown)', lines)

    def test_only_the_first_four_career_skills_are_shown(self):
        content = build_user_content(
            career_name='Welder', career_skills=[f'S{n}' for n in range(9)], courses=COURSES,
        )

        shown = content.split('\n')[2].split(': ', 1)[1].split(', ')
        self.assertEqual(len(shown), CAREER_SKILLS_SHOWN)

    def test_a_career_with_no_skills_says_so(self):
        content = build_user_content(career_name='Welder', career_skills=[], courses=COURSES)

        self.assertIn('(Lightcast): (none resolved)', content)

    def test_course_descriptions_and_skills_are_truncated_as_calibrated(self):
        content = build_user_content(career_name='Welder', career_skills=[], courses=COURSES)
        about = next(line for line in content.split('\n') if line.startswith('   about: '))
        skills = next(line for line in content.split('\n') if line.startswith('   course skills: '))

        self.assertEqual(len(about) - len('   about: '), DESCRIPTION_CHARS_SHOWN)
        self.assertEqual(len(skills.split(': ', 1)[1].split(', ')), COURSE_SKILLS_SHOWN)

    def test_a_course_without_a_description_or_skills_gets_no_empty_lines(self):
        content = build_user_content(career_name='Welder', career_skills=[], courses=COURSES[1:])

        self.assertNotIn('about:', content)
        self.assertNotIn('course skills:', content)


class TestParseJudgement(TestCase):
    """
    Scenario: The judge's output is untrusted.
    """

    def test_a_valid_judgement_is_read(self):
        result = parse_judgement(verdict_payload(), ['A+1', 'B+2'])

        self.assertEqual(result['verdict'], 'good')
        self.assertEqual(result['reason'], 'Fits.')
        self.assertEqual(result['on_topic'], {'A+1': True, 'B+2': False})
        self.assertEqual(result['unjudged_keys'], [])
        self.assertEqual(result['error'], '')

    def test_a_verdict_outside_the_rubric_is_an_error_not_a_verdict(self):
        result = parse_judgement(verdict_payload(verdict='excellent'), ['A+1', 'B+2'])

        self.assertEqual(result['verdict'], '')
        self.assertIn('excellent', result['error'])

    def test_a_non_object_response_is_an_error(self):
        result = parse_judgement(['good'], ['A+1'])

        self.assertEqual(result['verdict'], '')
        self.assertTrue(result['error'])
        self.assertEqual(result['unjudged_keys'], ['A+1'])

    def test_an_invented_key_is_dropped_and_counted(self):
        result = parse_judgement(
            verdict_payload(courses=(('A+1', True), ('Z+9', True))), ['A+1', 'B+2'],
        )

        self.assertEqual(result['on_topic'], {'A+1': True})
        self.assertEqual(result['fabricated_keys'], ['Z+9'])
        self.assertEqual(result['unjudged_keys'], ['B+2'])

    def test_a_repeated_key_keeps_its_first_answer(self):
        result = parse_judgement(
            verdict_payload(courses=(('A+1', True), ('A+1', False))), ['A+1'],
        )

        self.assertEqual(result['on_topic'], {'A+1': True})

    def test_a_non_boolean_answer_is_no_answer(self):
        payload = verdict_payload()
        payload['courses'][1]['on_topic'] = 'no'

        result = parse_judgement(payload, ['A+1', 'B+2'])

        self.assertEqual(result['on_topic'], {'A+1': True})
        self.assertEqual(result['unjudged_keys'], ['B+2'])


class TestJudgePathway(TestCase):
    """
    Scenario: Judging a pathway records a verdict, and a failure records an error.
    """

    def test_a_verdict_and_its_trace_are_returned(self):
        backend = FakeBackend(content=json.dumps(verdict_payload()))

        result = judge_pathway(
            career_name='Welder', career_skills=['Welding'], courses=COURSES,
            trace_id='trace-1', backend=backend,
        )

        self.assertEqual(result['verdict'], 'good')
        self.assertEqual(result['n_on_topic'], 1)
        self.assertEqual(result['n_courses'], 2)
        self.assertEqual(result['trace']['model'], 'fake-1')
        call = backend.calls[0]
        self.assertEqual(call['system_prompt'], JUDGE_SYSTEM_PROMPT)
        self.assertEqual(call['trace_id'], 'trace-1')
        self.assertIn('CAREER FAMILY: Welder', call['user_content'])

    def test_a_request_failure_is_recorded_without_its_message(self):
        backend = FakeBackend(error=ModelBackendRequestError('echoes the learner'))

        result = judge_pathway(
            career_name='Welder', career_skills=[], courses=COURSES, trace_id='t', backend=backend,
        )

        self.assertEqual(result['verdict'], '')
        self.assertIn('ModelBackendRequestError', result['error'])
        self.assertNotIn('echoes the learner', result['error'])
        self.assertEqual(result['unjudged_keys'], ['A+1', 'B+2'])

    @override_settings(PATHWAYS_JUDGE_BACKEND='xpert')
    def test_a_misconfigured_judge_is_recorded_not_raised(self):
        result = judge_pathway(career_name='Welder', career_skills=[], courses=COURSES, trace_id='t')

        self.assertIn('not configured', result['error'])

    def test_a_non_json_response_is_recorded(self):
        backend = FakeBackend(content='good, I think')

        result = judge_pathway(
            career_name='Welder', career_skills=[], courses=COURSES, trace_id='t', backend=backend,
        )

        self.assertEqual(result['error'], 'judge response was not JSON')
        self.assertEqual(result['trace']['backend'], 'fake')
