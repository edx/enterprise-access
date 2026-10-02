"""
Tests for model-backed candidate re-ranking.

The validation tests carry most of the weight. A re-ranker's output is untrusted text
that names content keys, and the platform has a known key-invention defect — so "the
model returned a key we never gave it" has to be a counted metric, and a bad response has
to cost the ordering rather than the pathway.
"""
from unittest import mock

import ddt
from django.test import TestCase

from enterprise_access.apps.pathways.model_backends import (
    ModelBackendConfigurationError,
    ModelBackendRequestError,
    ModelResponse
)
from enterprise_access.apps.pathways.reranking import (
    DESCRIPTION_CHARS_FOR_MODEL,
    build_user_content,
    parse_rerank_response,
    rerank_candidates
)

CANDIDATES = [
    {'key': 'A+1', 'title': 'Intro to Welding', 'short_description': 'Basics.'},
    {'key': 'B+2', 'title': 'Applied Welding', 'short_description': 'More.'},
    {'key': 'C+3', 'title': 'Water Treatment', 'short_description': 'Unrelated.'},
]


class FakeBackend:
    """A backend returning a scripted response or raising a scripted error."""

    name = 'fake'

    def __init__(self, content='{}', error=None, metadata=None):
        self.content = content
        self.error = error
        self.metadata = metadata or {}
        self.calls = []

    def complete(self, **kwargs):
        """Stand in for ``ModelBackend.complete``."""
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ModelResponse(
            content=self.content, backend=self.name, model='fake-1',
            input_tokens=10, output_tokens=5, elapsed_ms=12, metadata=self.metadata,
        )


class TestBuildUserContent(TestCase):
    """
    Tests for ``build_user_content``.
    """

    def test_the_career_and_candidate_keys_are_included(self):
        content = build_user_content(career_name='Welder', candidates=CANDIDATES)

        self.assertIn('Welder', content)
        for candidate in CANDIDATES:
            self.assertIn(candidate['key'], content)

    def test_level_and_partner_are_withheld_from_the_model(self):
        """
        Those are what assembly uses. Offering them invites the model to optimise for
        constraints it is not being asked about, which assembly then has to undo.
        """
        content = build_user_content(
            career_name='Welder',
            candidates=[{**CANDIDATES[0], 'level_type': 'Introductory', 'partner': 'edX'}],
        )

        self.assertNotIn('Introductory', content)
        self.assertNotIn('edX', content)

    def test_descriptions_are_truncated(self):
        content = build_user_content(
            career_name='Welder',
            candidates=[{'key': 'A+1', 'title': 't', 'short_description': 'x' * 5000}],
        )

        self.assertNotIn('x' * (DESCRIPTION_CHARS_FOR_MODEL + 1), content)

    def test_the_full_description_is_used_when_the_short_one_is_missing(self):
        content = build_user_content(
            career_name='Welder',
            candidates=[{'key': 'A+1', 'title': 't', 'full_description': 'the long one'}],
        )

        self.assertIn('the long one', content)


@ddt.ddt
class TestParseRerankResponse(TestCase):
    """
    Tests for ``parse_rerank_response``.
    """

    allowed = ['A+1', 'B+2', 'C+3']

    def test_a_valid_ordering_is_accepted(self):
        result = parse_rerank_response(
            {'ordered_keys': ['B+2', 'A+1'], 'rationales': {'B+2': 'because'}}, self.allowed,
        )

        self.assertEqual(result['ordered_keys'], ['B+2', 'A+1'])
        self.assertEqual(result['rationales'], {'B+2': 'because'})
        self.assertEqual(result['fabricated_keys'], [])

    def test_fabricated_keys_are_dropped_and_counted(self):
        """Scenario: Fabricated keys are rejected."""
        result = parse_rerank_response(
            {'ordered_keys': ['A+1', 'Invented+999']}, self.allowed,
        )

        self.assertEqual(result['ordered_keys'], ['A+1'])
        self.assertEqual(result['fabricated_keys'], ['Invented+999'])

    def test_repeated_keys_collapse_without_counting_as_fabrication(self):
        result = parse_rerank_response({'ordered_keys': ['A+1', 'A+1']}, self.allowed)

        self.assertEqual(result['ordered_keys'], ['A+1'])
        self.assertEqual(result['fabricated_keys'], [])

    def test_rationales_for_unknown_keys_are_discarded(self):
        result = parse_rerank_response(
            {'ordered_keys': ['A+1'], 'rationales': {'Invented+999': 'nope'}}, self.allowed,
        )

        self.assertEqual(result['rationales'], {})

    def test_non_string_rationale_values_are_discarded(self):
        result = parse_rerank_response(
            {'ordered_keys': ['A+1'], 'rationales': {'A+1': {'nested': 1}}}, self.allowed,
        )

        self.assertEqual(result['rationales'], {})

    @ddt.data(
        None, [], 'a string', 42,
        {'no_ordered_keys': 1},
        {'ordered_keys': 'not a list'},
    )
    def test_a_malformed_payload_yields_an_empty_ordering_rather_than_raising(self, payload):
        """A bad response should cost the ordering, not the pathway."""
        result = parse_rerank_response(payload, self.allowed)

        self.assertEqual(result['ordered_keys'], [])
        self.assertEqual(result['fabricated_keys'], [])

    def test_non_string_and_empty_keys_are_skipped(self):
        result = parse_rerank_response(
            {'ordered_keys': ['A+1', '', None, 7, 'B+2']}, self.allowed,
        )

        self.assertEqual(result['ordered_keys'], ['A+1', 'B+2'])


class TestRerankCandidates(TestCase):
    """
    Tests for ``rerank_candidates``.
    """

    def test_an_ordering_and_a_trace_are_returned(self):
        backend = FakeBackend(content='{"ordered_keys": ["B+2", "A+1"]}')

        result = rerank_candidates(
            career_name='Welder', candidates=CANDIDATES, trace_id='t', backend=backend,
        )

        self.assertEqual(result['ordered_keys'], ['B+2', 'A+1'])
        self.assertEqual(result['trace']['backend'], 'fake')
        self.assertEqual(result['trace']['input_tokens'], 10)
        self.assertEqual(result['trace']['elapsed_ms'], 12)

    def test_the_prompt_revision_is_stamped(self):
        """Scenario: The prompt revision is stamped."""
        backend = FakeBackend(content='{"ordered_keys": []}', metadata={'prompt_revision': '42'})

        result = rerank_candidates(
            career_name='Welder', candidates=CANDIDATES, trace_id='t', backend=backend,
        )

        self.assertEqual(result['prompt_revision'], '42')

    def test_the_trace_id_reaches_the_backend(self):
        backend = FakeBackend(content='{"ordered_keys": []}')

        rerank_candidates(
            career_name='Welder', candidates=CANDIDATES, trace_id='trace-9', backend=backend,
        )

        self.assertEqual(backend.calls[0]['trace_id'], 'trace-9')

    def test_a_backend_failure_degrades_to_no_ordering(self):
        backend = FakeBackend(error=ModelBackendRequestError('boom'))

        result = rerank_candidates(
            career_name='Welder', candidates=CANDIDATES, trace_id='t', backend=backend,
        )

        self.assertEqual(result['ordered_keys'], [])
        self.assertEqual(result['trace'], {})

    def test_a_configuration_failure_also_degrades_rather_than_raising(self):
        """
        A misconfigured backend must not take the pathway down: Chunk 9a produces a valid
        pathway from the unordered set.
        """
        backend = FakeBackend(error=ModelBackendConfigurationError('no key'))

        result = rerank_candidates(
            career_name='Welder', candidates=CANDIDATES, trace_id='t', backend=backend,
        )

        self.assertEqual(result['ordered_keys'], [])

    def test_a_non_json_response_degrades_but_keeps_the_cost_trace(self):
        """The call was still paid for, so its cost has to stay visible."""
        backend = FakeBackend(content='I cannot do that')

        result = rerank_candidates(
            career_name='Welder', candidates=CANDIDATES, trace_id='t', backend=backend,
        )

        self.assertEqual(result['ordered_keys'], [])
        self.assertEqual(result['trace']['input_tokens'], 10)

    @mock.patch('enterprise_access.apps.pathways.reranking.get_model_backend')
    def test_the_configured_backend_is_used_when_none_is_injected(self, mock_get_backend):
        mock_get_backend.return_value = FakeBackend(content='{"ordered_keys": []}')

        rerank_candidates(career_name='Welder', candidates=CANDIDATES, trace_id='t')

        self.assertEqual(
            mock_get_backend.call_args.kwargs['prompt_type'], 'candidate_rerank',
        )
