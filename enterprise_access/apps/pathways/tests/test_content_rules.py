"""
Tests for the three content rules that apply by default since 2026-10-05.

A product reviewer rated 167 generated pathways over two rounds. Three of his findings are
rules in code now, applied to the delivered pathway and every experiment arm alike:

* **No capstones** -- always; ineligible everywhere and a Tier 1 violation.
* **One vendor ecosystem** -- on by default, with the kill switch
  ``learner_pathways_disable_single_ecosystem``.
* **Level honesty** -- a title that flatly contradicts its rung refuses the placement.

Each scenario here is checked in every place the rule applies: assembly (the delivered
pathway), ``ranked_cut``, ``shape_cut``, ``apply_selection`` and the model arms, and the
repair round.
"""
import json
import uuid

import ddt
from django.test import TestCase
from edx_toggles.toggles.testutils import override_waffle_switch

from enterprise_access.apps.pathways.ecosystems import EcosystemTracker, candidate_ecosystems, resolve_single_ecosystem
from enterprise_access.apps.pathways.models import (
    AssemblePathwayInput,
    AssemblePathwayOutput,
    AssemblePathwayStep,
    CourseCandidate,
    RetrieveCandidatesOutput
)
from enterprise_access.apps.pathways.pathway_assembly import (
    LEVEL_ADVANCED,
    LEVEL_INTERMEDIATE,
    LEVEL_INTRODUCTORY,
    Candidate,
    assemble_pathway,
    eligible_candidates,
    is_capstone,
    title_contradicts_level,
    validate_pathway
)
from enterprise_access.apps.pathways.pathway_variants import (
    REPAIRABLE_DROPS,
    STRATEGY_MODEL_PICK,
    STRATEGY_RANKED_CUT,
    STRATEGY_SHAPE_CUT,
    STRATEGY_SHAPE_PICK,
    STRATEGY_SHAPE_PICK_V2,
    apply_selection,
    build_variants,
    model_select,
    ranked_cut,
    shape_cut
)
from enterprise_access.apps.pathways.tests.test_pathway_variants import SequencedBackend, candidate, eligible
from enterprise_access.apps.pathways.tests.test_reranking import FakeBackend
from enterprise_access.toggles import LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM


def hit(key, *, title=None, level=LEVEL_INTRODUCTORY, partner='P', language='English', skill_names=()):
    """A catalog hit in the shape ``assemble_pathway`` reads."""
    return {
        'key': key, 'title': title or f'Course {key}', 'level_type': level,
        'partners': [{'name': partner}] if partner else [], 'language': language,
        'skill_names': list(skill_names),
    }


def keys_of(courses):
    return [course.key for course in courses]


def spanned(courses):
    """The ecosystems a set of courses teaches between them."""
    return set().union(*[candidate_ecosystems(course) for course in courses])


# A delivered-pathway window where relevance order would mix Microsoft and Google. The most
# relevant intro course is Microsoft's, which settles the ecosystem; Google's intro course
# comes next and must give way to the vendor-free one behind it.
MIXED_HITS = [
    hit('MS+1', title='Data Analysis with Power BI', partner='P1'),
    hit('GC+1', title='Analytics on BigQuery', partner='P2'),
    hit('NEU+1', title='Foundations of Data Analysis', partner='P3'),
    hit('NEU+2', title='Statistics for Analysts', level=LEVEL_INTERMEDIATE, partner='P4'),
    hit('NEU+3', title='Data Wrangling', level=LEVEL_INTERMEDIATE, partner='P5'),
    hit('NEU+4', title='Machine Learning Methods', level=LEVEL_ADVANCED, partner='P6'),
]

# The same for the variant arms, as ``CourseCandidate`` dicts.
MIXED_WINDOW = [
    candidate('MS+1', partner='P1', title='Data Analysis with Power BI'),
    candidate('GC+1', partner='P2', title='Analytics on BigQuery'),
    candidate('NEU+1', partner='P3', title='Foundations of Data Analysis'),
    candidate('NEU+2', level='Intermediate', partner='P4', title='Statistics for Analysts'),
]


class TestKillSwitch(TestCase):
    """
    Scenario: the one-ecosystem rule is on unless the kill switch is on, and an explicit
    choice wins over the switch either way.
    """

    def test_the_rule_is_on_by_default(self):
        self.assertTrue(resolve_single_ecosystem())
        self.assertTrue(EcosystemTracker().enabled)

    def test_the_kill_switch_turns_it_off(self):
        with override_waffle_switch(LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM, True):
            self.assertFalse(resolve_single_ecosystem())
            self.assertFalse(EcosystemTracker().enabled)

    def test_an_explicit_choice_wins_over_the_switch(self):
        self.assertFalse(resolve_single_ecosystem(False))
        with override_waffle_switch(LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM, True):
            self.assertTrue(resolve_single_ecosystem(True))


class TestSingleEcosystemInTheDeliveredPathway(TestCase):
    """
    Scenario: the delivered pathway holds to one ecosystem, and a refusal does not shorten it.
    """

    def test_the_second_ecosystem_is_refused_and_the_place_filled_from_the_next_course(self):
        assembly = assemble_pathway(MIXED_HITS)

        self.assertTrue(assembly.is_complete)
        self.assertNotIn('GC+1', keys_of(assembly.courses))
        self.assertIn('NEU+1', keys_of(assembly.courses))
        self.assertEqual(spanned(assembly.courses), {'Microsoft'})
        self.assertEqual(assembly.refused, {'other_ecosystem': 1})
        self.assertTrue(assembly.single_ecosystem)

    def test_the_backfill_is_held_to_it_too(self):
        """An all-introductory window fills by backfill; Google must not get in that way."""
        hits = [
            hit('MS+1', title='Data Analysis with Power BI', partner='P1'),
            hit('GC+1', title='Analytics on BigQuery', partner='P2'),
        ] + [hit(f'NEU+{i}', title=f'Statistics {i}', partner=f'Q{i}') for i in range(4)]

        assembly = assemble_pathway(hits)

        self.assertTrue(assembly.is_complete)
        self.assertNotIn('GC+1', keys_of(assembly.courses))
        self.assertEqual(assembly.refused, {'other_ecosystem': 1})

    def test_a_refusal_is_counted_once_across_both_passes(self):
        assembly = assemble_pathway(MIXED_HITS[:2] + [hit('NEU+9', partner='P9')])

        self.assertFalse(assembly.is_complete)
        self.assertEqual(assembly.refused, {'other_ecosystem': 1})

    def test_the_kill_switch_lets_it_span_two(self):
        with override_waffle_switch(LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM, True):
            assembly = assemble_pathway(MIXED_HITS)

        self.assertEqual(spanned(assembly.courses), {'Microsoft', 'Google'})
        self.assertEqual(assembly.refused, {})
        self.assertFalse(assembly.single_ecosystem)

    def test_an_explicit_false_lets_it_span_two(self):
        assembly = assemble_pathway(MIXED_HITS, single_ecosystem=False)

        self.assertIn('GC+1', keys_of(assembly.courses))

    def test_an_explicit_true_holds_even_with_the_switch_on(self):
        with override_waffle_switch(LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM, True):
            assembly = assemble_pathway(MIXED_HITS, single_ecosystem=True)

        self.assertNotIn('GC+1', keys_of(assembly.courses))

    def test_a_vendor_free_window_is_unchanged(self):
        hits = [hit(h['key'], title=f"Plain {h['key']}", level=h['level_type'], partner=h['partners'][0]['name'])
                for h in MIXED_HITS]

        self.assertEqual(
            keys_of(assemble_pathway(hits).courses), keys_of(assemble_pathway(hits, single_ecosystem=False).courses),
        )
        self.assertEqual(assemble_pathway(hits).refused, {})


class TestAssemblePathwayStepRecordsTheRules(TestCase):
    """
    Scenario: the delivered pathway's output says what the rules refused and whether the
    ecosystem rule ran.
    """

    def _run(self, hits):
        """Execute the step on these hits; ``(output, step re-read from the database)``."""
        step = AssemblePathwayStep.objects.create(
            workflow_record_uuid=uuid.uuid4(), input_data=AssemblePathwayInput().to_dict(),
        )
        candidates = RetrieveCandidatesOutput(courses=[
            CourseCandidate.from_hit({**h, 'short_description': '', 'full_description': ''}) for h in hits
        ])

        class Accumulated:  # pylint: disable=too-few-public-methods
            retrieve_candidates_output = candidates

        output = step.execute(accumulated_output=Accumulated())
        step.refresh_from_db()
        return output, step

    def test_refusals_and_the_rule_are_recorded_and_persisted(self):
        output, step = self._run(MIXED_HITS + [hit('CAP+1', title='Data Analytics Capstone Project')])

        self.assertTrue(output.complete)
        self.assertEqual(output.refused, {'other_ecosystem': 1})
        self.assertTrue(output.single_ecosystem)
        self.assertEqual(output.ineligible, {'capstone': 1})
        self.assertEqual(output.violations, [])
        self.assertEqual(step.output_object.refused, {'other_ecosystem': 1})

    def test_the_kill_switch_shows_on_the_output(self):
        with override_waffle_switch(LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM, True):
            output, _ = self._run(MIXED_HITS)

        self.assertFalse(output.single_ecosystem)
        self.assertEqual(output.refused, {})

    def test_an_output_persisted_before_the_fields_still_loads(self):
        old = AssemblePathwayOutput.from_dict({'courses': [], 'complete': False})

        self.assertEqual(old.refused, {})
        self.assertFalse(old.single_ecosystem)


@ddt.ddt
class TestSingleEcosystemInEveryArm(TestCase):
    """
    Scenario: every experiment arm holds to one ecosystem without being asked.
    """

    @ddt.data(STRATEGY_RANKED_CUT, STRATEGY_SHAPE_CUT)
    def test_the_free_arms_apply_it_by_default(self, arm):
        window = eligible(MIXED_WINDOW)
        variant = ranked_cut(window, 3) if arm == STRATEGY_RANKED_CUT else shape_cut(window, (2, 1, 0))

        self.assertNotIn('GC+1', keys_of(variant.courses))
        self.assertEqual(variant.dropped.get('other_ecosystem'), 1)
        self.assertTrue(variant.is_complete)

    @ddt.data(STRATEGY_RANKED_CUT, STRATEGY_SHAPE_CUT)
    def test_the_kill_switch_turns_it_off_in_the_free_arms(self, arm):
        window = eligible(MIXED_WINDOW)
        with override_waffle_switch(LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM, True):
            variant = ranked_cut(window, 3) if arm == STRATEGY_RANKED_CUT else shape_cut(window, (2, 1, 0))

        self.assertIn('GC+1', keys_of(variant.courses))

    def test_a_model_pick_is_held_to_it_by_default(self):
        courses, dropped, _ = apply_selection(eligible(MIXED_WINDOW), ['MS+1', 'GC+1', 'NEU+1'], max_size=3)

        self.assertEqual(sorted(keys_of(courses)), ['MS+1', 'NEU+1'])
        self.assertEqual(dropped, {'other_ecosystem': 1})

    def test_the_model_arms_apply_it_by_default(self):
        backend = FakeBackend(content=json.dumps({'keys': ['MS+1', 'GC+1']}))

        variant = model_select(
            strategy=STRATEGY_MODEL_PICK, requested_size=2, career_name='Data Analyst', career_skills=[],
            candidate_dicts=MIXED_WINDOW, eligible=eligible(MIXED_WINDOW), trace_id='t', backend=backend,
        )

        self.assertEqual(keys_of(variant.courses), ['MS+1'])
        self.assertEqual(variant.dropped, {'other_ecosystem': 1})

    def test_the_repair_round_fills_the_gap_by_default(self):
        backend = SequencedBackend([json.dumps({'keys': ['MS+1', 'GC+1']}), json.dumps({'keys': ['NEU+1']})])

        variant = model_select(
            strategy=STRATEGY_SHAPE_PICK_V2, requested_size=2, shape=(2, 0, 0), career_name='Data Analyst',
            career_skills=[], candidate_dicts=MIXED_WINDOW, eligible=eligible(MIXED_WINDOW), trace_id='t',
            backend=backend,
        )

        self.assertEqual(sorted(keys_of(variant.courses)), ['MS+1', 'NEU+1'])
        shown = [c['key'] for c in json.loads(backend.calls[1]['user_content'])['candidates']]
        self.assertNotIn('GC+1', shown)

    def _all_arms(self, **kwargs):
        backend = SequencedBackend([json.dumps({'keys': ['MS+1', 'GC+1', 'NEU+1', 'NEU+2']})])
        return build_variants(
            career_name='Data Analyst', career_skills=[], ordered_candidates=MIXED_WINDOW, sizes=[3],
            strategies=[STRATEGY_RANKED_CUT, STRATEGY_MODEL_PICK, STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK],
            shapes=['2/1/0'], trace_prefix='t', backend=backend, **kwargs,
        )

    def test_build_variants_applies_it_to_every_arm_by_default(self):
        for variant in self._all_arms():
            self.assertNotIn('GC+1', keys_of(variant.courses), variant.label)

    def test_build_variants_follows_the_kill_switch(self):
        with override_waffle_switch(LEARNER_PATHWAYS_DISABLE_SINGLE_ECOSYSTEM, True):
            variants = self._all_arms()

        self.assertTrue(all('GC+1' in keys_of(variant.courses) for variant in variants))

    def test_an_experiment_can_still_turn_it_off_explicitly(self):
        self.assertTrue(all('GC+1' in keys_of(v.courses) for v in self._all_arms(single_ecosystem=False)))


@ddt.ddt
class TestCapstones(TestCase):
    """
    Scenario: no pathway contains a capstone (agreed 2026-09-10; "no capstone courses!",
    review round 2).
    """

    @ddt.data(
        'Data Science: Capstone',
        'Data Engineering Capstone Project',
        'Capstone for Project Management',
        'CAPSTONE PROJECT FOR SCRUM MASTERS',
        'Software Development Final Project',
    )
    def test_a_capstone_title_is_recognised(self, title):
        self.assertTrue(is_capstone(title))

    @ddt.data(
        'Introduction to Project Management',
        'Full Stack Application Development Project',
        'Final Exam Preparation',
        'Capstones',
        '',
    )
    def test_a_title_that_only_resembles_one_is_not(self, title):
        self.assertFalse(is_capstone(title))

    def test_a_capstone_is_ineligible_and_counted(self):
        candidates, ineligible = eligible_candidates([hit('A+1'), hit('CAP+1', title='Data Science: Capstone')])

        self.assertEqual(keys_of(candidates), ['A+1'])
        self.assertEqual(ineligible, {'capstone': 1})

    def test_other_reasons_still_count_first(self):
        """Checked last, so a Spanish capstone counts as a language, as it always did."""
        _, ineligible = eligible_candidates([hit('CAP+1', title='Capstone', language='Spanish')])

        self.assertEqual(ineligible, {'unsupported_language': 1})

    def test_the_delivered_pathway_never_contains_one(self):
        hits = [hit('CAP+1', title='Data Analytics Capstone Project', level=LEVEL_ADVANCED, partner='P0')] \
            + [hit(h['key'], title=f"Plain {h['key']}", level=h['level_type'], partner=h['partners'][0]['name'])
               for h in MIXED_HITS]

        assembly = assemble_pathway(hits)

        self.assertTrue(assembly.is_complete)
        self.assertNotIn('CAP+1', keys_of(assembly.courses))
        self.assertEqual(assembly.ineligible, {'capstone': 1})

    def test_no_arm_contains_or_is_shown_one(self):
        window = [candidate('CAP+1', title='Data Science: Capstone')] + [
            candidate(f'N+{i}', partner=f'P{i}', title=f'Plain {i}') for i in range(3)
        ]
        backend = SequencedBackend([json.dumps({'keys': ['CAP+1', 'N+0', 'N+1']})])

        variants = build_variants(
            career_name='Data Analyst', career_skills=[], ordered_candidates=window, sizes=[2],
            strategies=[STRATEGY_RANKED_CUT, STRATEGY_MODEL_PICK, STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK],
            shapes=['2/0/0'], trace_prefix='t', backend=backend,
        )

        for variant in variants:
            self.assertNotIn('CAP+1', keys_of(variant.courses), variant.label)
        for call in backend.calls:
            self.assertNotIn('CAP+1', [c['key'] for c in json.loads(call['user_content'])['candidates']])

    def test_a_capstone_in_a_pathway_is_a_tier_one_violation(self):
        courses = [
            Candidate(key=f'A+{i}', title=f'Course {i}', level_type=LEVEL_INTRODUCTORY, partner=f'P{i}')
            for i in range(4)
        ] + [Candidate(key='C+1', title='Data Science: Capstone', level_type=LEVEL_ADVANCED, partner='P9')]

        violations = validate_pathway(courses)

        self.assertEqual(violations, ["'C+1' is a capstone course ('Data Science: Capstone')"])


@ddt.ddt
class TestTitleContradictsLevel(TestCase):
    """
    Scenario: a title can veto the rung ``level_type`` would put a course on, but only by
    saying so explicitly.
    """

    @ddt.data(
        ('Advanced Project Management', LEVEL_INTRODUCTORY),
        ('iLabX - The Internet Masterclass', LEVEL_INTRODUCTORY),
        ('Introduction to Cloud Next Generation Firewall', LEVEL_ADVANCED),
        ('Equity Markets Fundamentals', LEVEL_ADVANCED),
    )
    @ddt.unpack
    def test_an_explicit_contradiction_is_caught(self, title, level):
        self.assertTrue(title_contradicts_level(title, level))

    @ddt.data(
        ('Advanced Project Management', LEVEL_ADVANCED),
        ('Advanced Project Management', LEVEL_INTERMEDIATE),
        ('Introduction to Data', LEVEL_INTRODUCTORY),
        ('Introduction to Data', LEVEL_INTERMEDIATE),
        # Both cues: ambiguous, so neither rung is contradicted.
        ('Advanced Python Fundamentals', LEVEL_INTRODUCTORY),
        ('Advanced Python Fundamentals', LEVEL_ADVANCED),
        # Names the basics in order to go past them.
        ('SafetyQuest: Level Two - Moving Beyond QI Basics', LEVEL_ADVANCED),
        # Hard by subject, plain by wording: the documented limit of a title rule.
        ('Introduction to Post-Quantum Cryptography', LEVEL_INTRODUCTORY),
        ('Data Wrangling', LEVEL_ADVANCED),
        ('', LEVEL_INTRODUCTORY),
    )
    @ddt.unpack
    def test_anything_else_is_not(self, title, level):
        self.assertFalse(title_contradicts_level(title, level))


# Each rung holds one course whose title contradicts it, ahead of an honest one.
DISHONEST_HITS = [
    hit('I+BAD', title='Advanced Project Management', partner='P1'),
    hit('I+1', title='Project Management Essentials', partner='P2'),
    hit('I+2', title='Planning Projects', partner='P3'),
    hit('M+1', title='Agile Delivery', level=LEVEL_INTERMEDIATE, partner='P4'),
    hit('M+2', title='Managing Risk', level=LEVEL_INTERMEDIATE, partner='P5'),
    hit('A+BAD', title='Introduction to Portfolio Management', level=LEVEL_ADVANCED, partner='P6'),
    hit('A+1', title='Programme Governance', level=LEVEL_ADVANCED, partner='P7'),
]
DISHONEST_WINDOW = [
    candidate(h['key'], level=h['level_type'], partner=h['partners'][0]['name'], title=h['title'])
    for h in DISHONEST_HITS
]


class TestLevelHonestyInTheDeliveredPathway(TestCase):
    """
    Scenario: the delivered pathway does not put an "Advanced" title on an introductory
    place, or an "Introduction" on the advanced one, and fills from the next honest course.
    """

    def test_both_directions_are_refused_and_the_places_still_filled(self):
        assembly = assemble_pathway(DISHONEST_HITS)

        self.assertTrue(assembly.is_complete)
        self.assertNotIn('I+BAD', keys_of(assembly.courses))
        self.assertNotIn('A+BAD', keys_of(assembly.courses))
        self.assertEqual(assembly.realised_level_mix, {LEVEL_INTRODUCTORY: 2, LEVEL_INTERMEDIATE: 2,
                                                       LEVEL_ADVANCED: 1})
        self.assertEqual(assembly.refused, {'level_mismatch': 2})

    def test_the_backfill_does_not_readmit_one(self):
        """With the advanced rung short, backfill must not slip the refused course back in."""
        hits = [h for h in DISHONEST_HITS if h['key'] != 'A+1']

        assembly = assemble_pathway(hits)

        self.assertNotIn('A+BAD', keys_of(assembly.courses))
        self.assertEqual(assembly.unfilled_rungs, [LEVEL_ADVANCED])
        self.assertEqual(assembly.refused, {'level_mismatch': 2})

    def test_it_can_be_turned_off(self):
        assembly = assemble_pathway(DISHONEST_HITS, level_honesty=False)

        self.assertIn('I+BAD', keys_of(assembly.courses))
        self.assertIn('A+BAD', keys_of(assembly.courses))
        self.assertEqual(assembly.refused, {})


class TestLevelHonestyInTheArms(TestCase):
    """
    Scenario: every arm that places a course on a rung holds to level honesty; the size arms,
    which place none, do not apply it.
    """

    def test_shape_cut_refuses_both_directions(self):
        variant = shape_cut(eligible(DISHONEST_WINDOW), (2, 2, 1))

        self.assertTrue(variant.is_complete)
        self.assertNotIn('I+BAD', keys_of(variant.courses))
        self.assertNotIn('A+BAD', keys_of(variant.courses))
        self.assertEqual(variant.dropped, {'level_mismatch': 2})

    def test_shape_cut_can_turn_it_off(self):
        variant = shape_cut(eligible(DISHONEST_WINDOW), (2, 2, 1), level_honesty=False)

        self.assertIn('I+BAD', keys_of(variant.courses))

    def test_a_seat_is_exempt_as_from_the_ecosystem_rule(self):
        seats = [{'key': 'I+BAD', 'level': 'Introductory', 'rule': 'flagship', 'reason': ''}]

        variant = shape_cut(eligible(DISHONEST_WINDOW), (2, 0, 0), seats=seats)

        self.assertIn('I+BAD', keys_of(variant.courses))

    def test_ranked_cut_places_no_course_on_a_rung_so_it_does_not_apply(self):
        variant = ranked_cut(eligible(DISHONEST_WINDOW), 2)

        self.assertIn('I+BAD', keys_of(variant.courses))
        self.assertNotIn('level_mismatch', variant.dropped)

    def test_apply_selection_refuses_it_under_a_quota(self):
        quota = {LEVEL_INTRODUCTORY: 1, LEVEL_INTERMEDIATE: 0, LEVEL_ADVANCED: 1}

        courses, dropped, _ = apply_selection(
            eligible(DISHONEST_WINDOW), ['I+BAD', 'A+BAD', 'I+1', 'A+1'], max_size=2, level_quota=quota,
        )

        self.assertEqual(sorted(keys_of(courses)), ['A+1', 'I+1'])
        self.assertEqual(dropped, {'level_mismatch': 2})

    def test_apply_selection_without_a_quota_does_not(self):
        courses, dropped, _ = apply_selection(eligible(DISHONEST_WINDOW), ['I+BAD', 'A+BAD'], max_size=2)

        self.assertEqual(keys_of(courses), ['I+BAD', 'A+BAD'])
        self.assertEqual(dropped, {})

    def test_it_is_a_repairable_drop(self):
        self.assertIn('level_mismatch', REPAIRABLE_DROPS)

    def test_shape_pick_is_held_to_it(self):
        """``shape_pick`` has no repair round: the refusal is recorded, never quietly kept."""
        backend = FakeBackend(content=json.dumps({'keys': ['I+BAD', 'I+1']}))

        variant = model_select(
            strategy=STRATEGY_SHAPE_PICK, requested_size=2, shape=(2, 0, 0), career_name='Project Manager',
            career_skills=[], candidate_dicts=DISHONEST_WINDOW, eligible=eligible(DISHONEST_WINDOW),
            trace_id='t', backend=backend,
        )

        self.assertEqual(keys_of(variant.courses), ['I+1'])
        self.assertEqual(variant.dropped, {'level_mismatch': 1})

    def test_a_refused_pick_triggers_the_repair_round_which_shows_only_honest_courses(self):
        backend = SequencedBackend([json.dumps({'keys': ['I+BAD', 'I+1']}), json.dumps({'keys': ['I+2']})])

        variant = model_select(
            strategy=STRATEGY_SHAPE_PICK_V2, requested_size=2, shape=(2, 0, 0), career_name='Project Manager',
            career_skills=[], candidate_dicts=DISHONEST_WINDOW, eligible=eligible(DISHONEST_WINDOW),
            trace_id='t', backend=backend,
        )

        self.assertTrue(variant.is_complete)
        self.assertEqual(sorted(keys_of(variant.courses)), ['I+1', 'I+2'])
        self.assertEqual(variant.dropped, {'level_mismatch': 1})
        self.assertEqual(variant.repair['added'], 1)
        first = [c['key'] for c in json.loads(backend.calls[0]['user_content'])['candidates']]
        repair = [c['key'] for c in json.loads(backend.calls[1]['user_content'])['candidates']]
        self.assertIn('I+BAD', first)
        self.assertNotIn('I+BAD', repair)

    def test_the_repair_round_is_held_to_it_whatever_it_returns(self):
        backend = SequencedBackend([json.dumps({'keys': ['I+BAD', 'I+1']}), json.dumps({'keys': ['I+BAD']})])

        variant = model_select(
            strategy=STRATEGY_SHAPE_PICK_V2, requested_size=2, shape=(2, 0, 0), career_name='Project Manager',
            career_skills=[], candidate_dicts=DISHONEST_WINDOW, eligible=eligible(DISHONEST_WINDOW),
            trace_id='t', backend=backend,
        )

        self.assertEqual(keys_of(variant.courses), ['I+1'])
        self.assertEqual(variant.repair['added'], 0)

    def test_build_variants_can_turn_it_off(self):
        on = build_variants(career_name='PM', career_skills=[], ordered_candidates=DISHONEST_WINDOW, sizes=[],
                            strategies=[STRATEGY_SHAPE_CUT], shapes=['2/2/1'], trace_prefix='t')
        off = build_variants(career_name='PM', career_skills=[], ordered_candidates=DISHONEST_WINDOW, sizes=[],
                             strategies=[STRATEGY_SHAPE_CUT], shapes=['2/2/1'], trace_prefix='t',
                             level_honesty=False)

        self.assertNotIn('I+BAD', keys_of(on[0].courses))
        self.assertIn('I+BAD', keys_of(off[0].courses))
