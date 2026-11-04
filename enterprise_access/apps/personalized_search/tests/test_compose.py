"""Tests for the compose prompt input and the streaming line parser (KD-5 validation)."""
import json

from django.test import TestCase

from enterprise_access.apps.personalized_search.core import compose
from enterprise_access.apps.personalized_search.tests.helpers import default_candidates


def line(obj):
    return json.dumps(obj) + '\n'


def section(item_type, ids, dek='Good picks'):
    return {'block': 'item_section', 'item_type': item_type, 'dek': dek,
            'items': [{'item_id': i, 'why': 'Because you said so.', 'fit': ['Short', 'Self-paced']} for i in ids]}


class ComposeParserTests(TestCase):
    """``ComposeParser`` streams text, validates lines and hydrates cards."""

    def parse(self, text, *, chunk=7, shown=(), attribute='level', candidates=None):
        """Feed ``text`` in fixed-size chunks and return the parser and every part."""
        candidates = default_candidates() if candidates is None else candidates
        parser = compose.ComposeParser(candidates=candidates,
                                       shown_ids=list(shown), followup_attribute=attribute)
        parts = []
        for i in range(0, len(text), chunk):
            parts += parser.feed(text[i:i + chunk])
        parts += parser.finish()
        return parser, parts

    def blocks(self, parts, block_type=None):
        """The data-block payloads, optionally of one type."""
        return [p['data'] for p in parts
                if p['type'] == 'data-block' and (not block_type or p['data']['type'] == block_type)]

    def test_full_turn(self):
        text = ('Here is your page.\n@@BLOCKS@@\n' +
                line({'block': 'page_header', 'title': 'Lead with confidence', 'dek': 'A short path.'}) +
                '\n' +  # blank lines are skipped silently
                line(section('course', ['edX+C1', 'edX+C2'])) +
                line(section('executive_education', ['Yale+E1'])) +
                line({'block': 'followup', 'question': 'What level?', 'suggestions': ['Beginner'],
                      'session_title': 'Leading a team'}) +
                line({'block': 'plan', 'steps': [{'item_id': 'edX+C1', 'note': 'First'},
                                                 {'item_id': 'Yale+E1', 'note': 'Then'}]}))
        parser, parts = self.parse(text)
        reply = ''.join(p['delta'] for p in parts if p['type'] == 'text-delta')
        self.assertEqual(reply, 'Here is your page.')
        self.assertEqual([p['type'] for p in parts][:1], ['text-start'])
        self.assertIn({'type': 'text-end', 'id': 'reply'}, parts)
        header, course, exec_ed = self.blocks(parts)[:3]
        self.assertEqual(header['props']['label'], 'Your learning page')
        self.assertEqual(course['props']['label'], 'Courses · 2 picks')
        self.assertEqual(course['props']['items'][0]['link']['course_key'], 'edX+C1')
        self.assertEqual(exec_ed['props']['label'], 'Executive education · 1 pick')
        followups = [p for p in parts if p['type'] == 'data-followups'][0]['data']
        self.assertEqual(followups['attribute'], 'level')
        self.assertEqual(followups['suggestions'], ['Beginner', 'Introductory', 'Intermediate'])  # topped up to 3
        self.assertEqual(len(self.blocks(parts, 'plan')[0]['props']['steps']), 2)
        self.assertEqual(parser.session_title, 'Leading a team')
        self.assertEqual(parser.dropped, [])

    def test_marker_split_across_chunks(self):
        text = 'Reply text.@@BLOCKS@@' + line({'block': 'page_header', 'title': 'T', 'dek': 'D'})
        for chunk in (1, 3, 5, 11):
            _, parts = self.parse(text, chunk=chunk)
            self.assertEqual(''.join(p['delta'] for p in parts if p['type'] == 'text-delta'), 'Reply text.')
            self.assertEqual(len(self.blocks(parts, 'page_header')), 1)

    def test_drops_invalid_lines(self):
        text = ('x\n@@BLOCKS@@\n' + 'not json\n' + line({'block': 'program', 'items': []}) +
                line(section('course', ['edX+NOPE', 'Yale+E1', 'edX+SEEN'])) +  # unknown, wrong type, shown
                line(section('course', ['edX+C1'])) +
                line(section('course', ['edX+C2'])) +  # second course section
                line({'block': 'plan', 'steps': [{'item_id': 'edX+C1', 'note': 'n'}]}))
        parser, parts = self.parse(text, shown=['edX+SEEN'])
        self.assertEqual(len(self.blocks(parts, 'item_section')), 1)
        self.assertEqual(parser.dropped, ['not_json', 'unknown_block:program', 'section_no_valid_items:course',
                                          'section_rejected:course', 'plan_too_few_items'])

    def test_caps(self):
        many = section('course', ['edX+C1', 'edX+C2', 'edX+C3', 'edX+C4'], dek='d' * 500)
        many['items'][0]['why'] = 'w' * 500
        many['items'][0]['fit'] = ['a' * 40, 'b', 'c', 'd']
        _, parts = self.parse('r\n@@BLOCKS@@\n' + line(many) + line(section('program', ['p-1', 'p-2', 'p-3'])))
        course, program = self.blocks(parts, 'item_section')
        self.assertEqual(len(course['props']['items']), 3)
        self.assertEqual(len(course['props']['dek']), 200)
        self.assertEqual(len(course['props']['items'][0]['why']), 400)
        self.assertEqual(course['props']['items'][0]['fit'], ['a' * 24, 'b', 'c'])
        self.assertEqual(len(program['props']['items']), 2)  # 5 per turn

    def test_fallback_section_and_template_followup(self):
        _, parts = self.parse('Nothing fits.\n@@BLOCKS@@\n', attribute='time')
        fallback = self.blocks(parts, 'item_section')[0]['props']
        self.assertTrue(fallback['fallback'])
        self.assertEqual(fallback['item_type'], 'course')
        self.assertEqual(fallback['items'][0]['why'], None)
        followups = [p for p in parts if p['type'] == 'data-followups'][0]['data']
        self.assertEqual(followups['question'], 'How much time can you give it?')

    def test_text_only_turn_has_no_blocks(self):
        text = 'Nothing in your catalog.\n@@BLOCKS@@\n' + line({'block': 'page_header', 'title': 'T', 'dek': 'D'})
        _, parts = self.parse(text, candidates=[])
        self.assertEqual(self.blocks(parts), [])

    def test_no_marker(self):
        _, parts = self.parse('Just some text with no blocks')
        reply = ''.join(p['delta'] for p in parts if p['type'] == 'text-delta')
        self.assertEqual(reply, 'Just some text with no blocks')
        self.assertEqual(len(self.blocks(parts, 'item_section')), 1)  # fallback

    def test_user_content_lists_candidates(self):
        content = compose.build_user_content(merged_intent={'query': 'q'}, followup_attribute='level', asked=['time'],
                                             history=[{'user': 'u', 'reply': 'r'}], message='m',
                                             candidates=default_candidates()[:1])
        self.assertIn('FOLLOW-UP ATTRIBUTE: level', content)
        self.assertIn('ALREADY ASKED: time', content)
        self.assertIn('"item_id": "edX+C1"', content)
