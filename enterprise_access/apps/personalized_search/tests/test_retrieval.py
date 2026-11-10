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
                         {'level_type': 'Introductory', 'weeks_to_complete': 4, 'pacing_type': 'self_paced',
                          'hours_per_week_min': 2, 'hours_per_week_max': 4})

    def test_effort_that_is_missing_or_not_a_positive_number_is_dropped(self):
        for effort in ((None, None), (0, -1), (True, '3')):
            facts = retrieval.hydrate_card(course_hit('edX+C1', effort=effort))['facts']
            self.assertEqual((facts['hours_per_week_min'], facts['hours_per_week_max']), (None, None))
        self.assertEqual(retrieval.compose_view(candidate(course_hit('edX+C1')))['hours_per_week'], [2, 4])

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
        self.assertEqual([r['tier'] for r in reqs], [2, 1, 2, 1, 0, 0, 0, 0, 0, 0])
        # Each type is counted with the keyword query and with the skills filter; the larger count wins.
        self.assertEqual([r.get('count_for') for r in reqs if r['tier'] == 0],
                         ['course', 'course', 'executive_education', 'executive_education', 'program', 'program'])
        self.assertTrue(all(r['hitsPerPage'] == 0 for r in reqs if r['tier'] == 0))
        self.assertIn('(skill_names:"Leadership" OR skill_names:"Coaching")', reqs[0]['filters'])
        self.assertIn('NOT key:"edX+OLD"', reqs[1]['filters'])
        self.assertIn('NOT aggregation_key:"program:edX+OLD"', reqs[3]['filters'])
        self.assertFalse(any(r.get('preferred') for r in reqs))
        self.assertTrue(all('(enterprise_customer_uuids:"e-1")' in r['filters'] for r in reqs))

    def test_programs_are_searched_unless_the_learner_asked_for_courses(self):
        def program_hits(types):
            reqs = retrieval.build_sub_requests(dict(INTENT, item_types=types), SCOPE, [])
            return [r for r in reqs if r['tier'] > 0 and r['filters'].startswith('content_type:program')]
        self.assertTrue(program_hits(['executive_education']))  # programs stay alongside a preferred type
        self.assertFalse(program_hits(['course']))
        self.assertFalse(program_hits(['course', 'executive_education']))  # "I don't want a program"
        self.assertEqual([retrieval.programs_wanted({'item_types': t}) for t in
                          (None, ['program'], ['executive_education'], ['course'], ['course', 'executive_education'])],
                         [True, True, True, False, False])

    def test_english_by_default_and_the_learners_language_when_given(self):
        english = retrieval.build_sub_requests(INTENT, SCOPE, [])
        self.assertTrue(all('language:"English" AND metadata_language:"en"' in r['filters']
                            for r in english if r['filters'].startswith('content_type:course')))
        spanish_scope = retrieval.CatalogScope(filter=SCOPE.filter, locale='es', language_name='Spanish')
        spanish = retrieval.build_sub_requests(INTENT, spanish_scope, [])
        courses = [r for r in spanish if r['filters'].startswith('content_type:course')]
        programs = [r for r in spanish if r['filters'].startswith('content_type:program')]
        spanish_courses = 'language:"Spanish" AND metadata_language:"es"'
        self.assertTrue(courses and all(spanish_courses in r['filters'] for r in courses))
        # Programs list every language they're taught in; one taught in the learner's language qualifies.
        self.assertTrue(programs and all(spanish_courses in r['filters'] for r in programs))
        english_programs = [r for r in english if r['filters'].startswith('content_type:program')]
        self.assertTrue(english_programs and all('language:"English" AND metadata_language:"en"' in r['filters']
                                                 for r in english_programs))

    def test_a_larger_shortlist_asks_for_more_hits_and_keeps_more_candidates(self):
        hits = [r['hitsPerPage'] for r in retrieval.build_sub_requests(INTENT, SCOPE, []) if r['hitsPerPage']]
        more = [r['hitsPerPage'] for r in retrieval.build_sub_requests(INTENT, SCOPE, [], shortlist=40)
                if r['hitsPerPage']]
        self.assertEqual(more, [h * 2 for h in hits])
        smaller = [r['hitsPerPage'] for r in retrieval.build_sub_requests(INTENT, SCOPE, [], shortlist=10)
                   if r['hitsPerPage']]
        self.assertEqual(smaller, hits)  # a smaller shortlist still searches as widely
        raw = [candidate(course_hit(f'edX+K{i}'), rank=i) for i in range(30)] + \
              [candidate(program_hit(f'p-{i}'), rank=i) for i in range(15)]
        self.assertEqual(len(retrieval.prefilter(raw, {})), 15)  # course cap 10 + program cap 5
        self.assertEqual(len(retrieval.prefilter(raw, {}, shortlist=40)), 30)  # caps scale: 20 + 10

    def test_compose_view_names_a_programs_credential(self):
        view = retrieval.compose_view(candidate(program_hit('p-1')))
        self.assertEqual(view['program_type'], 'Professional Certificate')
        self.assertNotIn('program_type', retrieval.compose_view(candidate(course_hit('edX+C1'))))

    @ddt.data(
        (['executive_education'], 'AND learning_type:"Executive Education"'),
        (['course'], 'AND NOT learning_type:"Executive Education"'),
        (['program'], None),
    )
    @ddt.unpack
    def test_a_preferred_type_adds_its_own_requests_and_keeps_every_type(self, types, clause):
        reqs = retrieval.build_sub_requests(dict(INTENT, item_types=types), SCOPE, [])
        hits = [r for r in reqs if r['tier'] > 0]
        general, preferred = [r for r in hits if not r.get('preferred')], [r for r in hits if r.get('preferred')]
        # Every type is still searched (courses with Executive Education, and programs unless the learner
        # asked for courses) ...
        searches_programs = any(r['filters'].startswith('content_type:program') for r in general)
        self.assertEqual(searches_programs, types != ['course'])
        self.assertTrue(any(r['filters'].startswith('content_type:course') and 'learning_type' not in r['filters']
                            for r in general))
        # ... plus the preferred type's own strict and broadened requests.
        self.assertEqual(len(preferred), 2)
        if clause:
            self.assertTrue(all(clause in r['filters'] for r in preferred))
        else:
            self.assertTrue(all(r['filters'].startswith('content_type:program') for r in preferred))

    def test_search_uses_scope_in_one_multi_query(self):
        client = mock.Mock()
        client.multi_search_catalog_index.return_value = [
            {'hits': [course_hit('edX+C1'), {'title': 'no id'}]}, {'hits': [course_hit('edX+C2')]},
            {'hits': []}, {'hits': []},
            # Counts: keyword then skills, per type; the larger one is the total.
            {'hits': [], 'nbHits': 1}, {'hits': [], 'nbHits': 42}, {'hits': [], 'nbHits': 9}, {'hits': [], 'nbHits': 3},
            {'hits': [], 'nbHits': 0}, {'hits': []},
        ]
        scope = retrieval.CatalogScope(filter='x:"y"', allow_unscoped=True)
        out = retrieval.search(INTENT, scope, [], catalog=AlgoliaCatalog(client), timeout_s=1.5)
        self.assertEqual([(c['card']['item_id'], c['tier']) for c in out], [('edX+C1', 2), ('edX+C2', 1)])
        queries = client.multi_search_catalog_index.call_args.args[0]
        self.assertEqual(len(queries), 10)
        self.assertTrue(all(q['distinct'] and not {'tier', 'count_for', 'preferred'} & set(q) for q in queries))
        self.assertEqual(out.totals, {'course': 42, 'executive_education': 9, 'program': 0})
        kwargs = client.multi_search_catalog_index.call_args.kwargs
        self.assertEqual((kwargs['allow_unscoped'], kwargs['secured_key'], kwargs['timeout_s']), (True, None, 1.5))

    def test_search_failure(self):
        client = mock.Mock()
        client.multi_search_catalog_index.side_effect = RuntimeError('down')
        with self.assertRaises(retrieval.RetrievalFailed):
            retrieval.search(INTENT, SCOPE, [], catalog=AlgoliaCatalog(client))


class PrefilterTests(TestCase):
    """``prefilter`` dedupes, scores and caps."""

    def test_with_no_type_asked_for_related_programs_lead(self):
        raw = [candidate(course_hit('edX+C1'), tier=2, rank=0), candidate(program_hit('p-1'), tier=2, rank=0)]
        kept = retrieval.prefilter(raw, dict(INTENT, level=None))
        self.assertEqual(kept[0]['card']['item_type'], 'program')

    def test_a_preferred_type_leads_and_may_fill_more_places(self):
        raw = [candidate(course_hit(f'edX+C{i}'), tier=2, rank=i) for i in range(12)]
        raw += [candidate(course_hit(f'Yale+E{i}', exec_ed=True), tier=1, rank=i) for i in range(12)]
        kept = retrieval.prefilter(raw, dict(INTENT, item_types=['executive_education']))
        types = [c['card']['item_type'] for c in kept]
        self.assertEqual(types[0], 'executive_education')
        self.assertEqual(types.count('executive_education'), 10)  # above the usual cap of 5
        self.assertEqual(types.count('course'), 10)  # the other type stays as alternatives

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

    def test_hours_per_week_within_the_learners_limit_scores_higher(self):
        raw = [candidate(course_hit('edX+HEAVY', effort=(8, 10)), tier=1, rank=0),
               candidate(course_hit('edX+LIGHT', effort=(2, 3)), tier=1, rank=5)]
        kept = retrieval.prefilter(raw, dict(INTENT, constraints={'max_hours_per_week': 4}))
        self.assertEqual(kept[0]['card']['item_id'], 'edX+LIGHT')

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
        self.assertEqual([r['tier'] for r in requests], [1, 1, 0, 0, 0])
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

    def test_a_known_hours_limit_counts_as_time_already_set(self):
        cands = [candidate(course_hit(f'edX+S{i}', level='Advanced', weeks=2)) for i in range(3)]
        cands += [candidate(course_hit(f'edX+L{i}', level='Advanced', weeks=20)) for i in range(3)]
        self.assertEqual(retrieval.choose_followup_attribute(cands, INTENT, []), 'time')
        self.assertEqual(retrieval.choose_followup_attribute(
            cands, dict(INTENT, constraints={'max_hours_per_week': 4}), []), 'open')
