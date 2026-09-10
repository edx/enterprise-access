"""
Resolving skill names onto the catalog's actual facet vocabulary.

The problem this solves, measured against the production catalog index on 2026-09-09:

===========================  =====  ================================  =====
Term a learner/model writes  Hits   What the catalog actually holds   Hits
===========================  =====  ================================  =====
``Python``                       0  ``Python (Programming Language)``    95
``SQL``                          0  ``SQL (Programming Language)``       42
``Java``                         0  ``Java (Programming Language)``      27
``Excel``                        0  ``Microsoft Excel``                  26
===========================  =====  ================================  =====

The catalog's ``skill_names`` are Lightcast-canonical, so the short name anyone would
actually write is frequently **absent from the vocabulary entirely**. Exact-match
grounding therefore drops the most in-demand technical skills silently — they do not
produce bad results, they produce a dropped filter.

The retrieval diagnostic established this is the binding constraint: against
product-authored ground truth, recall@20 was 12% by default and 23% even after widening
the query with ``removeWordsIfNoResults=allOptional`` -- and 0% for every technology
persona under both settings. Query construction is worth real recall, but it does not
reach the courses whose skills were never expressible in the first place.

Two things this deliberately is not
-----------------------------------
**Not a hand-maintained alias map.** Only 96 of 1,492 sampled vocabulary values carry a
parenthetical qualifier, so there is no small rule set to encode, and the taxonomy took
thousands of skill updates in a single month — a static map would rot.

**Not a model call.** Resolving ``Python`` to ``Python (Programming Language)`` is a
vocabulary lookup, not a judgement. The vocabulary is queryable, so ask it.

Instead: match candidate terms against the real vocabulary, exactly where possible and by
*whole-word* containment otherwise, with the match type recorded so a caller can decide
how much to trust each one. Candidates can be widened via Algolia's facet-search endpoint
(``AlgoliaSearchClient.search_facet_values``), but every candidate is still validated
against the scoped vocabulary before use — facet search is unfiltered, so its counts and
membership say nothing about a particular enterprise's catalog.
"""
import logging
import re
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# Catalog fields that hold skill facet values, in priority order. ``skill_names`` wins a
# collision, matching the MFE's own precedence in `catalogSkillTranslation.ts`.
SKILL_FACET_FIELDS = ('skill_names', 'skills.name')

# Terms shorter than this are not resolved by containment: two- and three-letter strings
# match far too much (``JS`` matches ``JSON``, ``ML`` matches nothing useful) and an
# over-eager expansion is worse than a dropped filter.
MIN_CONTAINMENT_TERM_LENGTH = 4


class MatchType(Enum):
    """
    How a term was matched, in descending order of confidence.

    Recorded rather than discarded because the caller's tolerance differs: a strict facet
    filter should only use high-confidence matches, while a soft boost can afford a
    weaker one.
    """

    EXACT = 'exact'
    #: The vocabulary value is the term plus a parenthetical qualifier --
    #: ``Python`` -> ``Python (Programming Language)``. Very high confidence.
    QUALIFIED = 'qualified'
    #: The term appears as a whole word inside the value -- ``Excel`` ->
    #: ``Microsoft Excel``. Good, but capable of drifting (``Azure`` ->
    #: ``Azure Machine Learning``), so ranked by how much extra the value carries.
    CONTAINED = 'contained'

    @property
    def is_high_confidence(self):
        """Whether a match of this type is safe to use as a hard facet filter."""
        return self in (MatchType.EXACT, MatchType.QUALIFIED)


@dataclass(frozen=True)
class SkillMatch:
    """One resolved skill: the term asked for, and the catalog value to actually query."""

    term: str
    catalog_value: str
    catalog_field: str
    match_type: MatchType

    @property
    def is_high_confidence(self):
        return self.match_type.is_high_confidence


def normalize_term(value):
    """Casefold and collapse whitespace for comparison. Never used as a query value."""
    return ' '.join((value or '').split()).casefold()


def _qualified_pattern(term):
    """Match ``<term> (<qualifier>)`` — the canonical Lightcast disambiguation shape."""
    return re.compile(rf'^{re.escape(term)}\s*\([^)]+\)$', re.IGNORECASE)


def _whole_word_pattern(term):
    r"""
    Match ``term`` as a whole word anywhere in a value.

    Whole-word is what keeps ``JS`` from matching ``JSON`` and ``Java`` from matching
    ``JavaScript``. ``\b`` is unreliable next to ``+``, ``#`` and ``.`` (``C++``, ``C#``,
    ``Node.js``), so the boundaries are spelled out as "not a word character".
    """
    escaped = re.escape(term)
    return re.compile(rf'(?<![^\W_]){escaped}(?![^\W_])', re.IGNORECASE)


class VocabularyIndex:
    """
    A searchable view of one catalog's skill facet vocabulary.

    Built from a facet snapshot — the real values present in the *scoped* catalog — so a
    resolution can never invent a value that would return zero hits.
    """

    def __init__(self, facet_snapshot):
        """
        Args:
            facet_snapshot: Mapping of facet field name to its values, e.g.
                ``{'skill_names': [...], 'skills.name': [...]}``.
        """
        self._by_normalized = {}
        self._values = []
        for facet_field in SKILL_FACET_FIELDS:
            for value in facet_snapshot.get(facet_field) or []:
                if not value:
                    continue
                normalized = normalize_term(value)
                # First field wins a collision, so skill_names takes precedence.
                if normalized not in self._by_normalized:
                    self._by_normalized[normalized] = (value, facet_field)
                self._values.append((value, facet_field))

    def __len__(self):
        return len(self._by_normalized)

    @property
    def values(self):
        """Every ``(value, facet_field)`` pair, in snapshot order."""
        return list(self._values)

    def exact(self, term):
        """Return ``(value, facet_field)`` for an exact case-insensitive hit, or ``None``."""
        return self._by_normalized.get(normalize_term(term))

    def resolve(self, term):
        """
        Resolve one term to the best available catalog value.

        Order of preference: exact, then ``term (qualifier)``, then whole-word
        containment. Qualified candidates are ranked shortest-first; containment
        candidates are ranked by position (see ``_containment_rank``), because a term at
        the end of a value names the same concept while a term followed by more words
        names a narrower one.

        Returns:
            A ``SkillMatch``, or ``None`` if the vocabulary cannot serve this term.
        """
        cleaned = ' '.join((term or '').split())
        if not cleaned:
            return None

        exact = self.exact(cleaned)
        if exact:
            value, facet_field = exact
            return SkillMatch(cleaned, value, facet_field, MatchType.EXACT)

        qualified = _qualified_pattern(cleaned)
        candidates = [
            (value, facet_field) for value, facet_field in self._values
            if qualified.match(value)
        ]
        if candidates:
            value, facet_field = min(candidates, key=lambda pair: (len(pair[0]), pair[0]))
            return SkillMatch(cleaned, value, facet_field, MatchType.QUALIFIED)

        # Containment is the loosest rule, so it is withheld from very short terms.
        if len(cleaned) < MIN_CONTAINMENT_TERM_LENGTH:
            return None

        whole_word = _whole_word_pattern(cleaned)
        candidates = [
            (value, facet_field) for value, facet_field in self._values
            if whole_word.search(value)
        ]
        if candidates:
            value, facet_field = min(candidates, key=lambda pair: self._containment_rank(cleaned, pair[0]))
            return SkillMatch(cleaned, value, facet_field, MatchType.CONTAINED)

        return None

    @staticmethod
    def _containment_rank(term, value):
        """
        Rank a containment candidate; lower is better.

        Position matters more than length, because it distinguishes *the same concept,
        vendor-qualified* from *a different, narrower concept*:

        * ``Microsoft Excel`` ends with the term — Excel is the thing being qualified, so
          this is the canonical name for what was asked for.
        * ``Excel Macros`` leads with the term — macros is the thing, Excel qualifies it,
          so this is a narrower sibling skill.

        Ranking on length alone gets this backwards, since ``Excel Macros`` (12 chars) is
        shorter than ``Microsoft Excel`` (15). Among candidates in the same position
        class, the shortest still wins: it carries the least extra meaning, which is what
        picks ``Microsoft Azure`` over ``Azure Machine Learning``.
        """
        ends_with_term = value.casefold().endswith(term.casefold())
        return (0 if ends_with_term else 1, len(value), value)


@dataclass
class ResolutionResult:
    """
    What resolving a set of terms produced.

    ``unresolved`` is a first-class part of the result rather than a logged aside: the
    whole defect being fixed is that dropped terms were invisible. A caller that wants to
    know how much of a career's skill set the catalog can express reads this.
    """

    matches: list
    unresolved: list

    @property
    def high_confidence_matches(self):
        """Matches safe to use as hard facet filters."""
        return [match for match in self.matches if match.is_high_confidence]

    @property
    def resolution_rate(self):
        """Fraction of input terms that resolved to a real catalog value."""
        total = len(self.matches) + len(self.unresolved)
        if not total:
            return None
        return len(self.matches) / total

    def to_dict(self):
        """A JSON-serialisable form, for persisting on a workflow step record."""
        return {
            'matches': [
                {
                    'term': match.term,
                    'catalog_value': match.catalog_value,
                    'catalog_field': match.catalog_field,
                    'match_type': match.match_type.value,
                }
                for match in self.matches
            ],
            'unresolved': list(self.unresolved),
            'resolution_rate': self.resolution_rate,
        }


def resolve_skill_terms(terms, facet_snapshot):
    """
    Resolve skill terms onto a catalog's real facet vocabulary.

    Duplicate terms (after normalisation) are resolved once, and two terms resolving to
    the same catalog value yield one match — a repeated facet filter narrows nothing and
    only costs query length.

    Args:
        terms: Iterable of skill name strings, e.g. from a career's ``skills.name``.
        facet_snapshot: Mapping of facet field to values, from the scoped catalog.

    Returns:
        ResolutionResult
    """
    index = VocabularyIndex(facet_snapshot)

    matches = []
    unresolved = []
    seen_terms = set()
    seen_values = set()

    for term in terms or []:
        cleaned = ' '.join((term or '').split())
        if not cleaned:
            continue
        normalized = normalize_term(cleaned)
        if normalized in seen_terms:
            continue
        seen_terms.add(normalized)

        match = index.resolve(cleaned)
        if match is None:
            unresolved.append(cleaned)
            continue
        if match.catalog_value in seen_values:
            continue
        seen_values.add(match.catalog_value)
        matches.append(match)

    if unresolved:
        logger.info(
            'Skill resolution dropped %d of %d term(s) absent from the catalog vocabulary: %s',
            len(unresolved), len(unresolved) + len(matches), unresolved,
        )

    return ResolutionResult(matches=matches, unresolved=unresolved)
