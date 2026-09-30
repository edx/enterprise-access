"""
Which vendor's world a course lives in, and why a pathway should stay in one of them.

Bench round 2 (2026-09-29) measured this and it is the largest effect the reviews have turned
up. Of 81 rated pathways, the 16 that spanned two vendors' products were rated good 25% of the
time against 69% for the rest; within the same career, where "this career is simply harder"
cannot explain it, 26% against 62%. They also drew four times the corrections: 1.00 dropped
courses per pathway against 0.25.

The reviewer's own words for it:

    "I don't like the overly specific Microsoft tech courses in the generic pathways. Should
    only appear if Microsoft is specifically mentioned."

    "It would be OK to create a pathway that was of a specfic company tech... Microsoft, AWS,
    google, whatever... But it needs to stay on topic with that."

    "too much mixed tech.. We have the courses, its moer how it's assembled."

The last is the point. For every one of those 16 pathways a course from the same vendor, or
from none, was already sitting in the same rung of the same retrieved window -- and for 9 of
them the reviewer had already marked one of those courses acceptable himself. Nothing had to be
found; the wrong one was chosen.

**A rule in code, not a line in a prompt.** The second selection prompt already asks for exactly
this ("prefer transferable skills over one vendor's product", "stay on the same programming
language or technical stack") and 16 pathways mixed anyway. This belongs with the provider cap
and the level quota, which assembly guarantees, rather than with the things a model is asked for
and may or may not do.

**A course's ecosystem comes from what it teaches, not from who published it.** IBM publishes
plenty of courses that teach nothing of IBM's -- "Project Management Basics" is one -- and
penalising those would be reading the byline rather than the content. So the patterns below are
matched against the title and the skill tags. Measured both ways on the round-2 reviews, the
stricter reading separates the verdicts better (25% against 69%, versus 30% against 76% when the
publisher counts), which is what a rule should be built on.

Nothing here prefers a vendor-free course to a vendor's one; that was considered and set aside
(Brian, 2026-09-29). The rule is only that a pathway may not span two.
"""

import re

#: Product and platform names, by the ecosystem they belong to. Matched case-insensitively
#: against a course's title and skill tags. A name only earns a place here if a course teaching
#: it would be of little use to someone working in another vendor's stack.
ECOSYSTEM_PATTERNS = {
    'Microsoft': r'\bmicrosoft\b|\bazure\b|\bpower ?bi\b|\bdynamics\b|\bcopilot\b|\bsharepoint\b|\bm365\b|office 365',
    'Amazon': r'\baws\b|amazon web|\bec2\b|\bredshift\b|\bsagemaker\b',
    'Google': r'\bgoogle\b|\bgcp\b|\bbigquery\b|\bappsheet\b|\blooker\b',
    'IBM': r'\bibm\b|\bwatson\b|\bcognos\b',
    'Oracle': r'\boracle\b',
    'Salesforce': r'\bsalesforce\b',
    'SAP': r'\bsap\b',
    'Tableau': r'\btableau\b',
}

_COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in ECOSYSTEM_PATTERNS.items()}

#: What a rejected course is counted under, beside ``provider_cap`` and the rest.
ECOSYSTEM_DROP = 'other_ecosystem'


def ecosystems_of(title: str = '', skill_names=()) -> frozenset:
    """
    The ecosystems a course teaches, empty when it teaches none.

    A course may name more than one -- a comparison of Azure and AWS, say -- in which case it
    belongs to both and can sit beside either.
    """
    text = ' '.join([title or ''] + [name for name in (skill_names or []) if isinstance(name, str)])
    return frozenset(name for name, pattern in _COMPILED.items() if pattern.search(text))


def candidate_ecosystems(candidate) -> frozenset:
    """``ecosystems_of`` for a ``pathway_assembly.Candidate`` or a ``CourseCandidate`` dict."""
    if isinstance(candidate, dict):
        return ecosystems_of(candidate.get('title', ''), candidate.get('skill_names') or ())
    return ecosystems_of(getattr(candidate, 'title', ''), getattr(candidate, 'skill_names', ()) or ())


def would_span_two(chosen: frozenset, candidate_ecosystem: frozenset) -> bool:
    """
    Whether adding a course would leave the pathway spanning two vendors' products.

    ``chosen`` is what the pathway is committed to so far: empty while it teaches no vendor's
    product, and otherwise the ecosystems every vendor-specific course in it shares. A course
    naming none is always allowed, and a course naming one the pathway already has is too.
    """
    if not candidate_ecosystem or not chosen:
        return False
    return not (chosen & candidate_ecosystem)


def commit(chosen: frozenset, candidate_ecosystem: frozenset) -> frozenset:
    """
    The pathway's ecosystems once a course joins it.

    Narrowing rather than accumulating: a course naming both Azure and AWS leaves the pathway
    free to take either later, and the first single-ecosystem course after it settles which.
    """
    if not candidate_ecosystem:
        return chosen
    return (chosen & candidate_ecosystem) if chosen else candidate_ecosystem


class EcosystemTracker:
    """
    The ecosystems a pathway has committed to, as courses are added one at a time.

    Used where the provider cap is: a small piece of state threaded through whichever arm is
    building, so the rule holds however the courses were chosen.
    """

    def __init__(self, enabled: bool = True, courses=()):
        self.enabled = enabled
        self.chosen = frozenset()
        self.refused = 0
        for course in courses:
            self.take(course)

    def refuses(self, course) -> bool:
        """Whether this course would leave the pathway spanning two ecosystems."""
        if not self.enabled:
            return False
        return would_span_two(self.chosen, candidate_ecosystems(course))

    def refuse(self) -> None:
        """Count a course turned away, for the variant's ``dropped`` tally."""
        self.refused += 1

    def take(self, course) -> None:
        """Record a course as part of the pathway."""
        if self.enabled:
            self.chosen = commit(self.chosen, candidate_ecosystems(course))

    @property
    def dropped(self) -> dict:
        """The tally to fold into a variant's ``dropped``."""
        return {ECOSYSTEM_DROP: self.refused} if self.refused else {}
