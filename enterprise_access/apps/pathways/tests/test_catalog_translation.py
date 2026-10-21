"""
Tests for translating career vocabulary into catalog vocabulary.
"""
from unittest import mock

import ddt
from django.test import TestCase

from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchError
from enterprise_access.apps.pathways import catalog_translation as translation

PATCH_ALGOLIA_CLIENT = 'enterprise_access.apps.pathways.catalog_translation.AlgoliaSearchClient'

# Verbatim `skill_names` values observed in the production catalog index on 2026-09-09.
SNAPSHOT = {
    'skill_names': [
        'Python (Programming Language)',
        'SQL (Programming Language)',
        'Microsoft Excel',
        'Excel Macros',
        'Data Analysis',
        'Machine Learning',
        'Nursing',
    ],
    'skills.name': ['Communication'],
    'subjects': ['Computer Science', 'Business & Management'],
}


class FakeAlgoliaClient:
    """Records catalog searches and facet searches, replaying scripted responses."""

    def __init__(self, search_response=None, facet_hits=None, facet_error=None):
        self.search_response = search_response or {}
        self.facet_hits = facet_hits or {}
        self.facet_error = facet_error
        self.search_calls = []
        self.facet_calls = []

    def search_catalog_index(self, query, **kwargs):
        """Stand in for ``AlgoliaSearchClient.search_catalog_index``."""
        self.search_calls.append({'query': query, **kwargs})
        return self.search_response

    def search_facet_values(self, facet_name, facet_query, **kwargs):
        """Stand in for ``AlgoliaSearchClient.search_facet_values``."""
        self.facet_calls.append({'facet': facet_name, 'query': facet_query, **kwargs})
        if self.facet_error:
            raise self.facet_error
        values = self.facet_hits.get(facet_query, [])
        return [{'value': value, 'count': 1} for value in values]


@ddt.ddt
class TestSnapshotCatalogFacets(TestCase):
    """
    Tests for ``snapshot_catalog_facets``.
    """

    def test_snapshot_reads_the_skill_and_subject_facets(self):
        client = FakeAlgoliaClient(search_response={'facets': {
            'skill_names': {'Python (Programming Language)': 95, 'Data Analysis': 141},
            'skills.name': {'Communication': 10},
            'subjects': {'Computer Science': 500},
        }})

        snapshot = translation.snapshot_catalog_facets(allow_unscoped=True, algolia_client=client)

        self.assertEqual(snapshot['skill_names'], ['Python (Programming Language)', 'Data Analysis'])
        self.assertEqual(snapshot['skills.name'], ['Communication'])
        self.assertEqual(snapshot['subjects'], ['Computer Science'])
        self.assertEqual(snapshot['truncated'], [])

    def test_snapshot_is_scoped_to_courses_and_asks_for_no_hits(self):
        """
        The snapshot must search the same scope course retrieval will, or a skill can be
        grounded against a value no in-scope course carries.
        """
        client = FakeAlgoliaClient(search_response={'facets': {}})

        translation.snapshot_catalog_facets(allow_unscoped=True, algolia_client=client)

        call = client.search_calls[0]
        self.assertEqual(call['query'], '')
        self.assertEqual(call['filters'], 'content_type:course')
        self.assertEqual(call['hitsPerPage'], 0)
        self.assertEqual(call['maxValuesPerFacet'], translation.MAX_VALUES_PER_FACET)
        self.assertEqual(call['facets'], list(translation.CATALOG_FACET_FIELDS))

    def test_truncation_is_detected_and_named(self):
        """
        A facet at the cap is almost certainly incomplete. Measured on the live index,
        ``skill_names`` returns exactly 1,000 values, which is why the refinement pass
        exists at all -- so this must be visible, not silent.
        """
        at_cap = {f'skill-{index}': 1 for index in range(translation.MAX_VALUES_PER_FACET)}
        client = FakeAlgoliaClient(search_response={'facets': {'skill_names': at_cap}})

        snapshot = translation.snapshot_catalog_facets(allow_unscoped=True, algolia_client=client)

        self.assertEqual(snapshot['truncated'], ['skill_names'])

    def test_absent_facets_become_empty_lists(self):
        """Algolia omits a facet entirely when it has no values in scope."""
        client = FakeAlgoliaClient(search_response={})

        snapshot = translation.snapshot_catalog_facets(allow_unscoped=True, algolia_client=client)

        for facet_field in translation.CATALOG_FACET_FIELDS:
            self.assertEqual(snapshot[facet_field], [])

    @mock.patch(PATCH_ALGOLIA_CLIENT)
    def test_client_is_constructed_when_not_injected(self, mock_client_class):
        mock_client_class.return_value.search_catalog_index.return_value = {'facets': {}}

        translation.snapshot_catalog_facets(allow_unscoped=True)

        mock_client_class.assert_called_once()


@ddt.ddt
class TestTranslateSkills(TestCase):
    """
    Tests for ``translate_skills``.
    """

    def test_high_confidence_matches_become_strict_and_weak_ones_boost(self):
        result = translation.translate_skills(terms=['Python', 'Excel'], facet_snapshot=SNAPSHOT)

        self.assertEqual(
            [entry['catalog_value'] for entry in result['strict']],
            ['Python (Programming Language)'],
        )
        self.assertEqual(
            [entry['catalog_value'] for entry in result['boost']],
            ['Microsoft Excel'],
        )

    def test_only_real_facet_values_survive(self):
        """Scenario: Only real facet values survive."""
        result = translation.translate_skills(
            terms=['Python', 'Underwater Basket Weaving'],
            facet_snapshot=SNAPSHOT,
        )

        emitted = [entry['catalog_value'] for entry in result['strict'] + result['boost']]
        self.assertNotIn('Underwater Basket Weaving', emitted)
        self.assertEqual(result['unresolved'], ['Underwater Basket Weaving'])

    def test_skill_counts_are_capped(self):
        """Scenario: Skill counts are capped."""
        strict_values = [f'Skill {index}' for index in range(20)]
        boost_values = [f'Prefixed Boost {index}' for index in range(20)]
        snapshot = {'skill_names': strict_values + boost_values, 'skills.name': []}
        terms = strict_values + [f'Boost {index}' for index in range(20)]

        result = translation.translate_skills(terms=terms, facet_snapshot=snapshot)

        self.assertEqual(len(result['strict']), translation.MAX_STRICT_SKILLS)
        self.assertEqual(len(result['boost']), translation.MAX_BOOST_SKILLS)

    def test_a_value_is_never_both_strict_and_boost(self):
        """A repeated facet value narrows nothing and only costs query length."""
        result = translation.translate_skills(
            terms=['Python', 'Python (Programming Language)'],
            facet_snapshot=SNAPSHOT,
        )

        strict = {entry['catalog_value'] for entry in result['strict']}
        boost = {entry['catalog_value'] for entry in result['boost']}
        self.assertFalse(strict & boost)

    def test_match_type_is_recorded(self):
        result = translation.translate_skills(
            terms=['Data Analysis', 'Python', 'Excel'],
            facet_snapshot=SNAPSHOT,
        )

        by_term = {entry['term']: entry['match_type']
                   for entry in result['strict'] + result['boost']}
        self.assertEqual(by_term['Data Analysis'], 'exact')
        self.assertEqual(by_term['Python'], 'qualified')
        self.assertEqual(by_term['Excel'], 'contained')

    @ddt.data(
        (['Python', 'Data Analysis'], 1.0),
        (['Python', 'Nonsense'], 0.5),
        (['Nonsense One', 'Nonsense Two'], 0.0),
    )
    @ddt.unpack
    def test_resolution_rate_is_reported(self, terms, expected):
        result = translation.translate_skills(terms=terms, facet_snapshot=SNAPSHOT)

        self.assertEqual(result['resolution_rate'], expected)

    def test_empty_terms_produce_an_empty_translation(self):
        result = translation.translate_skills(terms=[], facet_snapshot=SNAPSHOT)

        self.assertEqual(result['strict'], [])
        self.assertEqual(result['boost'], [])
        self.assertIsNone(result['resolution_rate'])


class TestRefineUnmatchedSkills(TestCase):
    """
    Tests for the conditional facet-search refinement pass.
    """

    def test_facet_search_recovers_a_term_missing_from_the_capped_snapshot(self):
        """
        The reason this pass exists. Measured on the live index, ``Welding`` matches a
        real course but falls outside the top 1,000 facet values, so the snapshot cannot
        serve it.
        """
        client = FakeAlgoliaClient(facet_hits={'Welding': ['Welding', 'Welding Equipment']})

        result = translation.refine_unmatched_skills(
            unresolved=['Welding'], facet_snapshot=SNAPSHOT,
            allow_unscoped=True, algolia_client=client,
        )

        self.assertEqual(
            [entry['catalog_value'] for entry in result['recovered']], ['Welding'],
        )
        self.assertEqual(result['unresolved'], [])

    def test_one_request_is_issued_per_unresolved_term(self):
        client = FakeAlgoliaClient(facet_hits={'Welding': ['Welding']})

        translation.refine_unmatched_skills(
            unresolved=['Welding', 'Brazing'], facet_snapshot=SNAPSHOT,
            allow_unscoped=True, algolia_client=client,
        )

        self.assertEqual([call['query'] for call in client.facet_calls], ['Welding', 'Brazing'])

    def test_candidates_already_in_the_snapshot_are_not_reconsidered(self):
        """
        They were considered and rejected on the first pass; re-offering them would
        change the answer for no new information.
        """
        client = FakeAlgoliaClient(facet_hits={'Spreadsheet': ['Microsoft Excel', 'Excel Macros']})

        result = translation.refine_unmatched_skills(
            unresolved=['Spreadsheet'], facet_snapshot=SNAPSHOT,
            allow_unscoped=True, algolia_client=client,
        )

        self.assertEqual(result['recovered'], [])
        self.assertEqual(result['unresolved'], ['Spreadsheet'])

    def test_a_term_that_only_matches_loosely_is_not_recovered(self):
        """
        ``AWS`` should not become ``AWS Certified Solutions Architect Associate`` merely
        because that is the top facet-search candidate -- a certification is not the
        platform. The same resolution rules apply here as to the snapshot.
        """
        client = FakeAlgoliaClient(facet_hits={'AWS': [
            'AWS Certified Solutions Architect Associate',
            'AWS Serverless',
        ]})

        result = translation.refine_unmatched_skills(
            unresolved=['AWS'], facet_snapshot=SNAPSHOT,
            allow_unscoped=True, algolia_client=client,
        )

        recovered = [entry['catalog_value'] for entry in result['recovered']]
        # Containment still applies, so *something* may match -- but never the longest,
        # most-specific certification name.
        self.assertNotIn('AWS Certified Solutions Architect Associate', recovered)

    def test_an_unrecoverable_term_stays_unresolved(self):
        client = FakeAlgoliaClient(facet_hits={})

        result = translation.refine_unmatched_skills(
            unresolved=['Underwater Basket Weaving'], facet_snapshot=SNAPSHOT,
            allow_unscoped=True, algolia_client=client,
        )

        self.assertEqual(result['recovered'], [])
        self.assertEqual(result['unresolved'], ['Underwater Basket Weaving'])

    def test_a_failed_facet_search_is_recorded_not_raised(self):
        """Losing one term is better than failing the step."""
        client = FakeAlgoliaClient(facet_error=AlgoliaSearchError('boom'))

        result = translation.refine_unmatched_skills(
            unresolved=['Welding'], facet_snapshot=SNAPSHOT,
            allow_unscoped=True, algolia_client=client,
        )

        self.assertEqual(result['recovered'], [])
        self.assertEqual(result['unresolved'], ['Welding'])
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('boom', result['errors'][0])


class TestMergeRefinement(TestCase):
    """
    Tests for folding recovered terms back into a translation.
    """

    def test_recovered_matches_are_appended_by_confidence(self):
        base = translation.translate_skills(terms=['Python'], facet_snapshot=SNAPSHOT)
        refinement = {
            'recovered': [
                {'term': 'Welding', 'catalog_value': 'Welding',
                 'catalog_field': 'skill_names', 'match_type': 'exact'},
                {'term': 'Cloud', 'catalog_value': 'Cloud Computing',
                 'catalog_field': 'skill_names', 'match_type': 'contained'},
            ],
            'unresolved': [],
            'errors': [],
        }

        merged = translation.merge_refinement(base, refinement)

        self.assertEqual(
            [entry['catalog_value'] for entry in merged['strict']],
            ['Python (Programming Language)', 'Welding'],
        )
        self.assertEqual(
            [entry['catalog_value'] for entry in merged['boost']], ['Cloud Computing'],
        )
        self.assertEqual(merged['resolution_rate'], 1.0)

    def test_snapshot_matches_keep_their_budget_slots(self):
        """
        The snapshot is the only source known to be in scope, so it outranks facet search
        when the budget is tight.
        """
        snapshot = {'skill_names': [f'Skill {index}' for index in range(20)], 'skills.name': []}
        base = translation.translate_skills(
            terms=[f'Skill {index}' for index in range(20)], facet_snapshot=snapshot,
        )
        refinement = {
            'recovered': [{'term': 'Welding', 'catalog_value': 'Welding',
                           'catalog_field': 'skill_names', 'match_type': 'exact'}],
            'unresolved': [],
            'errors': [],
        }

        merged = translation.merge_refinement(base, refinement)

        self.assertEqual(len(merged['strict']), translation.MAX_STRICT_SKILLS)
        self.assertNotIn('Welding', [entry['catalog_value'] for entry in merged['strict']])

    def test_duplicate_recoveries_are_dropped(self):
        base = translation.translate_skills(terms=['Python'], facet_snapshot=SNAPSHOT)
        refinement = {
            'recovered': [{'term': 'Python 3', 'catalog_value': 'Python (Programming Language)',
                           'catalog_field': 'skill_names', 'match_type': 'qualified'}],
            'unresolved': [],
            'errors': [],
        }

        merged = translation.merge_refinement(base, refinement)

        self.assertEqual(len(merged['strict']), 1)

    def test_unresolved_after_refinement_lowers_the_rate(self):
        base = translation.translate_skills(
            terms=['Python', 'Welding'], facet_snapshot=SNAPSHOT,
        )
        refinement = {'recovered': [], 'unresolved': ['Welding'], 'errors': []}

        merged = translation.merge_refinement(base, refinement)

        self.assertEqual(merged['unresolved'], ['Welding'])
        self.assertEqual(merged['resolution_rate'], 0.5)
