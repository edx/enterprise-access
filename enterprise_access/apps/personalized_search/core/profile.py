"""
The ``profile`` block (tech spec KD-11): what the AI knows about the learner, for the "Why these?" popover.

Built from the merged intent with no model call, so it can only restate what the learner said. Built after
the results are chosen: a preference stays only while the shown results reflect it, so the popover never
explains the results with something that no longer drives them.
"""
SUMMARY = ("I picked these from your organization's catalog using what you told me: your goal, how you like "
           'to learn and the time you have.')
MAX_CHIPS = 6
CHIP_CAP = 40
PACING_LABELS = {'self_paced': 'Self-paced', 'instructor_paced': 'Instructor-led'}
TYPE_LABELS = {'course': 'Courses', 'executive_education': 'Executive Education', 'program': 'Programs'}


def _chip(text: str) -> str:
    return text.strip()[:CHIP_CAP]


def _hit(candidate: dict) -> dict:
    return candidate.get('hit') or {}


def _card(candidate: dict) -> dict:
    return candidate.get('card') or {}


def _facts(candidate: dict) -> dict:
    return _card(candidate).get('facts') or {}


def _hours(candidate: dict):
    facts = _facts(candidate)
    return facts.get('hours_per_week_max') or facts.get('hours_per_week_min')


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


def _has_skill(skill: str):
    wanted = skill.casefold()
    return lambda names: any(wanted in name.casefold() or name.casefold() in wanted
                             for name in names if isinstance(name, str))


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
        chips.append(roles[0])
    chips += [skill for skill in (intent.get('skills') or [])[:2]
              if isinstance(skill, str) and _reflected(shown, lambda c: _hit(c).get('skill_names'), _has_skill(skill))]
    level = intent.get('level')
    if level and _reflected(shown, lambda c: _hit(c).get('level_type'),
                            lambda value: str(value).casefold() == str(level).casefold()):
        chips.append(str(level).capitalize())
    pacing = constraints.get('pacing')
    if pacing in PACING_LABELS and _reflected(shown, lambda c: _facts(c).get('pacing_type'),
                                              lambda value: value == pacing):
        chips.append(PACING_LABELS[pacing])
    max_hours = constraints.get('max_hours_per_week')
    if max_hours and _reflected(shown, _hours, lambda value: value <= max_hours):
        chips.append(f'Up to {max_hours} hrs/week')
    max_weeks = constraints.get('max_weeks')
    if max_weeks and _reflected(shown, lambda c: _facts(c).get('weeks_to_complete'),
                                lambda value: value <= max_weeks):
        chips.append(f'Up to {max_weeks} weeks')
    types = intent.get('item_types') or []
    if types and len(types) < len(TYPE_LABELS) and _reflected(
            shown, lambda c: _card(c).get('item_type'), lambda value: value in types):
        chips.append(' and '.join(TYPE_LABELS[t] for t in types if t in TYPE_LABELS))
    chips = list(dict.fromkeys(_chip(c) for c in chips if isinstance(c, str) and c.strip()))[:MAX_CHIPS]
    if not chips:
        return None
    return {'summary': SUMMARY, 'chips': chips}
