"""
Assembling five retrieved candidates into a pathway, and checking the result.

Pure: no network, no database, no model call. Takes Algolia hits, returns a selection.
That is deliberate -- everything here is a constraint-satisfaction rule that can be
reasoned about and tested directly, so none of it should be delegated to a model.

Why constrained assembly rather than "take the top 5"
-----------------------------------------------------
Product reported the defect that motivated this: *"you'll get 2 intro courses from
different providers, so 2 101 courses but zero 102 courses."* Measured against the pinned
2U catalog on 2026-09-10, that is exactly what relevance ranking does at rank 5:

===========================  ===============  ================
Query                        top 5 (I/M/A)    top 20 (I/M/A)
===========================  ===============  ================
``data analyst``             5 / 0 / 0        16 / 3 / 1
``project manager``          5 / 0 / 0        14 / 4 / 2
``machine learning engineer``  1 / 4 / 0      7 / 10 / 3
``biomedical engineer``      4 / 1 / 0        7 / 7 / 0
===========================  ===============  ================

Three things follow, and they shape this module:

1. **The rungs exist.** 12 of 14 probe skills have courses at all three ``level_type``
   values in the 2U catalog. The intermediate courses are not missing; they are just
   below the rank-5 cut. So this is a selection defect, not a content gap.
2. **"Duplicate" means a repeated rung, not a repeated key.** The collision product
   reported is two *different* courses from *different* providers at the same level.
   De-duplicating on ``key`` is necessary and catches none of it.
3. **Reaching to rank 20 for level diversity drags in other languages.** 26.2% of the 2U
   catalog (767 of 2,927 courses) is taught in a language other than English, and it
   shows from rank 6 onward -- ``python programming`` returns 6 Spanish courses in its
   top 20. So the fix for (1) requires the language filter in ``eligible_candidates``.
   The two defects are coupled: solving the first exposes the second.

Ordering is best-effort, and says so
------------------------------------
``level_type`` is a noisy difficulty signal. Across the full 4,094-course census, 27% of
intro-titled courses are not tagged ``Introductory``, and 19% of advanced-titled ones are
tagged ``Introductory`` -- ``Advanced Project Management``, ``Data Science: Capstone`` and
``Python Programming: Intermediate Concepts`` are all tagged ``Introductory``. It is
reliable enough in aggregate to drive the quota (the table above works), and not reliable
enough to order five specific courses, so ``TITLE_LEVEL_CUES`` breaks ties within a rung
and the result is not claimed to be a guaranteed difficulty ordering.
"""
import logging
import re
from dataclasses import dataclass, field

from enterprise_access.apps.pathways.content_keys import is_valid_course_key

logger = logging.getLogger(__name__)

# Decision 3: a pathway is exactly five courses, or explicitly no pathway. Never a short
# set -- four courses returned silently reads as success to a client.
PATHWAY_SIZE = 5

# Catalog ``level_type`` values, easiest first.
LEVEL_INTRODUCTORY = 'Introductory'
LEVEL_INTERMEDIATE = 'Intermediate'
LEVEL_ADVANCED = 'Advanced'
LEVEL_ORDER = (LEVEL_INTRODUCTORY, LEVEL_INTERMEDIATE, LEVEL_ADVANCED)

# The target shape of a pathway. Sums to ``PATHWAY_SIZE``. Advanced gets one slot rather
# than a fair share because it is only 7% of the pinned catalog -- asking for two would
# fail the quota on most skills and fall through to backfill every time.
DEFAULT_LEVEL_QUOTA = {
    LEVEL_INTRODUCTORY: 2,
    LEVEL_INTERMEDIATE: 2,
    LEVEL_ADVANCED: 1,
}

# No provider may supply more than this. ``biomedical engineer`` returned 5 of 5 from one
# partner, which is a pathway a learner would read as an advertisement.
MAX_PER_PARTNER = 2

# The only language of instruction currently supported. This is the ``language``
# attribute (what the course is taught in), *not* ``metadata_language`` (which
# translation of the record is served) -- see ``docs/references/algolia_search.md``.
SUPPORTED_LANGUAGE = 'English'

# Title cues used only to break ties inside a rung, never to override ``level_type``.
TITLE_LEVEL_CUES = (
    (re.compile(r'\b(introduction|introductory|intro|basics?|fundamentals?|foundations?|'
                r'beginner|getting started|101)\b', re.IGNORECASE), -1),
    (re.compile(r'\b(advanced|expert|mastering|masterclass|capstone|deep dive)\b',
                re.IGNORECASE), 1),
)


@dataclass(frozen=True)
class Candidate:
    """One retrieved course, normalised out of an Algolia hit."""

    key: str
    title: str = ''
    level_type: str = ''
    partner: str = ''
    language: str = ''

    @classmethod
    def from_hit(cls, hit: dict) -> 'Candidate':
        """
        Build a candidate from a raw catalog hit.

        ``partners`` is a list of objects on the hit; the first is treated as the owning
        provider, which is how the learner portal attributes a course.
        """
        partners = hit.get('partners') or []
        first_partner = partners[0] if partners and isinstance(partners[0], dict) else {}
        return cls(
            key=(hit.get('key') or '').strip(),
            title=(hit.get('title') or '').strip(),
            level_type=(hit.get('level_type') or '').strip(),
            partner=(first_partner.get('name') or '').strip(),
            language=(hit.get('language') or '').strip(),
        )

    @property
    def title_cue(self) -> int:
        """-1 if the title reads as introductory, 1 if advanced, 0 if it says nothing."""
        for pattern, weight in TITLE_LEVEL_CUES:
            if pattern.search(self.title):
                return weight
        return 0

    @property
    def difficulty_rank(self) -> tuple:
        """
        Sort key for "roughly easiest first".

        ``level_type`` dominates because it is the only signal that is populated for every
        course; the title cue only orders courses that share a level.
        """
        level_index = LEVEL_ORDER.index(self.level_type) if self.level_type in LEVEL_ORDER else len(LEVEL_ORDER)
        return (level_index, self.title_cue)


@dataclass
class PathwayAssembly:
    """The result of assembling a pathway, including what could not be satisfied."""

    courses: list = field(default_factory=list)
    unfilled_rungs: list = field(default_factory=list)
    ineligible: dict = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        """Whether a full pathway was built."""
        return len(self.courses) == PATHWAY_SIZE

    @property
    def realised_level_mix(self) -> dict:
        """How many courses landed on each rung. Tracked rather than gated."""
        mix = {level: 0 for level in LEVEL_ORDER}
        for course in self.courses:
            if course.level_type in mix:
                mix[course.level_type] += 1
        return mix


def eligible_candidates(hits, *, supported_language: str = SUPPORTED_LANGUAGE):
    """
    Filter raw hits down to the courses a pathway may contain.

    Returns ``(candidates, ineligible_counts)``. The counts are returned rather than
    logged away because "the candidate set was large but mostly unusable" and "retrieval
    found little" are different diagnoses that a bare pathway cannot distinguish.
    """
    candidates = []
    ineligible: dict = {}
    seen = set()

    def reject(reason):
        ineligible[reason] = ineligible.get(reason, 0) + 1

    for hit in hits:
        candidate = Candidate.from_hit(hit)
        if not is_valid_course_key(candidate.key):
            reject('invalid_course_key')
            continue
        if candidate.key in seen:
            reject('duplicate_key')
            continue
        if supported_language and candidate.language and candidate.language != supported_language:
            reject('unsupported_language')
            continue
        seen.add(candidate.key)
        candidates.append(candidate)

    return candidates, ineligible


def assemble_pathway(hits, *, level_quota=None, max_per_partner: int = MAX_PER_PARTNER):
    """
    Select ``PATHWAY_SIZE`` courses spanning the level quota, capped per provider.

    Two passes. The first fills the quota **rung by rung, scarcest rung first**; within a
    rung, candidates are taken in relevance order, so relevance still decides *which*
    course is chosen and the quota only decides how many. The second backfills from
    whatever is left when a rung is genuinely empty, because a skill can legitimately have
    no advanced course (``Nursing`` has none in the pinned catalog) and failing the pathway
    for that would be indistinguishable from a retrieval failure.

    Why scarcest-first, which is not obvious
    ----------------------------------------
    The level quota and the provider cap compete for the same candidates, and a single
    relevance-ordered pass lets the most plentiful rung spend the scarce resource. Measured
    against the pinned 2U catalog: ``Data Analyst`` returned 17 candidates spanning all
    three rungs and still assembled to 5/0/0, because the two Introductory picks used up
    IBM's entire provider allowance and every Intermediate candidate was also IBM's.

    Processing the rung with the fewest available candidates first gives the scarce rung
    first claim on provider capacity. Advanced is only 7% of the catalog, so under a
    relevance-ordered pass it loses that competition almost every time.

    Returns a ``PathwayAssembly``. When fewer than ``PATHWAY_SIZE`` eligible candidates
    exist it returns an incomplete assembly rather than padding -- the caller reports no
    pathway, per Decision 3.
    """
    quota = dict(level_quota or DEFAULT_LEVEL_QUOTA)
    candidates, ineligible = eligible_candidates(hits)

    chosen: list = []
    per_partner: dict = {}
    chosen_keys = set()

    def take(candidate):
        chosen.append(candidate)
        chosen_keys.add(candidate.key)
        per_partner[candidate.partner] = per_partner.get(candidate.partner, 0) + 1

    def partner_is_full(candidate):
        # An unattributed course cannot be attributed to a provider, so it cannot be
        # counted against one either.
        return bool(candidate.partner) and per_partner.get(candidate.partner, 0) >= max_per_partner

    by_rung = {
        level: [c for c in candidates if c.level_type == level]
        for level in quota
    }
    # Scarcest rung first. Ties break on LEVEL_ORDER so the result stays deterministic.
    rung_order = sorted(
        quota,
        key=lambda level: (
            len(by_rung[level]),
            LEVEL_ORDER.index(level) if level in LEVEL_ORDER else len(LEVEL_ORDER),
        ),
    )

    for level in rung_order:
        for candidate in by_rung[level]:
            if len(chosen) >= PATHWAY_SIZE or quota[level] <= 0:
                break
            if partner_is_full(candidate):
                continue
            quota[level] -= 1
            take(candidate)

    unfilled = [level for level, remaining in quota.items() if remaining > 0]

    for candidate in candidates:
        if len(chosen) >= PATHWAY_SIZE:
            break
        if candidate.key in chosen_keys or partner_is_full(candidate):
            continue
        take(candidate)

    assembly = PathwayAssembly(
        courses=sorted(chosen, key=lambda candidate: candidate.difficulty_rank),
        unfilled_rungs=unfilled,
        ineligible=ineligible,
    )
    if not assembly.is_complete:
        logger.info(
            'Assembled %d of %d courses from %d eligible candidates (%d hits, ineligible: %s).',
            len(assembly.courses), PATHWAY_SIZE, len(candidates), len(hits), ineligible or {},
        )
    return assembly


def validate_pathway(courses, *, customer_catalog_keys=None,
                     max_per_partner: int = MAX_PER_PARTNER,
                     supported_language: str = SUPPORTED_LANGUAGE):
    """
    Apply the Tier 1 correctness gates and return a list of violation strings.

    Tier 1 is the set of checks that are bugs rather than quality judgements, so an empty
    list is a precondition for reporting any quality metric at all -- a pathway that fails
    these should not be scored, it should fail the build.

    ``customer_catalog_keys`` is optional because proving catalog membership needs a
    browse-scoped key (Open Decision 6); when it is not supplied that gate is skipped
    rather than assumed to pass.
    """
    violations = []

    if len(courses) != PATHWAY_SIZE:
        violations.append(
            f'a pathway must contain exactly {PATHWAY_SIZE} courses, got {len(courses)}'
        )

    keys = [course.key for course in courses]
    for key in keys:
        if not is_valid_course_key(key):
            violations.append(f'{key!r} is not a valid catalog course key')

    duplicates = {key for key in keys if keys.count(key) > 1}
    for key in sorted(duplicates):
        violations.append(f'{key!r} appears more than once')

    for course in courses:
        if course.language and course.language != supported_language:
            violations.append(
                f'{course.key!r} is taught in {course.language!r}, not {supported_language!r}'
            )

    per_partner: dict = {}
    for course in courses:
        if course.partner:
            per_partner[course.partner] = per_partner.get(course.partner, 0) + 1
    for partner, count in sorted(per_partner.items()):
        if count > max_per_partner:
            violations.append(
                f'{count} courses from {partner!r} exceeds the cap of {max_per_partner}'
            )

    if customer_catalog_keys is not None:
        for key in keys:
            if key not in customer_catalog_keys:
                violations.append(f'{key!r} is not in the pinned customer catalog')

    return violations
