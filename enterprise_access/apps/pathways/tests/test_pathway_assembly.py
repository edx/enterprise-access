"""
Tests for pathway assembly and the Tier 1 correctness gates.

The assembly cases are written against the *measured* defects rather than invented ones:
the "all five introductory" and "all five from one partner" fixtures reproduce what
``data analyst`` and ``biomedical engineer`` actually returned from the pinned 2U catalog.
"""
import ddt
from django.test import TestCase

from enterprise_access.apps.pathways.pathway_assembly import (
    DEFAULT_LEVEL_QUOTA,
    LEVEL_ADVANCED,
    LEVEL_INTERMEDIATE,
    LEVEL_INTRODUCTORY,
    MAX_PER_PARTNER,
    PATHWAY_SIZE,
    Candidate,
    assemble_pathway,
    eligible_candidates,
    validate_pathway
)


def hit(key, *, title='A Course', level=LEVEL_INTRODUCTORY, partner='edX', language='English'):
    """Build a catalog hit in the shape Algolia returns."""
    return {
        'key': key,
        'title': title,
        'level_type': level,
        'partners': [{'name': partner}],
        'language': language,
    }


def hits_all_introductory(count=8):
    """Reproduces the reported defect: many intro courses, no higher rung."""
    return [
        hit(f'Org{i}+C{i}', title=f'Introduction to Thing {i}', partner=f'Partner{i % 3}')
        for i in range(count)
    ]


def hits_spanning_levels():
    """A candidate set where relevance puts the intro courses first."""
    return [
        hit('A+1', title='Introduction to Data', partner='Microsoft'),
        hit('A+2', title='Data Basics', partner='Microsoft'),
        hit('A+3', title='More Data', partner='Microsoft'),
        hit('B+1', title='Applied Data', level=LEVEL_INTERMEDIATE, partner='IBM'),
        hit('B+2', title='Data Engineering', level=LEVEL_INTERMEDIATE, partner='Delft'),
        hit('C+1', title='Data Capstone', level=LEVEL_ADVANCED, partner='IBM'),
    ]


@ddt.ddt
class TestEligibleCandidates(TestCase):
    """
    Tests for ``eligible_candidates``.
    """

    def test_valid_hits_survive(self):
        candidates, ineligible = eligible_candidates([hit('IBM+DA0101EN')])

        self.assertEqual([candidate.key for candidate in candidates], ['IBM+DA0101EN'])
        self.assertEqual(ineligible, {})

    @ddt.data('course-v1:IBM+DA0101EN+1T2024', 'no-plus-sign', '', None)
    def test_keys_that_are_not_course_keys_are_rejected(self, bad_key):
        candidates, ineligible = eligible_candidates([hit(bad_key)])

        self.assertEqual(candidates, [])
        self.assertEqual(ineligible['invalid_course_key'], 1)

    def test_non_english_courses_are_rejected(self):
        """
        26% of the pinned catalog is taught in another language, and it appears from rank
        6 -- exactly where reaching for level diversity looks.
        """
        candidates, ineligible = eligible_candidates([
            hit('A+1'),
            hit('B+1', title='Programación en Python', language='Spanish'),
        ])

        self.assertEqual([candidate.key for candidate in candidates], ['A+1'])
        self.assertEqual(ineligible['unsupported_language'], 1)

    def test_a_course_with_no_language_is_not_rejected(self):
        """Absent metadata is not evidence of a non-English course."""
        candidates, _ = eligible_candidates([hit('A+1', language='')])

        self.assertEqual(len(candidates), 1)

    def test_repeated_keys_collapse_to_one(self):
        candidates, ineligible = eligible_candidates([hit('A+1'), hit('A+1')])

        self.assertEqual(len(candidates), 1)
        self.assertEqual(ineligible['duplicate_key'], 1)


@ddt.ddt
class TestAssemblePathway(TestCase):
    """
    Tests for ``assemble_pathway``.
    """

    def test_the_quota_is_satisfied_when_the_rungs_are_populated(self):
        assembly = assemble_pathway(hits_spanning_levels())

        self.assertTrue(assembly.is_complete)
        self.assertEqual(assembly.realised_level_mix, DEFAULT_LEVEL_QUOTA)
        self.assertEqual(assembly.unfilled_rungs, [])

    def test_relevance_order_still_decides_which_course_fills_a_rung(self):
        """The quota decides how many, not which -- so the first intro hit is kept."""
        assembly = assemble_pathway(hits_spanning_levels())

        intro_keys = [c.key for c in assembly.courses if c.level_type == LEVEL_INTRODUCTORY]
        self.assertEqual(intro_keys, ['A+1', 'A+2'])

    def test_an_all_introductory_candidate_set_still_returns_five(self):
        """
        The measured `data analyst` case. There is no ladder to build, so the pathway
        degrades to five intro courses and records which rungs went unfilled -- it must
        not return two courses.
        """
        assembly = assemble_pathway(hits_all_introductory())

        self.assertTrue(assembly.is_complete)
        self.assertEqual(assembly.realised_level_mix[LEVEL_INTRODUCTORY], PATHWAY_SIZE)
        self.assertCountEqual(assembly.unfilled_rungs, [LEVEL_INTERMEDIATE, LEVEL_ADVANCED])

    def test_no_provider_can_supply_more_than_the_cap(self):
        """The measured `biomedical engineer` case: 5 of 5 from a single partner."""
        assembly = assemble_pathway([
            hit(f'Solo+{i}', title=f'Course {i}', partner='OnlyPartner') for i in range(8)
        ] + [
            hit('Other+1', partner='Second'), hit('Other+2', partner='Third'),
            hit('Other+3', partner='Fourth'),
        ])

        counts = {}
        for course in assembly.courses:
            counts[course.partner] = counts.get(course.partner, 0) + 1
        self.assertLessEqual(max(counts.values()), MAX_PER_PARTNER)
        self.assertGreaterEqual(len(counts), 3)

    def test_the_scarcest_rung_claims_provider_capacity_first(self):
        """
        The measured `Data Analyst` case: 17 candidates spanning all three rungs still
        assembled to 5/0/0, because the Introductory picks used up one provider's entire
        allowance and every Intermediate candidate belonged to that provider.
        """
        assembly = assemble_pathway([
            # Plentiful introductory rung, all from one provider plus filler.
            hit('A+1', title='Intro 1', partner='IBM'),
            hit('A+2', title='Intro 2', partner='IBM'),
            hit('A+3', title='Intro 3', partner='P2'),
            hit('A+4', title='Intro 4', partner='P3'),
            hit('A+5', title='Intro 5', partner='P4'),
            # Scarce higher rungs, only available from IBM.
            hit('B+1', title='Applied', level=LEVEL_INTERMEDIATE, partner='IBM'),
            hit('C+1', title='Capstone', level=LEVEL_ADVANCED, partner='IBM'),
        ])

        mix = assembly.realised_level_mix
        self.assertTrue(assembly.is_complete)
        self.assertEqual(mix[LEVEL_ADVANCED], 1)
        self.assertEqual(mix[LEVEL_INTERMEDIATE], 1)
        self.assertLessEqual(max(
            sum(1 for c in assembly.courses if c.partner == p)
            for p in {c.partner for c in assembly.courses}
        ), MAX_PER_PARTNER)

    def test_relevance_order_is_still_respected_inside_a_rung(self):
        """Scarcest-first reorders the rungs, never the candidates within one."""
        assembly = assemble_pathway(hits_spanning_levels())

        intro = [c.key for c in assembly.courses if c.level_type == LEVEL_INTRODUCTORY]
        self.assertEqual(intro, ['A+1', 'A+2'])

    def test_courses_are_ordered_easiest_first(self):
        assembly = assemble_pathway(hits_spanning_levels())

        levels = [course.level_type for course in assembly.courses]
        self.assertEqual(levels, [
            LEVEL_INTRODUCTORY, LEVEL_INTRODUCTORY,
            LEVEL_INTERMEDIATE, LEVEL_INTERMEDIATE,
            LEVEL_ADVANCED,
        ])

    def test_title_cues_break_ties_within_a_rung(self):
        """
        `level_type` is 19-36% unreliable, so a title that clearly reads as introductory
        sorts ahead of one that reads as advanced at the same tagged level.
        """
        assembly = assemble_pathway([
            hit('A+1', title='Advanced Widgets', partner='P1'),
            hit('A+2', title='Introduction to Widgets', partner='P2'),
            hit('B+1', title='Applied Widgets', level=LEVEL_INTERMEDIATE, partner='P3'),
            hit('B+2', title='Widget Systems', level=LEVEL_INTERMEDIATE, partner='P4'),
            hit('C+1', title='Widget Capstone', level=LEVEL_ADVANCED, partner='P5'),
        ])

        self.assertEqual(assembly.courses[0].key, 'A+2')

    def test_too_few_eligible_candidates_yields_an_incomplete_assembly(self):
        """Never pad. The caller turns this into an explicit no-pathway."""
        assembly = assemble_pathway([hit('A+1'), hit('B+1')])

        self.assertFalse(assembly.is_complete)
        self.assertEqual(len(assembly.courses), 2)

    def test_ineligible_reasons_survive_onto_the_assembly(self):
        """
        "Retrieval found little" and "retrieval found plenty, all unusable" need different
        fixes, so the assembly has to be able to tell them apart.
        """
        assembly = assemble_pathway([
            hit('A+1'),
            hit('course-v1:B+2+1T2024'),
            hit('C+3', language='Spanish'),
        ])

        self.assertEqual(assembly.ineligible['invalid_course_key'], 1)
        self.assertEqual(assembly.ineligible['unsupported_language'], 1)

    def test_unattributed_courses_are_not_counted_against_one_provider(self):
        assembly = assemble_pathway([
            hit(f'A+{i}', title=f'Course {i}', partner='') for i in range(6)
        ])

        self.assertTrue(assembly.is_complete)

    def test_an_empty_candidate_set_is_not_an_error(self):
        assembly = assemble_pathway([])

        self.assertFalse(assembly.is_complete)
        self.assertEqual(assembly.courses, [])


class TestValidatePathway(TestCase):
    """
    Tests for the Tier 1 correctness gates.
    """

    def valid_courses(self):
        return [
            Candidate(key='A+1', title='Intro', level_type=LEVEL_INTRODUCTORY, partner='P1', language='English'),
            Candidate(key='A+2', title='Intro 2', level_type=LEVEL_INTRODUCTORY, partner='P2', language='English'),
            Candidate(key='B+1', title='Applied', level_type=LEVEL_INTERMEDIATE, partner='P3', language='English'),
            Candidate(key='B+2', title='More', level_type=LEVEL_INTERMEDIATE, partner='P4', language='English'),
            Candidate(key='C+1', title='Capstone', level_type=LEVEL_ADVANCED, partner='P5', language='English'),
        ]

    def test_a_well_formed_pathway_has_no_violations(self):
        self.assertEqual(validate_pathway(self.valid_courses()), [])

    def test_a_short_pathway_is_a_violation(self):
        violations = validate_pathway(self.valid_courses()[:4])

        self.assertIn('exactly 5 courses, got 4', violations[0])

    def test_a_run_key_is_a_violation(self):
        courses = self.valid_courses()
        courses[0] = Candidate(key='course-v1:A+1+1T2024', language='English')

        violations = validate_pathway(courses)

        self.assertTrue(any('not a valid catalog course key' in v for v in violations))

    def test_a_repeated_key_is_a_violation(self):
        courses = self.valid_courses()
        courses[1] = courses[0]

        violations = validate_pathway(courses)

        self.assertTrue(any('appears more than once' in v for v in violations))

    def test_a_non_english_course_is_a_violation(self):
        courses = self.valid_courses()
        courses[0] = Candidate(key='A+1', partner='P1', language='Spanish')

        violations = validate_pathway(courses)

        self.assertTrue(any("taught in 'Spanish'" in v for v in violations))

    def test_provider_concentration_is_a_violation(self):
        courses = [
            Candidate(key=f'A+{i}', partner='OnlyPartner', language='English')
            for i in range(PATHWAY_SIZE)
        ]

        violations = validate_pathway(courses)

        self.assertTrue(any('exceeds the cap' in v for v in violations))

    def test_catalog_membership_is_checked_when_the_key_set_is_supplied(self):
        courses = self.valid_courses()

        violations = validate_pathway(courses, customer_catalog_keys={'A+1', 'A+2', 'B+1', 'B+2'})

        self.assertEqual(
            violations, ["'C+1' is not in the pinned customer catalog"],
        )

    def test_catalog_membership_is_skipped_rather_than_assumed_when_unknown(self):
        """
        Proving membership needs a browse-scoped key (Open Decision 6). Until then the
        gate must not quietly pass.
        """
        self.assertEqual(validate_pathway(self.valid_courses(), customer_catalog_keys=None), [])

    def test_an_assembled_pathway_passes_its_own_gates(self):
        """The two halves of the module must agree; a caught regression here is real."""
        assembly = assemble_pathway(hits_spanning_levels())

        self.assertEqual(validate_pathway(assembly.courses), [])
