"""
Public interface for pathway editorial rules.

The pathway pipeline codes against this module only. It reads the active rules once with
``load_policy`` and hands the resulting ``EditorialPolicy`` to ``plan_seats``, which is a
pure function: no database, no network. A policy round-trips through ``to_dict`` and
``from_dict``, so an experiment can freeze the rules it ran under and replay them later
(see the ``export_editorial_policy`` command).

``plan_seats`` decides which courses editorial rules reserve seats for. It does not fill
the rest of the pathway; relevance order does that, in the pipeline, around these seats.
That fill must drop excluded courses too (``is_excluded``): leaving an excluded course out
of the seats does not keep it out of the pathway.

Name matching (skills, subjects, course keys, levels) ignores case and surrounding space.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

LEVELS = ('Introductory', 'Intermediate', 'Advanced')

#: The order in which a promoted topic's seat is tried across rungs.
PROMOTION_RUNG_ORDER = ('Intermediate', 'Introductory', 'Advanced')

FLAGSHIP_RULE = 'flagship'
PROMOTED_RULE_PREFIX = 'promoted:'


def _norm(value) -> str:
    """Comparison form of a name: stripped and case-folded. Non-strings compare as ''."""
    return value.strip().casefold() if isinstance(value, str) else ''


def _names(values: Iterable | None) -> tuple[str, ...]:
    """
    Canonical tuple of names: stripped, blanks and case-insensitive repeats dropped, sorted.

    Sorted because these are sets in meaning, and a canonical order is what makes a policy
    compare equal to itself after a JSON round trip.
    """
    if isinstance(values, str):
        values = [values]
    unique = {}
    for value in values or ():
        if isinstance(value, str) and value.strip():
            unique.setdefault(_norm(value), value.strip())
    return tuple(sorted(unique.values(), key=lambda name: (name.casefold(), name)))


def _name_set(values: Iterable | None) -> set[str]:
    """Comparison set of names."""
    if isinstance(values, str):
        values = [values]
    return {_norm(value) for value in values or () if _norm(value)}


def _canonical_level(level) -> str:
    """The ``LEVELS`` spelling of ``level``, or raise ``ValueError``."""
    for known in LEVELS:
        if _norm(level) == known.casefold():
            return known
    raise ValueError(f'level must be one of {LEVELS}, got {level!r}.')


@dataclass(frozen=True)
class FlagshipRule:
    """A course to seat ahead of relevance order at ``level``, where it is on topic."""

    course_key: str
    level: str
    scope_skills: tuple[str, ...] = ()
    reason: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'course_key', (self.course_key or '').strip())
        object.__setattr__(self, 'level', _canonical_level(self.level))
        object.__setattr__(self, 'scope_skills', _names(self.scope_skills))
        object.__setattr__(self, 'reason', (self.reason or '').strip())
        if not self.course_key:
            raise ValueError('A flagship rule needs a course_key.')

    def to_dict(self) -> dict:
        """JSON-safe form."""
        return {
            'course_key': self.course_key,
            'level': self.level,
            'scope_skills': list(self.scope_skills),
            'reason': self.reason,
        }


@dataclass(frozen=True)
class PromotedTopic:
    """
    A topic that may take up to ``max_per_pathway`` seats, each one on the career's topic.

    A course belongs to the topic by one of its ``subjects`` or by a word in its title
    matching one of ``title_terms``. ``skill_names`` does not make a course belong: skill
    tags are too loosely applied. It lists the topic's own skills, which never count as
    evidence that a course is about a career's work.
    """

    name: str
    subjects: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    max_per_pathway: int = 1
    gate_top_k: int = 10
    title_terms: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'name', (self.name or '').strip())
        object.__setattr__(self, 'subjects', _names(self.subjects))
        object.__setattr__(self, 'skill_names', _names(self.skill_names))
        object.__setattr__(self, 'max_per_pathway', max(0, int(self.max_per_pathway)))
        object.__setattr__(self, 'gate_top_k', max(0, int(self.gate_top_k)))
        object.__setattr__(self, 'title_terms', _names(self.title_terms))
        if not self.name:
            raise ValueError('A promoted topic needs a name.')

    def to_dict(self) -> dict:
        """JSON-safe form."""
        return {
            'name': self.name,
            'subjects': list(self.subjects),
            'skill_names': list(self.skill_names),
            'max_per_pathway': self.max_per_pathway,
            'gate_top_k': self.gate_top_k,
            'title_terms': list(self.title_terms),
        }


@dataclass(frozen=True)
class EditorialPolicy:
    """
    Every editorial rule a pathway is built under, as plain values.

    ``flagships`` and ``promoted`` are ordered: earlier rules are seated first. That order
    is kept through ``to_dict``; everything that is a set in meaning is sorted.
    """

    excluded_keys: frozenset = frozenset()
    flagships: tuple = ()        # FlagshipRule, in rule order
    promoted: tuple = ()         # PromotedTopic

    def __post_init__(self):
        object.__setattr__(
            self, 'excluded_keys',
            frozenset(key.strip() for key in self.excluded_keys or () if isinstance(key, str) and key.strip()),
        )
        object.__setattr__(self, 'flagships', tuple(self.flagships or ()))
        object.__setattr__(self, 'promoted', tuple(self.promoted or ()))

    @property
    def is_empty(self) -> bool:
        """True when the policy holds no rule of any kind."""
        return not (self.excluded_keys or self.flagships or self.promoted)

    def to_dict(self) -> dict:
        """JSON-safe, sorted and stable: the same policy always serialises the same way."""
        return {
            'excluded_keys': sorted(self.excluded_keys, key=lambda key: (key.casefold(), key)),
            'flagships': [rule.to_dict() for rule in self.flagships],
            'promoted': [topic.to_dict() for topic in self.promoted],
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> 'EditorialPolicy':
        """Rebuild a policy from ``to_dict`` output. ``None`` or ``{}`` gives the empty policy."""
        data = data or {}
        return cls(
            excluded_keys=frozenset(data.get('excluded_keys') or ()),
            flagships=tuple(
                FlagshipRule(
                    course_key=item['course_key'],
                    level=item['level'],
                    scope_skills=tuple(item.get('scope_skills') or ()),
                    reason=item.get('reason') or '',
                )
                for item in data.get('flagships') or ()
            ),
            promoted=tuple(
                PromotedTopic(
                    name=item['name'],
                    subjects=tuple(item.get('subjects') or ()),
                    skill_names=tuple(item.get('skill_names') or ()),
                    max_per_pathway=item.get('max_per_pathway', 1),
                    gate_top_k=item.get('gate_top_k', 10),
                    title_terms=tuple(item.get('title_terms') or ()),
                )
                for item in data.get('promoted') or ()
            ),
        )


@dataclass(frozen=True)
class Seat:
    """A pathway seat an editorial rule reserved for one course."""

    key: str
    level: str
    rule: str      # 'flagship' or 'promoted:<topic name>'
    reason: str = ''

    def to_dict(self) -> dict:
        """JSON-safe form."""
        return {'key': self.key, 'level': self.level, 'rule': self.rule, 'reason': self.reason}


def load_policy() -> EditorialPolicy:
    """
    The policy formed by the ACTIVE rule rows.

    Flagships are in the order they were created, promoted topics in name order, so two
    loads of the same rows always give the same policy.
    """
    # Imported here so the pure half of this module never needs the app registry.
    from enterprise_access.apps.pathway_editorial.models import (  # pylint: disable=import-outside-toplevel
        CourseRuleAction,
        PathwayCourseRule,
        PathwayPromotedTopic
    )

    rules = PathwayCourseRule.objects.filter(is_active=True).order_by('created', 'pk')
    excluded, flagships = set(), []
    for row in rules:
        if row.action == CourseRuleAction.EXCLUDE:
            excluded.add(row.course_key)
        elif row.action == CourseRuleAction.FLAGSHIP:
            flagships.append(FlagshipRule(
                course_key=row.course_key,
                level=row.level,
                scope_skills=tuple(row.scope_skills or ()),
                reason=row.reason,
            ))
    promoted = tuple(
        PromotedTopic(
            name=row.name,
            subjects=tuple(row.subjects or ()),
            skill_names=tuple(row.skill_names or ()),
            max_per_pathway=row.max_per_pathway,
            gate_top_k=row.gate_top_k,
            title_terms=tuple(row.title_terms or ()),
        )
        for row in PathwayPromotedTopic.objects.filter(is_active=True).order_by('name', 'pk')
    )
    return EditorialPolicy(excluded_keys=frozenset(excluded), flagships=tuple(flagships), promoted=promoted)


def is_excluded(key: str, policy: EditorialPolicy) -> bool:
    """True when ``policy`` excludes the course ``key``."""
    wanted = _norm(key)
    return bool(wanted) and any(_norm(excluded) == wanted for excluded in policy.excluded_keys)


def is_promoted(candidate: dict, topic: PromotedTopic) -> bool:
    """
    True when ``candidate`` belongs to ``topic``: a subject in ``topic.subjects``, or a title
    matching one of ``topic.title_terms`` as whole words, ignoring case.

    A skill tag alone is not enough: tags such as "Artificial Intelligence" sit on courses
    that are not about the topic ("Project Management Basics"). ``subjects`` is optional
    on a candidate; windows stored before it was carried match on the title alone.
    """
    if _name_set(candidate.get('subjects')) & _name_set(topic.subjects):
        return True
    title = candidate.get('title')
    pattern = _title_pattern(topic.title_terms)
    return bool(pattern and isinstance(title, str) and pattern.search(title))


@lru_cache(maxsize=64)
def _title_pattern(terms: tuple[str, ...]):
    """
    One compiled pattern matching any of ``terms`` as whole words, ignoring case, or ``None``.

    "Whole words" means no letter, digit or underscore directly before or after the term,
    so "AI" matches "AI-Powered" and "Agile with AI" but not "Maintenance". Spaces inside
    a term match any run of whitespace.
    """
    alternatives = [
        r'\s+'.join(re.escape(word) for word in term.split())
        for term in sorted(terms, key=len, reverse=True) if term.strip()
    ]
    if not alternatives:
        return None
    return re.compile(r'(?<!\w)(?:' + '|'.join(alternatives) + r')(?!\w)', re.IGNORECASE)


def _plain_text(value) -> str:
    """``value`` with HTML tags and entities removed, whitespace collapsed, case-folded."""
    if not isinstance(value, str):
        return ''
    text = html.unescape(re.sub(r'<[^>]+>', ' ', value))
    return ' '.join(text.split()).casefold()


def _shape_quota(shape) -> dict[str, int]:
    """``shape`` as a quota per level, or raise ``ValueError``."""
    try:
        counts = tuple(int(count) for count in shape)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'shape must be three course counts, got {shape!r}.') from exc
    if len(counts) != len(LEVELS) or any(count < 0 for count in counts):
        raise ValueError(f'shape must be three non-negative course counts, got {shape!r}.')
    return dict(zip(LEVELS, counts))


def plan_seats(
    *,
    ordered_candidates: list[dict],
    shape: tuple[int, int, int],
    career_skills: list[str],
    policy: EditorialPolicy,
) -> list[Seat]:
    """
    The seats editorial rules reserve in a pathway of ``shape``, in the order they were seated.

    ``ordered_candidates`` is the career's candidate window in relevance order, and
    ``shape`` is the course count per rung (Introductory, Intermediate, Advanced).

    1. Excluded courses are ignored throughout.
    2. Flagships, in rule order: a rule's course is seated when its level has room, the
       course is in the window at that level (the window is the relevance gate), and the
       rule is unscoped or the career carries one of its ``scope_skills``.
    3. Each promoted topic, while fewer than ``max_per_pathway`` seats belong to it: try
       Intermediate, then Introductory, then Advanced, among rungs with room; within a
       rung, look only at its first ``gate_top_k`` candidates, and seat the first one that
       belongs to the topic (``is_promoted``), is about this career's work, and is not
       already seated. About the career's work means the course shares a skill tag with the
       career, and its title or descriptions name at least one of those shared skills. The
       topic's own names never count as shared: a career that lists "Artificial
       Intelligence" does not make every AI course about its work.

    No rung is ever filled past its quota, and no course is seated twice.
    """
    quota = _shape_quota(shape)
    career = _name_set(career_skills)
    usable = [
        candidate for candidate in ordered_candidates or ()
        if isinstance(candidate, dict) and _norm(candidate.get('key')) and not is_excluded(candidate['key'], policy)
    ]
    rungs = {
        level: [candidate for candidate in usable if _norm(candidate.get('level_type')) == level.casefold()]
        for level in LEVELS
    }
    seats: list[Seat] = []
    seated: dict[str, dict] = {}
    used = dict.fromkeys(LEVELS, 0)

    def has_room(level: str) -> bool:
        return used[level] < quota[level]

    def take(candidate: dict, level: str, rule: str, reason: str) -> None:
        seats.append(Seat(key=candidate['key'], level=level, rule=rule, reason=reason))
        seated[_norm(candidate['key'])] = candidate
        used[level] += 1

    for rule in policy.flagships:
        if not has_room(rule.level) or _norm(rule.course_key) in seated:
            continue
        if rule.scope_skills and not career & _name_set(rule.scope_skills):
            continue
        match = next(
            (candidate for candidate in rungs[rule.level] if _norm(candidate['key']) == _norm(rule.course_key)),
            None,
        )
        if match is not None:
            take(match, rule.level, FLAGSHIP_RULE, rule.reason)

    for topic in policy.promoted:
        count = sum(1 for candidate in seated.values() if is_promoted(candidate, topic))
        while count < topic.max_per_pathway:
            found = _next_promoted(topic, rungs, career, seated, has_room)
            if found is None:
                break
            candidate, level, named = found
            take(
                candidate, level, PROMOTED_RULE_PREFIX + topic.name,
                f'{topic.name} course about this career\'s work; its description covers {", ".join(named)}.',
            )
            count += 1

    return seats


def _next_promoted(topic, rungs, career, seated, has_room):
    """
    The next course ``topic`` may seat, as ``(candidate, level, named skills)``, or ``None``.

    Rungs are tried in ``PROMOTION_RUNG_ORDER``, each within its first ``gate_top_k``.
    """
    evidence = career - _name_set(topic.skill_names) - _name_set(topic.subjects)
    for level in PROMOTION_RUNG_ORDER:
        if not has_room(level):
            continue
        for candidate in rungs[level][:topic.gate_top_k]:
            if _norm(candidate['key']) in seated or not is_promoted(candidate, topic):
                continue
            named = _skills_named_in_text(candidate, _career_overlap(candidate, evidence))
            if named:
                return candidate, level, named
    return None


def _skills_named_in_text(candidate: dict, skills: list[str]) -> list[str]:
    """
    Those of ``skills`` that the candidate's title or descriptions contain, ignoring case.

    A plain substring test on the text with HTML removed. It is what separates a course
    whose tag merely coincides with the career's skills from one that says it teaches them.
    """
    text = ' '.join(
        _plain_text(candidate.get(field)) for field in ('title', 'short_description', 'full_description')
    )
    return [skill for skill in skills if ' '.join(skill.split()).casefold() in text]


def _career_overlap(candidate: dict, career_skills) -> list[str]:
    """The candidate's skill tags that are in ``career_skills``, as the candidate spells them, sorted."""
    career = _name_set(career_skills)
    shared = {}
    for name in candidate.get('skill_names') or ():
        if _norm(name) in career:
            shared.setdefault(_norm(name), name.strip())
    return sorted(shared.values(), key=lambda name: (name.casefold(), name))
