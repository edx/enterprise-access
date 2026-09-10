"""
Domain-layer API for translating a career's vocabulary into the catalog's.

Careers come from the Lightcast jobs index; courses come from the catalog index. This is
the join between them, and it is where the pipeline's measured quality problem lives.

Three stages, deliberately separated because each has a different cost and failure mode:

1. ``snapshot_catalog_facets`` — one zero-hit search that reads the scoped catalog's skill
   facet vocabulary. Cheap, and the *only* authority on what will actually match.
2. ``translate_skills`` — pure resolution against that snapshot, no network. Recovers the
   canonical form of a skill (``Python`` → ``Python (Programming Language)``) without a
   model call or a maintained alias map. See ``skill_vocabulary``.
3. ``refine_unmatched_skills`` — a *conditional* second pass over Algolia's facet-search
   endpoint, for terms the snapshot could not serve.

Why stage 3 exists, and why it is conditional
---------------------------------------------
A facet snapshot is capped at ``maxValuesPerFacet`` (1,000), and the live ``skill_names``
facet returns exactly 1,000 values — i.e. it is truncated. Measured consequence: the
resolver dropped ``Welding`` and ``AWS`` even though ``skill_names:Welding`` matches a
real course, because they fall outside the top 1,000 by count. So a snapshot-only design
silently loses the long tail, which is disproportionately the non-technology vocabulary.

Facet search has no such cap, but it costs one request per unresolved term. Most careers
resolve fully from the snapshot, so running it unconditionally would pay for nothing on
the common path — hence ``TranslateToCatalogStep`` gates it with ``should_execute``.

Facet-search results are treated as *candidates only* and re-validated, because that
endpoint is not filtered to the enterprise catalog or to ``content_type:course``, and its
counts are inflated by per-customer record duplication.
"""
import logging
from typing import Any

from enterprise_access.apps.api_client.algolia_client import AlgoliaClientError, AlgoliaSearchClient
from enterprise_access.apps.pathways.skill_vocabulary import (
    SKILL_FACET_FIELDS,
    MatchType,
    SkillMatch,
    VocabularyIndex,
    normalize_term,
    resolve_skill_terms
)

logger = logging.getLogger(__name__)

# Facet fields read from the catalog. ``subjects`` is snapshotted for later use by the
# re-ranker but is never a skill-filter candidate.
CATALOG_FACET_FIELDS = (*SKILL_FACET_FIELDS, 'subjects')

# Algolia's ceiling. Requesting it makes the truncation visible rather than silent: a
# facet that comes back with exactly this many values is almost certainly incomplete.
MAX_VALUES_PER_FACET = 1000

COURSE_SCOPE_FILTER = 'content_type:course'

# Filter budgets, per the plan. Strict values become hard facet filters, so the cap is
# tighter -- each one can only narrow the result set, and an over-constrained query is the
# failure mode the retrieval ladder was invented to paper over.
MAX_STRICT_SKILLS = 8
MAX_BOOST_SKILLS = 12

# How many facet-search candidates to consider per unresolved term.
FACET_SEARCH_HITS_PER_TERM = 20


def snapshot_catalog_facets(
    *,
    secured_key=None,
    allow_unscoped: bool = False,
    algolia_client: AlgoliaSearchClient | None = None,
) -> dict[str, Any]:
    """
    Read the scoped catalog's facet vocabulary with a single zero-hit search.

    ``hitsPerPage: 0`` because only the facets are wanted; the hits would be wasted
    payload. Scoped to ``content_type:course`` so the vocabulary matches what course
    retrieval will actually search — if the snapshot and the search disagree on scope, a
    skill can be "grounded" against a value no course in scope carries.

    Returns a dict of facet field to values, plus a ``truncated`` list naming any facet
    that came back at the cap and is therefore incomplete.

    Raises:
        AlgoliaClientError: On a misconfigured credential or a failed search.
    """
    client = algolia_client or AlgoliaSearchClient()
    response = client.search_catalog_index(
        '',
        secured_key=secured_key,
        allow_unscoped=allow_unscoped,
        filters=COURSE_SCOPE_FILTER,
        hitsPerPage=0,
        facets=list(CATALOG_FACET_FIELDS),
        maxValuesPerFacet=MAX_VALUES_PER_FACET,
    )

    facets = response.get('facets') or {}
    snapshot: dict[str, Any] = {}
    truncated = []
    for facet_field in CATALOG_FACET_FIELDS:
        # Algolia omits a facet entirely when it has no values in scope.
        values = list((facets.get(facet_field) or {}).keys())
        snapshot[facet_field] = values
        if len(values) >= MAX_VALUES_PER_FACET:
            truncated.append(facet_field)

    snapshot['truncated'] = truncated
    if truncated:
        logger.info(
            'Catalog facet snapshot is truncated at %d values for %s; '
            'unresolved terms will need facet search to reach the long tail.',
            MAX_VALUES_PER_FACET, truncated,
        )
    return snapshot


def translate_skills(*, terms: list[str], facet_snapshot: dict[str, Any]) -> dict[str, Any]:
    """
    Resolve skill terms against the snapshot and split them into strict and boost sets.

    High-confidence matches (exact, or a canonical ``term (qualifier)`` form) become
    strict hard filters; weaker containment matches become boosts, because containment can
    drift to a neighbouring concept and a wrong hard filter returns nothing.

    Returns a dict carrying ``strict``, ``boost``, ``unresolved`` and ``resolution_rate``,
    shaped for direct persistence on a step record.
    """
    result = resolve_skill_terms(terms, facet_snapshot)

    strict = [match for match in result.matches if match.is_high_confidence][:MAX_STRICT_SKILLS]
    strict_values = {match.catalog_value for match in strict}
    boost = [
        match for match in result.matches
        if not match.is_high_confidence and match.catalog_value not in strict_values
    ][:MAX_BOOST_SKILLS]

    return {
        'strict': [_match_to_dict(match) for match in strict],
        'boost': [_match_to_dict(match) for match in boost],
        'unresolved': result.unresolved,
        'resolution_rate': result.resolution_rate,
    }


def _match_to_dict(match: SkillMatch) -> dict[str, str]:
    return {
        'term': match.term,
        'catalog_value': match.catalog_value,
        'catalog_field': match.catalog_field,
        'match_type': match.match_type.value,
    }


def refine_unmatched_skills(
    *,
    unresolved: list[str],
    facet_snapshot: dict[str, Any],
    secured_key=None,
    allow_unscoped: bool = False,
    algolia_client: AlgoliaSearchClient | None = None,
) -> dict[str, Any]:
    """
    Recover unresolved terms via Algolia's facet-search endpoint.

    One request per term, so this is worth running only when the snapshot has already
    failed — see the module docstring.

    A candidate is accepted only if it also resolves under the same rules the snapshot
    path uses, applied to the candidate list. That keeps one definition of "this term
    means that catalog value" rather than a looser second one, and it is what stops
    ``AWS`` resolving to ``AWS Certified Solutions Architect Associate`` merely because
    that is the highest-count candidate.

    A term that cannot be recovered stays unresolved. Failures of individual facet
    searches are recorded, not raised: losing one term is better than failing the step.
    """
    client = algolia_client or AlgoliaSearchClient()
    known = {normalize_term(value) for values in
             (facet_snapshot.get(field) or [] for field in SKILL_FACET_FIELDS)
             for value in values}

    recovered = []
    still_unresolved = []
    errors = []

    for term in unresolved:
        candidates, error = _facet_search_candidates(
            client, term, secured_key, allow_unscoped,
        )
        if error:
            errors.append(f'{term}: {error}')
            still_unresolved.append(term)
            continue

        # Candidates already in the snapshot were considered and rejected on the first
        # pass; re-offering them would change the answer for no new information.
        novel = [value for value in candidates if normalize_term(value) not in known]
        match = VocabularyIndex({SKILL_FACET_FIELDS[0]: novel}).resolve(term)
        if match is None:
            still_unresolved.append(term)
            continue
        recovered.append(_match_to_dict(match))

    if recovered:
        logger.info(
            'Facet search recovered %d of %d term(s) missing from the capped snapshot.',
            len(recovered), len(unresolved),
        )

    return {
        'recovered': recovered,
        'unresolved': still_unresolved,
        'errors': errors,
    }


def _facet_search_candidates(client, term, secured_key, allow_unscoped):
    """Return ``(candidate_values, error)`` for one term, never raising."""
    try:
        hits = client.search_facet_values(
            SKILL_FACET_FIELDS[0],
            term,
            secured_key=secured_key,
            allow_unscoped=allow_unscoped,
            max_facet_hits=FACET_SEARCH_HITS_PER_TERM,
        )
    except AlgoliaClientError as exc:
        logger.warning('Facet search failed for term %r: %s', term, exc)
        return [], str(exc)
    return [hit['value'] for hit in hits if hit.get('value')], None


def merge_refinement(translation: dict[str, Any], refinement: dict[str, Any]) -> dict[str, Any]:
    """
    Fold recovered terms into a translation, respecting the original filter budgets.

    Recovered matches are appended rather than interleaved, so a snapshot match always
    outranks a facet-search match for the same budget slot: the snapshot is the only
    source that is definitely in scope.
    """
    merged = dict(translation)
    strict_values = {entry['catalog_value'] for entry in translation['strict']}
    boost_values = {entry['catalog_value'] for entry in translation['boost']}

    strict_additions = [
        entry for entry in refinement['recovered']
        if entry['match_type'] in (MatchType.EXACT.value, MatchType.QUALIFIED.value) and
        entry['catalog_value'] not in strict_values
    ]
    boost_additions = [
        entry for entry in refinement['recovered']
        if entry['match_type'] == MatchType.CONTAINED.value and
        entry['catalog_value'] not in boost_values and
        entry['catalog_value'] not in strict_values
    ]

    merged['strict'] = (translation['strict'] + strict_additions)[:MAX_STRICT_SKILLS]
    merged['boost'] = (translation['boost'] + boost_additions)[:MAX_BOOST_SKILLS]
    merged['unresolved'] = refinement['unresolved']

    resolved_count = len(merged['strict']) + len(merged['boost'])
    total = resolved_count + len(merged['unresolved'])
    merged['resolution_rate'] = (resolved_count / total) if total else None
    return merged
