"""
Tests for the pathway prompt defaults and the row the seed migration creates.

The contract tests here are the ones that were missing: they assert that what the parsers
accept and what the prompts *ask for* are the same thing, without a live model call. Every
other test in this app mocks the model response, which means those tests encode an
assumption about its shape -- and an assumption that is wrong fails identically in all of
them.
"""
import hashlib
import json

from django.test import TestCase

from enterprise_access.apps.pathways import judging, pathway_variants, prompts
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


# SHA-256 of each experiment instrument's text as it stood before the v2 prompts were added
# (``git show HEAD:enterprise_access/apps/pathways/prompts.py``, plus the uncommitted shape
# sentence). A failure here means a calibrated or measured prompt changed under its own name:
# revert it and add the change as a new constant instead.
V1_TEXT_DIGESTS = {
    'CANDIDATE_RERANK_SYSTEM_PROMPT': '2aba48a126d1cff8de150f94f5b96e1d69f99cf05e432c1d49df0d7d814886c5',
    'PATHWAY_SELECTION_SYSTEM_PROMPT': '7c1395124f1a08ecc9b64d2a349a62e0c3b42f9029ba8575be8b9debccf0aca6',
    'SELECTION_EXACT_SIZE_INSTRUCTION': '8caac7f4546adecbd5eb4cfd067671a35d38c098c765021d0ae28b3ce5269edd',
    'SELECTION_MODEL_SIZED_INSTRUCTION': '33c1cbbd4eb79b4f7318fe598b7681417530915f8737b9c56c63facf8b657f99',
    'SELECTION_SHAPE_INSTRUCTION': 'c3c6b4fdbf1e39a209d7bd93d1ead820a3a88d2ba1fc289dc1c33e8dfb2c1c65',
    'PATHWAY_JUDGE_SYSTEM_PROMPT': 'a089fc64eff2177570b8639db210e686484f99edc3cbf34fdc64af8b6b840b57',
}
V1_SCHEMA_DIGESTS = {
    'PATHWAY_JUDGE_OUTPUT_SCHEMA': 'c9150c7b3dfa511f85ac7e6d05a0f84856ff9019f8a82d88f75f66820933b04c',
    'PATHWAY_SELECTION_OUTPUT_SCHEMA': 'b4525be2610e62c2371283afb3fe0256cd5ea80a6d92f975547614ef520f9798',
    'CANDIDATE_RERANK_OUTPUT_SCHEMA': 'b522024e5db3a7bc36eb31785fbdd92d67d8dd61510cac14da6c010d31229cdc',
}
# The system prompts the v1 arms and the v1 judge actually send, composed with their schemas.
V1_COMPOSED_DIGESTS = {
    'judge': '383eac77cb03477c3a1360bf71388c39f84df0bd9720d5b1bec940e042a1a0e1',
    'model_pick:3': '7ab646bf23a18009fc60dfdaca06e03e653840756961d2a18d805f469129c8d4',
    'model_sized': '46d9771fca085a45f04d542fdd6184992b4d4ca1aacae556327af6319764095d',
    'shape_pick:2/2/1': '774bcccc10f0ad7d8f4c21ae9f97311da3d514521b388f67f2322d7ca3e24d66',
}

# Names the v2 rules must not lean on: they are general rules, not a list of exceptions.
NAMED_THINGS = (
    'IBM', 'Microsoft', 'Google', 'Amazon', 'AWS', 'Salesforce', 'Excel', 'Python', 'SQL',
    'Tableau', 'Harvard', 'MIT', 'edX', 'Analyst', 'Nurse', 'Project Manager', 'Engineer',
)


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class TestV1InstrumentsAreUnchanged(TestCase):
    """
    Scenario: Adding v2 prompts leaves every v1 prompt byte-identical.

    The v1 selection prompt is what ``model_pick``, ``model_sized`` and ``shape_pick`` were
    measured with, and the v1 judge is the calibrated instrument; changing either would make
    every earlier result incomparable without saying so.
    """

    def test_the_v1_prompt_texts_are_byte_identical(self):
        for name, digest in V1_TEXT_DIGESTS.items():
            with self.subTest(name):
                self.assertEqual(_digest(getattr(prompts, name)), digest)

    def test_the_v1_output_schemas_are_unchanged(self):
        for name, digest in V1_SCHEMA_DIGESTS.items():
            with self.subTest(name):
                self.assertEqual(_digest(json.dumps(getattr(prompts, name), sort_keys=True)), digest)

    def test_the_composed_v1_system_prompts_are_unchanged(self):
        composed = {
            'judge': judging.JUDGE_SYSTEM_PROMPT,
            'model_pick:3': pathway_variants.selection_system_prompt(3),
            'model_sized': pathway_variants.selection_system_prompt(None),
            'shape_pick:2/2/1': pathway_variants.selection_system_prompt(5, (2, 2, 1)),
        }
        for name, digest in V1_COMPOSED_DIGESTS.items():
            with self.subTest(name):
                self.assertEqual(_digest(composed[name]), digest)


class TestSelectionPromptV2(TestCase):
    """
    Scenario: The second selection prompt keeps v1's frame and adds general rules.
    """

    def test_it_is_a_separate_constant(self):
        self.assertNotEqual(prompts.PATHWAY_SELECTION_SYSTEM_PROMPT_V2, prompts.PATHWAY_SELECTION_SYSTEM_PROMPT)

    def test_it_keeps_v1s_frame(self):
        v2 = prompts.PATHWAY_SELECTION_SYSTEM_PROMPT_V2
        for kept in (
            '{size_instruction}',
            'Choose for what a course TEACHES, not for whether its title resembles the job title.',
            'Pick at most 2 courses from any one provider',
            'Use ONLY keys that appear in the',
            'Never invent, correct or reformat a key.',
            'Return the chosen keys in the order they should be taken',
            'Return JSON only, with no prose before or after it.',
        ):
            with self.subTest(kept):
                self.assertIn(kept, v2)

    def test_it_states_the_five_rules_and_the_fixed_courses(self):
        v2 = ' '.join(prompts.PATHWAY_SELECTION_SYSTEM_PROMPT_V2.split())
        for rule in (
            "transferable skills over one vendor's product or one institution's own practice",
            'unless the career is defined by that tool',
            'Never choose two courses that cover the same ground',
            'leading people, specialist depth, or individual contribution',
            'later courses should build on earlier ones and stay on the same programming language',
            'one a broad foundation and the other a more focused course',
            'already_chosen are fixed and count toward the pathway',
            'a whole family of job titles',
        ):
            with self.subTest(rule):
                self.assertIn(rule.lower(), v2.lower())

    def test_it_names_no_course_provider_or_career(self):
        for name in NAMED_THINGS:
            with self.subTest(name):
                self.assertNotIn(name, prompts.PATHWAY_SELECTION_SYSTEM_PROMPT_V2)

    def test_its_size_slot_takes_the_shape_sentence(self):
        rendered = prompts.PATHWAY_SELECTION_SYSTEM_PROMPT_V2.format(size_instruction='<SIZE>')

        self.assertIn('\n<SIZE>\n', rendered)


class TestJudgePromptV2(TestCase):
    """
    Scenario: The v2 rubric is a new instrument on v1's scale, judging quality only.
    """

    def test_it_is_a_separate_constant_on_the_same_verdict_scale(self):
        self.assertNotEqual(prompts.PATHWAY_JUDGE_SYSTEM_PROMPT_V2, prompts.PATHWAY_JUDGE_SYSTEM_PROMPT)
        self.assertEqual(
            prompts.PATHWAY_JUDGE_OUTPUT_SCHEMA_V2['properties']['verdict'],
            prompts.PATHWAY_JUDGE_OUTPUT_SCHEMA['properties']['verdict'],
        )

    def test_the_schema_asks_for_exactly_the_fields_the_parser_reads(self):
        course = prompts.PATHWAY_JUDGE_OUTPUT_SCHEMA_V2['properties']['courses']['items']

        self.assertEqual(set(course['properties']), {'key', 'on_topic', *judging.V2_FLAG_ORDER})
        self.assertEqual(set(course['required']), set(course['properties']))
        for flag in prompts.PATHWAY_JUDGE_V2_BOOLEAN_FLAGS:
            self.assertEqual(course['properties'][flag], {'type': 'boolean'})
        self.assertEqual(course['properties']['redundant_with']['type'], 'string')

    def test_the_prompt_names_every_flag_and_keeps_v1s_topical_test(self):
        v2 = prompts.PATHWAY_JUDGE_SYSTEM_PROMPT_V2
        for phrase in ('on_topic', *judging.V2_FLAG_ORDER, 'Being introductory is NOT a reason',
                       'Be willing to say bad', 'Return only the requested JSON.'):
            with self.subTest(phrase):
                self.assertIn(phrase, v2)

    def test_it_explains_the_pathway_serves_a_family(self):
        self.assertIn('ONE pathway has to\nserve', prompts.PATHWAY_JUDGE_SYSTEM_PROMPT_V2)

    def test_it_judges_quality_not_business_policy(self):
        v2 = ' '.join(prompts.PATHWAY_JUDGE_SYSTEM_PROMPT_V2.split())

        self.assertIn('Judge teaching quality and fit only', v2)
        for name in NAMED_THINGS + ('AI', 'artificial intelligence', 'region'):
            with self.subTest(name):
                self.assertNotIn(name, v2)

    def test_the_composed_prompt_carries_the_v2_schema(self):
        self.assertTrue(judging.JUDGE_SYSTEM_PROMPT_V2.startswith(prompts.PATHWAY_JUDGE_SYSTEM_PROMPT_V2))
        self.assertIn('"redundant_with"', judging.JUDGE_SYSTEM_PROMPT_V2)
