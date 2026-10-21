"""
Tests for skill-name resolution against the catalog facet vocabulary.
"""
import ddt
from django.test import TestCase

from enterprise_access.apps.pathways.skill_vocabulary import (
    MatchType,
    VocabularyIndex,
    normalize_term,
    resolve_skill_terms
)

# Verbatim `skill_names` facet values observed in the production catalog index on
# 2026-09-09. Using real values matters: the whole defect is that the vocabulary is not
# shaped the way a developer would guess.
REAL_VOCABULARY = {
    'skill_names': [
        'Python (Programming Language)',
        'SQL (Programming Language)',
        'Java (Programming Language)',
        'JavaScript (Programming Language)',
        'Microsoft Excel',
        'Excel Macros',
        'Excel Formulas',
        'Tableau (Business Intelligence Software)',
        'Microsoft Azure',
        'Azure Machine Learning',
        'Docker (Software)',
        'Docker Container',
        'Pandas (Python Package)',
        'NumPy (Python Package)',
        'Data Analysis',
        'Machine Learning',
        'Project Management',
        'Nursing',
        'Welding',
        'Power BI',
        'JSON',
        'Kubernetes',
    ],
    'skills.name': [
        'Communication',
        'Data Analysis',
    ],
}


@ddt.ddt
class TestVocabularyIndex(TestCase):
    """
    Tests for ``VocabularyIndex``.
    """

    def setUp(self):
        super().setUp()
        self.index = VocabularyIndex(REAL_VOCABULARY)

    def test_index_dedupes_across_facet_fields_preferring_skill_names(self):
        """``skill_names`` wins a collision, matching the MFE's precedence."""
        match = self.index.resolve('Data Analysis')

        self.assertEqual(match.catalog_field, 'skill_names')
        # 'Data Analysis' appears in both fields but is indexed once.
        self.assertEqual(
            len(self.index),
            len(set(v.casefold() for v in
                    REAL_VOCABULARY['skill_names'] + REAL_VOCABULARY['skills.name'])),
        )

    # -- the failures this module exists to fix ---------------------------------------

    @ddt.data(
        ('Python', 'Python (Programming Language)'),
        ('SQL', 'SQL (Programming Language)'),
        ('Java', 'Java (Programming Language)'),
        ('Tableau', 'Tableau (Business Intelligence Software)'),
        ('Docker', 'Docker (Software)'),
    )
    @ddt.unpack
    def test_short_names_resolve_to_their_qualified_form(self, term, expected):
        """These all returned zero hits under exact-match grounding."""
        match = self.index.resolve(term)

        self.assertEqual(match.catalog_value, expected)
        self.assertEqual(match.match_type, MatchType.QUALIFIED)
        self.assertTrue(match.is_high_confidence)

    def test_java_does_not_resolve_to_javascript(self):
        """
        The dangerous near-miss: ``Java (Programming Language)`` and ``JavaScript
        (Programming Language)`` are both present and, in the live index, have almost
        identical counts. Whole-word matching is what separates them.
        """
        self.assertEqual(
            self.index.resolve('Java').catalog_value,
            'Java (Programming Language)',
        )
        self.assertEqual(
            self.index.resolve('JavaScript').catalog_value,
            'JavaScript (Programming Language)',
        )

    def test_python_does_not_resolve_to_a_python_package(self):
        """``Pandas (Python Package)`` contains 'Python' but is a different concept."""
        match = self.index.resolve('Python')

        self.assertEqual(match.catalog_value, 'Python (Programming Language)')

    @ddt.data(
        ('Excel', 'Microsoft Excel'),
        ('Azure', 'Microsoft Azure'),
    )
    @ddt.unpack
    def test_vendor_prefixed_names_resolve_by_containment(self, term, expected):
        """
        ``Excel`` is a whole-word *suffix* of ``Microsoft Excel``, not a prefix, so it
        needs the containment rule. Shortest containing value wins, which is what picks
        ``Microsoft Azure`` over ``Azure Machine Learning``.
        """
        match = self.index.resolve(term)

        self.assertEqual(match.catalog_value, expected)
        self.assertEqual(match.match_type, MatchType.CONTAINED)
        # Containment is the loosest rule, so it must not be trusted as a hard filter.
        self.assertFalse(match.is_high_confidence)

    # -- exact matches ----------------------------------------------------------------

    @ddt.data('Data Analysis', 'Machine Learning', 'Project Management', 'Nursing',
              'Welding', 'Power BI', 'Kubernetes')
    def test_already_canonical_names_match_exactly(self, term):
        """Most of the vocabulary needs no resolution; those must not be perturbed."""
        match = self.index.resolve(term)

        self.assertEqual(match.catalog_value, term)
        self.assertEqual(match.match_type, MatchType.EXACT)

    @ddt.data('python (programming language)', 'PYTHON (PROGRAMMING LANGUAGE)',
              '  Python (Programming Language)  ')
    def test_exact_match_is_case_and_whitespace_insensitive(self, term):
        match = self.index.resolve(term)

        self.assertEqual(match.catalog_value, 'Python (Programming Language)')
        self.assertEqual(match.match_type, MatchType.EXACT)

    # -- guardrails -------------------------------------------------------------------

    @ddt.data('JS', 'ML', 'AI', 'R')
    def test_very_short_terms_are_not_expanded_by_containment(self, term):
        """
        ``JS`` would otherwise match ``JSON``. An over-eager expansion is worse than a
        dropped filter: it silently searches for the wrong thing.
        """
        self.assertIsNone(self.index.resolve(term))

    @ddt.data('Underwater Basket Weaving', 'Quantum Blockchain Synergy')
    def test_absent_terms_resolve_to_nothing(self, term):
        self.assertIsNone(self.index.resolve(term))

    @ddt.data('', '   ', None)
    def test_empty_terms_resolve_to_nothing(self, term):
        self.assertIsNone(self.index.resolve(term))

    def test_empty_vocabulary_resolves_nothing(self):
        empty = VocabularyIndex({})

        self.assertEqual(len(empty), 0)
        self.assertIsNone(empty.resolve('Python'))

    def test_vocabulary_with_falsy_values_is_tolerated(self):
        index = VocabularyIndex({'skill_names': ['Python (Programming Language)', '', None]})

        self.assertEqual(len(index), 1)


@ddt.ddt
class TestResolveSkillTerms(TestCase):
    """
    Tests for ``resolve_skill_terms``.
    """

    def test_resolution_reports_matches_and_what_it_dropped(self):
        result = resolve_skill_terms(
            ['Python', 'SQL', 'Underwater Basket Weaving'],
            REAL_VOCABULARY,
        )

        self.assertEqual(
            [match.catalog_value for match in result.matches],
            ['Python (Programming Language)', 'SQL (Programming Language)'],
        )
        # Dropped terms are part of the result, not a log line -- invisibility of dropped
        # terms is the defect being fixed.
        self.assertEqual(result.unresolved, ['Underwater Basket Weaving'])
        self.assertAlmostEqual(result.resolution_rate, 2 / 3)

    def test_high_confidence_matches_exclude_containment(self):
        result = resolve_skill_terms(['Python', 'Excel'], REAL_VOCABULARY)

        self.assertEqual(len(result.matches), 2)
        self.assertEqual(
            [match.catalog_value for match in result.high_confidence_matches],
            ['Python (Programming Language)'],
        )

    def test_duplicate_terms_are_resolved_once(self):
        result = resolve_skill_terms(['Python', 'python', 'PYTHON  '], REAL_VOCABULARY)

        self.assertEqual(len(result.matches), 1)

    def test_two_terms_resolving_to_one_value_yield_one_match(self):
        """A repeated facet filter narrows nothing and only costs query length."""
        result = resolve_skill_terms(
            ['Python', 'Python (Programming Language)'],
            REAL_VOCABULARY,
        )

        self.assertEqual(len(result.matches), 1)

    @ddt.data(None, [], ['', '  '])
    def test_no_usable_input_produces_an_empty_result(self, terms):
        result = resolve_skill_terms(terms, REAL_VOCABULARY)

        self.assertEqual(result.matches, [])
        self.assertEqual(result.unresolved, [])
        self.assertIsNone(result.resolution_rate)

    def test_everything_unresolved_is_reported_as_a_zero_rate(self):
        """
        A career whose whole skill set is absent must be distinguishable from one that
        was never resolved -- rate 0.0, not None.
        """
        result = resolve_skill_terms(['Nonsense Skill', 'Another Nonsense'], REAL_VOCABULARY)

        self.assertEqual(result.matches, [])
        self.assertEqual(result.resolution_rate, 0.0)

    def test_result_serializes_for_a_step_record(self):
        result = resolve_skill_terms(['Python', 'Excel', 'Nonsense'], REAL_VOCABULARY)

        payload = result.to_dict()

        self.assertEqual(payload['unresolved'], ['Nonsense'])
        self.assertEqual(payload['matches'][0], {
            'term': 'Python',
            'catalog_value': 'Python (Programming Language)',
            'catalog_field': 'skill_names',
            'match_type': 'qualified',
        })
        self.assertAlmostEqual(payload['resolution_rate'], 2 / 3)

    def test_a_real_career_skill_set_resolves(self):
        """
        The skills carried by ``IBM+DA0101EN`` in the live index -- an end-to-end shape
        check on realistic input rather than hand-picked terms.
        """
        career_skills = [
            'Machine Learning', 'Data Analysis', 'Basic Math', 'SciPy',
            'Data Visualization', 'Scikit-Learn (Python Package)',
            'NumPy (Python Package)', 'Pandas (Python Package)',
        ]

        result = resolve_skill_terms(career_skills, REAL_VOCABULARY)

        self.assertIn('Machine Learning', [m.catalog_value for m in result.matches])
        self.assertIn('NumPy (Python Package)', [m.catalog_value for m in result.matches])
        # Skills genuinely absent from this vocabulary are reported, not silently dropped.
        self.assertIn('SciPy', result.unresolved)


class TestNormalizeTerm(TestCase):
    """
    Tests for ``normalize_term``.
    """

    def test_casefolds_and_collapses_whitespace(self):
        self.assertEqual(normalize_term('  Python   (Programming  Language) '),
                         'python (programming language)')

    def test_handles_none(self):
        self.assertEqual(normalize_term(None), '')
