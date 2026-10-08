"""
Domain-layer API for career discovery.

Two stages, each with one external dependency and its own failure mode:

* ``derive_learning_intent`` asks Xpert what a learner's intake means, in terms of skills
  and a search query. It reuses the ``prompts`` app's domain functions so the prompt stays
  admin-editable and versioned instead of being hard-coded here.
* ``retrieve_careers`` turns that intent into exactly one search against the Lightcast
  jobs index.

The query shape is a port of the learner portal MFE's ``careerRetrieval.ts``. The
asymmetry in it is the load-bearing part: **skills are optional filters (boosts), never
hard filters.** A hard filter on a skill name that is not a facet value returns zero hits
and says nothing about why, which is exactly the silent-drop failure the Chunk 3
diagnostic measured on the catalog side. Only industries and job sources -- values a
caller is expected to have grounded against the index already -- become hard filters.

No HTTP or DRF machinery here, so both stages are testable without a request.
"""
import logging
from typing import Any

from django.conf import settings

from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchClient
from enterprise_access.apps.prompts import api as prompts_api
from enterprise_access.apps.prompts.models import PromptType, XpertLearnerPathwaysSystemPrompt

logger = logging.getLogger(__name__)

# Matches the MFE's CAREER_RETRIEVAL_LIMIT. Ten is a card-list length, not a corpus size:
# this is a "which career do you want?" prompt, not a search results page.
CAREER_HITS_PER_PAGE = 10

# Optional-filter budgets, per the MFE. Past a handful of boosts the ranking signal is
# diluted rather than sharpened, and preferred skills are deliberately weaker than
# required ones.
MAX_REQUIRED_SKILL_FILTERS = 4
MAX_PREFERRED_SKILL_FILTERS = 2
PREFERRED_SKILL_FILTER_SCORE = 1

SKILLS_FACET = 'skills.name'
INDUSTRY_NAMES_FACET = 'industry_names'
JOB_SOURCES_FACET = 'job_sources'

# The jobs index is multilingual and holds translated *duplicates* of the same role: the
# Spanish record's identifier is the English one plus a "-es" suffix, so both surface
# together for the same query. Measured against `prod_taxonomy` on 2026-09-09:
# 87,026 records total, 43,513 with `metadata_language:en`. A "biomedical engineer" query
# returns 37 hits unfiltered and 23 filtered, and the unfiltered set contains pairs like
# `ET609056574BB0BB43` / `ET609056574BB0BB43-es` ("Clinical Specialist" /
# "Especialista Clinico").
#
# That is exactly the defect persona 2's author recorded: "3 careers in spanish, which
# are identical the ones above them in english. When you select one, both are
# selected/highlighted" -- the shared identifier prefix is why selection affects both.
# The learner portal MFE applies the same restriction via `filterByMetadataLanguage`.
#
# So this filter is not optional polish. Omitting it reproduces a known, reported bug.
METADATA_LANGUAGE_FACET = 'metadata_language'
SUPPORTED_METADATA_LANGUAGE = 'en'

# The jobs index ANDs every query word and has no `removeWordsIfNoResults` configured,
# exactly as the catalog index does -- but the effect is harsher here, because a job
# record is short. Measured on `prod_taxonomy` on 2026-09-09 with the intake sentence
# "Become a biomedical engineer for a major pharma company": **every** prefix of it
# returns 0 hits, including the single word "Become", since no job name contains it. The
# same query with `allOptional` returns 1,358.
#
# `build_career_query` prefers Xpert's `condensed_algolia_query`, which is free text and
# may well be a phrase. Without this parameter, one unmatched word anywhere in it takes
# career retrieval to zero and dead-ends the whole pipeline before course retrieval is
# even reached. There is no safe query-length cap to apply instead -- one word is already
# enough to fail.
#
# This buys result *volume*, not relevance, so `retrieve_careers` persists the query and
# hit count for the harness to score. See the Chunk 3 gate result.
REMOVE_WORDS_IF_NO_RESULTS = 'allOptional'

# The only jobs-index attributes this pipeline consumes. ``external_id`` is the Lightcast
# job identifier and is the career's identity everywhere downstream -- names are neither
# unique nor stable in the taxonomy.
CAREER_ATTRIBUTES = ['external_id', 'name', 'skills', 'industry_names']

# Separators that only ever appear in a parsing artifact ("SQL & Python", "Excel +
# Tableau"), never in a Lightcast skill name. Filtering on one boosts nothing and spends
# a filter slot, so they are dropped on both the career and course paths in the MFE.
_COMPOUND_SEPARATORS = (' & ', ' + ')


def dedupe_names(values: Any) -> list[str]:
    """
    Strip, drop empties, and de-duplicate a sequence of names, preserving order.

    Order is preserved because it carries relevance: the first required skill is the
    fallback query, and the filter budgets take a prefix.
    """
    stripped = (value.strip() for value in values or [] if isinstance(value, str))
    return list(dict.fromkeys(name for name in stripped if name))


def coerce_name_list(value: Any) -> list[str]:
    """
    Coerce one untrusted field of a model response into a list of names.

    A model asked for a list will sometimes return a bare string; accepting that is
    cheaper than failing the whole run over it. Anything else yields an empty list, so a
    malformed field drops a filter rather than raising mid-pipeline.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return dedupe_names(value)


def is_malformed_compound(name: str) -> bool:
    """Whether ``name`` is a compound parsing artifact rather than a single skill."""
    return any(separator in name for separator in _COMPOUND_SEPARATORS)


def _quote_facet_value(value: str) -> str:
    """Quote and escape a value for interpolation into an Algolia filter expression."""
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def _or_clause(facet_name: str, values: list[str]) -> str:
    """Build a parenthesised ``OR`` group over one facet's values."""
    joined = ' OR '.join(f'{facet_name}:{_quote_facet_value(value)}' for value in values)
    return f'({joined})'


def build_career_query(*, condensed_query: str, skills_required: list[str]) -> str:
    """
    Derive the text query for the jobs search.

    Prefers the model's condensed query, falling back to the first required skill. The
    fallback matters: an empty Algolia query matches everything, so without it a run
    whose intent extraction produced no query would be ranked by filters alone.
    """
    query = (condensed_query or '').strip()
    if query:
        return query

    fallback_terms = dedupe_names(skills_required)
    return fallback_terms[0] if fallback_terms else ''


def build_career_filters(*, industries: list[str], job_sources: list[str]) -> str | None:
    """
    Build the hard (must-match) filter expression.

    Always includes the language restriction; industries and job sources are added only
    when supplied. Never returns ``None`` in practice, since the language clause always
    applies.
    """
    clauses = [f'{METADATA_LANGUAGE_FACET}:{SUPPORTED_METADATA_LANGUAGE}']
    clauses += [
        _or_clause(facet_name, values)
        for facet_name, values in (
            (INDUSTRY_NAMES_FACET, dedupe_names(industries)),
            (JOB_SOURCES_FACET, dedupe_names(job_sources)),
        )
        if values
    ]
    return ' AND '.join(clauses) if clauses else None


def build_optional_skill_filters(*, skills_required: list[str], skills_preferred: list[str]) -> list[str]:
    """
    Build Algolia ``optionalFilters`` from the derived skills.

    Required skills are unscored, so they carry Algolia's default optional-filter weight;
    preferred skills are added at a lower explicit score. Malformed compounds are dropped
    and each list is capped, so a model that returns thirty skills cannot flatten the
    ranking.
    """
    required = [
        name for name in dedupe_names(skills_required) if not is_malformed_compound(name)
    ][:MAX_REQUIRED_SKILL_FILTERS]
    preferred = [
        name for name in dedupe_names(skills_preferred) if not is_malformed_compound(name)
    ][:MAX_PREFERRED_SKILL_FILTERS]

    return [
        f'{SKILLS_FACET}:{_quote_facet_value(name)}' for name in required
    ] + [
        f'{SKILLS_FACET}:{_quote_facet_value(name)}<score={PREFERRED_SKILL_FILTER_SCORE}>'
        for name in preferred
    ]


def career_candidate_from_hit(hit: dict[str, Any]) -> dict[str, Any] | None:
    """
    Map one jobs-index hit to a career candidate, or ``None`` if it cannot be identified.

    A hit missing either its Lightcast ``external_id`` or its name is dropped rather than
    given a placeholder: a career the learner cannot be sent back to us by identifier is
    not a usable candidate, and a fabricated id would corrupt the harness's ground-truth
    comparison.

    Carries no match percentage. The POC hardcoded ``0.95`` on every card; the MFE has
    since removed it, because no verified compatible domain value exists.
    """
    external_id = (hit.get('external_id') or '').strip()
    name = (hit.get('name') or '').strip()
    if not external_id or not name:
        logger.warning(
            'Dropping jobs-index hit with objectID=%r: external_id and name are both required.',
            hit.get('objectID'),
        )
        return None

    raw_skills = hit.get('skills') or []
    return {
        'external_id': external_id,
        'name': name,
        'skills': dedupe_names(
            skill.get('name') for skill in raw_skills if isinstance(skill, dict)
        ),
        'industries': coerce_name_list(hit.get('industry_names')),
    }


def derive_learning_intent(*, intake: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    """
    Ask Xpert to derive skills and a search query from a learner's intake.

    Args:
        intake: The four validated intake fields, passed through to Xpert verbatim.
        conversation_id: Tracing identifier for the Xpert request.

    Raises:
        PromptError: If no prompt is configured or the Xpert call fails.
        XpertAPIResponseError: If the response body is not JSON.

    Returns:
        A dict of ``skills_required``, ``skills_preferred`` and ``condensed_algolia_query``.
    """
    prompt = prompts_api.get_current_prompt(
        prompt_model=XpertLearnerPathwaysSystemPrompt,
        prompt_type=PromptType.LEARNER_INTENT,
    )
    xpert_response = prompts_api.send_xpert_message(
        prompt=prompt,
        messages=prompts_api.build_messages(intake),
        conversation_id=conversation_id,
        tags=settings.XPERT_LEARNER_PATHWAYS_RAG_TAGS,
        prompt_type=PromptType.LEARNER_INTENT,
    )

    payload = xpert_response.as_json()
    if not isinstance(payload, dict):
        raise prompts_api.PromptError(
            f'Expected a JSON object from prompt_type={PromptType.LEARNER_INTENT!r}, '
            f'got {type(payload).__name__}.'
        )

    condensed_query = payload.get('condensed_algolia_query')
    return {
        'skills_required': coerce_name_list(payload.get('skills_required')),
        'skills_preferred': coerce_name_list(payload.get('skills_preferred')),
        'condensed_algolia_query': condensed_query.strip() if isinstance(condensed_query, str) else '',
    }


def retrieve_careers(
    *,
    intent: dict[str, Any],
    industries: list[str] | None = None,
    job_sources: list[str] | None = None,
) -> dict[str, Any]:
    """
    Search the Lightcast jobs index for careers matching a derived intent.

    Issues exactly one search. The jobs index takes the plain search key -- there is no
    secured-key variant, and ``search_jobs_index`` has no parameter for one.

    Args:
        intent: A ``derive_learning_intent`` result.
        industries: Hard-filter values for ``industry_names``. Expected to be real facet
            values; unlike a skill boost, an unmatched hard filter returns nothing.
        job_sources: Hard-filter values for ``job_sources``.

    Raises:
        AlgoliaClientError: On a misconfigured credential or a failed search.

    Returns:
        A dict of ``query``, ``hit_count`` and mapped ``careers``. The query and hit count
        are recorded because a full result set is not evidence that retrieval worked --
        relaxing a query buys volume, not relevance -- so the harness needs both to tell
        those apart without re-running anything.
    """
    query = build_career_query(
        condensed_query=intent.get('condensed_algolia_query', ''),
        skills_required=intent.get('skills_required', []),
    )
    search_params: dict[str, Any] = {
        'hitsPerPage': CAREER_HITS_PER_PAGE,
        'attributesToRetrieve': CAREER_ATTRIBUTES,
        'removeWordsIfNoResults': REMOVE_WORDS_IF_NO_RESULTS,
    }

    filters = build_career_filters(
        industries=industries or [],
        job_sources=job_sources or [],
    )
    if filters:
        search_params['filters'] = filters

    optional_filters = build_optional_skill_filters(
        skills_required=intent.get('skills_required', []),
        skills_preferred=intent.get('skills_preferred', []),
    )
    if optional_filters:
        search_params['optionalFilters'] = optional_filters

    response = AlgoliaSearchClient().search_jobs_index(query, **search_params)
    hits = response.get('hits') or []
    candidates = [career_candidate_from_hit(hit) for hit in hits if isinstance(hit, dict)]

    return {
        'query': query,
        'hit_count': len(hits),
        'careers': [candidate for candidate in candidates if candidate],
    }


def enrich_rationales(*, selected_career: str, course_keys: list[str],
                      learner_profile: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    """
    Ask Xpert why each delivered course fits the selected career.

    Reuses the existing ``recommendations_feedback`` prompt read-only, exactly as
    ``derive_learning_intent`` reuses ``learner_intent``. That is the point of doing this
    as a separate step rather than taking rationales off the re-rank response: the prompt
    stays admin-editable and versioned, and the wording a learner sees here cannot drift
    from the wording the live MFE endpoint produces.

    It also runs on the **delivered five**, not the candidate twenty, so four fifths of
    the explanation work is not paid for and thrown away.

    Args:
        selected_career: The career the learner chose.
        course_keys: The keys of the assembled pathway, in order.
        learner_profile: The learner's intake, passed through to the prompt.
        conversation_id: Tracing identifier for the Xpert request.

    Raises:
        PromptError: If no prompt is configured or the Xpert call fails.
        XpertAPIResponseError: If the response body is not JSON.

    Returns:
        A dict of ``reasons`` (course key to rationale) and ``prompt_revision``. Only keys
        that were actually asked about are returned -- a rationale for a course not in the
        pathway is a fabrication, and the same untrusted-output rule applies here as in
        re-ranking.
    """
    prompt = prompts_api.get_current_prompt(
        prompt_model=XpertLearnerPathwaysSystemPrompt,
        prompt_type=PromptType.RECOMMENDATIONS_FEEDBACK,
    )
    xpert_response = prompts_api.send_xpert_message(
        prompt=prompt,
        messages=prompts_api.build_messages({
            'selected_career': selected_career,
            'course_keys': list(course_keys),
            'learner_profile': dict(learner_profile or {}),
        }),
        conversation_id=conversation_id,
        tags=settings.XPERT_LEARNER_PATHWAYS_RAG_TAGS,
        prompt_type=PromptType.RECOMMENDATIONS_FEEDBACK,
    )

    payload = xpert_response.as_json()
    if not isinstance(payload, dict):
        raise prompts_api.PromptError(
            f'Expected a JSON object from prompt_type={PromptType.RECOMMENDATIONS_FEEDBACK!r}, '
            f'got {type(payload).__name__}.'
        )

    raw_reasons = payload.get('reasons')
    requested = set(course_keys)
    reasons = {}
    if isinstance(raw_reasons, dict):
        reasons = {
            key: value for key, value in raw_reasons.items()
            if isinstance(key, str) and key in requested and isinstance(value, str) and value.strip()
        }

    missing = requested - set(reasons)
    if missing:
        # Not an error. A course with no rationale renders without one, which is better
        # than failing the pathway or inventing an explanation.
        logger.info(
            'Rationale enrichment returned no reason for %d of %d course(s).',
            len(missing), len(requested),
        )

    return {'reasons': reasons, 'prompt_revision': prompt_revision(prompt)}


def prompt_revision(prompt) -> str:
    """
    Identify which stored prompt revision produced a result.

    django-simple-history captures every edit, so a rationale generated last week may have
    come from wording that is no longer in the row. Without this, a change in tone between
    runs cannot be attributed.
    """
    history = getattr(prompt, 'history', None)
    latest = history.first() if history is not None else None
    if latest is not None and getattr(latest, 'history_id', None) is not None:
        return str(latest.history_id)
    modified = getattr(prompt, 'modified', None)
    return modified.isoformat() if modified else ''
