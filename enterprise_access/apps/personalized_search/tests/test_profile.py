"""Tests for the profile block: what the AI knows about the learner, restated from the merged intent."""
import json
from unittest import TestCase

from enterprise_access.apps.personalized_search.core import profile
from enterprise_access.apps.personalized_search.core.compose import ComposeParser
from enterprise_access.apps.personalized_search.core.intent import empty_intent
from enterprise_access.apps.personalized_search.tests.helpers import (
    candidate,
    course_hit,
    default_candidates,
    program_hit
)


class ProfilePropsTests(TestCase):
    """``profile_props`` turns the merged intent into chips, without a model call."""

    def test_chips_restate_the_intent_in_order(self):
        merged = dict(empty_intent(), roles=['Sr. Product Manager'], skills=['Leadership', 'Strategy', 'Finance'],
                      level='intermediate', item_types=['course', 'program'],
                      constraints={'pacing': 'self_paced', 'max_hours_per_week': 5, 'max_weeks': 12})
        props = profile.profile_props(merged)
        self.assertEqual(props['summary'], profile.SUMMARY)
        self.assertEqual(props['chips'], ['Sr. Product Manager', 'Leadership', 'Strategy', 'Intermediate',
                                          'Self-paced', 'Up to 5 hrs/week'])

    def test_all_types_add_no_chip_and_some_types_do(self):
        merged = dict(empty_intent(), item_types=['executive_education'])
        self.assertEqual(profile.profile_props(merged)['chips'], ['Executive education'])  # as its section says

    def test_weeks_and_type_chips_have_their_formats(self):
        merged = dict(empty_intent(), item_types=['course', 'program'], constraints={'max_weeks': 12})
        self.assertEqual(profile.profile_props(merged)['chips'], ['Up to 12 weeks', 'Courses and Programs'])

    def test_a_repeated_type_is_named_once(self):
        for types, chip in ((['course', 'course'], 'Courses'),
                            (['course', 'course', 'program'], 'Courses and Programs')):
            self.assertEqual(profile.profile_props(dict(empty_intent(), item_types=types))['chips'], [chip], types)

    def test_chips_are_capped_deduplicated_and_non_empty(self):
        merged = dict(empty_intent(), roles=['x' * 60], skills=['Data', 'Data', '  '])
        self.assertEqual(profile.profile_props(merged)['chips'], ['x' * 40, 'Data'])

    def test_nothing_known_is_no_profile(self):
        self.assertIsNone(profile.profile_props(empty_intent()))
        self.assertIsNone(profile.profile_props(None))


class ProfileFromResultsTests(TestCase):
    """A preference stays only while the shown results reflect it."""

    MERGED = dict(empty_intent(), roles=['Team lead'], skills=['Leadership', 'Finance'], level='introductory',
                  item_types=['course'], constraints={'pacing': 'instructor_paced', 'max_weeks': 4,
                                                      'max_hours_per_week': 3})

    def test_chips_the_shown_results_do_not_meet_are_left_out(self):
        shown = [candidate(course_hit('edX+A', weeks=8, effort=(2, 3))),
                 candidate(course_hit('edX+B', weeks=10, effort=(2, 3)))]
        # Self-paced and longer than 4 weeks, with no finance skill: those three chips go; the rest stay.
        self.assertEqual(profile.profile_props(self.MERGED, shown)['chips'],
                         ['Team lead', 'Leadership', 'Introductory', 'Up to 3 hrs/week', 'Courses'])

    def test_half_the_results_meeting_a_preference_keeps_it(self):
        shown = [candidate(course_hit('edX+A', pacing='instructor_paced')), candidate(course_hit('edX+B'))]
        self.assertIn('Instructor-led', profile.profile_props(self.MERGED, shown)['chips'])
        shown.append(candidate(course_hit('edX+C')))
        self.assertNotIn('Instructor-led', profile.profile_props(self.MERGED, shown)['chips'])

    def test_a_preference_no_result_has_data_for_is_left_out(self):
        shown = [candidate(program_hit('p-1'))]  # programs carry no pacing, weeks or hours
        chips = profile.profile_props(self.MERGED, shown)['chips']
        self.assertFalse({'Instructor-led', 'Up to 4 weeks', 'Up to 3 hrs/week', 'Courses'} & set(chips))
        self.assertEqual(chips[0], 'Team lead')  # the goal is the search itself, so it always stays

    def test_skills_match_the_catalog_names_loosely(self):
        shown = [candidate(course_hit('edX+A', skills=('Leadership Development',)))]
        self.assertIn('Leadership', profile.profile_props(self.MERGED, shown)['chips'])

    def test_skills_match_whole_words_only(self):
        for skill, names in (('R', ('Leadership',)), ('SQL', ('PostgreSQL',)), ('Email Marketing', ('AI', 'Go'))):
            shown = [candidate(course_hit('edX+A', skills=names))]
            merged = dict(empty_intent(), roles=['Analyst'], skills=[skill])
            self.assertNotIn(skill, profile.profile_props(merged, shown)['chips'], skill)

    def test_a_range_that_starts_within_the_hours_limit_fits_as_in_the_prefilter(self):
        shown = [candidate(course_hit('edX+A', effort=(2, 6)))]  # 2-6 hrs/week against a limit of 3
        self.assertIn('Up to 3 hrs/week', profile.profile_props(self.MERGED, shown)['chips'])


class ProfileEmissionTests(TestCase):
    """The parser emits the profile once, after the results, and only when there are results."""

    MERGED = dict(empty_intent(), skills=['Leadership'], constraints={'pacing': 'instructor_paced'})

    def parse(self, text, candidates=None):
        parser = ComposeParser(candidates=default_candidates() if candidates is None else candidates, excluded_ids=[],
                               followup_attribute='level', intent=self.MERGED)
        return parser.feed(text) + parser.finish()

    def test_emitted_once_after_the_results_from_what_they_reflect(self):
        section = {'block': 'item_section', 'item_type': 'course', 'dek': 'd',
                   'items': [{'item_id': 'edX+C1', 'why': 'w'}, {'item_id': 'edX+C2', 'why': 'w'}]}
        parts = self.parse('Here you go.\n@@BLOCKS@@\n' + json.dumps(section) + '\n')
        blocks = [p['data'] for p in parts if p['type'] == 'data-block']
        self.assertEqual([b['type'] for b in blocks], ['item_section', 'item_section', 'profile'])  # + programs
        # Both shown courses are self-paced, so the instructor-led preference isn't shown.
        self.assertEqual(blocks[2]['props']['chips'], ['Leadership'])
        self.assertEqual([p['type'] for p in parts][-1], 'data-followups')

    def test_emitted_after_the_fallback_section_when_the_model_never_writes_blocks(self):
        blocks = [p['data']['type'] for p in self.parse('Only text') if p['type'] == 'data-block']
        self.assertEqual(blocks, ['item_section', 'item_section', 'profile'])

    def test_not_emitted_without_results(self):
        parts = self.parse('Nothing fits.\n@@BLOCKS@@\n', candidates=[])
        self.assertFalse([p for p in parts if p['type'] == 'data-block'])

    def test_an_early_profile_comes_with_the_header_and_the_final_one_replaces_it(self):
        header = {'block': 'page_header', 'title': 'Lead', 'dek': 'd'}
        section = {'block': 'item_section', 'item_type': 'course', 'dek': 'd',
                   'items': [{'item_id': 'edX+C1', 'why': 'w'}]}
        parts = self.parse('Hi.\n@@BLOCKS@@\n' + json.dumps(header) + '\n' + json.dumps(section) + '\n')
        blocks = [(p['id'], p['data']['type'], p['data']['props']) for p in parts if p['type'] == 'data-block']
        types = [t for _, t, _ in blocks]
        # The course section, then the related programs offered alongside it; the profile replaces itself.
        self.assertEqual(types, ['page_header', 'profile', 'item_section', 'item_section', 'profile'])
        (_, _, _), (early_id, _, early), _, _, (final_id, _, final) = blocks
        self.assertEqual(early_id, final_id)  # the client replaces the early profile in place
        self.assertEqual(early['chips'], ['Leadership', 'Instructor-led'])  # what the learner said
        self.assertEqual(final['chips'], ['Leadership'])  # what the results reflect

    def test_an_early_profile_with_nothing_reflected_ends_with_no_chips(self):
        parser = ComposeParser(candidates=default_candidates(), excluded_ids=[], followup_attribute='level',
                               intent=dict(empty_intent(), constraints={'pacing': 'instructor_paced'}))
        header = {'block': 'page_header', 'title': 'Lead', 'dek': 'd'}
        section = {'block': 'item_section', 'item_type': 'course', 'dek': 'd',
                   'items': [{'item_id': 'edX+C1', 'why': 'w'}]}
        parts = parser.feed('Hi.\n@@BLOCKS@@\n' + json.dumps(header) + '\n' + json.dumps(section) + '\n')
        parts += parser.finish()
        profiles = [p['data']['props'] for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'profile']
        self.assertEqual([p['chips'] for p in profiles], [['Instructor-led'], []])

    def test_not_emitted_when_nothing_the_learner_said_is_reflected(self):
        parser = ComposeParser(candidates=default_candidates(), excluded_ids=[], followup_attribute='level',
                               intent=dict(empty_intent(), constraints={'pacing': 'instructor_paced'}))
        section = {'block': 'item_section', 'item_type': 'course', 'dek': 'd',
                   'items': [{'item_id': 'edX+C1', 'why': 'w'}]}
        parts = parser.feed('Hi.\n@@BLOCKS@@\n' + json.dumps(section) + '\n') + parser.finish()
        self.assertNotIn('profile', [p['data']['type'] for p in parts if p['type'] == 'data-block'])
