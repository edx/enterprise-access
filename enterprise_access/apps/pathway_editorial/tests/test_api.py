"""Tests for the pathway editorial public interface."""
import copy
import importlib
import json
from pathlib import Path

import ddt
from django.test import SimpleTestCase, TestCase

from enterprise_access.apps.pathway_editorial import api
from enterprise_access.apps.pathway_editorial.api import (
    LEVELS,
    EditorialPolicy,
    FlagshipRule,
    PromotedTopic,
    Seat,
    is_excluded,
    is_promoted,
    load_policy,
    plan_seats
)
from enterprise_access.apps.pathway_editorial.models import PathwayCourseRule, PathwayPromotedTopic

AI_SKILLS = (
    'Artificial Intelligence', 'Generative Artificial Intelligence', 'Prompt Engineering',
    'Large Language Modeling', 'Responsible AI', 'ChatGPT', 'Deep Learning',
)

#: A small AI topic for unit tests. The real-data tests use the seeded one, below.
AI_TOPIC = PromotedTopic(
    name='Artificial Intelligence',
    subjects=('Artificial Intelligence',),
    skill_names=AI_SKILLS,
    title_terms=('AI', 'AI-Powered', 'ChatGPT', 'Gen AI', 'GenAI', 'Generative'),
)

SEED_0003 = importlib.import_module(
    'enterprise_access.apps.pathway_editorial.migrations.0003_promotedtopic_title_terms'
)

#: The AI topic exactly as migrations 0002 and 0003 seed it.
SEEDED_AI_TOPIC = PromotedTopic(
    name=SEED_0003.TOPIC_NAME,
    subjects=tuple(SEED_0003.AI_TOPIC_DEFAULTS['subjects']),
    skill_names=tuple(SEED_0003.AI_TOPIC_DEFAULTS['skill_names']),
    max_per_pathway=SEED_0003.AI_TOPIC_DEFAULTS['max_per_pathway'],
    gate_top_k=SEED_0003.AI_TOPIC_DEFAULTS['gate_top_k'],
    title_terms=tuple(SEED_0003.AI_TITLE_TERMS),
)

FIXTURES = Path(__file__).parent / 'fixtures'

#: The review shapes, as Introductory/Intermediate/Advanced counts.
REVIEW_SHAPES = (
    '2/0/0', '0/2/0', '2/2/1', '1/2/1', '1/1/1', '2/1/0', '1/2/0', '0/2/1', '3/2/0', '2/3/0', '0/1/2', '0/0/2',
)


def course(key, level, skills=(), subjects=None, title='', short='', full=''):
    """A ``CourseCandidate`` dict, as the pathways app stores it."""
    candidate = {
        'key': key,
        'title': title or key,
        'level_type': level,
        'partner': 'Partner',
        'skill_names': list(skills),
        'short_description': short,
        'full_description': full,
        'language': 'English',
    }
    if subjects is not None:
        candidate['subjects'] = list(subjects)
    return candidate


def ai_course(key, level, skills=('ChatGPT', 'Supply Chain'), short='Supply chain planning with AI.', **kwargs):
    """An AI course by title, tagged with and describing the career skill "Supply Chain"."""
    return course(key, level, skills, title=f'AI for {key}', short=short, **kwargs)


def seat_keys(seats):
    return [seat.key for seat in seats]


class PolicyValueTests(SimpleTestCase):
    """The frozen value types and their JSON form."""

    def test_empty_policy(self):
        policy = EditorialPolicy()
        self.assertTrue(policy.is_empty)
        self.assertEqual(policy.to_dict(), {'excluded_keys': [], 'flagships': [], 'promoted': []})

    def test_from_dict_none_or_empty_is_empty_policy(self):
        self.assertEqual(EditorialPolicy.from_dict(None), EditorialPolicy())
        self.assertEqual(EditorialPolicy.from_dict({}), EditorialPolicy())
        self.assertTrue(EditorialPolicy.from_dict(None).is_empty)

    def test_any_rule_makes_policy_non_empty(self):
        self.assertFalse(EditorialPolicy(excluded_keys=frozenset({'a+b'})).is_empty)
        self.assertFalse(EditorialPolicy(flagships=(FlagshipRule('a+b', 'Introductory'),)).is_empty)
        self.assertFalse(EditorialPolicy(promoted=(AI_TOPIC,)).is_empty)

    def test_round_trip_through_json(self):
        policy = EditorialPolicy(
            excluded_keys=frozenset({'Zeta+Z1', 'alpha+A1'}),
            flagships=(
                FlagshipRule('HarvardX+CS50P', 'Introductory', ('Software Development', 'Debugging'), 'why'),
                FlagshipRule('MITx+6.00', 'Intermediate'),
            ),
            promoted=(AI_TOPIC, PromotedTopic('Data', skill_names=('Data Analysis',), max_per_pathway=2, gate_top_k=5)),
        )
        data = json.loads(json.dumps(policy.to_dict()))

        self.assertEqual(EditorialPolicy.from_dict(data), policy)
        self.assertEqual(EditorialPolicy.from_dict(data).to_dict(), policy.to_dict())
        self.assertEqual(
            data['promoted'][0]['title_terms'], ['AI', 'AI-Powered', 'ChatGPT', 'Gen AI', 'GenAI', 'Generative'],
        )

    def test_from_dict_reads_a_snapshot_taken_before_title_terms(self):
        data = {'promoted': [{'name': 'AI', 'subjects': ['Artificial Intelligence'], 'skill_names': ['ChatGPT'],
                              'max_per_pathway': 1, 'gate_top_k': 10}]}
        self.assertEqual(EditorialPolicy.from_dict(data).promoted[0].title_terms, ())

    def test_to_dict_is_sorted_and_keeps_rule_order(self):
        policy = EditorialPolicy(
            excluded_keys=frozenset({'b+2', 'A+1', 'c+3'}),
            flagships=(FlagshipRule('z+1', 'Advanced', ('Unit Testing', 'debugging')), FlagshipRule('a+1', 'Advanced')),
        )
        data = policy.to_dict()

        self.assertEqual(data['excluded_keys'], ['A+1', 'b+2', 'c+3'])
        self.assertEqual([rule['course_key'] for rule in data['flagships']], ['z+1', 'a+1'])
        self.assertEqual(data['flagships'][0]['scope_skills'], ['debugging', 'Unit Testing'])

    def test_to_dict_is_stable_whatever_the_input_order(self):
        one = PromotedTopic('T', subjects=('b', 'a'), skill_names=('y', 'x', 'X '), title_terms=('LLM', 'AI'))
        two = PromotedTopic('T', subjects=['a', 'b', 'a'], skill_names=['x', 'y'], title_terms=['AI', 'LLM', 'llm'])
        self.assertEqual(one, two)
        self.assertEqual(json.dumps(one.to_dict()), json.dumps(two.to_dict()))

    def test_flagship_level_is_validated_and_canonicalised(self):
        self.assertEqual(FlagshipRule('a+b', ' intermediate ').level, 'Intermediate')
        with self.assertRaises(ValueError):
            FlagshipRule('a+b', 'Expert')
        with self.assertRaises(ValueError):
            FlagshipRule('a+b', '')
        with self.assertRaises(ValueError):
            FlagshipRule('  ', 'Advanced')

    def test_topic_needs_a_name(self):
        with self.assertRaises(ValueError):
            PromotedTopic(' ')

    def test_seat_to_dict(self):
        seat = Seat('a+b', 'Advanced', 'promoted:AI', 'because')
        self.assertEqual(
            seat.to_dict(), {'key': 'a+b', 'level': 'Advanced', 'rule': 'promoted:AI', 'reason': 'because'},
        )

    def test_levels(self):
        self.assertEqual(LEVELS, ('Introductory', 'Intermediate', 'Advanced'))


@ddt.ddt
class MatchingTests(SimpleTestCase):
    """``is_excluded`` and ``is_promoted``."""

    def test_is_excluded_ignores_case_and_space(self):
        policy = EditorialPolicy(excluded_keys=frozenset({'State-Bank-of-India+SBSC0015x'}))
        self.assertTrue(is_excluded('State-Bank-of-India+SBSC0015x', policy))
        self.assertTrue(is_excluded(' state-bank-of-india+sbsc0015X ', policy))
        self.assertFalse(is_excluded('State-Bank-of-India+SBSC0012x', policy))
        self.assertFalse(is_excluded('', policy))
        self.assertFalse(is_excluded('anything', EditorialPolicy()))

    @ddt.data(
        ('AI for Sales Professionals', True),
        ('Agile with AI', True),
        ("AI's Next Decade", True),
        ('Product Management: Building AI-Powered Products', True),
        ('Generative AI: A Game Changer for Program Managers', True),
        ('Using GENAI at Work', True),                       # a term matches in any case
        ('Working with Gen\n  AI', True),                   # spaces in a term match any whitespace
        ('ChatGPT for Teachers', True),
        ('Maintenance Planning', False),                    # "ai" inside a word is not the term
        ('Aircraft Systems', False),
        ('Machine Learning Foundations', False),            # not a term of this topic
        ('Project Management Basics', False),
        ('', False),
    )
    @ddt.unpack
    def test_is_promoted_by_title_term(self, title, expected):
        self.assertEqual(is_promoted({'title': title, 'skill_names': []}, AI_TOPIC), expected)

    @ddt.data(
        ({'title': 'Project Management Basics', 'skill_names': ['Artificial Intelligence', 'ChatGPT']}, False),
        ({'title': 'Statistics', 'subjects': ['artificial intelligence ']}, True),
        ({'title': 'Statistics', 'subjects': ['Computer Science']}, False),
        ({'skill_names': ['Artificial Intelligence']}, False),
        ({'title': None, 'subjects': None}, False),
        ({}, False),
    )
    @ddt.unpack
    def test_is_promoted_by_subject_and_never_by_skill_tag_alone(self, candidate, expected):
        self.assertEqual(is_promoted(candidate, AI_TOPIC), expected)

    def test_a_topic_without_terms_or_subjects_matches_nothing(self):
        topic = PromotedTopic('Tags only', skill_names=('Artificial Intelligence',))
        self.assertFalse(is_promoted({'title': 'AI for Everyone', 'skill_names': ['Artificial Intelligence']}, topic))

    def test_terms_are_literal_text(self):
        topic = PromotedTopic('Odd', title_terms=('C++', 'A.I.'))
        self.assertTrue(is_promoted({'title': 'Modern C++ in Practice'}, topic))
        self.assertTrue(is_promoted({'title': 'A.I. for Managers'}, topic))
        self.assertFalse(is_promoted({'title': 'AxIx for Managers'}, topic))


class FlagshipTests(SimpleTestCase):
    """Flagship seating. ``SimpleTestCase`` also proves ``plan_seats`` makes no queries."""

    RULE = FlagshipRule('HarvardX+CS50P', 'Introductory', ('Software Development', 'Debugging'), 'round 1')

    def plan(self, candidates, shape=(2, 2, 1), career=('Software Development',), rules=(RULE,), **policy):
        return plan_seats(
            ordered_candidates=candidates, shape=shape, career_skills=list(career),
            policy=EditorialPolicy(flagships=tuple(rules), **policy),
        )

    def test_seated_when_in_window_at_its_level_and_in_scope(self):
        window = [course('X+1', 'Introductory'), course('HarvardX+CS50P', 'Introductory')]
        self.assertEqual(self.plan(window), [Seat('HarvardX+CS50P', 'Introductory', 'flagship', 'round 1')])

    def test_not_seated_when_absent_from_window(self):
        self.assertEqual(self.plan([course('X+1', 'Introductory')]), [])

    def test_not_seated_when_window_has_it_at_another_level(self):
        self.assertEqual(self.plan([course('HarvardX+CS50P', 'Intermediate')]), [])

    def test_not_seated_when_its_level_has_no_quota(self):
        self.assertEqual(self.plan([course('HarvardX+CS50P', 'Introductory')], shape=(0, 2, 1)), [])

    def test_scope_must_intersect_career_skills(self):
        window = [course('HarvardX+CS50P', 'Introductory')]
        self.assertEqual(self.plan(window, career=('Data Analysis', 'Python (Programming Language)')), [])
        self.assertEqual(seat_keys(self.plan(window, career=(' debugging ',))), ['HarvardX+CS50P'])

    def test_unscoped_rule_applies_to_every_career(self):
        rule = FlagshipRule('HarvardX+CS50P', 'Introductory')
        window = [course('HarvardX+CS50P', 'Introductory')]
        self.assertEqual(seat_keys(self.plan(window, career=(), rules=(rule,))), ['HarvardX+CS50P'])

    def test_rule_order_decides_who_gets_limited_capacity(self):
        rules = (FlagshipRule('B+2', 'Introductory'), FlagshipRule('A+1', 'Introductory'))
        window = [course('A+1', 'Introductory'), course('B+2', 'Introductory')]
        self.assertEqual(seat_keys(self.plan(window, shape=(1, 0, 0), rules=rules)), ['B+2'])
        self.assertEqual(seat_keys(self.plan(window, shape=(2, 0, 0), rules=rules)), ['B+2', 'A+1'])

    def test_same_key_is_never_seated_twice(self):
        rules = (FlagshipRule('A+1', 'Introductory'), FlagshipRule('a+1', 'Introductory'))
        window = [course('A+1', 'Introductory'), course('A+1', 'Introductory')]
        self.assertEqual(seat_keys(self.plan(window, rules=rules)), ['A+1'])

    def test_excluded_flagship_is_never_seated(self):
        window = [course('HarvardX+CS50P', 'Introductory')]
        self.assertEqual(self.plan(window, excluded_keys=frozenset({'HarvardX+CS50P'})), [])

    def test_seat_uses_the_window_spelling_of_the_key(self):
        rule = FlagshipRule('harvardx+cs50p', 'introductory')
        seats = self.plan([course('HarvardX+CS50P', 'Introductory')], rules=(rule,))
        self.assertEqual(seat_keys(seats), ['HarvardX+CS50P'])


class PromotedTopicTests(SimpleTestCase):
    """Promoted topic seating."""

    CAREER = ['Operations Management', 'Supply Chain', 'Artificial Intelligence']

    def plan(self, candidates, shape=(2, 2, 1), career=None, topics=(AI_TOPIC,), **policy):
        return plan_seats(
            ordered_candidates=candidates, shape=shape,
            career_skills=self.CAREER if career is None else career,
            policy=EditorialPolicy(promoted=tuple(topics), **policy),
        )

    def test_seats_an_ai_course_about_the_careers_work(self):
        seats = self.plan([ai_course('AI+1', 'Intermediate')])
        self.assertEqual(seats, [Seat(
            'AI+1', 'Intermediate', 'promoted:Artificial Intelligence',
            "Artificial Intelligence course about this career's work; its description covers Supply Chain.",
        )])

    def test_an_ai_skill_tag_does_not_make_a_course_ai(self):
        # "Project Management Basics" carries the Artificial Intelligence tag.
        window = [course('PM+1', 'Intermediate', ['Artificial Intelligence', 'Supply Chain'],
                         title='Supply Chain Basics', short='Supply chain basics.')]
        self.assertEqual(self.plan(window), [])

    def test_a_subject_makes_a_course_ai(self):
        window = [course('AI+1', 'Intermediate', ['Supply Chain'], subjects=['Artificial Intelligence'],
                         title='Smarter Planning', short='Supply chain planning.')]
        self.assertEqual(seat_keys(self.plan(window)), ['AI+1'])

    def test_needs_a_skill_tag_shared_with_the_career(self):
        window = [ai_course('AI+1', 'Intermediate', skills=['ChatGPT', 'Poetry'])]
        self.assertEqual(self.plan(window), [])

    def test_a_shared_skill_must_be_named_in_the_course_text(self):
        # Copilot Foundations for Productivity carries Project Management but never says so.
        window = [ai_course('AI+1', 'Intermediate', short='Draft documents faster with an assistant.')]
        self.assertEqual(self.plan(window), [])

    def test_the_skill_may_be_named_in_title_short_or_full_description(self):
        for fields in ({'title': 'AI for Supply Chain'}, {'short': 'Your SUPPLY CHAIN, with AI.'},
                       {'full': '<p>Plan a supply\n   chain&nbsp;with models.</p>'}):
            values = {'title': 'AI Essentials', 'short': '', 'full': ''}
            values.update(fields)
            window = [course('AI+1', 'Intermediate', ['ChatGPT', 'Supply Chain'], **values)]
            self.assertEqual(seat_keys(self.plan(window)), ['AI+1'], fields)

    def test_html_entities_are_decoded_before_matching(self):
        window = [course('AI+1', 'Intermediate', ['ChatGPT', 'Research & Development'], title='AI in R&D',
                         short='<b>Research &amp; development</b> with AI.')]
        seats = self.plan(window, career=['Research & Development'])
        self.assertEqual(seat_keys(seats), ['AI+1'])

    def test_topic_skills_are_not_evidence_of_career_fit(self):
        # The career lists "Artificial Intelligence", and the generic course names it, but that
        # overlap is the topic itself, not the career's work.
        window = [course('AI+generic', 'Intermediate', ['Artificial Intelligence', 'Prompt Engineering'],
                         title='AI for Everyone', short='What artificial intelligence can do.')]
        self.assertEqual(self.plan(window), [])

    def test_only_the_first_gate_top_k_of_a_rung_are_considered(self):
        topic = PromotedTopic('AI', title_terms=('AI',), gate_top_k=2)
        target = ai_course('AI+3', 'Intermediate')
        third_in_rung = [course('Int+1', 'Intermediate'), course('Int+2', 'Intermediate'), target]
        # Candidates of other rungs come first in relevance order but do not use up the gate.
        second_in_rung = [course('Adv+0', 'Advanced'), course('Intro+0', 'Introductory'),
                          course('Int+1', 'Intermediate'), target]

        self.assertEqual(self.plan(third_in_rung, shape=(0, 2, 0), topics=(topic,)), [])
        self.assertEqual(seat_keys(self.plan(second_in_rung, shape=(0, 2, 0), topics=(topic,))), ['AI+3'])

    def test_excluded_courses_do_not_use_up_the_gate(self):
        topic = PromotedTopic('AI', title_terms=('AI',), gate_top_k=1)
        window = [course('Gone+1', 'Intermediate'), ai_course('AI+2', 'Intermediate')]
        self.assertEqual(self.plan(window, topics=(topic,)), [])
        seats = self.plan(window, topics=(topic,), excluded_keys=frozenset({'Gone+1'}))
        self.assertEqual(seat_keys(seats), ['AI+2'])

    def test_excluded_ai_course_is_never_seated(self):
        window = [ai_course('AI+1', 'Intermediate')]
        self.assertEqual(self.plan(window, excluded_keys=frozenset({'AI+1'})), [])

    def test_prefers_intermediate_then_introductory_then_advanced(self):
        window = [ai_course('Adv', 'Advanced'), ai_course('Intro', 'Introductory'), ai_course('Int', 'Intermediate')]
        self.assertEqual(seat_keys(self.plan(window, shape=(1, 1, 1))), ['Int'])
        self.assertEqual(seat_keys(self.plan(window, shape=(1, 0, 1))), ['Intro'])
        self.assertEqual(seat_keys(self.plan(window, shape=(0, 0, 1))), ['Adv'])
        self.assertEqual(self.plan(window, shape=(0, 0, 0)), [])

    def test_falls_through_a_rung_with_room_but_no_match(self):
        window = [ai_course('Int', 'Intermediate', short='Nothing about the work.'), ai_course('Intro', 'Introductory')]
        self.assertEqual(seat_keys(self.plan(window, shape=(1, 1, 0))), ['Intro'])

    def test_max_per_pathway(self):
        window = [ai_course(f'AI+{index}', 'Intermediate') for index in range(3)]
        for cap, expected in ((0, []), (1, ['AI+0']), (2, ['AI+0', 'AI+1'])):
            topic = PromotedTopic('AI', title_terms=('AI',), max_per_pathway=cap)
            self.assertEqual(seat_keys(self.plan(window, shape=(0, 3, 0), topics=(topic,))), expected)

    def test_never_exceeds_a_rung_quota(self):
        topic = PromotedTopic('AI', title_terms=('AI',), max_per_pathway=5)
        window = [ai_course(f'AI+{index}', 'Intermediate') for index in range(4)]
        self.assertEqual(seat_keys(self.plan(window, shape=(0, 2, 0), topics=(topic,))), ['AI+0', 'AI+1'])

    def test_flagship_that_belongs_to_the_topic_counts_toward_its_cap(self):
        window = [ai_course('AI+flag', 'Introductory'), ai_course('AI+2', 'Intermediate')]
        seats = plan_seats(
            ordered_candidates=window, shape=(1, 1, 0), career_skills=self.CAREER,
            policy=EditorialPolicy(flagships=(FlagshipRule('AI+flag', 'Introductory'),), promoted=(AI_TOPIC,)),
        )
        self.assertEqual([(seat.key, seat.rule) for seat in seats], [('AI+flag', 'flagship')])

    def test_flagship_capacity_is_respected_by_promotion(self):
        window = [course('Py+1', 'Intermediate', ['Python']), ai_course('AI+2', 'Intermediate'),
                  ai_course('AI+3', 'Introductory')]
        seats = plan_seats(
            ordered_candidates=window, shape=(1, 1, 0), career_skills=self.CAREER,
            policy=EditorialPolicy(flagships=(FlagshipRule('Py+1', 'Intermediate'),), promoted=(AI_TOPIC,)),
        )
        self.assertEqual(
            [(seat.key, seat.level, seat.rule) for seat in seats],
            [('Py+1', 'Intermediate', 'flagship'), ('AI+3', 'Introductory', 'promoted:Artificial Intelligence')],
        )

    def test_a_later_topic_does_not_reseat_an_earlier_topics_course(self):
        first = PromotedTopic('A', title_terms=('AI',))
        second = PromotedTopic('B', title_terms=('AI',))
        seats = self.plan([ai_course('AI+1', 'Intermediate')], topics=(first, second))
        self.assertEqual([(seat.key, seat.rule) for seat in seats], [('AI+1', 'promoted:A')])


@ddt.ddt
class PlanSeatsContractTests(SimpleTestCase):
    """Input handling that holds for every rule."""

    @ddt.data((1, 2), (1, 2, 3, 4), (-1, 0, 0), ('a', 1, 1), None)
    def test_rejects_a_bad_shape(self, shape):
        with self.assertRaises(ValueError):
            plan_seats(ordered_candidates=[], shape=shape, career_skills=[], policy=EditorialPolicy())

    def test_empty_policy_seats_nothing(self):
        window = [ai_course('AI+1', 'Introductory')]
        self.assertEqual(
            plan_seats(ordered_candidates=window, shape=(2, 2, 1), career_skills=['Supply Chain'],
                       policy=EditorialPolicy()),
            [],
        )

    def test_does_not_mutate_its_inputs(self):
        window = [ai_course('AI+1', 'Intermediate'), course('X+1', 'Introductory')]
        career = ['Supply Chain']
        before = copy.deepcopy((window, career))
        plan_seats(
            ordered_candidates=window, shape=(1, 1, 0), career_skills=career,
            policy=EditorialPolicy(flagships=(FlagshipRule('X+1', 'Introductory'),), promoted=(AI_TOPIC,)),
        )
        self.assertEqual((window, career), before)

    def test_ignores_malformed_candidates(self):
        window = [None, 'A+1', {'title': 'AI without a key'}, ai_course('AI+1', 'Intermediate')]
        seats = plan_seats(
            ordered_candidates=window, shape=(0, 1, 0), career_skills=['Supply Chain'],
            policy=EditorialPolicy(promoted=(AI_TOPIC,)),
        )
        self.assertEqual(seat_keys(seats), ['AI+1'])

    def test_tolerates_missing_or_non_string_text_fields(self):
        candidate = ai_course('AI+1', 'Intermediate', short=None)
        candidate['full_description'] = 42
        del candidate['title']
        candidate['subjects'] = ['Artificial Intelligence']
        self.assertEqual(
            plan_seats(ordered_candidates=[candidate], shape=(0, 1, 0), career_skills=['Supply Chain'],
                       policy=EditorialPolicy(promoted=(AI_TOPIC,))),
            [],
        )


@ddt.ddt
class DevWindowTests(SimpleTestCase):
    """
    Bench round 1 dev careers, from real stored windows (``fixtures/dev_windows.json``).

    The seeded AI topic must seat the reviewer's own AI picks where the shape allows, and
    never the four courses he ruled out: two that are AI only by a stray tag, one generic
    AI course, and one off-topic course.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.careers = json.loads((FIXTURES / 'dev_windows.json').read_text(encoding='utf-8'))['careers']

    def plan(self, career, shape, topic=SEEDED_AI_TOPIC):
        data = self.careers[career]
        return plan_seats(
            ordered_candidates=data['candidates'],
            shape=tuple(int(count) for count in shape.split('/')),
            career_skills=data['career_skills'],
            policy=EditorialPolicy(promoted=(topic,)),
        )

    def candidate(self, career, key):
        return next(item for item in self.careers[career]['candidates'] if item['key'] == key)

    def seated_in(self, career, key, topic=SEEDED_AI_TOPIC):
        return [shape for shape in REVIEW_SHAPES if key in seat_keys(self.plan(career, shape, topic))]

    @ddt.data(
        ('Operations Manager', 'Microsoft+MS-AI-134'),     # off topic
        ('Project Manager', 'IBM+PRM100EN'),               # AI only by a stray tag
        ('Program Manager', 'IBM+PRM100EN'),
        ('Project Manager', 'Microsoft+COPF'),             # generic AI
        ('Program Manager', 'Microsoft+COPF'),
    )
    @ddt.unpack
    def test_ruled_out_courses_are_never_seated(self, career, key):
        self.assertEqual(self.seated_in(career, key), [])

    def test_stray_ai_tag_is_not_ai_identity(self):
        prm100 = self.candidate('Project Manager', 'IBM+PRM100EN')
        self.assertIn('Artificial Intelligence', prm100['skill_names'])
        self.assertFalse(is_promoted(prm100, SEEDED_AI_TOPIC))

    def test_generic_ai_course_is_refused_by_the_text_rule(self):
        # COPF is AI by its title ("Copilot") and shares Project Management, but never names it.
        copf = self.candidate('Program Manager', 'Microsoft+COPF')
        self.assertTrue(is_promoted(copf, SEEDED_AI_TOPIC))
        self.assertIn('Project Management', copf['skill_names'])
        text = ' '.join(copf[field] for field in ('title', 'short_description', 'full_description')).lower()
        self.assertNotIn('project management', text)

    def test_ms_ai_134_is_refused_even_as_an_ai_course(self):
        # Its title carries no seeded term. Were "Agents" a term, the text rule would still refuse
        # it: it shares "Operations Management", and its text says only "operations managers".
        topic = PromotedTopic('Agents', title_terms=('Agents',), skill_names=AI_SKILLS)
        self.assertTrue(is_promoted(self.candidate('Operations Manager', 'Microsoft+MS-AI-134'), topic))
        self.assertEqual(self.seated_in('Operations Manager', 'Microsoft+MS-AI-134', topic), [])

    def test_operations_manager_gets_no_ai_seat(self):
        self.assertEqual({shape: self.plan('Operations Manager', shape) for shape in REVIEW_SHAPES},
                         dict.fromkeys(REVIEW_SHAPES, []))

    @ddt.data(
        ('Project Manager', '0/2/0', 'SkillUp-EdTech+AI0282EN', 'Intermediate'),
        ('Project Manager', '2/2/1', 'SkillUp-EdTech+AI0282EN', 'Intermediate'),
        ('Program Manager', '0/2/0', 'SkillUp-EdTech+AI0289EN', 'Intermediate'),
        ('Program Manager', '2/2/1', 'SkillUp-EdTech+AI0289EN', 'Intermediate'),
        ('Software Engineer', '2/0/0', 'AI+genai1x', 'Introductory'),
    )
    @ddt.unpack
    def test_reviewer_ai_picks_are_seated(self, career, shape, key, level):
        self.assertEqual([(seat.key, seat.level) for seat in self.plan(career, shape)], [(key, level)])

    def test_known_miss_vibe_coding_for_software_engineer(self):
        """
        The reviewer's intermediate pick for Software Engineer is not seatable.

        It is AI by title and tagged "Software Engineering", but its text says "Software
        engineers", which the substring rule does not count. The rung's first AI course,
        "AI Interviews - Software Design, Architecture, and More", takes the seat instead.
        """
        rapid = self.candidate('Software Engineer', 'edX+EDX-RapidAI')
        self.assertTrue(is_promoted(rapid, SEEDED_AI_TOPIC))
        self.assertEqual(self.seated_in('Software Engineer', 'edX+EDX-RapidAI'), [])
        self.assertEqual(seat_keys(self.plan('Software Engineer', '0/2/0')), ['CodeSignal+68'])

    def test_machine_learning_is_left_out_of_the_seeded_terms(self):
        # With it, "Machine Learning for Semiconductor Quantum Devices" takes Software Engineer's
        # Advanced seat, through Computer Science and Electrical Engineering.
        self.assertNotIn('Machine Learning', SEEDED_AI_TOPIC.title_terms)
        with_ml = PromotedTopic(
            SEEDED_AI_TOPIC.name, subjects=SEEDED_AI_TOPIC.subjects, skill_names=SEEDED_AI_TOPIC.skill_names,
            title_terms=SEEDED_AI_TOPIC.title_terms + ('Machine Learning',),
        )
        self.assertEqual(seat_keys(self.plan('Software Engineer', '0/0/2', with_ml)), ['DelftX+QCST1x'])
        self.assertEqual(self.plan('Software Engineer', '0/0/2'), [])


class LoadPolicyTests(TestCase):
    """``load_policy`` reads the active rows."""

    def setUp(self):
        super().setUp()
        # Start from no rows: the seed migrations have already populated the test database.
        PathwayCourseRule.objects.all().delete()
        PathwayPromotedTopic.objects.all().delete()

    def test_no_rows_is_empty_policy(self):
        self.assertTrue(load_policy().is_empty)

    def test_reads_only_active_rows(self):
        PathwayCourseRule.objects.create(course_key='A+1', action='exclude', reason='r')
        PathwayCourseRule.objects.create(course_key='B+2', action='exclude', reason='r', is_active=False)
        PathwayCourseRule.objects.create(
            course_key='C+3', action='flagship', level='Introductory', scope_skills=['Debugging'], reason='why',
        )
        PathwayCourseRule.objects.create(course_key='D+4', action='flagship', level='Advanced', reason='r',
                                         is_active=False)
        PathwayPromotedTopic.objects.create(name='AI', skill_names=['ChatGPT'], title_terms=['AI'], reason='r')
        PathwayPromotedTopic.objects.create(name='Off', title_terms=['X'], reason='r', is_active=False)

        policy = load_policy()

        self.assertEqual(policy.excluded_keys, frozenset({'A+1'}))
        self.assertEqual(policy.flagships, (FlagshipRule('C+3', 'Introductory', ('Debugging',), 'why'),))
        self.assertEqual(policy.promoted, (PromotedTopic('AI', skill_names=('ChatGPT',), title_terms=('AI',)),))

    def test_flagships_in_creation_order_and_topics_in_name_order(self):
        for key in ('Z+1', 'A+1', 'M+1'):
            PathwayCourseRule.objects.create(course_key=key, action='flagship', level='Advanced', reason='r')
        for name in ('Zed', 'Alpha'):
            PathwayPromotedTopic.objects.create(name=name, title_terms=['x'], max_per_pathway=2, gate_top_k=3,
                                                reason='r')

        policy = load_policy()

        self.assertEqual([rule.course_key for rule in policy.flagships], ['Z+1', 'A+1', 'M+1'])
        self.assertEqual([topic.name for topic in policy.promoted], ['Alpha', 'Zed'])
        self.assertEqual((policy.promoted[0].max_per_pathway, policy.promoted[0].gate_top_k), (2, 3))

    def test_round_trips_through_the_snapshot_form(self):
        PathwayCourseRule.objects.create(course_key='A+1', action='exclude', reason='r')
        PathwayCourseRule.objects.create(course_key='C+3', action='flagship', level='Intermediate', reason='why')
        PathwayPromotedTopic.objects.create(name='AI', subjects=['Artificial Intelligence'], title_terms=['AI'],
                                            reason='r')
        policy = load_policy()
        self.assertEqual(api.EditorialPolicy.from_dict(json.loads(json.dumps(policy.to_dict()))), policy)
