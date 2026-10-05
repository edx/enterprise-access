"""
Tests for pathway size variants.

The properties that matter: every arm enforces the delivered pathway's rules whatever a
model returns; nothing is ever padded to reach a size, so a short variant stays short; the
two model arms differ in their size rule and nothing else; and a failed arm is recorded,
not raised.
"""
import json
from dataclasses import asdict, dataclass
from unittest import mock

import ddt
from django.test import TestCase, override_settings

from enterprise_access.apps.pathways.ecosystems import ecosystems_of
from enterprise_access.apps.pathways.model_backends import (
    ModelBackendConfigurationError,
    ModelBackendRequestError,
    OpenAIBackend
)
from enterprise_access.apps.pathways.pathway_assembly import eligible_candidates
from enterprise_access.apps.pathways.pathway_variants import (
    ALL_STRATEGIES,
    DEFAULT_VARIANT_SIZES,
    SELECTION_V2_CAREER_DESCRIPTION_CHARS,
    SELECTION_V2_FAMILY_TITLES_SHOWN,
    SELECTION_V2_SKILL_NAMES_SHOWN,
    SHAPE_STRATEGIES,
    STRATEGY_MODEL_PICK,
    STRATEGY_MODEL_SIZED,
    STRATEGY_RANKED_CUT,
    STRATEGY_SHAPE_CUT,
    STRATEGY_SHAPE_PICK,
    STRATEGY_SHAPE_PICK_V2,
    VARIANT_STRATEGIES,
    Variant,
    apply_selection,
    build_selection_content,
    build_selection_content_v2,
    build_variants,
    estimated_model_calls,
    get_variant_backend,
    model_select,
    normalise_shapes,
    normalise_sizes,
    normalise_strategies,
    parse_shape,
    place_seats,
    policy_record,
    ranked_cut,
    resolve_editorial_policy,
    resolve_shape_request,
    resolve_variant_request,
    selection_system_prompt,
    selection_system_prompt_v2,
    shape_breakdown,
    shape_cut,
    variant_count,
    variant_label
)
from enterprise_access.apps.pathways.prompts import (
    PATHWAY_SELECTION_SYSTEM_PROMPT_V2,
    SELECTION_EXACT_SIZE_INSTRUCTION,
    SELECTION_MODEL_SIZED_INSTRUCTION,
    SELECTION_SHAPE_INSTRUCTION
)
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
        'skill_names': c.get('skill_names') or [],
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


# Two per rung, from four providers, in relevance order.
SHAPED_WINDOW = [
    candidate('I+1', partner='P1'),
    candidate('M+1', level='Intermediate', partner='P1'),
    candidate('I+2', partner='P1'),
    candidate('A+1', level='Advanced', partner='P2'),
    candidate('M+2', level='Intermediate', partner='P3'),
    candidate('I+3', partner='P4'),
]


@ddt.ddt
class TestShapeRequests(TestCase):
    """
    Scenario: A level shape is read the same way by every caller, and priced before it runs.
    """

    def test_a_shape_reads_as_courses_per_rung(self):
        self.assertEqual(parse_shape('2/2/1'), (2, 2, 1))
        self.assertEqual(parse_shape(' 0/2/0 '), (0, 2, 0))

    @ddt.data('2/2', '2-0-0', 'a/0/0', '1/0/0', '2/2/2', '')
    def test_a_malformed_or_out_of_range_shape_is_rejected(self, shape):
        with self.assertRaises(ValueError):
            parse_shape(shape)

    def test_shapes_are_canonicalised_and_deduplicated_in_request_order(self):
        self.assertEqual(normalise_shapes(['0/2/0', '2/0/0', ' 0/2/0']), ['0/2/0', '2/0/0'])

    def test_shapes_alone_run_the_free_shape_arm(self):
        self.assertEqual(resolve_shape_request(['2/0/0'], []), (['2/0/0'], [STRATEGY_SHAPE_CUT]))

    def test_a_shape_arm_without_a_shape_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'at least one shape'):
            resolve_shape_request([], [STRATEGY_SHAPE_PICK])

    def test_the_size_and_shape_requests_each_keep_their_own_arms(self):
        strategies = [STRATEGY_MODEL_SIZED, STRATEGY_SHAPE_PICK]

        self.assertEqual(resolve_variant_request([], strategies)[1], [STRATEGY_MODEL_SIZED])
        self.assertEqual(resolve_shape_request(['2/0/0'], strategies)[1], [STRATEGY_SHAPE_PICK])

    def test_a_shape_only_request_asks_for_no_sizes(self):
        self.assertEqual(resolve_variant_request([], [STRATEGY_SHAPE_CUT]), ([], []))

    def test_the_api_strategies_do_not_include_the_shape_arms(self):
        self.assertNotIn(STRATEGY_SHAPE_CUT, VARIANT_STRATEGIES)
        self.assertNotIn(STRATEGY_SHAPE_PICK, VARIANT_STRATEGIES)

    def test_shape_pick_costs_one_call_per_shape_and_shape_cut_none(self):
        shapes = ['2/0/0', '0/2/0', '2/2/1']
        strategies = [STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK]

        self.assertEqual(variant_count([], strategies, shapes), 6)
        self.assertEqual(estimated_model_calls(sizes=[], strategies=strategies, shapes=shapes,
                                               judge_enabled=False), 3)
        self.assertEqual(estimated_model_calls(sizes=[], strategies=strategies, shapes=shapes,
                                               judge_enabled=True), 3 + 1 + 6)

    def test_a_shape_label_names_the_strategy_and_shape(self):
        self.assertEqual(variant_label(STRATEGY_SHAPE_PICK, 5, (2, 2, 1)), 'shape_pick:2/2/1')


class TestShapeCut(TestCase):
    """
    Scenario: The free shape arm fills each rung from the relevance order and never backfills.
    """

    def test_it_fills_each_rung_in_relevance_order(self):
        variant = shape_cut(eligible(SHAPED_WINDOW), (2, 0, 0))

        self.assertEqual(keys_of(variant), ['I+1', 'I+2'])
        self.assertTrue(variant.is_complete)
        self.assertEqual(variant.label, 'shape_cut:2/0/0')
        self.assertEqual(variant.violations(), [])

    def test_a_ladder_is_delivered_easiest_first(self):
        variant = shape_cut(eligible(SHAPED_WINDOW), (1, 1, 1))

        self.assertEqual([c.level_type for c in variant.courses], ['Introductory', 'Intermediate', 'Advanced'])
        self.assertTrue(variant.is_complete)

    def test_the_scarce_rung_claims_the_provider_first(self):
        # P1 supplies the most relevant intro AND the only intermediate course. The
        # intermediate rung is scarcer (1 against 3), so it takes M+1 first, and the intro rung
        # skips I+2 at the cap and takes I+4 -- rather than spending both of P1's places on
        # intro courses and leaving the intermediate rung empty.
        window = SHAPED_WINDOW[:3] + [candidate('I+4', partner='P5')]
        variant = shape_cut(eligible(window), (2, 1, 0))

        self.assertEqual(keys_of(variant), ['I+1', 'I+4', 'M+1'])
        self.assertEqual(variant.dropped, {'provider_cap': 1})

    def test_an_empty_rung_leaves_the_variant_short_rather_than_backfilled(self):
        window = [c for c in SHAPED_WINDOW if c['level_type'] != 'Advanced']
        variant = shape_cut(eligible(window), (2, 2, 1))

        self.assertEqual(len(variant.courses), 4)
        self.assertFalse(variant.is_complete)
        self.assertIn('exactly 5 courses, got 4', variant.violations()[0])


class TestShapePick(TestCase):
    """
    Scenario: The model shape arm sees only the shape's rungs, and its counts are enforced.
    """

    def _pick(self, backend, shape=(0, 2, 0), window=None):
        window = window or SHAPED_WINDOW
        return model_select(
            strategy=STRATEGY_SHAPE_PICK, requested_size=sum(shape), shape=shape,
            career_name='Data Analyst', career_skills=['SQL'], candidate_dicts=window,
            eligible=eligible(window), trace_id='t', backend=backend,
        )

    def test_only_candidates_on_the_shapes_rungs_are_shown(self):
        backend = FakeBackend(content=json.dumps({'keys': []}))
        self._pick(backend)

        shown = [c['key'] for c in json.loads(backend.calls[0]['user_content'])['candidates']]
        self.assertEqual(shown, ['M+1', 'M+2'])

    def test_the_prompt_states_the_shape(self):
        backend = FakeBackend(content=json.dumps({'keys': []}))
        self._pick(backend, shape=(2, 1, 0))

        self.assertIn('Pick exactly 3 courses: 2 Introductory and 1 Intermediate.',
                      backend.calls[0]['system_prompt'])

    def test_a_rung_over_its_count_is_dropped(self):
        backend = FakeBackend(content=json.dumps({'keys': ['I+1', 'I+3', 'M+1']}))
        variant = self._pick(backend, shape=(1, 1, 0))

        self.assertEqual(keys_of(variant), ['I+1', 'M+1'])
        self.assertEqual(variant.dropped, {'over_level_quota': 1})
        self.assertTrue(variant.is_complete)
        self.assertEqual(variant.label, 'shape_pick:1/1/0')

    def test_a_key_off_the_shape_is_a_fabrication_since_it_was_never_shown(self):
        backend = FakeBackend(content=json.dumps({'keys': ['M+1', 'A+1']}))
        variant = self._pick(backend)

        self.assertEqual(keys_of(variant), ['M+1'])
        self.assertEqual(variant.fabricated_keys, ['A+1'])
        self.assertFalse(variant.is_complete)

    def test_no_candidates_on_the_rungs_means_no_call(self):
        backend = FakeBackend(content=json.dumps({'keys': ['A+1', 'A+2']}))
        no_advanced = [c for c in SHAPED_WINDOW if c['level_type'] != 'Advanced']
        variant = self._pick(backend, shape=(0, 0, 2), window=no_advanced)

        self.assertEqual(backend.calls, [])
        self.assertIn('no candidates on the rungs', variant.error)


class TestShapePrompt(TestCase):
    """
    Scenario: The shape arm shares the size arms' prompt and changes only its size rule.
    """

    def test_the_breakdown_reads_as_a_sentence(self):
        self.assertEqual(shape_breakdown((0, 2, 0)), '2 Intermediate')
        self.assertEqual(shape_breakdown((2, 2, 1)), '2 Introductory, 2 Intermediate and 1 Advanced')

    def test_only_the_size_instruction_differs_from_the_exact_size_arm(self):
        shaped = selection_system_prompt(5, (2, 2, 1))
        exact = selection_system_prompt(5)
        shape_sentence = SELECTION_SHAPE_INSTRUCTION.format(size=5, breakdown=shape_breakdown((2, 2, 1)))

        self.assertEqual(
            shaped.replace(shape_sentence, '<rule>'),
            exact.replace(SELECTION_EXACT_SIZE_INSTRUCTION.format(size=5), '<rule>'),
        )


class TestBuildShapeVariants(TestCase):
    """
    Scenario: Shape arms build beside the size arms, in a stable order.
    """

    def test_shape_arms_follow_the_size_arms_then_the_requested_shape_order(self):
        backend = FakeBackend(content=json.dumps({'keys': ['M+1', 'M+2']}))
        variants = build_variants(
            career_name='Data Analyst', career_skills=['SQL'], ordered_candidates=SHAPED_WINDOW,
            sizes=[2], strategies=[STRATEGY_SHAPE_PICK, STRATEGY_RANKED_CUT, STRATEGY_SHAPE_CUT],
            shapes=['0/2/0', '2/0/0'], trace_prefix='p', backend=backend,
        )

        self.assertEqual(
            [variant.label for variant in variants],
            ['ranked_cut:2', 'shape_cut:0/2/0', 'shape_cut:2/0/0', 'shape_pick:0/2/0', 'shape_pick:2/0/0'],
        )
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(backend.calls[0]['trace_id'], 'p:shape_pick:0/2/0')


# ---------------------------------------------------------------------------------------
# shape_pick_v2, editorial exclusions and seats. The editorial app is a separate module that
# may not be installed; everything here stands in for it, so these tests never import it.
# ---------------------------------------------------------------------------------------

PATCH_EDITORIAL_API = 'enterprise_access.apps.pathways.pathway_variants.load_editorial_api'


@dataclass(frozen=True)
class FakeSeat:
    """Stands in for ``pathway_editorial.api.Seat``: the same fields and ``to_dict``."""

    key: str
    level: str = 'Introductory'
    rule: str = 'flagship'
    reason: str = 'named by the policy'

    def to_dict(self):
        return asdict(self)


class FakePolicy:
    """Stands in for ``EditorialPolicy``: what the variant arms read of one."""

    def __init__(self, excluded_keys=()):
        self.excluded_keys = frozenset(excluded_keys)

    def to_dict(self):
        return {'excluded_keys': sorted(self.excluded_keys), 'flagships': [], 'promoted': []}


class FakePlanner:
    """Stands in for ``plan_seats``: records each call and seats the keys it was given."""

    def __init__(self, seats=(), error=None):
        self.seats = list(seats)
        self.error = error
        self.calls = []

    def __call__(self, *, ordered_candidates, shape, career_skills, policy):
        self.calls.append({
            'keys': [c['key'] for c in ordered_candidates], 'shape': shape,
            'career_skills': career_skills, 'policy': policy,
        })
        if self.error:
            raise self.error
        return list(self.seats)


def build(strategies, shapes, *, backend=None, policy=None, planner=None, sizes=(), **kwargs):
    """``build_variants`` over ``SHAPED_WINDOW``, as the variant step calls it."""
    return build_variants(
        career_name='Data Analyst', career_skills=['SQL'], ordered_candidates=SHAPED_WINDOW,
        sizes=list(sizes), strategies=strategies, shapes=shapes, trace_prefix='p',
        backend=backend or FakeBackend(content=json.dumps({'keys': []})), policy=policy,
        seat_planner=planner, **kwargs,
    )


class TestShapePickV2Arm(TestCase):
    """
    Scenario: shape_pick_v2 is a shape arm, priced like shape_pick, and off the API.
    """

    def test_it_is_a_shape_arm_the_api_does_not_offer(self):
        self.assertIn(STRATEGY_SHAPE_PICK_V2, SHAPE_STRATEGIES)
        self.assertIn(STRATEGY_SHAPE_PICK_V2, ALL_STRATEGIES)
        self.assertNotIn(STRATEGY_SHAPE_PICK_V2, VARIANT_STRATEGIES)
        self.assertEqual(resolve_shape_request(['2/0/0'], [STRATEGY_SHAPE_PICK_V2])[1], [STRATEGY_SHAPE_PICK_V2])

    def test_it_costs_up_to_two_calls_per_shape_for_its_repair_round(self):
        shapes = ['2/0/0', '0/2/0']

        self.assertEqual(estimated_model_calls(sizes=[], strategies=[STRATEGY_SHAPE_PICK_V2], shapes=shapes,
                                               judge_enabled=False), 4)
        self.assertEqual(estimated_model_calls(sizes=[], strategies=[STRATEGY_SHAPE_PICK, STRATEGY_SHAPE_PICK_V2],
                                               shapes=shapes, judge_enabled=True), 2 + 4 + 1 + 4)

    def test_each_judge_rubric_is_charged_for_every_pathway(self):
        self.assertEqual(estimated_model_calls(sizes=[2, 3], strategies=[STRATEGY_RANKED_CUT], judge_enabled=True,
                                               judge_rubrics=['v1', 'v2']), 2 * (1 + 2))
        self.assertEqual(estimated_model_calls(sizes=[2, 3], strategies=[STRATEGY_RANKED_CUT], judge_enabled=True,
                                               judge_rubrics=['v2']), 1 + 2)
        with self.assertRaises(ValueError):
            estimated_model_calls(sizes=[2], strategies=[STRATEGY_RANKED_CUT], judge_enabled=True,
                                  judge_rubrics=['v7'])

    def test_it_sends_the_v2_prompt_with_the_shape_sentence(self):
        backend = FakeBackend(content=json.dumps({'keys': ['M+1', 'M+2']}))

        variant = build([STRATEGY_SHAPE_PICK_V2], ['0/2/0'], backend=backend)[0]

        call = backend.calls[0]
        self.assertEqual(call['system_prompt'], selection_system_prompt_v2((0, 2, 0)))
        self.assertTrue(call['system_prompt'].startswith(PATHWAY_SELECTION_SYSTEM_PROMPT_V2.split('{')[0]))
        self.assertIn('Pick exactly 2 courses: 2 Intermediate.', call['system_prompt'])
        self.assertIn('"keys"', call['system_prompt'])
        self.assertEqual(call['trace_id'], 'p:shape_pick_v2:0/2/0')
        self.assertEqual((variant.label, keys_of(variant)), ('shape_pick_v2:0/2/0', ['M+1', 'M+2']))
        self.assertTrue(variant.is_complete)

    def test_it_shows_the_career_its_family_and_each_candidates_skills(self):
        window = [dict(c, skill_names=[f'K{n}' for n in range(12)]) for c in SHAPED_WINDOW]
        backend = FakeBackend(content=json.dumps({'keys': []}))
        build_variants(
            career_name='Data Analyst', career_skills=[f'S{n}' for n in range(12)], ordered_candidates=window,
            sizes=[], strategies=[STRATEGY_SHAPE_PICK_V2], shapes=['0/2/0'], trace_prefix='p', backend=backend,
            career_description='Finds patterns in data. ' * 50,
            family_titles=[f'Title {n}' for n in range(20)], family_size=31,
        )

        shown = json.loads(backend.calls[0]['user_content'])
        self.assertEqual(shown['career'], 'Data Analyst')
        self.assertEqual(len(shown['career_description']), SELECTION_V2_CAREER_DESCRIPTION_CHARS)
        self.assertEqual(shown['family_size'], 31)
        self.assertEqual(len(shown['family_titles']), SELECTION_V2_FAMILY_TITLES_SHOWN)
        self.assertEqual(len(shown['career_skills']), 8)
        self.assertEqual(shown['already_chosen'], [])
        self.assertEqual([c['key'] for c in shown['candidates']], ['M+1', 'M+2'])
        self.assertEqual(
            set(shown['candidates'][0]), {'key', 'title', 'level', 'provider', 'description', 'skill_names'},
        )
        self.assertEqual(len(shown['candidates'][0]['skill_names']), SELECTION_V2_SKILL_NAMES_SHOWN)

    def test_the_family_size_is_never_below_the_titles_known(self):
        content = json.loads(build_selection_content_v2(
            career_name='Welder', career_skills=[], career_description='', family_titles=['A', 'B', 'A', ' '],
            family_size=0, candidates=[], already_chosen=[],
        ))

        self.assertEqual((content['family_titles'], content['family_size']), (['A', 'B'], 2))

    def test_the_v1_arms_ignore_the_career_description_and_family(self):
        backend = FakeBackend(content=json.dumps({'keys': []}))
        build([STRATEGY_SHAPE_PICK], ['0/2/0'], backend=backend,
              career_description='Finds patterns.', family_titles=['X'], family_size=4)

        shown = json.loads(backend.calls[0]['user_content'])
        self.assertEqual(set(shown), {'career', 'career_skills', 'candidates'})


class TestSelectionContentWithoutSeats(TestCase):
    """
    Scenario: A run without seats sends the v1 arms exactly what they were sent before.
    """

    def test_the_v1_content_is_pinned(self):
        content = build_selection_content(career_name='Welder', career_skills=['Welding'], candidates=WINDOW[:1])

        self.assertEqual(content, (
            '{"career":"Welder","career_skills":["Welding"],"candidates":[{"key":"A+1","title":"Course A+1",'
            '"level":"Intermediate","provider":"P1","description":"About A+1."}]}'
        ))

    def test_seated_courses_are_listed_as_already_chosen_only_when_there_are_any(self):
        content = json.loads(build_selection_content(
            career_name='Welder', career_skills=[], candidates=WINDOW[1:2], already_chosen=WINDOW[:1],
        ))

        self.assertEqual(list(content), ['career', 'career_skills', 'already_chosen', 'candidates'])
        self.assertEqual(content['already_chosen'],
                         [{'key': 'A+1', 'title': 'Course A+1', 'level': 'Intermediate', 'provider': 'P1'}])


class TestPlaceSeats(TestCase):
    """
    Scenario: A seat plan is checked against the window and the shape, as a model's pick is.
    """

    def test_valid_seats_are_placed_and_close_their_places(self):
        placement = place_seats(eligible(SHAPED_WINDOW), (2, 1, 0), [FakeSeat('I+3'), FakeSeat('M+2')])

        self.assertEqual([c.key for c in placement.seated], ['I+3', 'M+2'])
        self.assertEqual(placement.open_shape, (1, 0, 0))
        self.assertEqual(placement.records[0], FakeSeat('I+3').to_dict())
        self.assertEqual(placement.rejected, 0)

    def test_seats_that_do_not_fit_are_refused_and_counted(self):
        placement = place_seats(eligible(SHAPED_WINDOW), (2, 1, 0), [
            FakeSeat('Z+9'),            # not an eligible candidate (unknown, or excluded)
            FakeSeat('A+1'),            # on a rung the shape does not have
            FakeSeat('M+1'),
            FakeSeat('M+2'),            # its rung is already full
            FakeSeat('M+1'),            # repeated
            FakeSeat('I+1'),
            FakeSeat('I+2'),            # P1's third course
        ])

        self.assertEqual([c.key for c in placement.seated], ['M+1', 'I+1'])
        self.assertEqual(placement.rejected, 5)

    def test_the_candidates_own_level_decides_its_rung(self):
        placement = place_seats(eligible(SHAPED_WINDOW), (0, 2, 0), [FakeSeat('M+1', level='Introductory')])

        self.assertEqual(placement.open_shape, (0, 1, 0))

    def test_plain_dict_seats_are_accepted(self):
        placement = place_seats(eligible(SHAPED_WINDOW), (2, 0, 0), [{'key': 'I+1', 'rule': 'promoted:x'}])

        self.assertEqual(placement.records, [{'key': 'I+1', 'rule': 'promoted:x'}])


class TestApplySelectionWithSeats(TestCase):
    """
    Scenario: Seated courses count against every rule a chosen course does.
    """

    def test_seats_count_against_the_quota_the_cap_and_the_size(self):
        window = eligible(SHAPED_WINDOW)
        seated = [c for c in window if c.key in ('I+1', 'M+1')]
        pool = [c for c in window if c.key in ('I+2', 'I+3')]

        courses, dropped, fabricated = apply_selection(
            pool, ['I+2', 'M+1', 'I+3', 'A+1'], max_size=3,
            level_quota={'Introductory': 2, 'Intermediate': 1, 'Advanced': 0}, seated=seated,
        )

        self.assertEqual([c.key for c in courses], ['I+1', 'I+3', 'M+1'])
        self.assertEqual(dropped, {'provider_cap': 1, 'already_seated': 1})
        self.assertEqual(fabricated, ['A+1'])


class TestShapeCutWithSeats(TestCase):
    """
    Scenario: The free shape arm places the seats, then fills what is left by relevance.
    """

    def test_seats_come_first_and_the_rest_follows_the_relevance_order(self):
        variant = shape_cut(eligible(SHAPED_WINDOW), (2, 0, 0), seats=[FakeSeat('I+3')])

        self.assertEqual(sorted(keys_of(variant)), ['I+1', 'I+3'])
        self.assertEqual(variant.seats, [FakeSeat('I+3').to_dict()])
        self.assertTrue(variant.is_complete)

    def test_seats_spend_their_providers_allowance(self):
        variant = shape_cut(eligible(SHAPED_WINDOW), (2, 1, 0), seats=[FakeSeat('I+1'), FakeSeat('M+1')])

        self.assertEqual(sorted(keys_of(variant)), ['I+1', 'I+3', 'M+1'])
        self.assertEqual(variant.dropped, {'provider_cap': 1})

    def test_a_refused_seat_is_counted_and_its_place_filled_as_usual(self):
        variant = shape_cut(eligible(SHAPED_WINDOW), (2, 0, 0), seats=[FakeSeat('A+1', level='Advanced')])

        self.assertEqual(keys_of(variant), ['I+1', 'I+2'])
        self.assertEqual(variant.dropped, {'seat_rejected': 1})
        self.assertEqual(variant.seats, [])

    def test_without_seats_it_is_unchanged(self):
        self.assertEqual(keys_of(shape_cut(eligible(SHAPED_WINDOW), (2, 1, 0), seats=())),
                         keys_of(shape_cut(eligible(SHAPED_WINDOW), (2, 1, 0))))


class TestBuildVariantsWithAPolicy(TestCase):
    """
    Scenario: An editorial policy excludes courses from every arm and seats the shape arms.
    """

    def test_excluded_courses_reach_no_arm_and_no_model(self):
        backend = FakeBackend(content=json.dumps({'keys': ['I+1', 'I+2']}))
        variants = build(
            [STRATEGY_RANKED_CUT, STRATEGY_MODEL_PICK, STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK_V2], ['2/0/0'],
            sizes=[2], backend=backend, policy=FakePolicy(excluded_keys={'I+1'}), planner=FakePlanner(),
        )

        for variant in variants:
            with self.subTest(variant.label):
                self.assertNotIn('I+1', keys_of(variant))
        for call in backend.calls:
            self.assertNotIn('"I+1"', call['user_content'])
        self.assertEqual(variants[0].label, 'ranked_cut:2')

    def test_the_planner_sees_the_eligible_window_once_per_shape(self):
        planner = FakePlanner()
        policy = FakePolicy(excluded_keys={'M+1'})

        build([STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK, STRATEGY_SHAPE_PICK_V2], ['2/0/0', '0/2/0'],
              policy=policy, planner=planner)

        self.assertEqual([call['shape'] for call in planner.calls], [(2, 0, 0), (0, 2, 0)])
        self.assertEqual(planner.calls[0]['keys'], ['I+1', 'I+2', 'A+1', 'M+2', 'I+3'])
        self.assertEqual(planner.calls[0]['career_skills'], ['SQL'])
        self.assertIs(planner.calls[0]['policy'], policy)

    def test_the_model_is_asked_only_for_the_places_the_seats_leave(self):
        backend = FakeBackend(content=json.dumps({'keys': ['I+2', 'M+1', 'I+3']}))
        planner = FakePlanner([FakeSeat('I+1'), FakeSeat('M+1', level='Intermediate')])

        variant = build([STRATEGY_SHAPE_PICK_V2], ['2/1/0'], backend=backend, policy=FakePolicy(),
                        planner=planner)[0]

        call = backend.calls[0]
        self.assertIn('Pick exactly 1 courses: 1 Introductory.', call['system_prompt'])
        shown = json.loads(call['user_content'])
        self.assertEqual([c['key'] for c in shown['already_chosen']], ['I+1', 'M+1'])
        self.assertEqual([c['key'] for c in shown['candidates']], ['I+2', 'I+3'])
        # I+2 would be P1's third course; M+1 is already seated.
        self.assertEqual(sorted(keys_of(variant)), ['I+1', 'I+3', 'M+1'])
        self.assertEqual(variant.dropped, {'provider_cap': 1, 'already_seated': 1})
        self.assertEqual([seat['key'] for seat in variant.seats], ['I+1', 'M+1'])
        self.assertTrue(variant.is_complete)

    def test_the_v1_shape_arm_is_shown_its_seats_too(self):
        backend = FakeBackend(content=json.dumps({'keys': ['I+3']}))

        variant = build([STRATEGY_SHAPE_PICK], ['2/0/0'], backend=backend, policy=FakePolicy(),
                        planner=FakePlanner([FakeSeat('I+1')]))[0]

        call = backend.calls[0]
        self.assertIn('Pick exactly 1 courses: 1 Introductory.', call['system_prompt'])
        self.assertNotIn('already_chosen are fixed', call['system_prompt'])
        self.assertEqual([c['key'] for c in json.loads(call['user_content'])['already_chosen']], ['I+1'])
        self.assertEqual(sorted(keys_of(variant)), ['I+1', 'I+3'])

    def test_seats_that_fill_the_shape_leave_nothing_to_ask(self):
        backend = FakeBackend(content=json.dumps({'keys': ['I+2']}))
        planner = FakePlanner([FakeSeat('I+1'), FakeSeat('M+2', level='Intermediate')])

        variants = build([STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK, STRATEGY_SHAPE_PICK_V2], ['1/1/0'],
                         backend=backend, policy=FakePolicy(), planner=planner)

        self.assertEqual(backend.calls, [])
        for variant in variants:
            with self.subTest(variant.label):
                self.assertEqual(keys_of(variant), ['I+1', 'M+2'])
                self.assertTrue(variant.is_complete)
                self.assertEqual(variant.error, '')

    def test_a_planner_failure_costs_the_shape_arms_and_nothing_else(self):
        backend = FakeBackend(content=json.dumps({'keys': []}))
        variants = build([STRATEGY_RANKED_CUT, STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK_V2], ['2/0/0'], sizes=[2],
                         backend=backend, policy=FakePolicy(), planner=FakePlanner(error=RuntimeError('boom')))

        self.assertEqual(len(variants), 3)
        self.assertTrue(variants[0].is_complete)
        for variant in variants[1:]:
            self.assertEqual(variant.courses, [])
            self.assertEqual(variant.error, 'seat planning failed (RuntimeError): boom')
        self.assertEqual(backend.calls, [])

    def test_a_missing_editorial_app_is_a_planner_failure_not_a_crash(self):
        with mock.patch(PATCH_EDITORIAL_API, side_effect=ImportError('no editorial app')):
            variant = build([STRATEGY_SHAPE_CUT], ['2/0/0'], policy=FakePolicy())[0]

        self.assertIn('ImportError', variant.error)

    def test_the_default_planner_is_the_editorial_apps(self):
        editorial_api = mock.Mock()
        editorial_api.plan_seats.return_value = [FakeSeat('I+3')]
        with mock.patch(PATCH_EDITORIAL_API, return_value=editorial_api):
            variant = build([STRATEGY_SHAPE_CUT], ['2/0/0'], policy=FakePolicy())[0]

        self.assertEqual(editorial_api.plan_seats.call_args.kwargs['shape'], (2, 0, 0))
        self.assertEqual(variant.seats, [FakeSeat('I+3').to_dict()])

    def test_without_a_policy_nothing_is_planned_or_excluded(self):
        planner = FakePlanner([FakeSeat('I+3')])
        with mock.patch(PATCH_EDITORIAL_API) as editorial:
            variant = build([STRATEGY_SHAPE_CUT], ['2/0/0'], planner=planner)[0]

        self.assertEqual(planner.calls, [])
        editorial.assert_not_called()
        self.assertEqual((keys_of(variant), variant.seats), (['I+1', 'I+2'], []))


class TestResolveEditorialPolicy(TestCase):
    """
    Scenario: A run's policy comes from its snapshot, else the active policy, else nowhere.
    """

    def test_a_snapshot_wins_over_the_active_policy(self):
        editorial_api = mock.Mock()
        with mock.patch(PATCH_EDITORIAL_API, return_value=editorial_api):
            policy = resolve_editorial_policy(use_active=True, snapshot={'excluded_keys': ['A+1']})

        self.assertIs(policy, editorial_api.EditorialPolicy.from_dict.return_value)
        editorial_api.EditorialPolicy.from_dict.assert_called_once_with({'excluded_keys': ['A+1']})
        editorial_api.load_policy.assert_not_called()

    def test_the_active_policy_is_loaded_when_asked_for(self):
        editorial_api = mock.Mock()
        with mock.patch(PATCH_EDITORIAL_API, return_value=editorial_api):
            policy = resolve_editorial_policy(use_active=True, snapshot={})

        self.assertIs(policy, editorial_api.load_policy.return_value)

    def test_no_opt_in_never_touches_the_editorial_app(self):
        with mock.patch(PATCH_EDITORIAL_API) as loader:
            self.assertIsNone(resolve_editorial_policy())

        loader.assert_not_called()

    def test_a_policy_is_recorded_as_its_to_dict(self):
        self.assertEqual(policy_record(None), {})
        self.assertEqual(policy_record(FakePolicy({'B+1', 'A+1'}))['excluded_keys'], ['A+1', 'B+1'])


class TestBuildVariantsWithTheEditorialApp(TestCase):
    """
    Scenario: The real editorial policy and planner, end to end through ``build_variants``.

    The one test here that imports the editorial app, so the rest of this module runs without
    it. It asserts only exclusion and a flagship seat -- not which promoted course is seated,
    which is the editorial app's own rule to change.
    """

    def test_an_excluded_course_never_appears_and_a_flagship_is_seated(self):
        # pylint: disable=import-outside-toplevel
        from enterprise_access.apps.pathway_editorial.api import EditorialPolicy, FlagshipRule
        policy = EditorialPolicy(
            excluded_keys=frozenset({'I+1'}),
            flagships=(FlagshipRule(course_key='I+3', level='Introductory', reason='house foundation course'),),
        )
        # The model tries the excluded course first; it was never shown, so it cannot land.
        backend = FakeBackend(content=json.dumps({'keys': ['I+1', 'I+2', 'M+2']}))

        variants = build_variants(
            career_name='Data Analyst', career_skills=['SQL'], ordered_candidates=SHAPED_WINDOW,
            sizes=[2], strategies=[STRATEGY_RANKED_CUT, STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK_V2],
            shapes=['2/1/0'], trace_prefix='p', backend=backend, policy=policy,
        )

        for variant in variants:
            with self.subTest(variant.label):
                self.assertNotIn('I+1', keys_of(variant))
                self.assertEqual(variant.error, '')
        for variant in variants[1:]:
            with self.subTest(variant.label):
                self.assertIn('I+3', keys_of(variant))
                self.assertIn({'key': 'I+3', 'level': 'Introductory', 'rule': 'flagship',
                               'reason': 'house foundation course'}, variant.seats)
                self.assertTrue(variant.is_complete)
        self.assertNotIn('"I+1"', backend.calls[0]['user_content'])
        self.assertEqual(policy_record(policy), policy.to_dict())


class SequencedBackend(FakeBackend):
    """A fake backend that answers each call with the next scripted response."""

    def __init__(self, contents, error_on=None):
        super().__init__(content=contents[0])
        self.contents = list(contents)
        self.error_on = error_on

    def complete(self, **kwargs):
        index = len(self.calls)
        if self.error_on is not None and index == self.error_on:
            self.calls.append(kwargs)
            raise ModelBackendRequestError('down')
        self.content = self.contents[min(index, len(self.contents) - 1)]
        return super().complete(**kwargs)


# P1 supplies the intro course and two of the three intermediate ones, so a model that picks
# P1 for every slot breaks the provider cap.
CAPPED_WINDOW = [
    candidate('I+1', partner='P1'),
    candidate('M+1', level='Intermediate', partner='P1'),
    candidate('M+2', level='Intermediate', partner='P1'),
    candidate('M+3', level='Intermediate', partner='P2'),
]


class TestShapePickV2Repair(TestCase):
    """
    Scenario: a v2 pick the code had to refuse gets one repair round, not a short pathway.
    """

    def _pick(self, backend, strategy=STRATEGY_SHAPE_PICK_V2, shape=(1, 2, 0)):
        return model_select(
            strategy=strategy, requested_size=sum(shape), shape=shape, career_name='Data Analyst',
            career_skills=['SQL'], candidate_dicts=CAPPED_WINDOW, eligible=eligible(CAPPED_WINDOW),
            trace_id='t', backend=backend,
        )

    def test_a_provider_cap_refusal_is_repaired_with_only_allowed_candidates(self):
        backend = SequencedBackend([json.dumps({'keys': ['I+1', 'M+1', 'M+2']}), json.dumps({'keys': ['M+3']})])

        variant = self._pick(backend)

        self.assertEqual(keys_of(variant), ['I+1', 'M+1', 'M+3'])
        self.assertTrue(variant.is_complete)
        self.assertEqual(variant.dropped, {'provider_cap': 1})
        self.assertEqual((variant.repair['attempted'], variant.repair['added']), (True, 1))
        second = backend.calls[1]
        self.assertEqual(second['trace_id'], 't:repair')
        content = json.loads(second['user_content'])
        self.assertEqual([c['key'] for c in content['candidates']], ['M+3'])
        self.assertEqual(sorted(c['key'] for c in content['already_chosen']), ['I+1', 'M+1'])
        self.assertIn('1 Intermediate', second['system_prompt'])

    def test_an_honest_short_answer_is_not_repaired(self):
        backend = SequencedBackend([json.dumps({'keys': ['I+1', 'M+3']})])

        variant = self._pick(backend)

        self.assertEqual(len(backend.calls), 1)
        self.assertFalse(variant.is_complete)
        self.assertEqual(variant.repair, {})

    def test_a_failed_repair_keeps_the_first_answer(self):
        backend = SequencedBackend([json.dumps({'keys': ['I+1', 'M+1', 'M+2']})], error_on=1)

        variant = self._pick(backend)

        self.assertEqual(keys_of(variant), ['I+1', 'M+1'])
        self.assertIn('repair failed', variant.repair['error'])
        self.assertEqual(variant.error, '')

    def test_the_repair_still_enforces_every_rule(self):
        backend = SequencedBackend([json.dumps({'keys': ['I+1', 'M+1', 'M+2']}),
                                    json.dumps({'keys': ['M+2', 'M+3', 'Z+9']})])

        variant = self._pick(backend)

        self.assertEqual(keys_of(variant), ['I+1', 'M+1', 'M+3'])
        self.assertEqual(variant.repair['fabricated_keys'], ['M+2', 'Z+9'])

    def test_the_first_selection_arm_never_repairs(self):
        backend = SequencedBackend([json.dumps({'keys': ['I+1', 'M+1', 'M+2']}), json.dumps({'keys': ['M+3']})])

        variant = self._pick(backend, strategy=STRATEGY_SHAPE_PICK)

        self.assertEqual(len(backend.calls), 1)
        self.assertFalse(variant.is_complete)
        self.assertEqual(variant.repair, {})


# A window whose rungs offer a Microsoft course, a Google one, and a vendor-free one.
STACK_WINDOW = [
    candidate('MS+1', partner='Microsoft', title='Data Analysis with Power BI'),
    candidate('GC+1', partner='Google Cloud', title='Analytics on BigQuery'),
    candidate('NEU+1', partner='Adelaide', title='Foundations of Data Analysis'),
    candidate('MS+2', level='Intermediate', partner='Microsoft', title='Azure Data Engineering'),
    candidate('AWS+1', level='Intermediate', partner='Amazon', title='Data Warehousing on AWS'),
    candidate('NEU+2', level='Intermediate', partner='Delft', title='Statistics for Analysts'),
]


class TestEcosystemsOf(TestCase):
    """
    Scenario: a course belongs to the ecosystem it TEACHES, not the one that published it.
    """

    def test_a_product_in_the_title_places_the_course(self):
        self.assertEqual(ecosystems_of('Data Analysis with Power BI'), frozenset({'Microsoft'}))
        self.assertEqual(ecosystems_of('Analytics on BigQuery'), frozenset({'Google'}))

    def test_a_skill_tag_places_it_too(self):
        self.assertEqual(ecosystems_of('Cloud Foundations', ['Amazon Web Services']), frozenset({'Amazon'}))

    def test_a_vendor_free_course_belongs_to_none(self):
        self.assertEqual(ecosystems_of('Foundations of Data Analysis', ['Statistics']), frozenset())

    def test_the_publisher_alone_does_not_place_it(self):
        """IBM publishes plenty that teaches nothing of IBM's; the byline is not the subject."""
        self.assertEqual(ecosystems_of('Project Management Basics', ['Project Management']), frozenset())

    def test_a_course_naming_two_belongs_to_both(self):
        self.assertEqual(ecosystems_of('Azure and AWS compared'), frozenset({'Microsoft', 'Amazon'}))


@ddt.ddt
class TestSingleEcosystem(TestCase):
    """
    Scenario: a pathway may teach one vendor's products or none, never two.

    Bench round 2, re-measured with the current detector: pathways spanning two were rated good
    14% of the time against 70% for the rest, and drew 1.21 dropped courses each against 0.33.
    """

    def keys_for(self, arm, **kwargs):
        window = eligible(STACK_WINDOW)
        if arm == 'ranked_cut':
            return keys_of(ranked_cut(window, 3, **kwargs))
        return keys_of(shape_cut(window, (2, 1, 0), **kwargs))

    @staticmethod
    def spanned(keys):
        by_key = {c['key']: c for c in STACK_WINDOW}
        return frozenset().union(*[ecosystems_of(by_key[k]['title']) for k in keys]) if keys else frozenset()

    @ddt.data('ranked_cut', 'shape_cut')
    def test_without_the_rule_a_pathway_may_span_two(self, arm):
        # Explicitly off: the rule is on by default since 2026-10-05, so leaving the argument
        # out no longer means "without the rule".
        self.assertGreaterEqual(len(self.spanned(self.keys_for(arm, single_ecosystem=False))), 2)

    @ddt.data('ranked_cut', 'shape_cut')
    def test_with_the_rule_the_second_ecosystem_is_refused(self, arm):
        chosen = self.keys_for(arm, single_ecosystem=True)

        self.assertNotIn('GC+1', chosen)
        self.assertEqual(sorted(chosen), ['MS+1', 'MS+2', 'NEU+1'])

    def test_a_refusal_is_counted(self):
        variant = ranked_cut(eligible(STACK_WINDOW), 3, single_ecosystem=True)

        self.assertEqual(variant.dropped.get('other_ecosystem'), 1)

    def test_a_vendor_free_pathway_is_never_refused_anything(self):
        neutral = [c for c in STACK_WINDOW if c['key'].startswith('NEU')]
        variant = ranked_cut(eligible(neutral), 2, single_ecosystem=True)

        self.assertEqual(len(variant.courses), 2)
        self.assertEqual(variant.dropped, {})

    def test_a_model_pick_is_held_to_it_whatever_it_returned(self):
        courses, dropped, _ = apply_selection(
            eligible(STACK_WINDOW), ['MS+1', 'GC+1', 'MS+2'], max_size=5, single_ecosystem=True,
        )

        self.assertEqual([c.key for c in courses], ['MS+1', 'MS+2'])
        self.assertEqual(dropped.get('other_ecosystem'), 1)

    def test_a_course_naming_two_leaves_the_pathway_free_to_choose(self):
        window = [candidate('BOTH+1', title='Azure and AWS compared'),
                  candidate('AWS+9', level='Intermediate', partner='Amazon', title='Data Warehousing on AWS')]
        courses, dropped, _ = apply_selection(
            eligible(window), ['BOTH+1', 'AWS+9'], max_size=5, single_ecosystem=True,
        )

        self.assertEqual([c.key for c in courses], ['BOTH+1', 'AWS+9'])
        self.assertEqual(dropped, {})

    def test_an_editorial_seat_settles_which_ecosystem_the_rest_must_match(self):
        seats = [{'key': 'GC+1', 'level': 'Introductory', 'rule': 'flagship', 'reason': ''}]
        variant = shape_cut(eligible(STACK_WINDOW), (2, 1, 0), seats=seats, single_ecosystem=True)

        self.assertIn('GC+1', keys_of(variant))
        self.assertNotIn('MS+1', keys_of(variant))
        self.assertNotIn('MS+2', keys_of(variant))


class TestEcosystemFromSkillTags(TestCase):
    """
    Scenario: a course names its vendor in its skill tags as often as in its title.

    Reading the title alone let eight pathways span two ecosystems in the first run of the rule,
    because the candidates assembly works with carried no tags at all.
    """

    def test_a_vendor_named_only_in_the_tags_is_seen(self):
        window = [
            candidate('A+1', title='Analytics on BigQuery'),
            dict(candidate('B+1', partner='Carlos III', title='Management Information Systems'),
                 skill_names=['Microsoft Excel', 'Databases']),
        ]

        variant = ranked_cut(eligible(window), 2, single_ecosystem=True)

        self.assertEqual(keys_of(variant), ['A+1'])
        self.assertEqual(variant.dropped.get('other_ecosystem'), 1)

    def test_the_tags_survive_the_round_trip_into_assembly(self):
        hit = {'key': 'A+1', 'title': 'Anything', 'level_type': 'Introductory',
               'partners': [{'name': 'P'}], 'language': 'English', 'skill_names': ['IBM Cognos', '']}

        restored = eligible_candidates([hit])[0][0]

        self.assertEqual(restored.skill_names, ('IBM Cognos',))


class TestEcosystemRefusalIsRepaired(TestCase):
    """
    Scenario: a course the ecosystem rule turns away leaves a gap the repair round fills.

    The rule refuses; it does not shorten. The repair is shown only courses the rule allows, so
    a second answer cannot reintroduce the vendor that was refused.
    """

    def test_the_gap_is_filled_from_courses_the_rule_allows(self):
        window = [
            candidate('MS+1', title='Data Analysis with Power BI'),
            candidate('GC+1', partner='P2', title='Analytics on BigQuery'),
            candidate('NEU+1', partner='P3', title='Foundations of Data Analysis'),
        ]
        backend = SequencedBackend([json.dumps({'keys': ['MS+1', 'GC+1']}), json.dumps({'keys': ['NEU+1']})])

        variant = model_select(
            strategy=STRATEGY_SHAPE_PICK_V2, requested_size=2, shape=(2, 0, 0), career_name='Data Analyst',
            career_skills=['SQL'], candidate_dicts=window, eligible=eligible(window), trace_id='t',
            backend=backend, single_ecosystem=True,
        )

        self.assertEqual(sorted(keys_of(variant)), ['MS+1', 'NEU+1'])
        self.assertTrue(variant.is_complete)
        self.assertEqual(variant.dropped.get('other_ecosystem'), 1)
        self.assertEqual(variant.repair['added'], 1)
        shown = [c['key'] for c in json.loads(backend.calls[1]['user_content'])['candidates']]
        self.assertNotIn('GC+1', shown)
