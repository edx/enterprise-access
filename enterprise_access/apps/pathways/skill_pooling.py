"""
One skill list for a family of careers, pooled from its members' Lightcast skills.

A career's ``skills`` in the jobs index are its top twenty by Lightcast *significance*, which
rewards a skill for being distinctive to the career rather than common in it. For a career with
a handful of postings, or a broad one, that ranking can be dominated by skills almost no posting
asks for. Bench round 4 (2026-10-05) showed what that does to a pathway:

* Operations Engineer, 2,032 postings: every one of its twenty skills appears in under 4% of them
  ("Bloom Filter" rests on 4), and its pathways drifted to supply chain.
* Field Development Representative, 119 postings, mostly animal-health sales: Veterinary
  Pathology, Animal Science, Animal Health. Ten of its eleven family members are sales or
  business development representatives.
* Reliability Engineer: mechanical reliability (thermography, vibration, Weibull), while seven
  of its twelve members are site reliability engineers.

Thirteen of that round's seventeen bad verdicts fell in four families like these.

**Pooling is the stopgap** (Brian, 2026-10-05): a family is searched with the skills its member
careers share, so one member's rare-skill noise is outweighed by the rest. A skill's weight is
the number of postings that ask for it, summed over the members (``unique_postings`` on each
skill); ties go to the skill more members carry, then by name, so the result is deterministic.

Summing postings lets large members dominate, which can wash out a specialist family whose
namesake is a small, distinct career (PLM Solution Architect is 3% of its family's postings).
How members and skills should be weighted is an open question to settle, not tune, so it is not
parameterised here beyond the list length.

Used by the evaluation harness (``collect_pathway_variants --families-file``); the delivered
pathway still searches with the learner's own career's skills.
"""
from collections import Counter
from typing import Any, Iterable

#: Matches the twenty skills the jobs index carries per career.
POOLED_SKILL_LIMIT = 20


def skill_postings(hit: dict[str, Any] | None) -> dict[str, float]:
    """
    A jobs-index hit's skills, each with the number of the career's postings that ask for it.

    Keyed by the skill's stripped name; a name repeated within one career counts once, at its
    largest figure. A missing or malformed count is 0, so the skill still joins the pool and can
    win a tie on how many members carry it.
    """
    found = {}
    for skill in (hit or {}).get('skills') or []:
        if not isinstance(skill, dict) or not isinstance(skill.get('name'), str):
            continue
        name = skill['name'].strip()
        if not name:
            continue
        try:
            postings = max(0.0, float(skill.get('unique_postings') or 0))
        except (TypeError, ValueError):
            postings = 0.0
        found[name] = max(found.get(name, 0.0), postings)
    return found


def pool_member_skills(member_hits: Iterable[dict[str, Any] | None], *,
                       limit: int = POOLED_SKILL_LIMIT) -> list[str]:
    """
    The family's skills: every member's, ranked by postings summed across members.

    Args:
        member_hits: Raw jobs-index hits, one per member career. ``None`` (a member the index
            does not hold) is skipped.
        limit: How many skills to return.

    Names are matched case-insensitively across members and returned in the spelling the
    first member to carry them used.
    """
    totals, carriers, spelling = Counter(), Counter(), {}
    for hit in member_hits:
        for name, postings in skill_postings(hit).items():
            key = name.lower()
            spelling.setdefault(key, name)
            totals[key] += postings
            carriers[key] += 1
    ranked = sorted(totals, key=lambda key: (-totals[key], -carriers[key], key))
    return [spelling[key] for key in ranked[:max(0, limit)]]
