"""
Tests for the career discovery domain layer.

Every test mocks Xpert and Algolia; nothing here issues a network call.
"""
from unittest import mock

import ddt
from django.test import TestCase

from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchError
from enterprise_access.apps.pathways import api as pathways_api
from enterprise_access.apps.prompts.api import PromptError
from enterprise_access.apps.prompts.api_client import XpertAPIResponseError, XpertResponseMessage
from enterprise_access.apps.prompts.models import PromptType
from enterprise_access.apps.prompts.tests.factories import XpertLearnerPathwaysSystemPromptFactory

PATCH_XPERT_CLIENT = 'enterprise_access.apps.prompts.api.XpertAPIClient'
PATCH_ALGOLIA_CLIENT = 'enterprise_access.apps.pathways.api.AlgoliaSearchClient'

INTAKE = {
    'selected_goals': 'move into data analysis',
    'free_text': 'I report on spreadsheets all day and want to automate it',
    'known_context': 'operations analyst, five years',
    'interested_industries': 'healthcare, technology',
}

JOBS_HIT = {
    'external_id': 'ETE78CD2CDFFFAC66B',
    'name': 'Data Analyst',
    'skills': [{'name': 'SQL (Programming Language)'}, {'name': 'Data Analysis'}],
    'industry_names': ['Health Care', 'Information'],
}


@ddt.ddt
class TestNameHelpers(TestCase):
    """Tests for the name-normalisation helpers."""

    @ddt.data(
        ([' SQL ', 'SQL', ''], ['SQL']),
        (['Python', 'Excel', 'Python'], ['Python', 'Excel']),
        ([None, 3, 'Nursing'], ['Nursing']),
        (None, []),
    )
    @ddt.unpack
    def test_dedupe_names(self, values, expected):
        assert pathways_api.dedupe_names(values) == expected

    def test_dedupe_names_preserves_order(self):
        # Order carries relevance: the first required skill becomes the fallback query.
        assert pathways_api.dedupe_names(['C', 'A', 'B', 'A']) == ['C', 'A', 'B']

    @ddt.data(
        ('SQL', ['SQL']),
        (['SQL', 'SQL'], ['SQL']),
        (None, []),
        ({'skills': ['SQL']}, []),
        (7, []),
    )
    @ddt.unpack
    def test_coerce_name_list(self, value, expected):
        assert pathways_api.coerce_name_list(value) == expected

    @ddt.data(
        ('SQL & Python', True),
        ('Excel + Tableau', True),
        ('Research & Development Management', True),
        ('SQL (Programming Language)', False),
        ('C++', False),
    )
    @ddt.unpack
    def test_is_malformed_compound(self, name, expected):
        assert pathways_api.is_malformed_compound(name) is expected


@ddt.ddt
class TestQueryConstruction(TestCase):
    """Tests for the Algolia query, filters and optional filters."""

    def test_condensed_query_is_preferred(self):
        query = pathways_api.build_career_query(
            condensed_query='  data analyst  ',
            skills_required=['SQL'],
        )
        assert query == 'data analyst'

    def test_falls_back_to_first_required_skill(self):
        query = pathways_api.build_career_query(condensed_query='', skills_required=['', 'SQL', 'Python'])
        assert query == 'SQL'

    def test_query_is_empty_when_nothing_is_available(self):
        assert pathways_api.build_career_query(condensed_query=None, skills_required=[]) == ''

    def test_language_is_always_filtered_even_with_no_other_criteria(self):
        """
        The jobs index holds translated duplicates of the same role -- the Spanish
        record's identifier is the English one plus "-es" -- so both surface for the same
        query, which is the defect persona 2's author reported. The language clause is
        therefore unconditional, not a refinement.
        """
        assert pathways_api.build_career_filters(
            industries=[], job_sources=[],
        ) == 'metadata_language:en'

    @ddt.data(
        (['Health Care'], [], 'metadata_language:en AND (industry_names:"Health Care")'),
        ([], ['lightcast'], 'metadata_language:en AND (job_sources:"lightcast")'),
        (
            ['Health Care', 'Information'],
            ['lightcast'],
            'metadata_language:en AND (industry_names:"Health Care" OR industry_names:"Information")'
            ' AND (job_sources:"lightcast")',
        ),
    )
    @ddt.unpack
    def test_hard_filters(self, industries, job_sources, expected):
        assert pathways_api.build_career_filters(industries=industries, job_sources=job_sources) == expected

    def test_filter_values_are_quote_escaped(self):
        built = pathways_api.build_career_filters(industries=['Say "Yes"'], job_sources=[])
        assert built == 'metadata_language:en AND (industry_names:"Say \\"Yes\\"")'

    def test_required_skills_are_unscored_and_preferred_are_scored(self):
        optional_filters = pathways_api.build_optional_skill_filters(
            skills_required=['SQL'],
            skills_preferred=['Tableau'],
        )
        assert optional_filters == [
            'skills.name:"SQL"',
            'skills.name:"Tableau"<score=1>',
        ]

    def test_optional_filters_are_capped(self):
        optional_filters = pathways_api.build_optional_skill_filters(
            skills_required=[f'required-{index}' for index in range(9)],
            skills_preferred=[f'preferred-{index}' for index in range(9)],
        )

        assert len(optional_filters) == (
            pathways_api.MAX_REQUIRED_SKILL_FILTERS + pathways_api.MAX_PREFERRED_SKILL_FILTERS
        )
        assert optional_filters[0] == 'skills.name:"required-0"'
        assert optional_filters[-1] == 'skills.name:"preferred-1"<score=1>'

    def test_malformed_compounds_are_dropped(self):
        optional_filters = pathways_api.build_optional_skill_filters(
            skills_required=['SQL & Python', 'SQL'],
            skills_preferred=['Excel + Tableau'],
        )
        assert optional_filters == ['skills.name:"SQL"']

    def test_no_skills_yields_no_optional_filters(self):
        assert pathways_api.build_optional_skill_filters(skills_required=[], skills_preferred=[]) == []


@ddt.ddt
class TestCareerCandidateMapping(TestCase):
    """Tests for mapping jobs-index hits onto career candidates."""

    def test_hit_is_mapped(self):
        assert pathways_api.career_candidate_from_hit(JOBS_HIT) == {
            'external_id': 'ETE78CD2CDFFFAC66B',
            'name': 'Data Analyst',
            'skills': ['SQL (Programming Language)', 'Data Analysis'],
            'industries': ['Health Care', 'Information'],
        }

    @ddt.data(
        {'name': 'Data Analyst'},
        {'external_id': 'ETE78CD2CDFFFAC66B'},
        {'external_id': '  ', 'name': 'Data Analyst'},
        {},
    )
    def test_unidentifiable_hits_are_dropped(self, hit):
        # Dropped rather than given a placeholder id: a fabricated identifier would
        # corrupt the harness's ground-truth comparison.
        assert pathways_api.career_candidate_from_hit(hit) is None

    def test_malformed_skill_entries_are_ignored(self):
        hit = {**JOBS_HIT, 'skills': ['SQL', None, {'name': 'SQL'}, {'name': ''}, {}]}
        assert pathways_api.career_candidate_from_hit(hit)['skills'] == ['SQL']

    def test_no_match_percentage_is_fabricated(self):
        candidate = pathways_api.career_candidate_from_hit(JOBS_HIT)
        assert 'match_percentage' not in candidate
        assert not any('match' in key for key in candidate)


class TestDeriveLearningIntent(TestCase):
    """Tests for the Xpert-backed intent extraction, with the Xpert client mocked."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.prompt = XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)

    def _mock_xpert(self, mock_client_class, content):
        """Point the patched Xpert client at a canned response body."""
        mock_client_class.return_value.send_message.return_value = XpertResponseMessage(
            role='assistant',
            content=content,
        )
        return mock_client_class.return_value.send_message

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_intent_is_normalized(self, mock_client_class):
        self._mock_xpert(mock_client_class, (
            '{"skills_required": [" SQL ", "SQL"], "skills_preferred": ["Tableau"], '
            '"condensed_algolia_query": " data analyst "}'
        ))

        intent = pathways_api.derive_learning_intent(intake=INTAKE, conversation_id='conv-1')

        assert intent == {
            'skills_required': ['SQL'],
            'skills_preferred': ['Tableau'],
            'condensed_algolia_query': 'data analyst',
        }

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_learner_intent_prompt_and_rag_tags_are_used(self, mock_client_class):
        send_message = self._mock_xpert(mock_client_class, '{"skills_required": []}')

        pathways_api.derive_learning_intent(intake=INTAKE, conversation_id='conv-1')

        _, kwargs = send_message.call_args
        assert kwargs['conversation_id'] == 'conv-1'
        assert kwargs['tags'] == ['discovery', 'edx-available-course']
        assert self.prompt.system_prompt in kwargs['system_prompt']

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_bare_string_skill_list_is_tolerated(self, mock_client_class):
        self._mock_xpert(mock_client_class, '{"skills_required": "SQL", "skills_preferred": 4}')

        intent = pathways_api.derive_learning_intent(intake=INTAKE, conversation_id='conv-1')

        assert intent['skills_required'] == ['SQL']
        assert not intent['skills_preferred']

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_non_object_response_raises(self, mock_client_class):
        self._mock_xpert(mock_client_class, '["SQL"]')

        with self.assertRaises(PromptError):
            pathways_api.derive_learning_intent(intake=INTAKE, conversation_id='conv-1')

    @mock.patch(PATCH_XPERT_CLIENT)
    def test_unparseable_response_raises(self, mock_client_class):
        self._mock_xpert(mock_client_class, 'not json')

        with self.assertRaises(XpertAPIResponseError):
            pathways_api.derive_learning_intent(intake=INTAKE, conversation_id='conv-1')

    def test_missing_prompt_raises(self):
        self.prompt.delete()

        with self.assertRaises(PromptError):
            pathways_api.derive_learning_intent(intake=INTAKE, conversation_id='conv-1')


class TestRetrieveCareers(TestCase):
    """Tests for the jobs-index search, with the Algolia client mocked."""

    def _mock_search(self, mock_client_class, response):
        mock_client_class.return_value.search_jobs_index.return_value = response
        return mock_client_class.return_value.search_jobs_index

    @mock.patch(PATCH_ALGOLIA_CLIENT)
    def test_search_params_and_result(self, mock_client_class):
        search = self._mock_search(mock_client_class, {'hits': [JOBS_HIT], 'nbHits': 1})

        result = pathways_api.retrieve_careers(
            intent={
                'condensed_algolia_query': 'data analyst',
                'skills_required': ['SQL'],
                'skills_preferred': ['Tableau'],
            },
            industries=['Health Care'],
        )

        args, kwargs = search.call_args
        assert args == ('data analyst',)
        assert kwargs['hitsPerPage'] == pathways_api.CAREER_HITS_PER_PAGE == 10
        assert kwargs['attributesToRetrieve'] == ['external_id', 'name', 'skills', 'industry_names']
        assert kwargs['filters'] == 'metadata_language:en AND (industry_names:"Health Care")'
        assert kwargs['optionalFilters'] == ['skills.name:"SQL"', 'skills.name:"Tableau"<score=1>']
        assert result['query'] == 'data analyst'
        assert result['hit_count'] == 1
        assert result['careers'] == [pathways_api.career_candidate_from_hit(JOBS_HIT)]

    @mock.patch(PATCH_ALGOLIA_CLIENT)
    def test_query_words_are_made_optional(self, mock_client_class):
        """
        The jobs index ANDs every query word with no fallback configured, and a job record
        is short: measured against the live index, *every* prefix of a normal intake
        sentence returns 0 hits, including a single common word. Since the query prefers
        Xpert's free-text `condensed_algolia_query`, one unmatched word would otherwise
        take career retrieval to zero and dead-end the pipeline. There is no safe
        query-length cap to use instead -- one word already fails.
        """
        search = self._mock_search(mock_client_class, {'hits': []})

        pathways_api.retrieve_careers(intent={'condensed_algolia_query': 'nurse practitioner'})

        _, kwargs = search.call_args
        assert kwargs['removeWordsIfNoResults'] == 'allOptional'

    @mock.patch(PATCH_ALGOLIA_CLIENT)
    def test_language_filter_is_sent_even_with_no_other_criteria(self, mock_client_class):
        search = self._mock_search(mock_client_class, {'hits': []})

        pathways_api.retrieve_careers(intent={'condensed_algolia_query': 'nurse'})

        _, kwargs = search.call_args
        assert kwargs['filters'] == 'metadata_language:en'
        assert 'optionalFilters' not in kwargs

    @mock.patch(PATCH_ALGOLIA_CLIENT)
    def test_hit_count_counts_hits_not_candidates(self, mock_client_class):
        # A full result set is not evidence retrieval worked, so both numbers are kept.
        self._mock_search(mock_client_class, {'hits': [JOBS_HIT, {'name': 'No id'}, 'garbage']})

        result = pathways_api.retrieve_careers(intent={'condensed_algolia_query': 'data analyst'})

        assert result['hit_count'] == 3
        assert len(result['careers']) == 1

    @mock.patch(PATCH_ALGOLIA_CLIENT)
    def test_search_failure_propagates(self, mock_client_class):
        mock_client_class.return_value.search_jobs_index.side_effect = AlgoliaSearchError('boom')

        with self.assertRaises(AlgoliaSearchError):
            pathways_api.retrieve_careers(intent={'condensed_algolia_query': 'data analyst'})
