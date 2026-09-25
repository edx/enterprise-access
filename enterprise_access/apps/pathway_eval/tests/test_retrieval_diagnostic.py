"""
Tests for the retrieval diagnostic.
"""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import ddt
import yaml
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchError
from enterprise_access.apps.pathway_eval.personas import persona_from_dict
from enterprise_access.apps.pathway_eval.retrieval_diagnostic import (
    MAX_QUERY_CHARS,
    Outcome,
    RetrievalDiagnostic,
    build_query_strategies,
    summarize
)
from enterprise_access.apps.pathway_eval.tests.test_personas import VALID_INPUTS, make_persona_dict

EXPECTED_KEY = 'IBM+DA0101EN'
EXPECTED_TITLE = 'Analyzing Data with Python'
OTHER_KEY = 'HarvardX+CS109x'


def make_persona(**overrides):
    return persona_from_dict(make_persona_dict(**overrides))


def hits_for(*keys):
    """An Algolia response body containing the given keys, in order."""
    return {'hits': [{'key': key, 'title': f'Title for {key}'} for key in keys]}


class FakeAlgoliaClient:
    """
    Records every catalog search and replays scripted responses.

    Scripted by *query substring* rather than call order, because the diagnostic issues
    several strategies and asserting on order would make these tests fragile.
    """

    def __init__(self, responses_by_query_substring=None, default=None, error=None):
        self.responses = responses_by_query_substring or {}
        self.default = default if default is not None else {'hits': []}
        self.error = error
        self.calls = []

    def search_catalog_index(self, query, **kwargs):
        """Stand in for ``AlgoliaSearchClient.search_catalog_index``."""
        self.calls.append({'query': query, **kwargs})
        if self.error:
            raise self.error
        for substring, response in self.responses.items():
            if substring.lower() in query.lower():
                return response
        return self.default


@ddt.ddt
class TestBuildQueryStrategies(TestCase):
    """
    Tests for deterministic query construction.
    """

    def test_strategies_are_built_from_persona_inputs(self):
        strategies = build_query_strategies(make_persona())

        self.assertEqual(
            set(strategies),
            {'goals_only', 'goals_and_free_text', 'career_title'},
        )
        self.assertEqual(strategies['goals_only'], VALID_INPUTS['selected_goals'])
        self.assertIn(VALID_INPUTS['selected_goals'], strategies['goals_and_free_text'])
        self.assertIn(VALID_INPUTS['free_text'], strategies['goals_and_free_text'])
        self.assertEqual(strategies['career_title'], 'Data Analyst Consultant')

    def test_career_title_strategy_is_absent_without_a_named_career(self):
        """A strategy with no text must be skipped, not issued as an empty query."""
        strategies = build_query_strategies(make_persona(expected={'careers': []}))

        self.assertNotIn('career_title', strategies)

    def test_long_free_text_is_truncated_at_a_word_boundary(self):
        inputs = dict(VALID_INPUTS, free_text='word ' * 200)

        query = build_query_strategies(make_persona(inputs=inputs))['goals_and_free_text']

        self.assertLessEqual(len(query), MAX_QUERY_CHARS)
        self.assertFalse(query.endswith('wor'))

    def test_whitespace_is_collapsed(self):
        inputs = dict(VALID_INPUTS, selected_goals='Move   into\n\na  data role')

        query = build_query_strategies(make_persona(inputs=inputs))['goals_only']

        self.assertEqual(query, 'Move into a data role')


@ddt.ddt
class TestRetrievalDiagnostic(TestCase):
    """
    Tests for ``RetrievalDiagnostic``.
    """

    def test_recall_and_rank_are_reported_per_persona(self):
        """Scenario: Recall is reported per persona."""
        client = FakeAlgoliaClient(default=hits_for(OTHER_KEY, 'X+1', EXPECTED_KEY))

        result = RetrievalDiagnostic(algolia_client=client).run_for_persona(make_persona())

        self.assertEqual(result.expected_course_keys, [EXPECTED_KEY])
        self.assertEqual(result.retrieved_keys, {EXPECTED_KEY})
        # Third in the hit list, so rank 3 -- 1-based, as a human reads a results page.
        self.assertEqual(result.best_rank, 3)
        self.assertEqual(result.recall_at_top_n, 1.0)
        self.assertEqual(result.outcome, Outcome.IN_TOP_N)

    def test_partial_recall_is_reported_as_a_fraction(self):
        persona = make_persona(expected={'courses': [
            {'key': EXPECTED_KEY, 'title': EXPECTED_TITLE},
            {'key': 'IBM+RP0321EN', 'title': 'R Data Science Capstone Project'},
        ]})
        client = FakeAlgoliaClient(default=hits_for(EXPECTED_KEY))

        result = RetrievalDiagnostic(algolia_client=client).run_for_persona(persona)

        self.assertEqual(result.recall_at_top_n, 0.5)
        # One expected course was retrieved, so retrieval demonstrably works here.
        self.assertEqual(result.outcome, Outcome.IN_TOP_N)

    def test_top_n_bounds_what_counts_as_retrieved(self):
        client = FakeAlgoliaClient(default=hits_for(EXPECTED_KEY))

        RetrievalDiagnostic(algolia_client=client, top_n=5).run_for_persona(make_persona())

        self.assertTrue(client.calls)
        for call in client.calls:
            # Probes deliberately use a wider window; strategy searches must honour top_n.
            self.assertIn(call['hitsPerPage'], (5, 50))

    # -- the three outcomes -----------------------------------------------------------

    def test_outcome_not_in_top_n_when_the_course_exists_but_is_not_retrieved(self):
        """Scenario: The three outcomes are distinguished (present, not retrieved)."""
        client = FakeAlgoliaClient(
            # The persona's own queries miss it...
            default=hits_for(OTHER_KEY),
            # ...but a targeted title probe finds it, so it is in the index.
            responses_by_query_substring={EXPECTED_TITLE: hits_for(EXPECTED_KEY)},
        )

        result = RetrievalDiagnostic(algolia_client=client).run_for_persona(make_persona())

        self.assertEqual(result.outcome, Outcome.NOT_IN_TOP_N)
        self.assertEqual(result.probe_found, {EXPECTED_KEY: True})
        self.assertEqual(result.recall_at_top_n, 0.0)
        self.assertIn('upstream', Outcome.CONSEQUENCES[result.outcome])

    def test_outcome_not_found_in_index_when_even_the_probe_misses(self):
        """Scenario: The three outcomes are distinguished (absent entirely)."""
        client = FakeAlgoliaClient(default=hits_for(OTHER_KEY))

        result = RetrievalDiagnostic(algolia_client=client).run_for_persona(make_persona())

        self.assertEqual(result.outcome, Outcome.NOT_FOUND_IN_INDEX)
        self.assertEqual(result.probe_found, {EXPECTED_KEY: False})
        self.assertIn('coverage', Outcome.CONSEQUENCES[result.outcome])

    def test_a_course_with_no_title_cannot_be_probed(self):
        """The probe searches titles, so a key-only expectation is unprobeable."""
        persona = make_persona(expected={'courses': [{'key': EXPECTED_KEY}]})
        client = FakeAlgoliaClient(default=hits_for(OTHER_KEY))

        result = RetrievalDiagnostic(algolia_client=client).run_for_persona(persona)

        self.assertEqual(result.probe_found, {EXPECTED_KEY: False})

    def test_expect_no_coverage_persona_records_incidental_hits(self):
        persona = make_persona(expected={'expect_no_coverage': True, 'courses': []})
        client = FakeAlgoliaClient(default=hits_for('IRRELEVANT+1', 'IRRELEVANT+2'))

        result = RetrievalDiagnostic(algolia_client=client).run_for_persona(persona)

        self.assertEqual(result.outcome, Outcome.EXPECTED_NO_COVERAGE)
        # The padding a learner would have been shown, so a human can confirm it is padding.
        self.assertEqual(
            {hit['key'] for hit in result.incidental_hits},
            {'IRRELEVANT+1', 'IRRELEVANT+2'},
        )
        self.assertIsNone(result.recall_at_top_n)

    def test_persona_without_ground_truth_is_not_a_failure(self):
        persona = make_persona(expected={'courses': [], 'expect_no_coverage': False})
        client = FakeAlgoliaClient(default=hits_for(OTHER_KEY))

        result = RetrievalDiagnostic(algolia_client=client).run_for_persona(persona)

        self.assertEqual(result.outcome, Outcome.NO_GROUND_TRUTH)
        self.assertIsNone(result.recall_at_top_n)

    @ddt.data(
        Outcome.NO_GROUND_TRUTH,
        Outcome.EXPECTED_NO_COVERAGE,
        Outcome.IN_TOP_N,
        Outcome.NOT_IN_TOP_N,
        Outcome.NOT_FOUND_IN_INDEX,
    )
    def test_every_outcome_states_its_consequence(self, outcome):
        """The gate is a decision, so each outcome must name the decision it implies."""
        self.assertIn(outcome, Outcome.CONSEQUENCES)
        self.assertTrue(Outcome.CONSEQUENCES[outcome].strip())

    # -- credential handling ----------------------------------------------------------

    def test_unscoped_flag_is_passed_through_to_the_client(self):
        client = FakeAlgoliaClient()

        RetrievalDiagnostic(algolia_client=client, allow_unscoped=True).run_for_persona(make_persona())

        self.assertTrue(client.calls)
        for call in client.calls:
            self.assertTrue(call['allow_unscoped'])
            self.assertIsNone(call['secured_key'])

    def test_searches_are_scoped_to_course_content(self):
        client = FakeAlgoliaClient()

        RetrievalDiagnostic(algolia_client=client).run_for_persona(make_persona())

        for call in client.calls:
            self.assertEqual(call['filters'], 'content_type:course')

    # -- enterprise customer scoping --------------------------------------------------

    def test_customer_uuid_is_added_to_the_filters(self):
        client = FakeAlgoliaClient()
        customer_uuid = '91dc5e6c-7166-4c24-9514-cd871bc46deb'

        RetrievalDiagnostic(
            algolia_client=client, customer_uuid=customer_uuid,
        ).run_for_persona(make_persona())

        self.assertTrue(client.calls)
        for call in client.calls:
            self.assertEqual(
                call['filters'],
                f'content_type:course AND enterprise_customer_uuids:"{customer_uuid}"',
            )

    @ddt.data('not-a-uuid', '', '852eac48-b5a9-4849', 12345)
    def test_a_malformed_customer_uuid_is_rejected_rather_than_filtered_on(self, bad_uuid):
        """
        Algolia does not error on a filter that matches nothing, so a typo would report
        0% recall for every persona -- a wrong answer that looks exactly like a real one.
        """
        with self.assertRaisesRegex(ValueError, 'not a UUID'):
            RetrievalDiagnostic(algolia_client=FakeAlgoliaClient(), customer_uuid=bad_uuid)

    def test_no_customer_uuid_leaves_the_filters_unscoped(self):
        client = FakeAlgoliaClient()

        diagnostic = RetrievalDiagnostic(algolia_client=client)

        self.assertIsNone(diagnostic.customer_uuid)
        self.assertEqual(diagnostic.catalog_filters, 'content_type:course')

    def test_count_scoped_courses_reads_nbhits_under_the_scope(self):
        client = FakeAlgoliaClient(default={'hits': [], 'nbHits': 4057})

        count = RetrievalDiagnostic(
            algolia_client=client,
            customer_uuid='91dc5e6c-7166-4c24-9514-cd871bc46deb',
        ).count_scoped_courses()

        self.assertEqual(count, 4057)
        self.assertEqual(client.calls[0]['hitsPerPage'], 0)

    # -- failures ---------------------------------------------------------------------

    def test_a_failing_strategy_is_recorded_not_raised(self):
        """One flaky search must not abandon the whole persona set."""
        client = FakeAlgoliaClient(error=AlgoliaSearchError('boom'))

        result = RetrievalDiagnostic(algolia_client=client).run_for_persona(make_persona())

        self.assertTrue(result.errors)
        for strategy in result.strategy_results:
            self.assertIn('boom', strategy.error)
        # No expected course was retrieved and the probe also failed, so the honest
        # classification is "not found" -- with the errors attached alongside.
        self.assertEqual(result.outcome, Outcome.NOT_FOUND_IN_INDEX)


class TestSummarize(TestCase):
    """
    Tests for report aggregation.
    """

    def _run(self, personas, client):
        return RetrievalDiagnostic(algolia_client=client).run(personas)

    def test_results_are_split_by_technology(self):
        """Scenario: Results split by domain."""
        tech = make_persona(id='p001-tech', domain='technology')
        non_tech = make_persona(id='p002-nurse', domain='healthcare')
        client = FakeAlgoliaClient(default=hits_for(EXPECTED_KEY))

        summary = summarize(self._run([tech, non_tech], client))

        self.assertEqual(summary['technology']['personas'], 1)
        self.assertEqual(summary['non_technology']['personas'], 1)
        self.assertEqual(summary['total_personas'], 2)

    def test_placeholder_personas_are_reported_separately(self):
        """
        Scoring a search-picked guess as though it were expert judgement is worse than
        reporting nothing, so the split has to be mechanical.
        """
        expert = make_persona(id='p001-expert', expected={'ground_truth_status': 'expert_authored'})
        placeholder = make_persona(id='p002-placeholder', expected={'ground_truth_status': 'placeholder'})
        client = FakeAlgoliaClient(default=hits_for(EXPECTED_KEY))

        summary = summarize(self._run([expert, placeholder], client))

        self.assertEqual(summary['expert_authored_personas'], 1)
        self.assertEqual(summary['placeholder_personas'], 1)
        self.assertEqual(summary['expert_authored_only']['personas'], 1)

    def test_mean_recall_is_none_when_nothing_is_scoreable(self):
        persona = make_persona(expected={'courses': [], 'expect_no_coverage': False})
        client = FakeAlgoliaClient()

        summary = summarize(self._run([persona], client))

        self.assertIsNone(summary['overall']['mean_recall_at_top_n'])
        self.assertEqual(summary['overall']['scoreable'], 0)

    def test_outcome_counts_are_reported_per_split(self):
        client = FakeAlgoliaClient(default=hits_for(EXPECTED_KEY))
        personas = [
            make_persona(id='p001-a', domain='technology'),
            make_persona(id='p002-b', domain='technology'),
        ]

        summary = summarize(self._run(personas, client))

        self.assertEqual(summary['technology']['outcomes'][Outcome.IN_TOP_N], 2)


class TestRunRetrievalDiagnosticCommand(TestCase):
    """
    Tests for the ``run_retrieval_diagnostic`` management command.

    These write their own persona fixtures to a temporary directory rather than running
    against the shipped set. The shipped personas are real, product-authored ground truth
    that changes as it is edited; coupling command tests to a particular persona id makes
    those edits look like command regressions.
    """

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.persona_dir = Path(self._tmpdir.name)
        (self.persona_dir / 'p001-test.yaml').write_text(
            yaml.safe_dump(make_persona_dict(id='p001-test'), sort_keys=False)
        )

    def call(self, **kwargs):
        stdout = StringIO()
        kwargs.setdefault('persona_dir', str(self.persona_dir))
        call_command('run_retrieval_diagnostic', stdout=stdout, **kwargs)
        return stdout.getvalue()

    @mock.patch('enterprise_access.apps.pathway_eval.management.commands.run_retrieval_diagnostic.AlgoliaSearchClient')
    def test_command_reports_the_persona_set(self, mock_client_class):
        mock_client_class.return_value = FakeAlgoliaClient(default=hits_for(EXPECTED_KEY))

        output = self.call()

        self.assertIn('RETRIEVAL DIAGNOSTIC', output)
        self.assertIn('SUMMARY', output)
        self.assertIn('GATE:', output)
        self.assertIn('p001-test', output)

    @mock.patch('enterprise_access.apps.pathway_eval.management.commands.run_retrieval_diagnostic.AlgoliaSearchClient')
    def test_unscoped_run_is_flagged_in_the_output(self, mock_client_class):
        mock_client_class.return_value = FakeAlgoliaClient()

        output = self.call(unscoped=True)

        self.assertIn('UNSCOPED', output)

    @mock.patch('enterprise_access.apps.pathway_eval.management.commands.run_retrieval_diagnostic.AlgoliaSearchClient')
    def test_json_output_is_written(self, mock_client_class):
        mock_client_class.return_value = FakeAlgoliaClient(default=hits_for(EXPECTED_KEY))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'nested' / 'out.json'
            self.call(persona_ids=['p001-test'], output_json=str(path))

            payload = json.loads(path.read_text())

        self.assertEqual(len(payload['personas']), 1)
        self.assertEqual(payload['personas'][0]['persona_id'], 'p001-test')
        self.assertIn('summary', payload)
        self.assertIn('strategies', payload['personas'][0])

    @mock.patch('enterprise_access.apps.pathway_eval.management.commands.run_retrieval_diagnostic.AlgoliaSearchClient')
    def test_customer_scoped_run_reports_the_scope_and_its_size(self, mock_client_class):
        mock_client_class.return_value = FakeAlgoliaClient(
            default={**hits_for(EXPECTED_KEY), 'nbHits': 4057},
        )
        customer_uuid = '91dc5e6c-7166-4c24-9514-cd871bc46deb'

        output = self.call(customer_uuid=customer_uuid)

        self.assertIn(customer_uuid, output)
        self.assertIn('4057 courses in scope', output)
        # Scoped by filter, so the blanket "upper bound" caveat no longer applies...
        self.assertNotIn('Results are not restricted', output)
        # ...but the weaker guarantee a filter gives is stated instead.
        self.assertIn('not a substitute for a secured key', output)

    @mock.patch('enterprise_access.apps.pathway_eval.management.commands.run_retrieval_diagnostic.AlgoliaSearchClient')
    def test_an_empty_customer_scope_is_an_error_not_a_run_of_zeroes(self, mock_client_class):
        """
        A well-formed UUID that is not a customer's -- a catalog UUID, say -- matches no
        courses. Reporting that as 0% recall would be a false negative on the whole
        pipeline, so the command refuses to run.
        """
        mock_client_class.return_value = FakeAlgoliaClient(default={'hits': [], 'nbHits': 0})

        with self.assertRaisesRegex(CommandError, 'no courses in the catalog index'):
            self.call(customer_uuid='91dc5e6c-7166-4c24-9514-cd871bc46deb')

    def test_a_malformed_customer_uuid_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, 'not a UUID'):
            self.call(customer_uuid='2u')

    def test_unknown_persona_id_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, 'p999-nope'):
            self.call(persona_ids=['p999-nope'])

    def test_missing_persona_dir_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, 'does not exist'):
            self.call(persona_dir='/nonexistent/persona/dir')
