"""
Pathway size variants: an experiment alongside the delivered pathway, never instead of it.

The delivered pathway is exactly five courses on a 2/2/1 level quota (``pathway_assembly``,
Decision 3). In September 2026 product relaxed the definition to *two to five courses at any
level*, and measurement then showed the obvious implementation does not produce it: a model
told to "pick the 5" returned five courses 96% of the time, including when a judge rated two
or fewer of them on topic. How to build a shorter pathway is therefore an open question, and
this module runs three answers side by side so they can be compared on the same candidates:

``ranked_cut``
    Cut the relevance order at each requested size. The re-ranker already orders every
    candidate by topical fit, so this costs no model calls and keeps the pipeline's existing
    split -- the model judges relevance, code builds the pathway. Sizes nest: the three-course
    variant is the two-course one plus the next most relevant eligible course.

``model_pick``
    Ask a model for exactly N courses, once per size. Each size is chosen independently, so
    compositions can differ between sizes. One paid call per size.

``model_sized``
    Ask a model for two to five courses, including only those that genuinely fit. One call;
    the model decides the length. This is the relaxed rule stated directly.

All three share the delivered pathway's eligibility rules (valid key, no duplicate, English)
and provider cap, enforced here in code whatever a model returns. Level is not constrained:
the relaxed rule allows any level, and the realised mix is recorded rather than gated.

Nothing here reaches the learner. Variants are persisted on the run and returned only when a
caller asks for them, so an experiment can never change what the endpoint delivers.
"""
import json
import logging
from dataclasses import dataclass, field

from django.conf import settings

from enterprise_access.apps.pathways.model_backends import ModelBackendError, get_direct_backend
from enterprise_access.apps.pathways.pathway_assembly import (
    LEVEL_ORDER,
    MAX_PER_PARTNER,
    eligible_candidates,
    validate_pathway
)
from enterprise_access.apps.pathways.prompts import (
    PATHWAY_SELECTION_OUTPUT_SCHEMA,
    PATHWAY_SELECTION_SYSTEM_PROMPT,
    SELECTION_EXACT_SIZE_INSTRUCTION,
    SELECTION_MODEL_SIZED_INSTRUCTION
)
from enterprise_access.apps.prompts.api import compose_system_prompt

logger = logging.getLogger(__name__)

STRATEGY_RANKED_CUT = 'ranked_cut'
STRATEGY_MODEL_PICK = 'model_pick'
STRATEGY_MODEL_SIZED = 'model_sized'
VARIANT_STRATEGIES = (STRATEGY_RANKED_CUT, STRATEGY_MODEL_PICK, STRATEGY_MODEL_SIZED)

# Which strategies take a requested size. ``model_sized`` does not: choosing the size is
# the whole point of that arm, so it runs once however many sizes were asked for.
SIZED_STRATEGIES = (STRATEGY_RANKED_CUT, STRATEGY_MODEL_PICK)

# The relaxed pathway definition of 2026-09-23: at least two courses, at most five.
MIN_PATHWAY_SIZE = 2
MAX_PATHWAY_SIZE = 5
DEFAULT_VARIANT_SIZES = (2, 3, 4, 5)

# The label the delivered pathway is recorded under, alongside the variants', so a judge
# result can be attributed to either without a second naming scheme.
DEFAULT_PATHWAY_LABEL = 'default'

# What a selecting model sees of each candidate. Unlike the re-ranker, it IS shown level
# and provider: it is being asked to build a pathway, not to order by topic alone.
SELECTION_DESCRIPTION_CHARS = 400
SELECTION_CAREER_SKILLS_SHOWN = 8


def variant_label(strategy: str, requested_size: int | None) -> str:
    """``ranked_cut:3``, ``model_pick:5``, or ``model_sized:2-5`` when the model chose."""
    if requested_size is None:
        return f'{strategy}:{MIN_PATHWAY_SIZE}-{MAX_PATHWAY_SIZE}'
    return f'{strategy}:{requested_size}'


def normalise_sizes(sizes) -> list[int]:
    """
    De-duplicate and sort requested sizes, rejecting any outside the relaxed definition.

    Raises:
        ValueError: A size below ``MIN_PATHWAY_SIZE`` or above ``MAX_PATHWAY_SIZE``.
    """
    normalised = sorted({int(size) for size in sizes or []})
    out_of_range = [size for size in normalised if not MIN_PATHWAY_SIZE <= size <= MAX_PATHWAY_SIZE]
    if out_of_range:
        raise ValueError(
            f'Pathway sizes must be between {MIN_PATHWAY_SIZE} and {MAX_PATHWAY_SIZE}; '
            f'got {out_of_range}.'
        )
    return normalised


def normalise_strategies(strategies) -> list[str]:
    """
    De-duplicate strategies in canonical order, rejecting unknown names.

    Raises:
        ValueError: A name not in ``VARIANT_STRATEGIES``.
    """
    requested = set(strategies or [])
    unknown = sorted(requested - set(VARIANT_STRATEGIES))
    if unknown:
        raise ValueError(f'Unknown variant strategies {unknown}; expected {list(VARIANT_STRATEGIES)}.')
    return [strategy for strategy in VARIANT_STRATEGIES if strategy in requested]


def resolve_variant_request(variant_sizes, variant_strategies) -> tuple[list[int], list[str]]:
    """
    Apply the request defaults, so every caller reads a request the same way.

    Sizes alone run the free ``ranked_cut`` arm; strategies alone run every size in
    ``DEFAULT_VARIANT_SIZES``; neither requests no variants at all.

    Raises:
        ValueError: A size outside 2-5 or an unknown strategy.
    """
    strategies = normalise_strategies(variant_strategies)
    if variant_sizes and not strategies:
        strategies = [STRATEGY_RANKED_CUT]
    sizes = normalise_sizes(variant_sizes or (DEFAULT_VARIANT_SIZES if strategies else []))
    return sizes, strategies


def variant_count(sizes, strategies) -> int:
    """How many variants a resolved request builds."""
    return sum(len(sizes) if strategy in SIZED_STRATEGIES else 1 for strategy in strategies)


def estimated_model_calls(*, sizes, strategies, judge_enabled: bool) -> int:
    """
    Upper bound on the paid model calls the experiment steps add to one run.

    ``ranked_cut`` is free, ``model_pick`` costs one call per size and ``model_sized`` one.
    The judge costs one per judged pathway -- the delivered one plus each variant -- less
    any identical course list it reuses, which is why this is a bound and not a count.
    """
    calls = sum(
        (len(sizes) if strategy == STRATEGY_MODEL_PICK else 1)
        for strategy in strategies if strategy != STRATEGY_RANKED_CUT
    )
    if judge_enabled:
        calls += 1 + variant_count(sizes, strategies)
    return calls


@dataclass
class Variant:
    """One size variant, including what had to be discarded to build it."""

    strategy: str
    requested_size: int | None
    courses: list = field(default_factory=list)
    dropped: dict = field(default_factory=dict)
    fabricated_keys: list = field(default_factory=list)
    error: str = ''
    trace: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        """See ``variant_label``."""
        return variant_label(self.strategy, self.requested_size)

    @property
    def is_complete(self) -> bool:
        """
        Whether the variant has the length it was asked for.

        An exact size must be met exactly. A model-sized variant must land within the
        relaxed range. Nothing is ever padded to reach either: a short variant is recorded
        short, because a padded one would hide the thing this experiment measures.
        """
        if self.requested_size is None:
            return MIN_PATHWAY_SIZE <= len(self.courses) <= MAX_PATHWAY_SIZE
        return len(self.courses) == self.requested_size

    @property
    def realised_level_mix(self) -> dict:
        """How many courses landed on each rung. Recorded, never gated."""
        mix = {level: 0 for level in LEVEL_ORDER}
        for course in self.courses:
            if course.level_type in mix:
                mix[course.level_type] += 1
        return mix

    def violations(self) -> list[str]:
        """The Tier 1 gates, with the size rule this variant was built under."""
        if self.requested_size is None:
            return validate_pathway(self.courses, size_range=(MIN_PATHWAY_SIZE, MAX_PATHWAY_SIZE))
        return validate_pathway(self.courses, expected_size=self.requested_size)


def _assembly_hit(candidate: dict) -> dict:
    """Render a ``CourseCandidate`` dict into the hit shape ``eligible_candidates`` reads."""
    partner = candidate.get('partner') or ''
    return {
        'key': candidate.get('key') or '',
        'title': candidate.get('title') or '',
        'level_type': candidate.get('level_type') or '',
        'partners': [{'name': partner}] if partner else [],
        'language': candidate.get('language') or '',
    }


def _in_taught_order(courses) -> list:
    """Order a variant the way the delivered pathway is ordered: roughly easiest first."""
    return sorted(courses, key=lambda course: course.difficulty_rank)


def ranked_cut(candidates, size: int, *, max_per_partner: int = MAX_PER_PARTNER) -> Variant:
    """
    Take the ``size`` most relevant eligible candidates, honouring the provider cap.

    Args:
        candidates: Eligible ``pathway_assembly.Candidate`` objects in relevance order --
            the re-rank order when it ran, retrieval order otherwise.
        size: How many courses to take.
    """
    chosen, per_partner, skipped_for_cap = [], {}, 0
    for candidate in candidates:
        if len(chosen) >= size:
            break
        if candidate.partner and per_partner.get(candidate.partner, 0) >= max_per_partner:
            skipped_for_cap += 1
            continue
        chosen.append(candidate)
        if candidate.partner:
            per_partner[candidate.partner] = per_partner.get(candidate.partner, 0) + 1

    return Variant(
        strategy=STRATEGY_RANKED_CUT,
        requested_size=size,
        courses=_in_taught_order(chosen),
        dropped={'provider_cap': skipped_for_cap} if skipped_for_cap else {},
    )


def apply_selection(candidates, keys, *, max_size: int, max_per_partner: int = MAX_PER_PARTNER):
    """
    Turn a model's chosen keys into courses, enforcing every rule the model was told.

    Returns ``(courses, dropped, fabricated)``. Keys are taken in the model's order; a key
    that repeats, exceeds the provider cap, or arrives after ``max_size`` is dropped and
    counted by reason, and a key that was never a candidate is a fabrication.
    """
    by_key = {candidate.key: candidate for candidate in candidates}
    chosen, seen, per_partner = [], set(), {}
    dropped: dict = {}
    fabricated: list = []

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    for key in keys:
        if not isinstance(key, str) or not key:
            continue
        if key in seen:
            drop('duplicate')
            continue
        seen.add(key)
        candidate = by_key.get(key)
        if candidate is None:
            fabricated.append(key)
            continue
        if candidate.partner and per_partner.get(candidate.partner, 0) >= max_per_partner:
            drop('provider_cap')
            continue
        if len(chosen) >= max_size:
            drop('over_size')
            continue
        chosen.append(candidate)
        if candidate.partner:
            per_partner[candidate.partner] = per_partner.get(candidate.partner, 0) + 1

    return _in_taught_order(chosen), dropped, fabricated


def selection_system_prompt(requested_size: int | None) -> str:
    """The selection prompt for one arm, with its size rule and the output schema."""
    if requested_size is None:
        instruction = SELECTION_MODEL_SIZED_INSTRUCTION.format(
            min_size=MIN_PATHWAY_SIZE, max_size=MAX_PATHWAY_SIZE,
        )
    else:
        instruction = SELECTION_EXACT_SIZE_INSTRUCTION.format(size=requested_size)
    return compose_system_prompt(
        PATHWAY_SELECTION_SYSTEM_PROMPT.format(size_instruction=instruction),
        PATHWAY_SELECTION_OUTPUT_SCHEMA,
    )


def build_selection_content(*, career_name: str, career_skills: list[str], candidates: list[dict]) -> str:
    """Render the candidates a selecting model chooses from."""
    return json.dumps(
        {
            'career': career_name,
            'career_skills': list(career_skills or [])[:SELECTION_CAREER_SKILLS_SHOWN],
            'candidates': [
                {
                    'key': candidate.get('key', ''),
                    'title': candidate.get('title', ''),
                    'level': candidate.get('level_type', ''),
                    'provider': candidate.get('partner', ''),
                    'description': (
                        candidate.get('short_description') or candidate.get('full_description') or ''
                    )[:SELECTION_DESCRIPTION_CHARS],
                }
                for candidate in candidates
            ],
        },
        separators=(',', ':'),
    )


def get_variant_backend():
    """
    The backend the model-selected arms run on.

    Defaults to the re-rank's own backend and model, so the model arms differ from
    ``ranked_cut`` in method rather than in model. ``get_direct_backend`` refuses xpert.
    """
    return get_direct_backend(
        backend_name=settings.PATHWAYS_VARIANT_BACKEND or settings.PATHWAYS_MODEL_BACKEND,
        model=settings.PATHWAYS_VARIANT_MODEL or None,
    )


def model_select(*, strategy: str, requested_size: int | None, career_name: str,
                 career_skills: list[str], candidate_dicts: list[dict], eligible: list,
                 trace_id: str, backend=None) -> Variant:
    """
    Ask a model to choose a pathway from the candidate window.

    A backend, parse or configuration failure yields a variant with no courses and the
    failure in ``error``, never an exception: one failed arm must not cost the run the
    others, or the pathway that is actually delivered.
    """
    variant = Variant(strategy=strategy, requested_size=requested_size)
    eligible_keys = {candidate.key for candidate in eligible}
    shown = [candidate for candidate in candidate_dicts if candidate.get('key') in eligible_keys]

    try:
        model_backend = backend or get_variant_backend()
        response = model_backend.complete(
            system_prompt=selection_system_prompt(requested_size),
            user_content=build_selection_content(
                career_name=career_name, career_skills=career_skills, candidates=shown,
            ),
            trace_id=trace_id,
        )
    except ModelBackendError as exc:
        variant.error = f'selection failed ({type(exc).__name__}): {exc}'
        return variant

    variant.trace = response.to_trace_dict()
    try:
        payload = response.as_json()
    except ModelBackendError:
        variant.error = 'selection response was not JSON'
        return variant

    keys = payload.get('keys') if isinstance(payload, dict) else None
    if not isinstance(keys, list):
        variant.error = 'selection response had no keys list'
        return variant

    courses, dropped, fabricated = apply_selection(
        eligible, keys, max_size=requested_size or MAX_PATHWAY_SIZE,
    )
    if fabricated:
        logger.warning('Variant selection returned %d key(s) absent from the candidates; dropped.',
                       len(fabricated))
    variant.courses, variant.dropped, variant.fabricated_keys = courses, dropped, fabricated
    return variant


def build_variants(*, career_name: str, career_skills: list[str], ordered_candidates: list[dict],
                   sizes, strategies, trace_prefix: str, backend=None) -> list[Variant]:
    """
    Build every requested variant from one candidate window.

    Args:
        ordered_candidates: ``CourseCandidate`` dicts in relevance order.
        sizes: Requested sizes; see ``normalise_sizes``.
        strategies: Requested strategies; see ``normalise_strategies``.
        trace_prefix: Each model call's trace id is this plus the variant label.
        backend: Overrides ``get_variant_backend()`` for the model arms.

    Returns:
        Variants in a stable order: by strategy as listed in ``VARIANT_STRATEGIES``, then
        by size.
    """
    sizes = normalise_sizes(sizes)
    strategies = normalise_strategies(strategies)
    eligible, _ = eligible_candidates([_assembly_hit(candidate) for candidate in ordered_candidates])

    variants = []
    for strategy in strategies:
        if strategy == STRATEGY_RANKED_CUT:
            variants.extend(ranked_cut(eligible, size) for size in sizes)
            continue
        requested = sizes if strategy in SIZED_STRATEGIES else [None]
        for size in requested:
            variants.append(model_select(
                strategy=strategy,
                requested_size=size,
                career_name=career_name,
                career_skills=career_skills,
                candidate_dicts=ordered_candidates,
                eligible=eligible,
                trace_id=f'{trace_prefix}:{variant_label(strategy, size)}',
                backend=backend,
            ))
    return variants
