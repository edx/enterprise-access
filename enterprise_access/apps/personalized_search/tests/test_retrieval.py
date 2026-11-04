"""Tests for hydration, sub-request building, prefilter and the follow-up attribute."""
from unittest import mock

import ddt
from django.test import TestCase

from enterprise_access.apps.personalized_search.adapters.algolia import AlgoliaCatalog
from enterprise_access.apps.personalized_search.core import retrieval
from enterprise_access.apps.personalized_search.tests.helpers import candidate, course_hit, program_hit

SCOPE = retrieval.CatalogScope(filter='enterprise_customer_uuids:"e-1"')
INTENT = {
    'query': 'team leadership', 'skills': ['Leadership', 'Coaching'], 'roles': ['Manager'], 'level': None,
    'item_types': ['course', 'executive_education', 'program'], 'constraints': {'max_weeks': None, 'pacing': None},
}


@ddt.ddt
class HydrationTests(TestCase):
    """``hydrate_card`` follows the spec's ItemCard table."""

    def test_course(self):
        card = retrieval.hydrate_card(course_hit('edX+C1'))
        self.assertEqual(card['item_id'], 'edX+C1')
        self.assertEqual(card['item_type'], 'course')
        self.assertEqual(card['image_url'], 'https://img/edX+C1.png')  # falls back to original_image_url
        self.assertEqual(card['partner_name'], 'edX')
        self.assertEqual(card['link'], {'kind': 'course', 'course_key': 'edX+C1', 'course_type': 'verified-audit'})
        self.assertEqual(card['facts'],
                         {'level_type': 'Introductory', 'weeks_to_complete': 4, 'pacing_type': 'self_paced'})

    def test_exec_ed(self):
        card = retrieval.hydrate_card(course_hit('Yale+E1', exec_ed=True))
        self.assertEqual(card['item_type'], 'executive_education')

    def test_program(self):
        card = retrieval.hydrate_card(program_hit('p-1'))
        self.assertEqual(card['item_id'], 'p-1')
        self.assertEqual(card['link'], {'kind': 'program', 'program_uuid': 'p-1'})
        self.assertEqual(card['partner_name'], 'MITx')
        self.assertEqual(card['facts']['course_count'], 3)
        self.assertEqual(card['facts']['program_type'], 'Professional Certificate')

    def test_compose_view_strips_html(self):
        view = retrieval.compose_view(candidate(course_hit('edX+C1')))
        self.assertEqual(view['short_description'], 'Learn things')


@ddt.ddt
class SubRequestTests(TestCase):
    """``build_sub_requests`` matches the spec's sub-request table."""

    def test_all_types_strict_and_broad(self):
        reqs = retrieval.build_sub_requests(INTENT, SCOPE, ['edX+OLD'])
        self.assertEqual([r['tier'] for r in reqs], [2, 1, 2, 1])
        self.assertIn('(skill_names:"Leadership" OR skill_names:"Coaching")', reqs[0]['filters'])
        self.assertIn('NOT key:"edX+OLD"', reqs[1]['filters'])
        self.assertIn('NOT aggregation_key:"program:edX+OLD"', reqs[3]['filters'])
        self.assertTrue(all('(enterprise_customer_uuids:"e-1")' in r['filters'] for r in reqs))

    @ddt.data(
        (['executive_education'], 'AND learning_type:"Executive Education"', 2),
        (['course'], 'AND NOT learning_type:"Executive Education"', 2),
        (['program'], None, 2),
    )
    @ddt.unpack
    def test_type_filters(self, types, clause, count):
        reqs = retrieval.build_sub_requests(dict(INTENT, item_types=types), SCOPE, [])
        self.assertEqual(len(reqs), count)
        if clause:
            self.assertIn(clause, reqs[0]['filters'])
        else:
            self.assertTrue(reqs[0]['filters'].startswith('content_type:program'))

    def test_search_uses_scope_in_one_multi_query(self):
        client = mock.Mock()
        client.multi_search_catalog_index.return_value = [
            {'hits': [course_hit('edX+C1'), {'title': 'no id'}]}, {'hits': [course_hit('edX+C2')]},
        ]
        scope = retrieval.CatalogScope(filter='x:"y"', allow_unscoped=True)
        out = retrieval.search(dict(INTENT, item_types=['course']), scope, [], catalog=AlgoliaCatalog(client),
                               timeout_s=1.5)
        self.assertEqual([(c['card']['item_id'], c['tier']) for c in out], [('edX+C1', 2), ('edX+C2', 1)])
        queries = client.multi_search_catalog_index.call_args.args[0]
        self.assertEqual(len(queries), 2)
        self.assertTrue(all(q['distinct'] and 'tier' not in q for q in queries))
        kwargs = client.multi_search_catalog_index.call_args.kwargs
        self.assertEqual((kwargs['allow_unscoped'], kwargs['secured_key'], kwargs['timeout_s']), (True, None, 1.5))

    def test_search_failure(self):
        client = mock.Mock()
        client.multi_search_catalog_index.side_effect = RuntimeError('down')
        with self.assertRaises(retrieval.RetrievalFailed):
            retrieval.search(INTENT, SCOPE, [], catalog=AlgoliaCatalog(client))


class PrefilterTests(TestCase):
    """``prefilter`` dedupes, scores and caps."""

    def test_caps_and_dedupe(self):
        raw = [candidate(course_hit(f'edX+C{i}'), tier=1, rank=i) for i in range(15)]
        raw += [candidate(course_hit('edX+C0'), tier=2, rank=0)]  # duplicate, better tier
        raw += [candidate(course_hit(f'Yale+E{i}', exec_ed=True), tier=1, rank=i) for i in range(8)]
        raw += [candidate(program_hit(f'p-{i}'), tier=1, rank=i) for i in range(8)]
        kept = retrieval.prefilter(raw, INTENT)
        types = [c['card']['item_type'] for c in kept]
        self.assertEqual(len(kept), 20)
        counts = (types.count('course'), types.count('executive_education'), types.count('program'))
        self.assertEqual(counts, (10, 5, 5))
        self.assertEqual(kept[0]['card']['item_id'], 'edX+C0')

    def test_constraints_score(self):
        raw = [candidate(course_hit('edX+LONG', weeks=10), rank=0),
               candidate(course_hit('edX+SHORT', weeks=3), rank=5)]
        kept = retrieval.prefilter(raw, dict(INTENT, constraints={'max_weeks': 4, 'pacing': None}))
        self.assertEqual(kept[0]['card']['item_id'], 'edX+SHORT')

    def test_level_match_scores_higher(self):
        raw = [candidate(course_hit('edX+ADV', level='Advanced'), tier=1, rank=0),
               candidate(course_hit('edX+INTRO', level='Introductory'), tier=1, rank=5)]
        kept = retrieval.prefilter(raw, dict(INTENT, level='introductory'))
        self.assertEqual(kept[0]['card']['item_id'], 'edX+INTRO')

    def test_pacing_match_scores_higher(self):
        raw = [candidate(course_hit('edX+LED', pacing='instructor_paced'), tier=1, rank=0),
               candidate(course_hit('edX+SELF', pacing='self_paced'), tier=1, rank=5)]
        kept = retrieval.prefilter(raw, dict(INTENT, constraints={'max_weeks': None, 'pacing': 'self_paced'}))
        self.assertEqual(kept[0]['card']['item_id'], 'edX+SELF')

    def test_exec_ed_is_scored_as_strict(self):
        raw = [candidate(course_hit('edX+C1'), tier=1, rank=0),
               candidate(course_hit('Yale+E1', exec_ed=True), tier=1, rank=50)]
        kept = retrieval.prefilter(raw, dict(INTENT, level=None))
        self.assertEqual(kept[0]['card']['item_id'], 'Yale+E1')


class FilterHelperTests(TestCase):
    """Quoting, the no-skills sub-requests and the weeks buckets."""

    def test_quote_escapes_backslashes_before_quotes(self):
        self.assertEqual(retrieval._quote('a"b'), '"a\\"b"')  # pylint: disable=protected-access
        self.assertEqual(retrieval._quote('a\\'), '"a\\\\"')  # pylint: disable=protected-access

    def test_no_skills_sends_only_broadened_requests(self):
        requests = retrieval.build_sub_requests(dict(INTENT, skills=[], roles=['Team lead']), SCOPE, [])
        self.assertEqual([r['tier'] for r in requests], [1, 1])
        self.assertTrue(all(r['query'] == 'Team lead' for r in requests))

    def test_weeks_buckets(self):
        buckets = [retrieval._weeks_bucket(w) for w in (None, 0, 4, 5, 12, 13)]  # pylint: disable=protected-access
        self.assertEqual(buckets, [None, None, '<=4', '5-12', '5-12', '>12'])


class FollowupAttributeTests(TestCase):
    """``choose_followup_attribute`` picks the most even split."""

    def test_most_even(self):
        cands = [candidate(course_hit(f'edX+L{i}', level='Introductory', weeks=4)) for i in range(5)]
        cands += [candidate(course_hit(f'edX+M{i}', level='Advanced', weeks=4)) for i in range(5)]
        self.assertEqual(retrieval.choose_followup_attribute(cands, INTENT, []), 'level')

    def test_skips_asked_and_set(self):
        cands = [candidate(course_hit(f'edX+L{i}', level='Introductory', pacing='self_paced')) for i in range(3)]
        cands += [candidate(course_hit(f'edX+M{i}', level='Advanced', pacing='instructor_paced')) for i in range(3)]
        self.assertEqual(retrieval.choose_followup_attribute(cands, INTENT, ['level']), 'pacing')
        self.assertEqual(retrieval.choose_followup_attribute(
            cands, dict(INTENT, constraints={'max_weeks': None, 'pacing': 'self_paced'}), ['level']), 'open')
