"""Tests for the top pick and comparison blocks (KD-11): validated, hydrated, once per turn."""
import json

from django.test import TestCase

from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.compose import ComposeParser
from enterprise_access.apps.personalized_search.tests.helpers import default_candidates


def line(obj):
    return json.dumps(obj) + '\n'


def option(item_id, best_for='a quick start'):
    return {'item_id': item_id, 'best_for': best_for}


class DecisionBlockTests(TestCase):
    """``top_pick`` and ``compare`` lines."""

    def parse(self, body, shown=()):
        parser = ComposeParser(candidates=default_candidates(), shown_ids=list(shown), followup_attribute='level')
        parts = parser.feed('r\n@@BLOCKS@@\n' + body) + parser.finish()
        return parser, [p['data'] for p in parts if p['type'] == 'data-block']

    def of(self, blocks, block_type):
        return [b for b in blocks if b['type'] == block_type]

    def test_top_pick_is_hydrated_with_its_reason_and_checks(self):
        parser, blocks = self.parse(line({'block': 'top_pick', 'item_id': 'p-1', 'reason': 'Because.',
                                          'checks': ['Self-paced', '', 'x' * 50, 'a', 'b']}))
        props = self.of(blocks, 'top_pick')[0]['props']
        self.assertEqual(props['item']['item_id'], 'p-1')
        self.assertEqual(props['item']['title'], 'Program p-1')
        self.assertEqual(props['reason'], 'Because.')
        self.assertEqual(props['checks'], ['Self-paced', 'x' * 40, 'a'])
        self.assertIn('p-1', parser.emitted_ids)

    def test_top_pick_needs_a_known_reason_and_comes_once(self):
        _, blocks = self.parse(line({'block': 'top_pick', 'item_id': 'nope', 'reason': 'R'}) +
                               line({'block': 'top_pick', 'item_id': 'p-1', 'reason': ''}) +
                               line({'block': 'top_pick', 'item_id': 'p-1', 'reason': 'R'}) +
                               line({'block': 'top_pick', 'item_id': 'p-2', 'reason': 'R'}))
        self.assertEqual([b['props']['item']['item_id'] for b in self.of(blocks, 'top_pick')], ['p-1'])

    def test_compare_keeps_two_or_three_distinct_known_options_and_may_repeat_the_top_pick(self):
        _, blocks = self.parse(
            line({'block': 'top_pick', 'item_id': 'p-1', 'reason': 'R'}) +
            line({'block': 'compare', 'options': [option('p-1', 'a credential'), option('p-1'), option('nope'),
                                                  'junk', option('Yale+E1', ''), option('Yale+E2'),
                                                  option('edX+C3'), option('edX+C4')]}))
        options = self.of(blocks, 'compare')[0]['props']['options']
        self.assertEqual([o['item']['item_id'] for o in options], ['p-1', 'Yale+E2', 'edX+C3'])
        self.assertEqual(options[0]['best_for'], 'a credential')

    def test_compare_with_fewer_than_two_options_is_dropped_and_comes_once(self):
        parser, blocks = self.parse(
            line({'block': 'compare', 'options': [option('p-1')]}) +
            line({'block': 'compare', 'options': [option('p-1'), option('p-2')]}) +
            line({'block': 'compare', 'options': [option('edX+C1'), option('edX+C2')]}))
        self.assertEqual(len(self.of(blocks, 'compare')), 1)
        self.assertIn('compare_too_few_options', parser.dropped)
        self.assertIn('compare_extra_or_text_only', parser.dropped)

    def test_compare_skips_items_already_featured_other_than_the_top_pick(self):
        _, blocks = self.parse(
            line({'block': 'item_section', 'item_type': 'course', 'dek': 'd',
                  'items': [{'item_id': 'edX+C1', 'why': 'w', 'fit': []}]}) +
            line({'block': 'compare', 'options': [option('edX+C1'), option('p-1'), option('p-2')]}))
        self.assertEqual([o['item']['item_id'] for o in self.of(blocks, 'compare')[0]['props']['options']],
                         ['p-1', 'p-2'])

    def test_featured_items_stay_out_of_the_sections(self):
        _, blocks = self.parse(
            line({'block': 'top_pick', 'item_id': 'edX+C1', 'reason': 'R'}) +
            line({'block': 'item_section', 'item_type': 'course', 'dek': 'd',
                  'items': [{'item_id': 'edX+C1', 'why': 'w', 'fit': []},
                            {'item_id': 'edX+C2', 'why': 'w', 'fit': []}]}))
        self.assertEqual([i['item_id'] for i in self.of(blocks, 'item_section')[0]['props']['items']], ['edX+C2'])

    def test_dismissed_items_and_the_per_turn_cap_are_respected(self):
        _, blocks = self.parse(line({'block': 'top_pick', 'item_id': 'p-1', 'reason': 'R'}), shown=['p-1'])
        self.assertFalse(self.of(blocks, 'top_pick'))
        parser = ComposeParser(candidates=default_candidates(), shown_ids=[], followup_attribute='level')
        parser.emitted_ids = [f'x{i}' for i in range(constants.MAX_ITEMS_PER_TURN)]
        self.assertIsNone(parser._option('p-1'))  # pylint: disable=protected-access

    def test_text_only_turns_get_no_decision_blocks(self):
        parser = ComposeParser(candidates=[], shown_ids=[], followup_attribute='level')
        parts = parser.feed('r\n@@BLOCKS@@\n' + line({'block': 'top_pick', 'item_id': 'p-1', 'reason': 'R'}) +
                            line({'block': 'compare', 'options': [option('p-1'), option('p-2')]})) + parser.finish()
        self.assertFalse([p for p in parts if p['type'] == 'data-block'])


class CaveatAndInsightTests(TestCase):
    """A caveat warns once per turn, even with no results; an insight also comes at most once."""

    def parse(self, body, candidates=None):
        parser = ComposeParser(candidates=default_candidates() if candidates is None else candidates, shown_ids=[],
                               followup_attribute='level')
        parts = parser.feed('r\n@@BLOCKS@@\n' + body) + parser.finish()
        return parser, [p['data'] for p in parts if p['type'] == 'data-block']

    def test_caveat_is_capped_and_comes_once(self):
        parser, blocks = self.parse(line({'block': 'caveat', 'title': 'T' * 80, 'body': 'B' * 400}) +
                                    line({'block': 'caveat', 'title': 'Again', 'body': 'Again'}) +
                                    line({'block': 'caveat', 'title': '', 'body': 'x'}))
        caveats = [b['props'] for b in blocks if b['type'] == 'caveat']
        self.assertEqual(caveats, [{'title': 'T' * 60, 'body': 'B' * 300}])
        self.assertEqual(parser.dropped, ['caveat_extra', 'caveat_invalid'])

    def test_caveat_shows_on_a_text_only_turn(self):
        caveat = line({'block': 'caveat', 'title': 'No match', 'body': 'Nothing on this.'})
        _, blocks = self.parse(caveat, candidates=[])
        self.assertEqual([b['type'] for b in blocks], ['caveat'])

    def test_a_second_insight_is_dropped(self):
        parser, blocks = self.parse(line({'block': 'insight', 'title': 'A', 'body': 'a'}) +
                                    line({'block': 'insight', 'title': 'B', 'body': 'b'}))
        self.assertEqual([b['props']['title'] for b in blocks if b['type'] == 'insight'], ['A'])
        self.assertIn('insight_extra', parser.dropped)


class SectionExtrasTests(TestCase):
    """Sections carry the catalog total for their type and up to two reserve cards."""

    def parse(self, body, totals=None, shown=()):
        parser = ComposeParser(candidates=default_candidates(), shown_ids=list(shown), followup_attribute='level',
                               totals=totals)
        parts = parser.feed('r\n@@BLOCKS@@\n' + body) + parser.finish()
        return [p['data']['props'] for p in parts if p['type'] == 'data-block' and p['data']['type'] == 'item_section']

    def section(self, item_type, ids):
        return line({'block': 'item_section', 'item_type': item_type, 'dek': 'd',
                     'items': [{'item_id': i, 'why': 'w', 'fit': []} for i in ids]})

    def test_total_and_reserve(self):
        props = self.parse(self.section('course', ['edX+C1']), totals={'course': 37}, shown=['edX+C2'])[0]
        self.assertEqual(props['total'], 37)
        self.assertEqual([c['item_id'] for c in props['reserve']], ['edX+C3', 'edX+C4'])
        self.assertTrue(all(c['why'] is None and c['fit'] == [] for c in props['reserve']))

    def test_cards_new_to_the_conversation_are_flagged_wherever_they_sit(self):
        parser = ComposeParser(candidates=default_candidates(), shown_ids=[], followup_attribute='level',
                               seen_ids=['edX+C1'])
        body = (line({'block': 'top_pick', 'item_id': 'edX+C2', 'reason': 'Because you want to lead.'}) +
                line({'block': 'compare', 'options': [{'item_id': 'edX+C1', 'best_for': 'a'},
                                                      {'item_id': 'edX+C2', 'best_for': 'b'}]}) +
                self.section('executive_education', ['Yale+E1']))
        parts = parser.feed('r\n@@BLOCKS@@\n' + body) + parser.finish()
        props = {p['data']['type']: p['data']['props'] for p in parts if p['type'] == 'data-block'}
        self.assertEqual(props['top_pick']['item'].get('status'), 'new')
        self.assertEqual([o['item'].get('status') for o in props['compare']['options']], [None, 'new'])
        self.assertTrue(all(c.get('status') == 'new' for c in props['item_section'].get('reserve', [])))

    def test_no_total_or_reserve_when_unknown_or_empty(self):
        props = self.parse(self.section('program', ['p-1', 'p-2']))[0]
        self.assertNotIn('total', props)
        self.assertNotIn('reserve', props)

    def test_the_fallback_section_gets_them_too(self):
        props = self.parse('', totals={'course': 9})[0]
        self.assertTrue(props['fallback'])
        self.assertEqual(props['total'], 9)
        self.assertEqual([c['item_id'] for c in props['reserve']], ['edX+C4'])
