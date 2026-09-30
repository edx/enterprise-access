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

Two further arms build a pathway to a fixed **shape** -- a number of courses per rung, written
``Introductory/Intermediate/Advanced`` as everywhere else in this app, so ``2/0/0`` is two
introductory courses and ``2/2/1`` the delivered ladder. They exist so reviewers can compare
shapes for one career rather than take the delivered one as given:

``shape_cut``
    Fill each rung's quota from the relevance order, scarcest rung first, as assembly does.
    Free. Unlike assembly it never backfills: a rung the window cannot fill stays short, so
    the variant is recorded incomplete rather than quietly becoming a different shape.

``shape_pick``
    Ask a model for the shape, shown only candidates on the shape's rungs. One paid call per
    shape. It shares the size arms' prompt, with a shape sentence as its size rule.

``shape_pick_v2``
    ``shape_pick`` with the second selection prompt (``PATHWAY_SELECTION_SYSTEM_PROMPT_V2``),
    which adds a reviewer's five recurring reasons as rules and shows the model the career's
    description, its family's job titles and each candidate's skills. One paid call per shape.
    ``shape_pick`` is untouched, so the two can run side by side.

The shape arms are not offered through the pathway API; they are reached through
``generate_input_dict`` and the ``collect_pathway_variants`` command.

Editorial policy, opt-in
------------------------
Given an editorial ``policy`` (from ``pathway_editorial``; see ``resolve_editorial_policy``),
``build_variants`` applies it in two ways and records both:

* **Exclusions** apply to every arm: an excluded course is ineligible, counted as
  ``editorial_excluded`` like any other ineligibility.
* **Seats** apply to the shape arms: the policy's planner seats named courses on a shape's
  rungs, and each arm fills only the places left. ``shape_cut`` fills them from the relevance
  order; the model arms are shown the seats as already chosen and asked for the rest, with
  no call at all when the seats fill the shape. Seats count against the rung quotas and the
  provider cap like any chosen course, and are validated here against the window and the
  shape, whatever the planner returned.

Without a policy none of this runs, and every arm behaves exactly as it did before.

Nothing here reaches the learner. Variants are persisted on the run and returned only when a
caller asks for them, so an experiment can never change what the endpoint delivers.
"""
import json
import logging
from dataclasses import dataclass, field

from django.conf import settings

from enterprise_access.apps.pathways.ecosystems import ECOSYSTEM_DROP, EcosystemTracker
from enterprise_access.apps.pathways.judging import normalise_rubrics
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
    PATHWAY_SELECTION_SYSTEM_PROMPT_V2,
    SELECTION_EXACT_SIZE_INSTRUCTION,
    SELECTION_MODEL_SIZED_INSTRUCTION,
    SELECTION_SHAPE_INSTRUCTION
)
from enterprise_access.apps.prompts.api import compose_system_prompt

logger = logging.getLogger(__name__)

STRATEGY_RANKED_CUT = 'ranked_cut'
STRATEGY_MODEL_PICK = 'model_pick'
STRATEGY_MODEL_SIZED = 'model_sized'
VARIANT_STRATEGIES = (STRATEGY_RANKED_CUT, STRATEGY_MODEL_PICK, STRATEGY_MODEL_SIZED)

# The level-shaped arms. Kept out of ``VARIANT_STRATEGIES``, which is what the pathway API
# accepts, so the endpoint's experiment surface is unchanged by their existence.
STRATEGY_SHAPE_CUT = 'shape_cut'
STRATEGY_SHAPE_PICK = 'shape_pick'
STRATEGY_SHAPE_PICK_V2 = 'shape_pick_v2'
SHAPE_STRATEGIES = (STRATEGY_SHAPE_CUT, STRATEGY_SHAPE_PICK, STRATEGY_SHAPE_PICK_V2)
# The shape arms that ask a model, one call per shape.
MODEL_SHAPE_STRATEGIES = (STRATEGY_SHAPE_PICK, STRATEGY_SHAPE_PICK_V2)
ALL_STRATEGIES = VARIANT_STRATEGIES + SHAPE_STRATEGIES

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

# What ``shape_pick_v2`` shows beyond v1: the career's description, the family's size and
# job titles, and each candidate's skill tags.
SELECTION_V2_CAREER_DESCRIPTION_CHARS = 600
SELECTION_V2_FAMILY_TITLES_SHOWN = 12
SELECTION_V2_SKILL_NAMES_SHOWN = 8


def variant_label(strategy: str, requested_size: int | None, shape: tuple | None = None) -> str:
    """
    ``ranked_cut:3``, ``model_pick:5``, ``model_sized:2-5`` when the model chose, or
    ``shape_pick:2/2/1`` for a shape arm.
    """
    if shape is not None:
        return f'{strategy}:{shape_name(shape)}'
    if requested_size is None:
        return f'{strategy}:{MIN_PATHWAY_SIZE}-{MAX_PATHWAY_SIZE}'
    return f'{strategy}:{requested_size}'


def parse_shape(shape) -> tuple[int, int, int]:
    """
    Read a shape written ``Introductory/Intermediate/Advanced``: ``'2/2/1'`` -> ``(2, 2, 1)``.

    Raises:
        ValueError: Not three non-negative whole numbers, or a total outside the relaxed
            two-to-five definition.
    """
    parts = str(shape).strip().split('/')
    if len(parts) != len(LEVEL_ORDER) or not all(part.strip().isdigit() for part in parts):
        raise ValueError(
            f'A pathway shape is three course counts, Introductory/Intermediate/Advanced '
            f'(such as "2/2/1"); got {shape!r}.'
        )
    quota = tuple(int(part) for part in parts)
    if not MIN_PATHWAY_SIZE <= sum(quota) <= MAX_PATHWAY_SIZE:
        raise ValueError(
            f'A pathway shape must total {MIN_PATHWAY_SIZE} to {MAX_PATHWAY_SIZE} courses; '
            f'{shape!r} totals {sum(quota)}.'
        )
    return quota


def shape_name(shape) -> str:
    """``(2, 2, 1)`` -> ``'2/2/1'``, the form shapes are requested and recorded in."""
    return '/'.join(str(count) for count in shape)


def normalise_shapes(shapes) -> list[str]:
    """
    Canonicalise and de-duplicate requested shapes, keeping the order they were asked in.

    Raises:
        ValueError: See ``parse_shape``.
    """
    return list(dict.fromkeys(shape_name(parse_shape(shape)) for shape in shapes or []))


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
        ValueError: A name not in ``ALL_STRATEGIES``.
    """
    requested = set(strategies or [])
    unknown = sorted(requested - set(ALL_STRATEGIES))
    if unknown:
        raise ValueError(f'Unknown variant strategies {unknown}; expected {list(ALL_STRATEGIES)}.')
    return [strategy for strategy in ALL_STRATEGIES if strategy in requested]


def resolve_variant_request(variant_sizes, variant_strategies) -> tuple[list[int], list[str]]:
    """
    Apply the request defaults, so every caller reads a request the same way.

    Sizes alone run the free ``ranked_cut`` arm; strategies alone run every size in
    ``DEFAULT_VARIANT_SIZES``; neither requests no variants at all. Only the size arms are
    returned: shape arms are resolved by ``resolve_shape_request``.

    Raises:
        ValueError: A size outside 2-5 or an unknown strategy.
    """
    strategies = [
        strategy for strategy in normalise_strategies(variant_strategies)
        if strategy in VARIANT_STRATEGIES
    ]
    if variant_sizes and not strategies:
        strategies = [STRATEGY_RANKED_CUT]
    sizes = normalise_sizes(variant_sizes or (DEFAULT_VARIANT_SIZES if strategies else []))
    return sizes, strategies


def resolve_shape_request(variant_shapes, variant_strategies) -> tuple[list[str], list[str]]:
    """
    The shapes requested, and the shape arms to build them with.

    Shapes alone run the free ``shape_cut`` arm, mirroring sizes alone. A shape arm with no
    shape is refused rather than defaulted: unlike sizes, there is no natural set of shapes
    to fall back on.

    Raises:
        ValueError: A malformed shape, an unknown strategy, or a shape arm with no shapes.
    """
    strategies = [
        strategy for strategy in normalise_strategies(variant_strategies)
        if strategy in SHAPE_STRATEGIES
    ]
    shapes = normalise_shapes(variant_shapes)
    if shapes and not strategies:
        strategies = [STRATEGY_SHAPE_CUT]
    if strategies and not shapes:
        raise ValueError(f'The {strategies} arm(s) need at least one shape, such as "2/0/0".')
    return shapes, strategies


def variant_count(sizes, strategies, shapes=()) -> int:
    """How many variants a resolved request builds."""
    def per_strategy(strategy):
        if strategy in SHAPE_STRATEGIES:
            return len(shapes)
        return len(sizes) if strategy in SIZED_STRATEGIES else 1
    return sum(per_strategy(strategy) for strategy in strategies)


def estimated_model_calls(*, sizes, strategies, judge_enabled: bool, shapes=(), judge_rubrics=None) -> int:
    """
    Upper bound on the paid model calls the experiment steps add to one run.

    ``ranked_cut`` and ``shape_cut`` are free, ``model_pick`` costs one call per size,
    ``shape_pick`` one per shape, ``shape_pick_v2`` up to two per shape (its repair round)
    and ``model_sized`` one. The judge
    costs one per judged pathway per rubric -- the delivered one plus each variant, under each
    of ``judge_rubrics`` (v1 alone by default) -- less any identical course list it reuses
    and any shape whose seats leave no call to make, which is why this is a bound and not a
    count.

    Raises:
        ValueError: An unknown rubric.
    """
    def calls_for(strategy):
        if strategy in (STRATEGY_RANKED_CUT, STRATEGY_SHAPE_CUT):
            return 0
        if strategy == STRATEGY_MODEL_PICK:
            return len(sizes)
        if strategy == STRATEGY_SHAPE_PICK_V2:
            # At most one repair round per shape; see ``_repair_shape_pick``.
            return 2 * len(shapes)
        if strategy in MODEL_SHAPE_STRATEGIES:
            return len(shapes)
        return 1
    calls = sum(calls_for(strategy) for strategy in strategies)
    if judge_enabled:
        calls += len(normalise_rubrics(judge_rubrics)) * (1 + variant_count(sizes, strategies, shapes))
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
    #: Courses per rung for a shape arm, ``(introductory, intermediate, advanced)``.
    shape: tuple | None = None
    #: Editorial seats this variant was built around, as the planner's ``Seat.to_dict()``,
    #: in seat order. Empty without a policy. Seated courses are also in ``courses``.
    seats: list = field(default_factory=list)
    #: The repair round, when one ran (``shape_pick_v2`` only; see ``REPAIRABLE_DROPS``):
    #: ``attempted``, ``added``, what the second answer had refused (``dropped``,
    #: ``fabricated_keys``), any ``error``, and the second call's ``trace``. Empty otherwise.
    repair: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        """See ``variant_label``."""
        return variant_label(self.strategy, self.requested_size, self.shape)

    @property
    def is_complete(self) -> bool:
        """
        Whether the variant has the length it was asked for.

        An exact size must be met exactly. A model-sized variant must land within the
        relaxed range. A shape must be met rung for rung. Nothing is ever padded to reach
        any of them: a short variant is recorded short, because a padded one would hide the
        thing this experiment measures.
        """
        if self.shape is not None:
            mix = self.realised_level_mix
            return tuple(mix[level] for level in LEVEL_ORDER) == tuple(self.shape) \
                and len(self.courses) == sum(self.shape)
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
        'skill_names': candidate.get('skill_names') or [],
    }


def _in_taught_order(courses) -> list:
    """Order a variant the way the delivered pathway is ordered: roughly easiest first."""
    return sorted(courses, key=lambda course: course.difficulty_rank)


def load_editorial_api():
    """
    The editorial app's domain API, imported on first use.

    Imported lazily so this module -- and every run that never opts into a policy -- does
    not depend on the editorial app at all. Tests substitute this function rather than the
    app.
    """
    from enterprise_access.apps.pathway_editorial import api as editorial_api  # pylint: disable=import-outside-toplevel
    return editorial_api


def resolve_editorial_policy(*, use_active: bool = False, snapshot: dict | None = None):
    """
    The editorial policy a run opted into, or ``None`` when it opted into none.

    A non-empty ``snapshot`` (an ``EditorialPolicy.to_dict()``) wins, so a run can be
    repeated against the exact policy an earlier run used; otherwise ``use_active`` loads
    the policy the editorial tables hold now.
    """
    if snapshot:
        return load_editorial_api().EditorialPolicy.from_dict(snapshot)
    if use_active:
        return load_editorial_api().load_policy()
    return None


def policy_excluded_keys(policy) -> frozenset:
    """A policy's excluded course keys; empty for no policy."""
    if policy is None:
        return frozenset()
    return frozenset(getattr(policy, 'excluded_keys', None) or ())


def policy_record(policy) -> dict:
    """What a run records of the policy it applied: its ``to_dict()``, or ``{}`` for none."""
    if policy is None:
        return {}
    to_dict = getattr(policy, 'to_dict', None)
    if callable(to_dict):
        return dict(to_dict())
    return {'excluded_keys': sorted(policy_excluded_keys(policy))}


def seat_record(seat) -> dict:
    """A planner's seat as a plain dict: its ``to_dict()``, or the dict itself."""
    to_dict = getattr(seat, 'to_dict', None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(seat, dict):
        return dict(seat)
    return {name: getattr(seat, name, '') for name in ('key', 'level', 'rule', 'reason')}


@dataclass
class SeatPlacement:
    """Seats checked against one window and one shape, and the places they leave open."""

    #: Seated ``pathway_assembly.Candidate`` objects, in seat order.
    seated: list = field(default_factory=list)
    #: The seats kept, as ``seat_record`` dicts, in the same order.
    records: list = field(default_factory=list)
    #: Seats refused: not an eligible candidate, repeated, over their rung, or over the cap.
    rejected: int = 0
    #: Places still open per rung once the seats are placed.
    open_quota: dict = field(default_factory=dict)
    #: Courses per provider among the seats, for the provider cap.
    per_partner: dict = field(default_factory=dict)

    @property
    def open_shape(self) -> tuple:
        """``open_quota`` as a shape, ``(introductory, intermediate, advanced)``."""
        return tuple(self.open_quota.get(level, 0) for level in LEVEL_ORDER)


def place_seats(candidates, shape, seats=(), *, max_per_partner: int = MAX_PER_PARTNER) -> SeatPlacement:
    """
    Check a seat plan against the window and the shape, in seat order.

    The planner promises to respect the shape and never to seat an excluded course, but its
    output is still checked here, as a model's is: a seat is kept only if its key is an
    eligible candidate not already seated, its rung -- the candidate's own ``level_type``,
    whatever level the seat names -- has a place left, and its provider is under the cap.
    Anything else is refused and counted.
    """
    by_key = {candidate.key: candidate for candidate in candidates}
    placement = SeatPlacement(open_quota=dict(zip(LEVEL_ORDER, shape)))
    seated_keys = set()

    def fits(candidate):
        if candidate is None or candidate.key in seated_keys:
            return False
        if placement.open_quota.get(candidate.level_type, 0) <= 0:
            return False
        return not candidate.partner or placement.per_partner.get(candidate.partner, 0) < max_per_partner

    for seat in seats or ():
        record = seat_record(seat)
        candidate = by_key.get(record.get('key'))
        if not fits(candidate):
            placement.rejected += 1
            continue
        seated_keys.add(candidate.key)
        placement.seated.append(candidate)
        placement.records.append(record)
        placement.open_quota[candidate.level_type] -= 1
        if candidate.partner:
            placement.per_partner[candidate.partner] = placement.per_partner.get(candidate.partner, 0) + 1
    if placement.rejected:
        logger.warning('Refused %d editorial seat(s) that did not fit the window or the shape.',
                       placement.rejected)
    return placement


def ranked_cut(candidates, size: int, *, max_per_partner: int = MAX_PER_PARTNER,
               single_ecosystem: bool = False) -> Variant:
    """
    Take the ``size`` most relevant eligible candidates, honouring the provider cap.

    Args:
        candidates: Eligible ``pathway_assembly.Candidate`` objects in relevance order --
            the re-rank order when it ran, retrieval order otherwise.
        size: How many courses to take.
        single_ecosystem: Refuse a course that would leave the pathway spanning two vendors'
            products. See ``ecosystems``.
    """
    chosen, per_partner, skipped_for_cap = [], {}, 0
    stack = EcosystemTracker(single_ecosystem)
    for candidate in candidates:
        if len(chosen) >= size:
            break
        if candidate.partner and per_partner.get(candidate.partner, 0) >= max_per_partner:
            skipped_for_cap += 1
            continue
        if stack.refuses(candidate):
            stack.refuse()
            continue
        chosen.append(candidate)
        stack.take(candidate)
        if candidate.partner:
            per_partner[candidate.partner] = per_partner.get(candidate.partner, 0) + 1

    return Variant(
        strategy=STRATEGY_RANKED_CUT,
        requested_size=size,
        courses=_in_taught_order(chosen),
        dropped={**({'provider_cap': skipped_for_cap} if skipped_for_cap else {}), **stack.dropped},
    )


def shape_cut(candidates, shape, *, max_per_partner: int = MAX_PER_PARTNER, seats=(),
              single_ecosystem: bool = False) -> Variant:
    """
    Fill each rung's quota from the relevance order, honouring the provider cap.

    Rungs are filled scarcest first, for the reason ``assemble_pathway`` gives: the quota and
    the provider cap compete for the same candidates, and the plentiful rung would otherwise
    spend a provider's allowance the scarce rung needed. Unlike assembly there is no
    backfill, so a rung the window cannot fill leaves the variant short of its shape.

    Args:
        candidates: Eligible ``pathway_assembly.Candidate`` objects in relevance order.
        shape: Courses per rung, ``(introductory, intermediate, advanced)``.
        seats: Editorial seats (see ``place_seats``). They are placed first, and count
            against their rung's quota and their provider's cap; the rest of each rung is
            filled as without them.
        single_ecosystem: Refuse a course that would leave the pathway spanning two vendors'
            products. Seats are placed before it applies, so an editorial rule still wins.
    """
    placement = place_seats(candidates, shape, seats, max_per_partner=max_per_partner)
    seated_keys = {candidate.key for candidate in placement.seated}
    quota = placement.open_quota
    by_rung = {
        level: [c for c in candidates if c.level_type == level and c.key not in seated_keys]
        for level in LEVEL_ORDER
    }
    rung_order = sorted(
        (level for level in LEVEL_ORDER if quota[level] > 0),
        key=lambda level: (len(by_rung[level]), LEVEL_ORDER.index(level)),
    )

    chosen, per_partner, skipped_for_cap = list(placement.seated), dict(placement.per_partner), 0
    stack = EcosystemTracker(single_ecosystem, placement.seated)
    for level in rung_order:
        taken = 0
        for candidate in by_rung[level]:
            if taken >= quota[level]:
                break
            if candidate.partner and per_partner.get(candidate.partner, 0) >= max_per_partner:
                skipped_for_cap += 1
                continue
            if stack.refuses(candidate):
                stack.refuse()
                continue
            chosen.append(candidate)
            stack.take(candidate)
            taken += 1
            if candidate.partner:
                per_partner[candidate.partner] = per_partner.get(candidate.partner, 0) + 1

    dropped = {'provider_cap': skipped_for_cap} if skipped_for_cap else {}
    dropped.update(stack.dropped)
    if placement.rejected:
        dropped['seat_rejected'] = placement.rejected
    return Variant(
        strategy=STRATEGY_SHAPE_CUT,
        requested_size=sum(shape),
        shape=tuple(shape),
        courses=_in_taught_order(chosen),
        dropped=dropped,
        seats=placement.records,
    )


def apply_selection(candidates, keys, *, max_size: int, max_per_partner: int = MAX_PER_PARTNER,
                    level_quota: dict | None = None, seated=(), single_ecosystem: bool = False):
    """
    Turn a model's chosen keys into courses, enforcing every rule the model was told.

    Returns ``(courses, dropped, fabricated)``. Keys are taken in the model's order; a key
    that repeats, exceeds the provider cap, arrives after ``max_size``, or lands on a rung
    whose ``level_quota`` is already met is dropped and counted by reason, and a key that
    was never a candidate is a fabrication.

    ``seated`` are ``Candidate`` objects already on the pathway (editorial seats). They are
    in ``courses``, and they count toward ``max_size``, their rung's quota and their
    provider's cap exactly as a chosen course would. A key naming one is dropped as
    ``already_seated``: it cannot be chosen twice.

    With ``single_ecosystem``, a key whose course would leave the pathway spanning two vendors'
    products is dropped as ``other_ecosystem``, whatever the model returned. See ``ecosystems``.
    """
    by_key = {candidate.key: candidate for candidate in candidates}
    remaining = dict(level_quota) if level_quota is not None else None
    chosen, seen, per_partner = list(seated), set(), {}
    stack = EcosystemTracker(single_ecosystem, seated)
    seated_keys = {candidate.key for candidate in seated}
    for candidate in seated:
        if remaining is not None and candidate.level_type in remaining:
            remaining[candidate.level_type] -= 1
        if candidate.partner:
            per_partner[candidate.partner] = per_partner.get(candidate.partner, 0) + 1
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
        if key in seated_keys:
            drop('already_seated')
            continue
        candidate = by_key.get(key)
        if candidate is None:
            fabricated.append(key)
            continue
        if candidate.partner and per_partner.get(candidate.partner, 0) >= max_per_partner:
            drop('provider_cap')
            continue
        if stack.refuses(candidate):
            drop(ECOSYSTEM_DROP)
            continue
        if len(chosen) >= max_size:
            drop('over_size')
            continue
        if remaining is not None:
            if remaining.get(candidate.level_type, 0) <= 0:
                drop('over_level_quota')
                continue
            remaining[candidate.level_type] -= 1
        chosen.append(candidate)
        stack.take(candidate)
        if candidate.partner:
            per_partner[candidate.partner] = per_partner.get(candidate.partner, 0) + 1

    return _in_taught_order(chosen), dropped, fabricated


def shape_breakdown(shape) -> str:
    """``(2, 1, 0)`` -> ``'2 Introductory and 1 Intermediate'``, for the shape sentence."""
    parts = [f'{count} {level}' for level, count in zip(LEVEL_ORDER, shape) if count]
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


def selection_system_prompt(requested_size: int | None, shape: tuple | None = None) -> str:
    """The selection prompt for one arm, with its size rule and the output schema."""
    if shape is not None:
        instruction = SELECTION_SHAPE_INSTRUCTION.format(
            size=sum(shape), breakdown=shape_breakdown(shape),
        )
    elif requested_size is None:
        instruction = SELECTION_MODEL_SIZED_INSTRUCTION.format(
            min_size=MIN_PATHWAY_SIZE, max_size=MAX_PATHWAY_SIZE,
        )
    else:
        instruction = SELECTION_EXACT_SIZE_INSTRUCTION.format(size=requested_size)
    return compose_system_prompt(
        PATHWAY_SELECTION_SYSTEM_PROMPT.format(size_instruction=instruction),
        PATHWAY_SELECTION_OUTPUT_SCHEMA,
    )


def selection_system_prompt_v2(shape: tuple) -> str:
    """
    The ``shape_pick_v2`` prompt for a shape -- or, with seats, for the places they leave.

    Shares ``SELECTION_SHAPE_INSTRUCTION`` with ``shape_pick`` as its size rule, so the two
    arms differ in their rules and inputs, not in how the shape is stated.
    """
    instruction = SELECTION_SHAPE_INSTRUCTION.format(size=sum(shape), breakdown=shape_breakdown(shape))
    return compose_system_prompt(
        PATHWAY_SELECTION_SYSTEM_PROMPT_V2.format(size_instruction=instruction),
        PATHWAY_SELECTION_OUTPUT_SCHEMA,
    )


def _description(candidate: dict) -> str:
    """A candidate's short description, else its full one, cut for a selecting model."""
    return (
        candidate.get('short_description') or candidate.get('full_description') or ''
    )[:SELECTION_DESCRIPTION_CHARS]


def _chosen_entry(candidate: dict) -> dict:
    """How a seated course is shown to a selecting model under ``already_chosen``."""
    return {
        'key': candidate.get('key', ''),
        'title': candidate.get('title', ''),
        'level': candidate.get('level_type', ''),
        'provider': candidate.get('partner', ''),
    }


def build_selection_content(*, career_name: str, career_skills: list[str], candidates: list[dict],
                            already_chosen=None) -> str:
    """
    Render the candidates a selecting model chooses from.

    ``already_chosen`` (seated courses) is added only when there are any, so a run without
    seats sends exactly what it always sent.
    """
    content = {
        'career': career_name,
        'career_skills': list(career_skills or [])[:SELECTION_CAREER_SKILLS_SHOWN],
    }
    if already_chosen:
        content['already_chosen'] = [_chosen_entry(candidate) for candidate in already_chosen]
    content['candidates'] = [
        {
            'key': candidate.get('key', ''),
            'title': candidate.get('title', ''),
            'level': candidate.get('level_type', ''),
            'provider': candidate.get('partner', ''),
            'description': _description(candidate),
        }
        for candidate in candidates
    ]
    return json.dumps(content, separators=(',', ':'))


def build_selection_content_v2(*, career_name: str, career_skills: list[str], career_description: str,
                               family_titles, family_size: int, candidates: list[dict],
                               already_chosen) -> str:
    """
    Render what ``shape_pick_v2`` chooses from: the career and its family, then the courses.

    Beyond v1 it carries the career's description, the family's size and first job titles
    (the pathway serves all of them), any seated courses under ``already_chosen``, and each
    candidate's skill tags.
    """
    titles = list(dict.fromkeys(
        title.strip() for title in (family_titles or []) if isinstance(title, str) and title.strip()
    ))
    return json.dumps(
        {
            'career': career_name,
            'career_description': ' '.join((career_description or '').split())[
                :SELECTION_V2_CAREER_DESCRIPTION_CHARS
            ],
            'family_size': max(int(family_size or 0), len(titles)),
            'family_titles': titles[:SELECTION_V2_FAMILY_TITLES_SHOWN],
            'career_skills': list(career_skills or [])[:SELECTION_CAREER_SKILLS_SHOWN],
            'already_chosen': [_chosen_entry(candidate) for candidate in already_chosen or []],
            'candidates': [
                {
                    'key': candidate.get('key', ''),
                    'title': candidate.get('title', ''),
                    'level': candidate.get('level_type', ''),
                    'provider': candidate.get('partner', ''),
                    'description': _description(candidate),
                    'skill_names': list(candidate.get('skill_names') or [])[:SELECTION_V2_SKILL_NAMES_SHOWN],
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


# The refusals a repair round answers: a course the rules turned away, rather than the model
# declining to fill a place, which is an honest answer about a thin rung and is left short.
# Measured on the bench round-1 windows (2026-09-29): with a seated course from a provider that
# dominates the window, the model chose two more from that provider for 9 of Sales Manager's 12
# shapes, and the cap left every one of them short.
#
# ``other_ecosystem`` is here for a slightly different reason. The model is not told that rule,
# so refusing its choice is not it breaking a promise -- but the gap is ours to fill, and the
# repair round is shown only courses the rule already allows, so whatever comes back complies.
# Without it the rule cost eight of 48 pathways a course they could have had (2026-09-30).
REPAIRABLE_DROPS = ('provider_cap', 'over_level_quota', 'already_seated', 'duplicate', ECOSYSTEM_DROP)


def model_select(*, strategy: str, requested_size: int | None, career_name: str,
                 career_skills: list[str], candidate_dicts: list[dict], eligible: list,
                 trace_id: str, backend=None, shape: tuple | None = None, seats=(),
                 career_description: str = '', family_titles=(), family_size: int = 0,
                 single_ecosystem: bool = False) -> Variant:
    """
    Ask a model to choose a pathway from the candidate window.

    With a ``shape``, the model is shown only candidates on the shape's rungs and each
    rung's count is enforced in code, as the provider cap is.

    With ``seats`` (shape arms only; see ``place_seats``), the seated courses are fixed: the
    model is shown them as already chosen, shown candidates only on rungs with places left,
    and asked for the remaining counts. Seats count against the quotas and the provider cap
    in ``apply_selection``. When the seats fill the shape, no call is made.

    ``shape_pick_v2`` uses the second selection prompt and content, which also show
    ``career_description``, ``family_titles`` and ``family_size``; every other arm ignores
    them. When the code refused some of its picks for breaking a stated rule, it gets one
    repair round (``_repair_shape_pick``) instead of coming back short.

    A backend, parse or configuration failure yields a variant with no courses and the
    failure in ``error``, never an exception: one failed arm must not cost the run the
    others, or the pathway that is actually delivered.
    """
    variant = Variant(strategy=strategy, requested_size=requested_size, shape=shape)
    level_quota = dict(zip(LEVEL_ORDER, shape)) if shape is not None else None
    # Seats belong to a shape; a size arm has no rungs to seat them on.
    placement = place_seats(eligible, shape, seats) if shape is not None else SeatPlacement()
    variant.seats = placement.records
    seat_dropped = {'seat_rejected': placement.rejected} if placement.rejected else {}
    variant.dropped = dict(seat_dropped)
    seated_keys = {candidate.key for candidate in placement.seated}
    if level_quota is not None:
        eligible = [
            candidate for candidate in eligible
            if placement.open_quota.get(candidate.level_type, 0) > 0 and candidate.key not in seated_keys
        ]
    eligible_keys = {candidate.key for candidate in eligible}
    shown = [candidate for candidate in candidate_dicts if candidate.get('key') in eligible_keys]
    seated_dicts = _seated_dicts(candidate_dicts, placement.seated)
    if shape is not None and placement.seated and not any(placement.open_quota.values()):
        # The seats are the whole shape: there is nothing left to ask for.
        variant.courses = _in_taught_order(placement.seated)
        return variant
    if shape is not None and not shown:
        # Nothing on the shape's open rungs: asking would only invite invented keys.
        variant.courses = _in_taught_order(placement.seated)
        variant.error = 'no candidates on the rungs this shape needs'
        return variant

    if strategy == STRATEGY_SHAPE_PICK_V2:
        system_prompt = selection_system_prompt_v2(placement.open_shape)
        user_content = build_selection_content_v2(
            career_name=career_name, career_skills=career_skills, career_description=career_description,
            family_titles=family_titles, family_size=family_size, candidates=shown,
            already_chosen=seated_dicts,
        )
    else:
        system_prompt = selection_system_prompt(
            requested_size, placement.open_shape if shape is not None else None,
        )
        user_content = build_selection_content(
            career_name=career_name, career_skills=career_skills, candidates=shown,
            already_chosen=seated_dicts,
        )

    try:
        model_backend = backend or get_variant_backend()
        response = model_backend.complete(
            system_prompt=system_prompt,
            user_content=user_content,
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
        eligible, keys, max_size=requested_size or MAX_PATHWAY_SIZE, level_quota=level_quota,
        seated=placement.seated, single_ecosystem=single_ecosystem,
    )
    if fabricated:
        logger.warning('Variant selection returned %d key(s) absent from the candidates; dropped.',
                       len(fabricated))
    variant.courses, variant.dropped, variant.fabricated_keys = courses, {**dropped, **seat_dropped}, fabricated
    if strategy == STRATEGY_SHAPE_PICK_V2 and shape is not None and not variant.is_complete \
            and (fabricated or any(dropped.get(reason) for reason in REPAIRABLE_DROPS)):
        _repair_shape_pick(
            variant, backend=model_backend, trace_id=trace_id, eligible=eligible, candidate_dicts=candidate_dicts,
            level_quota=level_quota, requested_size=requested_size, career_name=career_name,
            career_skills=career_skills, career_description=career_description, family_titles=family_titles,
            family_size=family_size, single_ecosystem=single_ecosystem,
        )
    return variant


def _repair_shape_pick(variant, *, backend, trace_id, eligible, candidate_dicts, level_quota, requested_size,
                       career_name, career_skills, career_description, family_titles, family_size,
                       single_ecosystem=False):
    """
    Ask once more for the places the code had to refuse, with the rules now impossible to break.

    Everything accepted so far -- seats and the first answer's picks -- is shown as already
    chosen, the shape sentence states only the places still open, and the candidates shown
    exclude any on a full rung or from a provider already at the cap. The second answer goes
    through ``apply_selection`` with the accepted courses seated, so every rule still holds.
    One round only; a second shortfall is recorded, never retried. A failed repair keeps the
    first answer, with the failure in ``variant.repair['error']``.
    """
    chosen = list(variant.courses)
    chosen_keys = {course.key for course in chosen}
    open_quota = dict(level_quota)
    per_partner: dict = {}
    for course in chosen:
        if course.level_type in open_quota:
            open_quota[course.level_type] -= 1
        if course.partner:
            per_partner[course.partner] = per_partner.get(course.partner, 0) + 1

    def allowed_now(candidate):
        if candidate.key in chosen_keys or open_quota.get(candidate.level_type, 0) <= 0:
            return False
        return not (candidate.partner and per_partner.get(candidate.partner, 0) >= MAX_PER_PARTNER)

    stack = EcosystemTracker(single_ecosystem, chosen)
    allowed = [candidate for candidate in eligible if allowed_now(candidate) and not stack.refuses(candidate)]
    variant.repair = {'attempted': False, 'added': 0}
    if not allowed:
        variant.repair['error'] = 'no candidate left that the rules allow'
        return
    allowed_keys = {candidate.key for candidate in allowed}
    open_shape = tuple(max(0, open_quota.get(level, 0)) for level in LEVEL_ORDER)
    variant.repair['attempted'] = True
    try:
        response = backend.complete(
            system_prompt=selection_system_prompt_v2(open_shape),
            user_content=build_selection_content_v2(
                career_name=career_name, career_skills=career_skills, career_description=career_description,
                family_titles=family_titles, family_size=family_size,
                candidates=[c for c in candidate_dicts if c.get('key') in allowed_keys],
                already_chosen=_seated_dicts(candidate_dicts, chosen),
            ),
            trace_id=f'{trace_id}:repair',
        )
        variant.repair['trace'] = response.to_trace_dict()
        payload = response.as_json()
    except ModelBackendError as exc:
        variant.repair['error'] = f'repair failed ({type(exc).__name__})'
        return
    keys = payload.get('keys') if isinstance(payload, dict) else None
    if not isinstance(keys, list):
        variant.repair['error'] = 'repair response had no keys list'
        return
    courses, dropped, fabricated = apply_selection(
        allowed, keys, max_size=requested_size or MAX_PATHWAY_SIZE, level_quota=level_quota, seated=chosen,
        single_ecosystem=single_ecosystem,
    )
    variant.repair.update({
        'added': len(courses) - len(chosen), 'dropped': dropped, 'fabricated_keys': fabricated,
    })
    variant.courses = courses


def _seated_dicts(candidate_dicts: list[dict], seated: list) -> list[dict]:
    """The ``CourseCandidate`` dicts of the seated courses, in seat order."""
    by_key = {}
    for candidate in candidate_dicts:
        by_key.setdefault(candidate.get('key'), candidate)
    return [
        by_key.get(course.key) or {
            'key': course.key, 'title': course.title, 'level_type': course.level_type, 'partner': course.partner,
        }
        for course in seated
    ]


def _eligible_dicts(ordered_candidates: list[dict], eligible: list) -> list[dict]:
    """The ``CourseCandidate`` dicts of the eligible candidates, in relevance order, once each."""
    eligible_keys = {candidate.key for candidate in eligible}
    seen, kept = set(), []
    for candidate in ordered_candidates:
        key = candidate.get('key')
        if key in eligible_keys and key not in seen:
            seen.add(key)
            kept.append(candidate)
    return kept


def plan_shape_seats(*, policy, shape: tuple, ordered_candidates: list[dict], career_skills: list[str],
                     seat_planner=None) -> tuple[list, str]:
    """
    Ask the editorial planner for one shape's seats: ``(seats, error)``.

    ``seat_planner`` defaults to ``pathway_editorial.api.plan_seats``, imported only now. A
    planner failure -- including an editorial app that is not installed -- is returned as an
    error for the shape's variants to carry, never raised: like a failed model call, it
    costs those arms and nothing else.
    """
    try:
        planner = seat_planner or load_editorial_api().plan_seats
        seats = planner(
            ordered_candidates=ordered_candidates, shape=tuple(shape),
            career_skills=list(career_skills or []), policy=policy,
        )
        return list(seats or []), ''
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning('Editorial seat planning failed for shape %s: %s', shape_name(shape), exc)
        return [], f'seat planning failed ({type(exc).__name__}): {exc}'


def build_variants(*, career_name: str, career_skills: list[str], ordered_candidates: list[dict],
                   sizes, strategies, trace_prefix: str, backend=None, shapes=(), policy=None,
                   career_description: str = '', family_titles=(), family_size: int = 0,
                   seat_planner=None, single_ecosystem: bool = False) -> list[Variant]:
    """
    Build every requested variant from one candidate window.

    Args:
        ordered_candidates: ``CourseCandidate`` dicts in relevance order.
        sizes: Requested sizes; see ``normalise_sizes``.
        strategies: Requested strategies; see ``normalise_strategies``.
        trace_prefix: Each model call's trace id is this plus the variant label.
        backend: Overrides ``get_variant_backend()`` for the model arms.
        shapes: Shapes for the shape arms; see ``normalise_shapes``.
        policy: An editorial policy, or ``None`` for none (the default). Its
            ``excluded_keys`` are ineligible for every arm; its seats are planned once per
            shape and shared by every shape arm. See the module docstring.
        career_description, family_titles, family_size: What ``shape_pick_v2`` shows of
            the career and the job titles its pathway serves.
        seat_planner: Overrides ``pathway_editorial.api.plan_seats``, with its signature.

    Returns:
        Variants in a stable order: by strategy as listed in ``ALL_STRATEGIES``, then by
        size or by shape as requested.
    """
    sizes = normalise_sizes(sizes)
    strategies = normalise_strategies(strategies)
    shapes = [parse_shape(shape) for shape in normalise_shapes(shapes)]
    eligible, _ = eligible_candidates(
        [_assembly_hit(candidate) for candidate in ordered_candidates],
        excluded_keys=policy_excluded_keys(policy),
    )

    # One seat plan per shape, shared by every shape arm, so the arms differ in how they fill
    # the open places and not in which places were open.
    seat_plans: dict = {}
    planner_window = _eligible_dicts(ordered_candidates, eligible) if policy is not None else []

    def seats_for(shape):
        if policy is None:
            return [], ''
        if shape not in seat_plans:
            seat_plans[shape] = plan_shape_seats(
                policy=policy, shape=shape, ordered_candidates=planner_window,
                career_skills=career_skills, seat_planner=seat_planner,
            )
        return seat_plans[shape]

    def shape_variant(strategy, shape):
        seats, error = seats_for(shape)
        if error:
            return Variant(strategy=strategy, requested_size=sum(shape), shape=shape, error=error)
        if strategy == STRATEGY_SHAPE_CUT:
            return shape_cut(eligible, shape, seats=seats, single_ecosystem=single_ecosystem)
        return model_select(
            strategy=strategy,
            requested_size=sum(shape),
            shape=shape,
            seats=seats,
            single_ecosystem=single_ecosystem,
            career_name=career_name,
            career_skills=career_skills,
            career_description=career_description,
            family_titles=family_titles,
            family_size=family_size,
            candidate_dicts=ordered_candidates,
            eligible=eligible,
            trace_id=f'{trace_prefix}:{variant_label(strategy, sum(shape), shape)}',
            backend=backend,
        )

    variants = []
    for strategy in strategies:
        if strategy == STRATEGY_RANKED_CUT:
            variants.extend(ranked_cut(eligible, size, single_ecosystem=single_ecosystem) for size in sizes)
            continue
        if strategy in SHAPE_STRATEGIES:
            variants.extend(shape_variant(strategy, shape) for shape in shapes)
            continue
        requested = sizes if strategy in SIZED_STRATEGIES else [None]
        for size in requested:
            variants.append(model_select(
                strategy=strategy,
                requested_size=size,
                single_ecosystem=single_ecosystem,
                career_name=career_name,
                career_skills=career_skills,
                candidate_dicts=ordered_candidates,
                eligible=eligible,
                trace_id=f'{trace_prefix}:{variant_label(strategy, size)}',
                backend=backend,
            ))
    return variants
