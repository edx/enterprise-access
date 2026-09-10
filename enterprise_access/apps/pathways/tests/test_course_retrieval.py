"""
Tests for course candidate retrieval.

The broadening cases are worth the most attention. A strict skill filter buys precision
and narrows the window before ``pathway_assembly`` can span the difficulty rungs, so
broadening *appends* rather than substitutes -- and the tests pin that ordering, because
substituting would throw away the precision instead of supplementing it.
"""
import ddt
from django.test import TestCase, override_settings

from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchError
from enterprise_access.apps.pathways.course_retrieval import (
    CANDIDATE_HITS_PER_PAGE,
    MAX_QUERY_WORDS,
    MAX_STRICT_FILTERS,
    MIN_CANDIDATES_FOR_ASSEMBLY,
    build_course_filters,
    build_course_query,
    eval_customer_uuid,
    retrieve_candidate_courses,
    skill_values
)

CUSTOMER_UUID = '417306cb-b24a-4d06-b83c-fb2a61d7fb96'


def translation(strict=(), boost=()):
    """Build a ``translate_skills``-shaped result."""
    def entries(values):
        return [
            {'term': v, 'catalog_value': v, 'catalog_field': 'skill_names', 'match_type': 'exact'}
            for v in values
        ]
    return {'strict': entries(strict), 'boost': entries(boost), 'unresolved': []}


class FakeAlgoliaClient:
    """Replays scripted catalog responses and records every search."""

    def __init__(self, responses=None, error=None):
        # A list, consumed in order, so the retry can return something different.
        self.responses = list(responses or [{'hits': []}])
        self.error = error
        self.calls = []

    def search_catalog_index(self, query, **kwargs):
        """Stand in for ``AlgoliaSearchClient.search_catalog_index``."""
        self.calls.append({'query': query, **kwargs})
        if self.error:
            raise self.error
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def hits(*keys, levels=None):
    """Build a hit list. ``levels`` cycles through difficulty rungs when supplied."""
    cycle = list(levels or ['Introductory'])
    return {'hits': [
        {'key': key, 'title': key, 'level_type': cycle[index % len(cycle)]}
        for index, key in enumerate(keys)
    ]}


def spanning(*keys):
    """A hit list wide enough and laddered enough to need no broadening."""
    return hits(*keys, levels=['Introductory', 'Intermediate', 'Advanced'])


class TestBuildCourseQuery(TestCase):
    """
    Tests for ``build_course_query``.
    """

    def test_the_career_name_leads(self):
        query = build_course_query(career_name='Data Analyst', boost_terms=['Python', 'SQL'])

        self.assertTrue(query.startswith('Data Analyst'))

    def test_the_query_is_capped_on_a_word_boundary(self):
        """
        The index ANDs every word; even relaxed, a very long query is mostly noise
        competing for ranking signal.
        """
        query = build_course_query(
            career_name='A B C D E', boost_terms=['F G H I J K L M N O P'],
        )

        words = query.split()
        self.assertEqual(len(words), MAX_QUERY_WORDS)
        self.assertNotIn('  ', query)

    def test_an_empty_career_name_still_produces_a_query_from_boosts(self):
        self.assertEqual(build_course_query(career_name='', boost_terms=['Welding']), 'Welding')


@ddt.ddt
class TestBuildCourseFilters(TestCase):
    """
    Tests for ``build_course_filters``.
    """

    def test_content_type_and_language_are_unconditional(self):
        filters = build_course_filters(strict_skills=[], customer_uuid='')

        self.assertIn('content_type:course', filters)
        self.assertIn('language:"English"', filters)

    def test_the_customer_scope_is_applied_when_supplied(self):
        filters = build_course_filters(strict_skills=[], customer_uuid=CUSTOMER_UUID)

        self.assertIn(f'enterprise_customer_uuids:"{CUSTOMER_UUID}"', filters)

    def test_strict_skills_are_ored_not_anded(self):
        """
        A course rarely carries every skill of a career, so ANDing them would routinely
        return nothing.
        """
        filters = build_course_filters(strict_skills=['Welding', 'Blueprint Reading'])

        self.assertIn('(skill_names:"Welding" OR skill_names:"Blueprint Reading")', filters)

    def test_no_skill_clause_is_added_when_none_resolved(self):
        self.assertNotIn('skill_names', build_course_filters(strict_skills=[]))


class TestSkillValues(TestCase):
    """
    Tests for ``skill_values``.
    """

    def test_catalog_values_are_read_from_the_bucket(self):
        values = skill_values(translation(strict=['Welding']), 'strict', 4)

        self.assertEqual(values, ['Welding'])

    def test_compound_artifacts_are_dropped(self):
        """"SQL & Python" matches no facet value, so it spends a slot to boost nothing."""
        values = skill_values(translation(boost=['SQL & Python', 'Welding']), 'boost', 4)

        self.assertEqual(values, ['Welding'])

    def test_the_budget_is_a_prefix_because_order_carries_relevance(self):
        values = skill_values(translation(strict=['A', 'B', 'C', 'D', 'E', 'F']), 'strict', 2)

        self.assertEqual(values, ['A', 'B'])

    def test_a_missing_bucket_is_empty_rather_than_an_error(self):
        self.assertEqual(skill_values({}, 'strict', 4), [])


class TestRetrieveCandidateCourses(TestCase):
    """
    Tests for ``retrieve_candidate_courses``.
    """

    def test_twenty_candidates_are_requested(self):
        """
        Not five. Relevance ranking is intro-heavy at rank 5 and recovers by rank 20, and
        assembly needs the wider window to span the rungs.
        """
        client = FakeAlgoliaClient([hits('A+1')])

        retrieve_candidate_courses(
            career_name='Welder', translation=translation(), algolia_client=client,
        )

        self.assertEqual(client.calls[0]['hitsPerPage'], CANDIDATE_HITS_PER_PAGE)

    def test_the_query_is_relaxed_because_the_index_ands_every_word(self):
        client = FakeAlgoliaClient([hits('A+1')])

        retrieve_candidate_courses(
            career_name='Welder', translation=translation(), algolia_client=client,
        )

        self.assertEqual(client.calls[0]['removeWordsIfNoResults'], 'allOptional')

    def test_candidates_carry_the_metadata_the_reranker_needs(self):
        """Scenario: Candidates carry the metadata the re-ranker needs."""
        client = FakeAlgoliaClient([hits('A+1')])

        retrieve_candidate_courses(
            career_name='Welder', translation=translation(), algolia_client=client,
        )

        requested = client.calls[0]['attributesToRetrieve']
        for attribute in ('key', 'title', 'short_description', 'full_description', 'level_type'):
            self.assertIn(attribute, requested)

    def test_boosts_become_optional_filters_not_hard_ones(self):
        client = FakeAlgoliaClient([hits('A+1')])

        retrieve_candidate_courses(
            career_name='Welder', translation=translation(boost=['Welding']),
            algolia_client=client,
        )

        self.assertEqual(client.calls[0]['optionalFilters'], ['skill_names:Welding'])
        self.assertNotIn('skill_names:"Welding"', client.calls[0]['filters'])

    def test_the_strict_filter_budget_is_respected(self):
        client = FakeAlgoliaClient([hits('A+1')])

        result = retrieve_candidate_courses(
            career_name='Welder',
            translation=translation(strict=[f'S{i}' for i in range(10)]),
            algolia_client=client,
        )

        self.assertEqual(len(result['strict_filters_applied']), MAX_STRICT_FILTERS)

    def test_a_thin_strict_set_is_broadened(self):
        """
        Measured: a strict skill filter turned `Data Analyst` from 2/2/1 into 5/0/0 by
        narrowing the window before assembly could span it.
        """
        client = FakeAlgoliaClient([hits('A+1', 'B+2'), hits('C+3', 'D+4')])

        result = retrieve_candidate_courses(
            career_name='Welder', translation=translation(strict=['Welding']),
            algolia_client=client,
        )

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(result['broadened'])
        self.assertEqual(result['strict_hit_count'], 2)
        self.assertNotIn('skill_names', client.calls[1]['filters'])

    def test_broadening_appends_rather_than_substitutes(self):
        """
        The precisely-matched courses must keep their rank, or the precision the strict
        filter bought is thrown away rather than supplemented.
        """
        client = FakeAlgoliaClient([hits('A+1'), hits('Z+9', 'A+1', 'B+2')])

        result = retrieve_candidate_courses(
            career_name='Welder', translation=translation(strict=['Welding']),
            algolia_client=client,
        )

        keys = [hit['key'] for hit in result['courses']]
        self.assertEqual(keys[0], 'A+1')
        # And the duplicate from the broad search is not re-added.
        self.assertEqual(keys.count('A+1'), 1)
        self.assertEqual(keys, ['A+1', 'Z+9', 'B+2'])

    def test_a_wide_laddered_strict_set_is_not_broadened(self):
        """A second search would be pure cost when the window is already sufficient."""
        client = FakeAlgoliaClient([
            spanning(*[f'A+{i}' for i in range(MIN_CANDIDATES_FOR_ASSEMBLY)]),
        ])

        result = retrieve_candidate_courses(
            career_name='Welder', translation=translation(strict=['Welding']),
            algolia_client=client,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertFalse(result['broadened'])
        self.assertEqual(result['strict_rungs_spanned'], 3)

    def test_a_wide_but_single_rung_strict_set_is_still_broadened(self):
        """
        The measured `Data Analyst` case: 17 strict hits cleared the count threshold and
        still assembled to 5/0/0, because every hit sat on the Introductory rung. A hit
        count alone cannot detect that.
        """
        flat = hits(*[f'A+{i}' for i in range(MIN_CANDIDATES_FOR_ASSEMBLY + 5)])
        client = FakeAlgoliaClient([flat, spanning('B+1', 'B+2', 'B+3')])

        result = retrieve_candidate_courses(
            career_name='Data Analyst', translation=translation(strict=['Data Analysis']),
            algolia_client=client,
        )

        self.assertTrue(result['broadened'])
        self.assertEqual(result['strict_rungs_spanned'], 1)

    def test_zero_hits_without_strict_filters_does_not_broaden(self):
        """There is nothing left to relax, so a second identical search is pure cost."""
        client = FakeAlgoliaClient([{'hits': []}])

        result = retrieve_candidate_courses(
            career_name='Welder', translation=translation(), algolia_client=client,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertFalse(result['broadened'])
        self.assertTrue(result['zero_hits'])

    def test_a_career_with_no_courses_is_reported_not_raised(self):
        """
        Scenario (replacing the plan's scope-only fallback): "no courses for this career
        in this catalog" is a real answer, and the caller turns it into a no-pathway.
        """
        client = FakeAlgoliaClient([{'hits': []}, {'hits': []}])

        result = retrieve_candidate_courses(
            career_name='Underwater Basket Weaver',
            translation=translation(strict=['Basket Weaving']),
            algolia_client=client,
        )

        self.assertTrue(result['zero_hits'])
        self.assertEqual(result['courses'], [])

    def test_a_search_failure_propagates(self):
        """A transport failure is not a "no coverage" answer and must not look like one."""
        client = FakeAlgoliaClient(error=AlgoliaSearchError('boom'))

        with self.assertRaises(AlgoliaSearchError):
            retrieve_candidate_courses(
                career_name='Welder', translation=translation(), algolia_client=client,
            )

    def test_non_dict_hits_are_discarded(self):
        client = FakeAlgoliaClient([{'hits': [{'key': 'A+1'}, 'garbage', None]}])

        result = retrieve_candidate_courses(
            career_name='Welder', translation=translation(), algolia_client=client,
        )

        self.assertEqual(result['courses'], [{'key': 'A+1'}])


class TestEvalCustomerUuid(TestCase):
    """
    Tests for ``eval_customer_uuid``.
    """

    @override_settings(PATHWAYS_EVAL_CUSTOMER_UUID=f'  {CUSTOMER_UUID}  ')
    def test_the_configured_uuid_is_normalised(self):
        self.assertEqual(eval_customer_uuid(), CUSTOMER_UUID)

    @override_settings(PATHWAYS_EVAL_CUSTOMER_UUID='')
    def test_no_pinned_customer_is_an_empty_string(self):
        self.assertEqual(eval_customer_uuid(), '')
