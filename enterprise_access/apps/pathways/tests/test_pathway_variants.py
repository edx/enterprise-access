"""
Tests for pathway size variants.

The properties that matter: every arm enforces the delivered pathway's rules whatever a
model returns; nothing is ever padded to reach a size, so a short variant stays short; the
two model arms differ in their size rule and nothing else; and a failed arm is recorded,
not raised.
"""
import json

import ddt
from django.test import TestCase, override_settings

from enterprise_access.apps.pathways.model_backends import (
    ModelBackendConfigurationError,
    ModelBackendRequestError,
    OpenAIBackend
)
from enterprise_access.apps.pathways.pathway_assembly import eligible_candidates
from enterprise_access.apps.pathways.pathway_variants import (
    DEFAULT_VARIANT_SIZES,
    STRATEGY_MODEL_PICK,
    STRATEGY_MODEL_SIZED,
    STRATEGY_RANKED_CUT,
    Variant,
    apply_selection,
    build_variants,
    estimated_model_calls,
    get_variant_backend,
    model_select,
    normalise_sizes,
    normalise_strategies,
    ranked_cut,
    resolve_variant_request,
    selection_system_prompt,
    variant_count,
    variant_label
)
from enterprise_access.apps.pathways.prompts import SELECTION_EXACT_SIZE_INSTRUCTION, SELECTION_MODEL_SIZED_INSTRUCTION
from enterprise_access.apps.pathways.tests.test_reranking import FakeBackend


def candidate(key, *, level='Introductory', partner='P1', language='English', title=None):
    """A ``CourseCandidate`` dict, as the variant step passes it."""
    return {
        'key': key, 'title': title or f'Course {key}', 'short_description': f'About {key}.',
        'full_description': '', 'level_type': level, 'partner': partner, 'language': language,
        'skill_names': [],
    }


# Relevance order. P1 supplies three, so the provider cap bites at the third.
WINDOW = [
    candidate('A+1', level='Intermediate', partner='P1'),
    candidate('A+2', partner='P1'),
    candidate('A+3', level='Advanced', partner='P1'),
    candidate('B+1', partner='P2'),
    candidate('C+1', level='Advanced', partner='P3'),
    candidate('D+1', level='Intermediate', partner='P4'),
]


def eligible(window=None):
    """The window as ``pathway_assembly.Candidate`` objects, as the variant step builds it."""
    hits = [{
        'key': c['key'], 'title': c['title'], 'level_type': c['level_type'],
        'partners': [{'name': c['partner']}] if c['partner'] else [], 'language': c['language'],
    } for c in (window or WINDOW)]
    return eligible_candidates(hits)[0]


def keys_of(variant):
    return [course.key for course in variant.courses]


@ddt.ddt
class TestRequestNormalisation(TestCase):
    """
    Scenario: A variant request is read the same way by every caller.
    """

    def test_sizes_are_sorted_and_deduplicated(self):
        self.assertEqual(normalise_sizes([5, 2, 5, 3]), [2, 3, 5])

    @ddt.data(1, 6, 0)
    def test_a_size_outside_the_relaxed_definition_is_rejected(self, size):
        with self.assertRaisesRegex(ValueError, 'between 2 and 5'):
            normalise_sizes([3, size])

    def test_strategies_come_back_in_canonical_order(self):
        self.assertEqual(
            normalise_strategies([STRATEGY_MODEL_SIZED, STRATEGY_RANKED_CUT, STRATEGY_RANKED_CUT]),
            [STRATEGY_RANKED_CUT, STRATEGY_MODEL_SIZED],
        )

    def test_an_unknown_strategy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            normalise_strategies(['best_of_n'])

    def test_sizes_alone_run_the_free_arm(self):
        self.assertEqual(resolve_variant_request([3], []), ([3], [STRATEGY_RANKED_CUT]))

    def test_strategies_alone_run_every_size(self):
        self.assertEqual(
            resolve_variant_request([], [STRATEGY_MODEL_PICK]),
            (list(DEFAULT_VARIANT_SIZES), [STRATEGY_MODEL_PICK]),
        )

    def test_neither_requests_nothing(self):
        self.assertEqual(resolve_variant_request(None, None), ([], []))

    def test_labels_name_the_strategy_and_size(self):
        self.assertEqual(variant_label(STRATEGY_RANKED_CUT, 3), 'ranked_cut:3')
        self.assertEqual(variant_label(STRATEGY_MODEL_SIZED, None), 'model_sized:2-5')


class TestCostBound(TestCase):
    """
    Scenario: The paid calls a request can add are bounded before it runs.
    """

    def test_the_ranked_arm_is_free(self):
        self.assertEqual(
            estimated_model_calls(sizes=[2, 3, 4, 5], strategies=[STRATEGY_RANKED_CUT], judge_enabled=False),
            0,
        )

    def test_model_pick_costs_one_call_per_size_and_model_sized_one(self):
        self.assertEqual(
            estimated_model_calls(
                sizes=[2, 3, 4, 5], strategies=[STRATEGY_MODEL_PICK, STRATEGY_MODEL_SIZED],
                judge_enabled=False,
            ),
            5,
        )

    def test_the_judge_costs_one_call_per_pathway_including_the_delivered_one(self):
        strategies = [STRATEGY_RANKED_CUT, STRATEGY_MODEL_PICK, STRATEGY_MODEL_SIZED]
        self.assertEqual(variant_count([2, 3], strategies), 5)
        self.assertEqual(
            estimated_model_calls(sizes=[2, 3], strategies=strategies, judge_enabled=True),
            3 + 1 + 5,
        )


class TestRankedCut(TestCase):
    """
    Scenario: The ranked arm takes the most relevant eligible courses, in taught order.
    """

    def test_it_takes_the_top_of_the_relevance_order(self):
        variant = ranked_cut(eligible(), 2)

        self.assertEqual(sorted(keys_of(variant)), ['A+1', 'A+2'])
        self.assertTrue(variant.is_complete)

    def test_the_provider_cap_skips_a_third_course_and_records_it(self):
        variant = ranked_cut(eligible(), 4)

        self.assertNotIn('A+3', keys_of(variant))
        self.assertEqual(sorted(keys_of(variant)), ['A+1', 'A+2', 'B+1', 'C+1'])
        self.assertEqual(variant.dropped, {'provider_cap': 1})

    def test_courses_are_delivered_easiest_first(self):
        variant = ranked_cut(eligible(), 4)

        self.assertEqual(
            [course.level_type for course in variant.courses],
            ['Introductory', 'Introductory', 'Intermediate', 'Advanced'],
        )

    def test_sizes_nest(self):
        smaller, larger = ranked_cut(eligible(), 3), ranked_cut(eligible(), 4)

        self.assertLessEqual(set(keys_of(smaller)), set(keys_of(larger)))

    def test_a_short_window_yields_a_short_variant_never_a_padded_one(self):
        variant = ranked_cut(eligible(WINDOW[:2]), 5)

        self.assertEqual(len(variant.courses), 2)
        self.assertFalse(variant.is_complete)
        self.assertIn('exactly 5 courses, got 2', variant.violations()[0])

    def test_any_level_mix_is_allowed_and_recorded(self):
        variant = ranked_cut(eligible([candidate('A+1'), candidate('B+1', partner='P2')]), 2)

        self.assertEqual(variant.violations(), [])
        self.assertEqual(variant.realised_level_mix,
                         {'Introductory': 2, 'Intermediate': 0, 'Advanced': 0})


class TestApplySelection(TestCase):
    """
    Scenario: A model's choice is held to the rules it was told.
    """

    def test_valid_keys_become_courses_in_taught_order(self):
        courses, dropped, fabricated = apply_selection(eligible(), ['C+1', 'B+1'], max_size=5)

        self.assertEqual([c.key for c in courses], ['B+1', 'C+1'])
        self.assertEqual((dropped, fabricated), ({}, []))

    def test_every_refusal_is_counted_by_reason(self):
        courses, dropped, fabricated = apply_selection(
            eligible(),
            ['A+1', 'A+1', 'Z+9', 'A+2', 'A+3', 'B+1', 'C+1'],
            max_size=3,
        )

        self.assertEqual(sorted(c.key for c in courses), ['A+1', 'A+2', 'B+1'])
        self.assertEqual(dropped, {'duplicate': 1, 'provider_cap': 1, 'over_size': 1})
        self.assertEqual(fabricated, ['Z+9'])

    def test_non_string_keys_are_ignored(self):
        courses, _, fabricated = apply_selection(eligible(), [None, 7, '', 'B+1'], max_size=5)

        self.assertEqual([c.key for c in courses], ['B+1'])
        self.assertEqual(fabricated, [])


class TestVariantCompleteness(TestCase):
    """
    Scenario: A variant is complete only at the length it was built for.
    """

    def test_a_model_sized_variant_is_complete_anywhere_from_two_to_five(self):
        courses = eligible()
        self.assertFalse(Variant(STRATEGY_MODEL_SIZED, None, courses=courses[:1]).is_complete)
        self.assertTrue(Variant(STRATEGY_MODEL_SIZED, None, courses=courses[:2]).is_complete)
        self.assertTrue(Variant(STRATEGY_MODEL_SIZED, None, courses=[courses[i] for i in (0, 1, 3, 4, 5)]).is_complete)

    def test_a_model_sized_variant_is_gated_on_the_range(self):
        variant = Variant(STRATEGY_MODEL_SIZED, None, courses=eligible()[:1])

        self.assertIn('2 to 5 courses, got 1', variant.violations()[0])


class TestSelectionPrompt(TestCase):
    """
    Scenario: The two model arms differ in their size rule and nothing else.
    """

    def test_the_exact_size_prompt_asks_for_exactly_n(self):
        self.assertIn('Pick exactly 3 courses', selection_system_prompt(3))

    def test_the_model_sized_prompt_asks_for_two_to_five_without_padding(self):
        prompt = selection_system_prompt(None)

        self.assertIn('between 2 and 5 courses', prompt)
        self.assertIn('do not add courses just to reach 5', prompt)

    def test_only_the_size_instruction_differs(self):
        exact = selection_system_prompt(4).replace(SELECTION_EXACT_SIZE_INSTRUCTION.format(size=4), '<SIZE>')
        sized = selection_system_prompt(None).replace(
            SELECTION_MODEL_SIZED_INSTRUCTION.format(min_size=2, max_size=5), '<SIZE>',
        )

        self.assertEqual(exact, sized)

    def test_the_output_schema_is_appended(self):
        self.assertIn('"keys"', selection_system_prompt(3))


class TestModelSelect(TestCase):
    """
    Scenario: A model arm records what it chose, what was refused, and what it cost.
    """

    def _select(self, backend, *, strategy=STRATEGY_MODEL_PICK, size=3, window=None):
        window = window or WINDOW
        return model_select(
            strategy=strategy, requested_size=size, career_name='Welder',
            career_skills=['Welding'], candidate_dicts=window, eligible=eligible(window),
            trace_id='trace', backend=backend,
        )

    def test_the_chosen_keys_become_the_variant(self):
        backend = FakeBackend(content=json.dumps({'keys': ['B+1', 'C+1', 'D+1']}))

        variant = self._select(backend)

        self.assertEqual(sorted(keys_of(variant)), ['B+1', 'C+1', 'D+1'])
        self.assertTrue(variant.is_complete)
        self.assertEqual(variant.trace['model'], 'fake-1')
        self.assertEqual(variant.label, 'model_pick:3')

    def test_the_model_sees_levels_and_providers(self):
        backend = FakeBackend(content=json.dumps({'keys': []}))

        self._select(backend)

        shown = json.loads(backend.calls[0]['user_content'])
        self.assertEqual(shown['career'], 'Welder')
        self.assertEqual(shown['candidates'][0]['level'], 'Intermediate')
        self.assertEqual(shown['candidates'][0]['provider'], 'P1')

    def test_ineligible_candidates_are_never_shown(self):
        window = WINDOW + [candidate('X+1', language='Spanish', partner='P9')]
        backend = FakeBackend(content=json.dumps({'keys': []}))

        self._select(backend, window=window)

        shown = [c['key'] for c in json.loads(backend.calls[0]['user_content'])['candidates']]
        self.assertNotIn('X+1', shown)

    def test_a_model_that_returns_too_few_leaves_the_variant_short(self):
        backend = FakeBackend(content=json.dumps({'keys': ['B+1', 'Z+9']}))

        variant = self._select(backend, size=3)

        self.assertEqual(keys_of(variant), ['B+1'])
        self.assertFalse(variant.is_complete)
        self.assertEqual(variant.fabricated_keys, ['Z+9'])

    def test_a_model_sized_choice_is_capped_at_five(self):
        backend = FakeBackend(content=json.dumps({'keys': ['A+1', 'A+2', 'B+1', 'C+1', 'D+1']}))

        variant = self._select(backend, strategy=STRATEGY_MODEL_SIZED, size=None)

        self.assertEqual(len(variant.courses), 5)
        self.assertTrue(variant.is_complete)
        self.assertIn('between 2 and 5', backend.calls[0]['system_prompt'])

    def test_a_backend_failure_is_recorded_not_raised(self):
        variant = self._select(FakeBackend(error=ModelBackendRequestError('down')))

        self.assertEqual(variant.courses, [])
        self.assertIn('ModelBackendRequestError', variant.error)

    def test_a_non_json_response_is_recorded(self):
        variant = self._select(FakeBackend(content='A+1, B+1'))

        self.assertEqual(variant.error, 'selection response was not JSON')

    def test_a_response_without_keys_is_recorded(self):
        variant = self._select(FakeBackend(content=json.dumps({'ordered_keys': ['A+1']})))

        self.assertEqual(variant.error, 'selection response had no keys list')


class TestVariantBackend(TestCase):
    """
    Scenario: The model arms run on the re-rank's model unless told otherwise.
    """

    @override_settings(PATHWAYS_MODEL_BACKEND='openai', PATHWAYS_OPENAI_MODEL='gpt-4o',
                       PATHWAYS_VARIANT_BACKEND='', PATHWAYS_VARIANT_MODEL='')
    def test_it_defaults_to_the_rerank_backend_and_model(self):
        backend = get_variant_backend()

        self.assertIsInstance(backend, OpenAIBackend)
        self.assertEqual(backend.model, 'gpt-4o')

    @override_settings(PATHWAYS_MODEL_BACKEND='openai', PATHWAYS_VARIANT_MODEL='gpt-5.4-mini')
    def test_a_variant_model_overrides_the_default(self):
        self.assertEqual(get_variant_backend().model, 'gpt-5.4-mini')

    @override_settings(PATHWAYS_MODEL_BACKEND='xpert', PATHWAYS_VARIANT_BACKEND='')
    def test_an_xpert_pipeline_needs_an_explicit_variant_backend(self):
        with self.assertRaises(ModelBackendConfigurationError):
            get_variant_backend()

    @override_settings(PATHWAYS_MODEL_BACKEND='xpert', PATHWAYS_VARIANT_BACKEND='')
    def test_that_failure_is_recorded_on_the_variant(self):
        variant = model_select(
            strategy=STRATEGY_MODEL_SIZED, requested_size=None, career_name='Welder',
            career_skills=[], candidate_dicts=WINDOW, eligible=eligible(), trace_id='t',
        )

        self.assertIn('ModelBackendConfigurationError', variant.error)


class TestBuildVariants(TestCase):
    """
    Scenario: Every requested arm is built from the same candidates, in a stable order.
    """

    def test_arms_come_back_in_strategy_then_size_order(self):
        backend = FakeBackend(content=json.dumps({'keys': ['B+1', 'C+1']}))

        variants = build_variants(
            career_name='Welder', career_skills=[], ordered_candidates=WINDOW,
            sizes=[3, 2], strategies=[STRATEGY_MODEL_SIZED, STRATEGY_MODEL_PICK, STRATEGY_RANKED_CUT],
            trace_prefix='wf', backend=backend,
        )

        self.assertEqual([v.label for v in variants], [
            'ranked_cut:2', 'ranked_cut:3', 'model_pick:2', 'model_pick:3', 'model_sized:2-5',
        ])

    def test_model_sized_runs_once_however_many_sizes_and_ranked_cut_is_free(self):
        backend = FakeBackend(content=json.dumps({'keys': ['B+1', 'C+1']}))

        build_variants(
            career_name='Welder', career_skills=[], ordered_candidates=WINDOW,
            sizes=[2, 3, 4, 5], strategies=[STRATEGY_RANKED_CUT, STRATEGY_MODEL_SIZED],
            trace_prefix='wf', backend=backend,
        )

        self.assertEqual([call['trace_id'] for call in backend.calls], ['wf:model_sized:2-5'])

    def test_a_bad_size_is_rejected_before_any_call(self):
        backend = FakeBackend()

        with self.assertRaises(ValueError):
            build_variants(
                career_name='Welder', career_skills=[], ordered_candidates=WINDOW,
                sizes=[6], strategies=[STRATEGY_MODEL_PICK], trace_prefix='wf', backend=backend,
            )
        self.assertEqual(backend.calls, [])
