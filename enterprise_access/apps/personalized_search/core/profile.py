"""
The ``profile`` block (tech spec KD-11): what the AI knows about the learner, for the "Why these?" popover.

Built from the merged intent with no model call, so it can only restate what the learner said.
"""
SUMMARY = ("I picked these from your organization's catalog using what you told me: your goal, how you like "
           'to learn and the time you have.')
MAX_CHIPS = 6
CHIP_CAP = 40
PACING_LABELS = {'self_paced': 'Self-paced', 'instructor_paced': 'Instructor-led'}
TYPE_LABELS = {'course': 'Courses', 'executive_education': 'Executive Education', 'program': 'Programs'}


def _chip(text: str) -> str:
    return text.strip()[:CHIP_CAP]


def profile_props(merged_intent: dict | None) -> dict | None:
    """``{summary, chips}`` for the learner's merged intent, or None when there's nothing to show."""
    intent = merged_intent or {}
    constraints = intent.get('constraints') or {}
    chips = []
    roles = intent.get('roles') or []
    if roles:
        chips.append(roles[0])
    chips += (intent.get('skills') or [])[:2]
    if intent.get('level'):
        chips.append(str(intent['level']).capitalize())
    if constraints.get('pacing') in PACING_LABELS:
        chips.append(PACING_LABELS[constraints['pacing']])
    if constraints.get('max_hours_per_week'):
        chips.append(f"Up to {constraints['max_hours_per_week']} hrs/week")
    if constraints.get('max_weeks'):
        chips.append(f"Up to {constraints['max_weeks']} weeks")
    types = intent.get('item_types') or []
    if types and len(types) < len(TYPE_LABELS):
        chips.append(' and '.join(TYPE_LABELS[t] for t in types if t in TYPE_LABELS))
    chips = list(dict.fromkeys(_chip(c) for c in chips if isinstance(c, str) and c.strip()))[:MAX_CHIPS]
    if not chips:
        return None
    return {'summary': SUMMARY, 'chips': chips}
