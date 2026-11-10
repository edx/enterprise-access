"""
Usage events for a turn (tech spec KD-12): raw facts and join keys only.

No event carries a count, rate, cost or age, and none carries learner-written text or model prose.
Those are derived downstream by joining events. The host sends them (``Steps.emit``); this module only
builds the properties from what the turn already recorded.
"""
import hashlib
import json

from enterprise_access.apps.personalized_search.core import constants

TURN_COMPLETED = 'turn.completed'
TURN_FAILED = 'turn.failed'
TURN_STOPPED = 'turn.stopped'

_USAGE_FIELDS = (('input_tokens', 'input_tokens'), ('cached_tokens', 'cached_input_tokens'),
                 ('output_tokens', 'output_tokens'), ('reasoning_tokens', 'reasoning_tokens'))


# Server-set card fields that change between turns without the model rewriting anything.
# Set per turn by the server, not written by the model: a repeated block must digest the same across turns.
_VOLATILE_CARD_FIELDS = frozenset({'status', 'section', 'position', 'total', 'reserve'})


def _without_volatile(value):
    """``value`` without the per-turn card fields, at any depth."""
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if k not in _VOLATILE_CARD_FIELDS}
    if isinstance(value, list):
        return [_without_volatile(v) for v in value]
    return value


def block_digest(block: dict) -> str:
    """
    A short, stable hash of a block's type and learner-visible content, so the warehouse can tell when a
    turn repeats a block unchanged from the previous turn (KD-13: measuring the case for model diffs).
    """
    content = json.dumps({'type': block.get('type'), 'props': _without_volatile(block.get('props'))},
                         sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]


def join_keys(ctx, record: dict) -> dict:
    """
    The keys every event carries, so events join to each other and to enrollments. ``is_debug`` is whether
    this turn's models came from debug overrides (the host's tag), not whether the environment allows them.
    """
    session = ctx.session
    return {
        'conversation_id': session['uuid'], 'turn_id': record.get('turn_uuid'), 'turn_index': record['sequence'],
        'enterprise_customer_uuid': session['enterprise_customer_uuid'],
        'cohort': ctx.analytics.get('cohort'), 'engine': ctx.analytics.get('engine'),
        'is_debug': bool(ctx.analytics.get('is_debug')),
    }


def _reason_code(reason: str) -> str:
    """
    A dropped line's reason without model-written text: the code, plus its item type when the suffix is one
    (``section_no_valid_items:course``). Other suffixes (an unknown block or item type) are the model's.
    """
    code, _, suffix = reason.partition(':')
    return reason if suffix in constants.ITEM_TYPES else code


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
        'intent_max_weeks': constraints.get('max_weeks'),
        'intent_max_hours_per_week': constraints.get('max_hours_per_week'), 'intent_pacing': constraints.get('pacing'),
        'shown_item_keys': [p['item_id'] for p in placements],
        'shown_item_types': [p['item_type'] for p in placements],
        'shown_item_sections': [p['section'] for p in placements],
        'shown_item_positions': list(range(1, len(placements) + 1)),
        'shown_item_statuses': [p.get('status') or '' for p in placements],
        # Parallel arrays, in stream order: each block's type and content digest.
        'shown_block_types': [b.get('type') for b in record.get('blocks') or []],
        'shown_block_digests': [block_digest(b) for b in record.get('blocks') or []],
        'retrieved_item_keys': list(record.get('candidate_ids') or []),
        'excluded_item_keys': list(record.get('excluded_ids') or []),
        'dropped_reasons': [_reason_code(reason) for reason in record.get('dropped') or []],
        'followup_attribute': record.get('followup_attribute'),
        'intent_model': ctx.intent_model.model, 'intent_effort': ctx.intent_model.effort,
        'compose_model': ctx.compose_model.model, 'compose_effort': ctx.compose_model.effort,
        'intent_ms': timing.get('intent_ms'), 'search_ms': timing.get('retrieval_ms'),
        'first_text_ms': timing.get('first_text_ms'), 'first_tiles_ms': timing.get('first_section_ms'),
        'page_done_ms': timing.get('complete_ms'),
        'prompt_length': len(ctx.message),
        # The catalog records' language (``CatalogScope.locale``); a turn with no scope (harness, tests) is English.
        'content_locale': getattr(ctx.scope, 'locale', None) or 'en',
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
