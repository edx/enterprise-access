"""
The ``profile`` block (tech spec KD-11): what the AI knows about the learner, for the "Why these?" popover.

Built from the merged intent with no model call. That intent is the intent model's reading of the conversation,
so a chip can be inferred rather than said, and the summary says what the AI understood, not what the learner
told it. Built after the results are chosen: a preference stays only while the shown results reflect it, so the
popover never explains the results with something that no longer drives them.
"""
import re

from enterprise_access.apps.personalized_search.core import constants

SUMMARY = "Here's what I understood you're looking for."
# What each kind of chip is about, in summary order: the summary names only the kinds its chips show.
SUMMARY_TOPICS = (('goal', 'your goal'), ('level', 'your level'), ('format', 'how you like to learn'),
                  ('time', 'the time you have'))
MAX_CHIPS = 6
CHIP_CAP = 40
PACING_LABELS = {'self_paced': 'Self-paced', 'instructor_paced': 'Instructor-led'}


def _chip(text: str) -> str:
    return text.strip()[:CHIP_CAP]


def _hit(candidate: dict) -> dict:
    return candidate.get('hit') or {}


def _card(candidate: dict) -> dict:
    return candidate.get('card') or {}


def _facts(candidate: dict) -> dict:
    return _card(candidate).get('facts') or {}


def _hours(candidate: dict):
    """The fewest weekly hours a result asks for: within the learner's limit, it fits (as ``retrieval.prefilter``)."""
    return _facts(candidate).get('hours_per_week_min')


def _reflected(shown: list[dict] | None, fact, meets) -> bool:
    """
    True when at least half of the shown results that have the fact meet the preference. With no shown
    results to check (``None``), every preference counts. When no shown result has the fact, the
    preference couldn't have shaped them, so it doesn't count.
    """
    if shown is None:
        return True
    known = [value for value in (fact(c) for c in shown) if value not in (None, '', [])]
    return bool(known) and 2 * sum(1 for value in known if meets(value)) >= len(known)


def _words(text: str) -> set[str]:
    return set(re.findall(r'[\w+#]+', text.casefold()))


def _has_skill(skill: str):
    """
    Whether a result's skill names include ``skill``: every word of the shorter name is a whole word of the
    longer one ("Leadership" matches "Leadership Development"; "R" doesn't match "Leadership", nor "SQL" "PostgreSQL").
    """
    wanted = _words(skill)

    def matches(name: str) -> bool:
        have = _words(name)
        shorter, longer = (wanted, have) if len(wanted) <= len(have) else (have, wanted)
        return bool(shorter) and shorter <= longer

    return lambda names: any(matches(name) for name in names if isinstance(name, str))


def profile_props(merged_intent: dict | None, shown: list[dict] | None = None) -> dict | None:
    """
    ``{summary, chips}`` for the learner's merged intent, or None when there's nothing to show. ``shown``
    is the candidates on screen this turn; a preference none or few of them meet is left out. The goal
    (role) always stays: it is the search itself.
    """
    intent = merged_intent or {}
    constraints = intent.get('constraints') or {}
    chips = []
    roles = intent.get('roles') or []
    if roles:
        chips.append((roles[0], 'goal'))
    chips += [(skill, 'goal') for skill in (intent.get('skills') or [])[:2]
              if isinstance(skill, str) and _reflected(shown, lambda c: _hit(c).get('skill_names'), _has_skill(skill))]
    level = intent.get('level')
    if level and _reflected(shown, lambda c: _hit(c).get('level_type'),
                            lambda value: str(value).casefold() == str(level).casefold()):
        chips.append((str(level).capitalize(), 'level'))
    pacing = constraints.get('pacing')
    if pacing in PACING_LABELS and _reflected(shown, lambda c: _facts(c).get('pacing_type'),
                                              lambda value: value == pacing):
        chips.append((PACING_LABELS[pacing], 'format'))
    max_hours = constraints.get('max_hours_per_week')
    if max_hours and _reflected(shown, _hours, lambda value: value <= max_hours):
        chips.append((f'Up to {max_hours} hrs/week', 'time'))
    max_weeks = constraints.get('max_weeks')
    if max_weeks and _reflected(shown, lambda c: _facts(c).get('weeks_to_complete'),
                                lambda value: value <= max_weeks):
        chips.append((f'Up to {max_weeks} weeks', 'time'))
    # Distinct known types, labelled as their sections are: a repeated type neither doubles nor hides the chip.
    types = [t for t in dict.fromkeys(intent.get('item_types') or []) if t in constants.SECTION_LABELS]
    if types and len(types) < len(constants.ITEM_TYPES) and _reflected(
            shown, lambda c: _card(c).get('item_type'), lambda value: value in types):
        chips.append((' and '.join(constants.SECTION_LABELS[t] for t in types), 'format'))
    kept = {}
    for chip, kind in chips:
        if isinstance(chip, str) and chip.strip():
            kept.setdefault(_chip(chip), kind)
    kept = dict(list(kept.items())[:MAX_CHIPS])
    if not kept:
        return None
    return {'summary': summary(set(kept.values())), 'chips': list(kept)}


def summary(kinds: set[str]) -> str:
    """What the AI understood, naming only the kinds of chip shown (``SUMMARY_TOPICS``); ``SUMMARY`` for none."""
    topics = [topic for kind, topic in SUMMARY_TOPICS if kind in kinds]
    if not topics:
        return SUMMARY
    named = topics[0] if len(topics) == 1 else f"{', '.join(topics[:-1])} and {topics[-1]}"
    return f"Here's what I understood about {named}."
