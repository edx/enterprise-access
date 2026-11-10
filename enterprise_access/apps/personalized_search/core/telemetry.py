"""
Usage events for a turn (tech spec KD-12): raw facts and join keys only.

No event carries a count, rate, cost or age, and none carries learner-written text or model prose.
Those are derived downstream by joining events. The host sends them (``Steps.emit``); this module only
builds the properties from what the turn already recorded.
"""
TURN_COMPLETED = 'turn.completed'
TURN_FAILED = 'turn.failed'
TURN_STOPPED = 'turn.stopped'

_USAGE_FIELDS = (('input_tokens', 'input_tokens'), ('cached_tokens', 'cached_input_tokens'),
                 ('output_tokens', 'output_tokens'), ('reasoning_tokens', 'reasoning_tokens'))


def join_keys(ctx, record: dict) -> dict:
    """The keys every event carries, so events join to each other and to enrollments."""
    session = ctx.session
    return {
        'conversation_id': session['uuid'], 'turn_id': record.get('turn_uuid'), 'turn_index': record['sequence'],
        'enterprise_customer_uuid': session['enterprise_customer_uuid'],
        'cohort': ctx.analytics.get('cohort'), 'engine': ctx.analytics.get('engine'), 'is_debug': bool(ctx.debug),
    }


def _completed(ctx, record: dict) -> dict:
    """The ``turn.completed`` facts: intent, where each shown card sat, models, raw tokens and timings."""
    merged = record.get('merged_intent') or {}
    constraints = merged.get('constraints') or {}
    placements = record.get('placements') or []
    timing = record.get('timing') or {}
    props = {
        'outcome': 'redirected' if record.get('redirected') else ('done' if placements else 'no_match'),
        'redirect_reason': record.get('redirect_reason'),
        'intent_merge_mode': record.get('merge_mode'),
        'intent_skills': list(merged.get('skills') or []), 'intent_roles': list(merged.get('roles') or []),
        'intent_level': merged.get('level'), 'intent_item_types': list(merged.get('item_types') or []),
        'intent_max_weeks': constraints.get('max_weeks'), 'intent_pacing': constraints.get('pacing'),
        'shown_item_keys': [p['item_id'] for p in placements],
        'shown_item_types': [p['item_type'] for p in placements],
        'shown_item_sections': [p['section'] for p in placements],
        'shown_item_positions': list(range(1, len(placements) + 1)),
        'shown_item_statuses': [p.get('status') or '' for p in placements],
        'retrieved_item_keys': list(record.get('candidate_ids') or []),
        'excluded_item_keys': list(record.get('excluded_ids') or []),
        'dropped_reasons': list(record.get('dropped') or []),
        'followup_attribute': record.get('followup_attribute'),
        'intent_model': ctx.intent_model.model, 'intent_effort': ctx.intent_model.effort,
        'compose_model': ctx.compose_model.model, 'compose_effort': ctx.compose_model.effort,
        'intent_ms': timing.get('intent_ms'), 'search_ms': timing.get('retrieval_ms'),
        'first_text_ms': timing.get('first_text_ms'), 'first_tiles_ms': timing.get('first_section_ms'),
        'page_done_ms': timing.get('complete_ms'),
        'prompt_length': len(ctx.message),
    }
    for step in ('intent', 'compose'):
        usage = (record.get('usages') or {}).get(step)
        for name, attr in _USAGE_FIELDS:
            props[f'{step}_{name}'] = getattr(usage, attr, None) if usage else None
    return props


def turn_event(ctx, record: dict) -> tuple[str, dict]:
    """The one event for a finished, failed or stopped turn: its name and flat properties."""
    elapsed = {'stage': record.get('stage'), 'elapsed_ms': record.get('total_ms')}
    if record['outcome'] in ('done', 'redirect'):
        name, props = TURN_COMPLETED, _completed(ctx, record)
    elif record['outcome'] == 'error':
        name, props = TURN_FAILED, dict(elapsed, error_type=record.get('error'))
    else:
        name, props = TURN_STOPPED, elapsed
    return name, {**join_keys(ctx, record), **props}
