"""
Domain-layer API for retrieving course candidates from the catalog index.

One broad query, then curate. This replaces the POC's four-step retrieval ladder, which
existed to compensate for a query too narrow to return anything -- the ladder was a
symptom, not a design. Widening once and selecting afterwards is the alternative the
Chunk 3 gate prescribed, and ``pathway_assembly`` is the "afterwards".

Four things here are measured decisions rather than defaults, all against the pinned 2U
catalog on 2026-09-10:

1. **Retrieve 20, not 5.** Relevance ranking is heavily introductory at rank 5 and
   recovers by rank 20 -- ``data analyst`` returns 5/0/0 by level in its top 5 and
   16/3/1 in its top 20. Assembling a spanning set of 5 needs the wider window.
2. **``removeWordsIfNoResults: allOptional``.** The index ANDs every query word and has
   no fallback configured, so an 8-word query returns *zero* hits rather than poor ones.
   This buys volume, not relevance, so ``hit_count`` is persisted for scoring.
3. **A hard ``language`` filter.** 26.2% of the 2U catalog is taught in a language other
   than English, and it appears from rank 6 -- exactly where (1) looks. Note this is
   ``language`` (instruction), not ``metadata_language`` (record translation).
4. **Strict skill filters narrow, so a set that cannot form a ladder is broadened.** A
   hard filter on a facet value buys precision, and it also shrinks the window *before*
   ``pathway_assembly`` can span the difficulty rungs -- measured, a strict filter turned
   ``Data Analyst`` from 2/2/1 into 5/0/0. So when the strict set is too thin *or* sits
   entirely on one rung, a second unfiltered search runs and its hits are **appended**
   rather than substituted: the precise courses keep their rank, and assembly gets the
   width it needs. See ``MIN_CANDIDATES_FOR_ASSEMBLY`` and ``MIN_RUNGS_SPANNED``.
"""
import logging
from typing import Any

from django.conf import settings

from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchClient
from enterprise_access.apps.pathways.api import dedupe_names, is_malformed_compound
from enterprise_access.apps.pathways.pathway_assembly import LEVEL_ORDER, PATHWAY_SIZE, SUPPORTED_LANGUAGE

logger = logging.getLogger(__name__)

# The window ``pathway_assembly`` selects five courses out of. See (1) above.
#
# Widened 20 -> 50 on 2026-09-23. Depth alone buys nothing: an arm retrieving 50 and
# assembling in relevance order scored the same as one retrieving 12 (bad 33.1% vs 34.6%,
# course-level on-topic 38.1% vs 39.1%, identical "good" count over 127 careers). It is the
# re-ranker that turns the extra depth into better pathways -- the same 50 candidates, model
# selected, moved "good" 8.7% -> 15.0% and "bad" 34.6% -> 19.7%. So this constant is only
# worth its latency while ``rerank_candidates`` runs; if the kill switch is ever thrown,
# 50 unranked candidates are no better than 20.
CANDIDATE_HITS_PER_PAGE = 50

# Everything the re-ranker and the assembler need, and nothing else. Descriptions are the
# bulk of a course record, so they are requested here and nowhere upstream.
COURSE_ATTRIBUTES = [
    'key',
    'title',
    'short_description',
    'full_description',
    'level_type',
    'partners',
    'language',
    'skill_names',
    'subjects',
]

CONTENT_TYPE_FACET = 'content_type'
COURSE_CONTENT_TYPE = 'course'
LANGUAGE_FACET = 'language'
CUSTOMER_FACET = 'enterprise_customer_uuids'
SKILL_NAMES_FACET = 'skill_names'

REMOVE_WORDS_IF_NO_RESULTS = 'allOptional'

# ``removeWordsIfNoResults`` only relaxes the AND *after* it has returned nothing, which
# makes the query a filter that occasionally gives up. ``optionalWords`` relaxes it from the
# start, so the query ranks by how many of the career's skills a course matches instead of
# demanding all of them. With a skills-led query that distinction is the whole point -- the
# measured arms above used ``optionalWords``. ``removeWordsIfNoResults`` is kept as a
# belt-and-braces fallback for the no-skills case, where the query is the career name.
USE_OPTIONAL_WORDS = True

# Strict skill values become hard facet filters, so the budget is tight -- each one can
# only narrow. Boosts are optional filters and cost nothing but ranking signal.
MAX_STRICT_FILTERS = 4
MAX_BOOST_FILTERS = 8

# Query length is capped rather than left to the intake. The index ANDs every word, and
# even with ``allOptional`` a very long query is mostly noise competing for ranking.
MAX_QUERY_WORDS = 12

# Below this many candidates, the strict-filtered set is broadened. Measured 2026-09-10
# against the pinned 2U catalog: a strict skill filter narrows the window *before*
# assembly can span it, so precision bought at retrieval time costs the level ladder.
#
#   career                       strict hits / mix    loose hits / mix
#   Data Analyst                     17    5/0/0          20    2/2/1
#   Project Manager                  15    3/2/0          20    2/2/1
#   Financial Analyst                 2    1/1/0          12    2/2/1
#   Machine Learning Engineer        20    2/2/1          20    2/2/1
#
MIN_CANDIDATES_FOR_ASSEMBLY = PATHWAY_SIZE * 3

# ...but a hit count alone is the wrong signal, and ``Data Analyst`` is the proof: 17
# strict hits cleared the count above and *still* assembled to 5/0/0, because every one of
# them sat on the Introductory rung. What matters is whether the set can form a ladder at
# all, so the number of distinct populated rungs is checked as well.
MIN_RUNGS_SPANNED = 2


def build_course_query(*, career_name: str, boost_terms: list[str]) -> str:
    """
    Build the text query for course retrieval, from the career's SKILLS.

    Changed on 2026-09-23, reversing the previous construction in which the career name
    led. The career name is now a fallback used only when no skill terms resolved.

    The reasoning that put the career name first -- that it is the one phrase a learner
    would recognise -- turned out to describe the learner, not the catalog. Courses do not
    describe themselves by job title: across 1,000 courses examined on the enterprise
    catalog, **not one** carried the queried job title in its own title, and only 7.8%
    contained the query's words anywhere in their text.

    Measured over 127 careers, each retrieval arm judged pathway-by-pathway:

        query                       bad      course on-topic   pathways with nothing on topic
        career name (was)          48.0%          28.8%                    45
        skills (now)               34.6%          37.8%                    20
        full job title             48.8%          28.0%                    41
        career name UNION skills   47.2%          27.7%                    42

    Net 17 careers moved out of "bad", p≈0.006 paired. Two negative results are worth
    keeping, because both are tempting: the full job title is no better than the truncated
    name, and the *union* of both queries is no better than the name alone -- name-matched
    hits rank first and fill the pathway before any skill match is reached, so adding the
    skill query without removing the name buys nothing.

    Truncated at ``MAX_QUERY_WORDS`` on a word boundary. Pair with ``OPTIONAL_WORDS``: the
    index ANDs every word, so a four-skill query filters to nothing unless the words are
    optional.
    """
    parts = list(boost_terms) or [career_name]
    words: list[str] = []
    for part in parts:
        for word in (part or '').split():
            if len(words) >= MAX_QUERY_WORDS:
                return ' '.join(words)
            words.append(word)
    return ' '.join(words)


def build_course_filters(*, strict_skills: list[str], customer_uuid: str = '') -> str:
    """
    Build the Algolia ``filters`` expression.

    ``content_type`` and ``language`` are unconditional; the customer scope is applied
    when one is supplied. Strict skills are ``OR``-ed together rather than ``AND``-ed --
    a course rarely carries every skill of a career, and ``AND`` would routinely return
    nothing.
    """
    clauses = [
        f'{CONTENT_TYPE_FACET}:{COURSE_CONTENT_TYPE}',
        f'{LANGUAGE_FACET}:"{SUPPORTED_LANGUAGE}"',
    ]
    if customer_uuid:
        clauses.append(f'{CUSTOMER_FACET}:"{customer_uuid}"')
    if strict_skills:
        joined = ' OR '.join(f'{SKILL_NAMES_FACET}:"{value}"' for value in strict_skills)
        clauses.append(f'({joined})')
    return ' AND '.join(clauses)


def build_optional_skill_filters(boost_terms: list[str]) -> list[str]:
    """Turn boost terms into Algolia ``optionalFilters`` on the skill facet."""
    return [
        f'{SKILL_NAMES_FACET}:{value}'
        for value in boost_terms[:MAX_BOOST_FILTERS]
    ]


def skill_values(translation: dict[str, Any], bucket: str, limit: int) -> list[str]:
    """
    Read the catalog values out of one bucket of a ``translate_skills`` result.

    Compound artifacts ("SQL & Python") are dropped: they match no facet value, so they
    spend a filter slot to boost nothing.
    """
    entries = translation.get(bucket) or []
    values = [
        entry.get('catalog_value', '') if isinstance(entry, dict) else str(entry)
        for entry in entries
    ]
    return [value for value in dedupe_names(values) if not is_malformed_compound(value)][:limit]


def rungs_spanned(hits) -> int:
    """
    How many distinct difficulty rungs a candidate set populates.

    Counted rather than assumed from the hit count, because a large single-rung set is
    exactly the case that produces five introductory courses.
    """
    return len({
        hit.get('level_type') for hit in hits
        if hit.get('level_type') in LEVEL_ORDER
    })


def retrieve_candidate_courses(
    *,
    career_name: str,
    translation: dict[str, Any],
    customer_uuid: str = '',
    secured_key=None,
    allow_unscoped: bool = False,
    algolia_client: AlgoliaSearchClient | None = None,
) -> dict[str, Any]:
    """
    Retrieve up to ``CANDIDATE_HITS_PER_PAGE`` course candidates for one career.

    Issues one search with the strict skill filters applied. When that set is too thin, or
    sits on too few difficulty rungs to form a ladder, a second unfiltered search runs and
    its hits are appended to the first -- not substituted. Appending keeps the
    precisely-matched courses ahead of the broadly-matched ones, so assembly still prefers
    them while having the width it needs.

    Whether the broadening fired is returned, because "this career has no courses in this
    catalog" and "these skill values over-constrained a set that does exist" are different
    diagnoses that lead to different work.

    Args:
        career_name: The selected career's display name; leads the text query.
        translation: A ``catalog_translation.translate_skills`` result.
        customer_uuid: Enterprise customer to scope to. Empty means unscoped.
        secured_key: Optional secured Algolia key for request-scoped traffic.
        allow_unscoped: Permit the plain search key. Required for offline runs.

    Raises:
        AlgoliaClientError: On a misconfigured credential or a failed search.

    Returns:
        ``query``, ``hit_count``, ``courses``, ``strict_filters_applied``,
        ``strict_hit_count``, ``strict_rungs_spanned``, ``broadened`` and ``zero_hits``.
    """
    client = algolia_client or AlgoliaSearchClient()
    strict = skill_values(translation, 'strict', MAX_STRICT_FILTERS)
    boosts = skill_values(translation, 'boost', MAX_BOOST_FILTERS)

    query = build_course_query(career_name=career_name, boost_terms=boosts)
    optional_filters = build_optional_skill_filters(boosts)

    def search(strict_skills):
        params: dict[str, Any] = {
            'hitsPerPage': CANDIDATE_HITS_PER_PAGE,
            'attributesToRetrieve': COURSE_ATTRIBUTES,
            'removeWordsIfNoResults': REMOVE_WORDS_IF_NO_RESULTS,
            'filters': build_course_filters(
                strict_skills=strict_skills, customer_uuid=customer_uuid,
            ),
        }
        if USE_OPTIONAL_WORDS and query:
            params['optionalWords'] = query
        if optional_filters:
            params['optionalFilters'] = optional_filters
        response = client.search_catalog_index(
            query, secured_key=secured_key, allow_unscoped=allow_unscoped, **params,
        )
        return [hit for hit in (response.get('hits') or []) if isinstance(hit, dict)]

    hits = search(strict)
    strict_hit_count = len(hits)
    broadened = False

    strict_rungs = rungs_spanned(hits)
    too_thin = len(hits) < MIN_CANDIDATES_FOR_ASSEMBLY
    too_flat = strict_rungs < MIN_RUNGS_SPANNED

    if strict and (too_thin or too_flat):
        logger.info(
            'Course retrieval returned %d hit(s) across %d rung(s) with %d strict skill '
            'filter(s) (thin=%s, flat=%s); broadening.',
            strict_hit_count, strict_rungs, len(strict), too_thin, too_flat,
        )
        broadened = True
        seen = {hit.get('key') for hit in hits}
        hits = hits + [hit for hit in search([]) if hit.get('key') not in seen]

    if not hits:
        # Not an error. "No courses for this career in this catalog" is a real answer, and
        # the caller turns it into an explicit no-pathway rather than a padded one.
        logger.info(
            'Course retrieval found no candidates for career %r (query=%r, broadened=%s).',
            career_name, query, broadened,
        )

    return {
        'query': query,
        'hit_count': len(hits),
        'courses': hits,
        'strict_filters_applied': strict,
        'strict_hit_count': strict_hit_count,
        'strict_rungs_spanned': strict_rungs,
        'broadened': broadened,
        'zero_hits': not hits,
    }


def eval_customer_uuid() -> str:
    """The customer the offline harness scopes to, or empty when none is pinned."""
    return (getattr(settings, 'PATHWAYS_EVAL_CUSTOMER_UUID', '') or '').strip()
