"""Tests for the profile block: what the AI knows about the learner, restated from the merged intent."""
from unittest import TestCase

from enterprise_access.apps.personalized_search.core import profile
from enterprise_access.apps.personalized_search.core.compose import ComposeParser
from enterprise_access.apps.personalized_search.core.intent import empty_intent
from enterprise_access.apps.personalized_search.tests.helpers import default_candidates


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
        self.assertEqual(profile.profile_props(merged)['chips'], ['Executive Education'])

    def test_chips_are_capped_deduplicated_and_non_empty(self):
        merged = dict(empty_intent(), roles=['x' * 60], skills=['Data', 'Data', '  '])
        self.assertEqual(profile.profile_props(merged)['chips'], ['x' * 40, 'Data'])

    def test_nothing_known_is_no_profile(self):
        self.assertIsNone(profile.profile_props(empty_intent()))
        self.assertIsNone(profile.profile_props(None))


class ProfileEmissionTests(TestCase):
    """The parser emits the profile once, before the other blocks, and only when there are results."""

    PROPS = {'summary': 'S', 'chips': ['Leadership']}

    def test_emitted_once_right_after_the_reply(self):
        parser = ComposeParser(candidates=default_candidates(), shown_ids=[], followup_attribute='level',
                               profile=self.PROPS)
        parts = parser.feed('Here you go.\n@@BLOCKS@@\n') + parser.finish()
        blocks = [p['data']['type'] for p in parts if p['type'] == 'data-block']
        self.assertEqual(blocks[0], 'profile')
        self.assertEqual(blocks.count('profile'), 1)
        self.assertEqual([p['type'] for p in parts].index('text-end') + 1,
                         [p['type'] for p in parts].index('data-block'))

    def test_emitted_at_finish_when_the_model_never_writes_blocks(self):
        parser = ComposeParser(candidates=default_candidates(), shown_ids=[], followup_attribute='level',
                               profile=self.PROPS)
        parts = parser.feed('Only text') + parser.finish()
        self.assertEqual([p['data']['type'] for p in parts if p['type'] == 'data-block'][0], 'profile')

    def test_not_emitted_without_results(self):
        parser = ComposeParser(candidates=[], shown_ids=[], followup_attribute='level', profile=self.PROPS)
        parts = parser.feed('Nothing fits.\n@@BLOCKS@@\n') + parser.finish()
        self.assertFalse([p for p in parts if p['type'] == 'data-block'])
