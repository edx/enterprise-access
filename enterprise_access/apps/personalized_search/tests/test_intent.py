"""Tests for intent parsing and the spec's merge rule."""
import json

import ddt
from django.test import TestCase

from enterprise_access.apps.personalized_search.core import intent


@ddt.ddt
class IntentParseTests(TestCase):
    """``intent.parse`` normalizes the model's JSON and rejects anything else."""

    def test_on_topic(self):
        out = intent.parse('Sure: ' + json.dumps({
            'on_topic': True, 'redirect_reason': None, 'merge': 'replace',
            'intent': {'query': ' team leadership ', 'skills': ['a'] * 3 + [f's{i}' for i in range(10)],
                       'level': 'expert', 'item_types': ['course', 'video'], 'constraints': {'max_weeks': 4.0}},
        }))
        self.assertTrue(out['on_topic'])
        self.assertEqual(out['intent']['query'], 'team leadership')
        self.assertEqual(len(out['intent']['skills']), 8)
        self.assertIsNone(out['intent']['level'])
        self.assertEqual(out['intent']['item_types'], ['course'])
        self.assertEqual(out['intent']['constraints'], {'max_weeks': 4, 'pacing': None})

    @ddt.data(
        ({'on_topic': False, 'redirect_reason': 'unsafe'}, 'unsafe'),
        ({'on_topic': False, 'redirect_reason': 'nonsense'}, 'off_topic'),
        ({'on_topic': True, 'redirect_reason': 'injection'}, 'injection'),
    )
    @ddt.unpack
    def test_redirects(self, raw, expected):
        out = intent.parse(json.dumps(raw))
        self.assertFalse(out['on_topic'])
        self.assertEqual(out['redirect_reason'], expected)
        self.assertIsNone(out['intent'])

    @ddt.data('no json here', '{"redirect_reason": null}', '[1, 2]')
    def test_invalid(self, text):
        with self.assertRaises(intent.IntentInvalid):
            intent.parse(text)


class IntentMergeTests(TestCase):
    """``intent.merge`` implements refine/replace."""

    prior = {'query': 'team leadership', 'skills': ['leadership'], 'roles': ['manager'], 'level': None,
             'item_types': ['course', 'executive_education', 'program'],
             'constraints': {'max_weeks': None, 'pacing': None}}

    def test_refine_overlays(self):
        merged = intent.merge(self.prior, {'skills': ['coaching'], 'item_types': ['program'],
                                           'constraints': {'max_weeks': 4, 'pacing': None}}, 'refine', message='x')
        self.assertEqual(merged['query'], 'team leadership')
        self.assertEqual(merged['skills'], ['leadership', 'coaching'])
        self.assertEqual(merged['roles'], ['manager'])
        self.assertEqual(merged['item_types'], ['program'])
        self.assertEqual(merged['constraints'], {'max_weeks': 4, 'pacing': None})

    def test_replace_starts_over(self):
        merged = intent.merge(self.prior, {'query': 'python', 'skills': ['python']}, 'replace', message='x')
        self.assertEqual(merged['query'], 'python')
        self.assertEqual(merged['skills'], ['python'])
        self.assertEqual(merged['roles'], [])
        self.assertEqual(len(merged['item_types']), 3)

    def test_query_falls_back_to_message(self):
        merged = intent.merge(None, {}, 'replace', message='learn ' * 20)
        self.assertEqual(len(merged['query'].split()), 12)
