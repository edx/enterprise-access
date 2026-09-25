"""
Tests for the pathway prompt defaults and the row the seed migration creates.

The contract tests here are the ones that were missing: they assert that what the parsers
accept and what the prompts *ask for* are the same thing, without a live model call. Every
other test in this app mocks the model response, which means those tests encode an
assumption about its shape -- and an assumption that is wrong fails identically in all of
them.
"""
import json

from django.test import TestCase

from enterprise_access.apps.pathways.prompts import CANDIDATE_RERANK_OUTPUT_SCHEMA, CANDIDATE_RERANK_SYSTEM_PROMPT
from enterprise_access.apps.pathways.reranking import FALLBACK_SYSTEM_PROMPT, parse_rerank_response
from enterprise_access.apps.prompts.api import build_system_prompt, compose_system_prompt
from enterprise_access.apps.prompts.models import PromptType, XpertLearnerPathwaysSystemPrompt


class TestCandidateRerankPromptDefaults(TestCase):
    """
    Tests for the module-level prompt default.
    """

    def test_the_direct_backends_use_the_canonical_text(self):
        """
        One definition, so a change to the wording reaches the claude and openai paths
        automatically. The Xpert path reads its database row instead, which is the
        deliberate asymmetry.
        """
        self.assertTrue(FALLBACK_SYSTEM_PROMPT.startswith(CANDIDATE_RERANK_SYSTEM_PROMPT.strip()))

    def test_the_direct_backends_are_sent_the_output_schema(self):
        """
        Regression test for a live defect: the fallback used to be the prompt text alone.
        The text asks for JSON but never names ``ordered_keys`` -- that field name only
        exists in the schema -- so gpt-4o returned a valid ranking under field names of
        its own choosing and the parser discarded all of it.

        The Xpert path never had the bug, because ``build_system_prompt`` appends the
        schema from the database row. The direct backends have no row, so they must
        append it from the constant.
        """
        self.assertIn('EXPECTED OUTPUT SCHEMA', FALLBACK_SYSTEM_PROMPT)
        self.assertIn('ordered_keys', FALLBACK_SYSTEM_PROMPT)
        self.assertIn('rationales', FALLBACK_SYSTEM_PROMPT)

    def test_both_prompt_paths_compose_identically(self):
        """
        The direct backends and the Xpert row must produce the same string from the same
        text and schema, or a prompt validated on one backend is not the prompt the other
        sends.
        """
        composed = compose_system_prompt(
            CANDIDATE_RERANK_SYSTEM_PROMPT, CANDIDATE_RERANK_OUTPUT_SCHEMA,
        )

        self.assertEqual(FALLBACK_SYSTEM_PROMPT, composed)

    def test_the_prompt_tells_the_model_what_not_to_optimise_for(self):
        """
        Assembly guarantees level spread, provider spread and de-duplication
        deterministically. A prompt that also asked for them would create disagreements
        somebody then has to adjudicate.
        """
        text = CANDIDATE_RERANK_SYSTEM_PROMPT.lower()
        self.assertIn('topical relevance only', text)
        for excluded in ('difficulty', 'provider', 'similar ground', 'how many courses'):
            self.assertIn(excluded, text)

    def test_the_prompt_forbids_inventing_keys(self):
        """The platform has a known key-invention defect; the prompt names it explicitly."""
        text = CANDIDATE_RERANK_SYSTEM_PROMPT.lower()
        self.assertIn('never invent', text)
        self.assertIn('only keys that appear in the input', text)

    def test_the_prompt_forbids_outcome_claims_in_rationales(self):
        """A rationale a learner reads must not promise employability or salary."""
        self.assertIn('no claims about outcomes', CANDIDATE_RERANK_SYSTEM_PROMPT.lower())

    def test_the_prompt_says_json_which_openai_json_mode_requires(self):
        """
        ``OpenAIBackend`` sends ``response_format={'type': 'json_object'}``, and OpenAI
        rejects that unless the word appears in the prompt. Easy to break by rewording.
        """
        self.assertIn('json', CANDIDATE_RERANK_SYSTEM_PROMPT.lower())

    def test_the_output_schema_is_a_json_object_the_model_can_be_given(self):
        """
        ``build_system_prompt`` appends it as formatted JSON, so it has to be a dict and
        has to serialize.
        """
        self.assertIsInstance(CANDIDATE_RERANK_OUTPUT_SCHEMA, dict)
        self.assertEqual(
            json.loads(json.dumps(CANDIDATE_RERANK_OUTPUT_SCHEMA)),
            CANDIDATE_RERANK_OUTPUT_SCHEMA,
        )


class TestCandidateRerankSchemaMatchesTheParser(TestCase):
    """
    Contract tests: a response conforming to the advertised schema must parse.

    This is the check that catches prompt-and-parser drift offline. If someone edits the
    schema to advertise a different shape, these fail rather than the pipeline silently
    dropping every ordering at runtime.
    """

    def test_the_schema_advertises_exactly_the_fields_the_parser_reads(self):
        properties = set(CANDIDATE_RERANK_OUTPUT_SCHEMA['properties'])

        self.assertEqual(properties, {'ordered_keys', 'rationales'})
        self.assertEqual(CANDIDATE_RERANK_OUTPUT_SCHEMA['required'], ['ordered_keys'])

    def test_a_schema_conforming_response_parses_fully(self):
        allowed = ['A+1', 'B+2', 'C+3']
        response = {
            'ordered_keys': ['B+2', 'A+1', 'C+3'],
            'rationales': {
                'B+2': 'Teaches the query skills this role uses daily.',
                'A+1': 'A grounding in the tools the role expects.',
                'C+3': 'Covers water treatment rather than the data work this role involves.',
            },
        }

        result = parse_rerank_response(response, allowed)

        self.assertEqual(result['ordered_keys'], ['B+2', 'A+1', 'C+3'])
        self.assertEqual(len(result['rationales']), 3)
        self.assertEqual(result['fabricated_keys'], [])

    def test_rationales_are_optional_per_the_schema(self):
        """``required`` lists only ordered_keys, so a response without them must parse."""
        result = parse_rerank_response({'ordered_keys': ['A+1']}, ['A+1'])

        self.assertEqual(result['ordered_keys'], ['A+1'])
        self.assertEqual(result['rationales'], {})

    def test_the_prompt_asks_for_every_key_which_the_parser_preserves_in_order(self):
        """
        The prompt says rank every key rather than dropping the poor fits, because
        assembly fills each rung from the front of the window -- so last place is how the
        model says "poor fit" and dropping loses that signal.
        """
        self.assertIn('Rank EVERY key', CANDIDATE_RERANK_SYSTEM_PROMPT)

        result = parse_rerank_response(
            {'ordered_keys': ['C+3', 'B+2', 'A+1']}, ['A+1', 'B+2', 'C+3'],
        )

        self.assertEqual(result['ordered_keys'], ['C+3', 'B+2', 'A+1'])


class TestSeededCandidateRerankPrompt(TestCase):
    """
    Tests for the row created by ``prompts/migrations/0003_seed_candidate_rerank_prompt``.

    Django runs migrations to build the test database, so the row exists here exactly as
    it will in a freshly set-up environment.
    """

    def test_the_row_exists_after_migration(self):
        """
        Without it, re-ranking degrades to retrieval order and logs a warning -- a silent
        no-op that reads as "the model does not help".
        """
        self.assertTrue(
            XpertLearnerPathwaysSystemPrompt.objects.filter(
                prompt_type=PromptType.CANDIDATE_RERANK,
            ).exists()
        )

    def test_the_seeded_row_is_resolvable_as_the_current_prompt(self):
        prompt = XpertLearnerPathwaysSystemPrompt.get_current(
            prompt_type=PromptType.CANDIDATE_RERANK,
        )

        self.assertIsNotNone(prompt)
        self.assertTrue(prompt.system_prompt.strip())

    def test_the_seeded_row_carries_the_output_schema(self):
        prompt = XpertLearnerPathwaysSystemPrompt.get_current(
            prompt_type=PromptType.CANDIDATE_RERANK,
        )

        self.assertIsInstance(prompt.output_schema, dict)
        self.assertEqual(set(prompt.output_schema['properties']), {'ordered_keys', 'rationales'})

    def test_the_seeded_row_builds_a_system_prompt_with_its_schema_appended(self):
        """End to end through the real prompt-assembly path the Xpert backend uses."""
        prompt = XpertLearnerPathwaysSystemPrompt.get_current(
            prompt_type=PromptType.CANDIDATE_RERANK,
        )

        built = build_system_prompt(prompt)

        self.assertIn('topical relevance', built.lower())
        self.assertIn('EXPECTED OUTPUT SCHEMA', built)
        self.assertIn('ordered_keys', built)

    def test_the_seeded_row_passes_the_models_own_validation(self):
        """
        The migration writes through the historical model, which skips ``full_clean()``.
        Saving it through the real model proves the seeded content is still valid.
        """
        prompt = XpertLearnerPathwaysSystemPrompt.get_current(
            prompt_type=PromptType.CANDIDATE_RERANK,
        )

        prompt.save()  # full_clean() runs here; a ValidationError would fail this test

    def test_the_row_notes_that_it_is_editable(self):
        """
        The prompts app exists so wording can change without a deploy. Someone finding a
        migration-seeded row should be told they may edit it.
        """
        prompt = XpertLearnerPathwaysSystemPrompt.get_current(
            prompt_type=PromptType.CANDIDATE_RERANK,
        )

        self.assertIn('Edit freely', prompt.notes)
