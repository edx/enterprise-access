"""
Encoding for the AI SDK UI Message Stream v1 (tech spec KD-4): one SSE ``data:`` frame per part, and
the parts kept for replay. The JSON Schemas for the parts and blocks are in ``schemas/``.
"""
import json
from typing import Callable, Iterable, Iterator

from enterprise_access.apps.personalized_search.core import constants

DONE_FRAME = 'data: [DONE]\n\n'
# Response header the AI SDK UI Message Stream protocol defines; clients use it to pick the parser.
STREAM_PROTOCOL_HEADER = ('x-vercel-ai-ui-message-stream', 'v1')


def encode_part(part: dict) -> str:
    return f'data: {json.dumps(part, separators=(",", ":"))}\n\n'


def sse_frames(parts: Iterator[dict], on_close: Callable[[], None]) -> Iterable[str]:
    """
    Closing wrapper (spec step 7): Django only closes iterables that have ``close()``, so a bare
    ``map`` would never deliver ``GeneratorExit`` to the orchestrator when the client stops.
    """
    try:
        for part in parts:
            yield encode_part(part)
        yield DONE_FRAME
    finally:
        try:
            parts.close()
        finally:
            on_close()


def stored_parts(parts: list[dict]) -> list[dict]:
    """
    The parts kept for replay: transient parts and ``data-timing`` dropped, the reply's text deltas
    coalesced into one ``text-delta`` (tech spec "Data Model", ``ChatTurn.parts``).
    """
    out, reply = [], None
    for part in parts:
        if part.get('transient') or part['type'] in ('data-timing', 'finish'):
            continue
        if part['type'] == 'text-delta':
            if reply is None:
                reply = {'type': 'text-delta', 'id': part['id'], 'delta': ''}
                out.append(reply)
            reply['delta'] += part['delta']
            continue
        out.append(part)
    return out


def screen_order(parts: list[dict]) -> list[dict]:
    """
    What a turn showed, in on-screen order and once per item: the top pick, the comparison, then each section's row
    (its cards padded to three from the reserve, as the portal fills it). Each entry is ``{'where', 'item_id',
    'title'}``; the next turn reads it to resolve "the first two" or "the second one".
    """
    screen: list[dict] = []
    for part in parts:
        block = (part.get('data') or {}) if part.get('type') == 'data-block' else {}
        props = block.get('props') or {}
        if block.get('type') == 'top_pick':
            cards = [('top pick', props.get('item'))]
        elif block.get('type') == 'compare':
            cards = [(f'compare {i}', o.get('item')) for i, o in enumerate(props.get('options') or [], 1)]
        elif block.get('type') == 'item_section':
            row = ((props.get('items') or []) + (props.get('reserve') or []))[:constants.MAX_ITEMS_PER_SECTION]
            label = constants.SECTION_LABELS.get(props.get('item_type'), 'results')
            cards = [(f'{label} {i}', card) for i, card in enumerate(row, 1)]
        else:
            continue
        shown = {entry['item_id'] for entry in screen}
        screen += [{'where': where, 'item_id': card['item_id'], 'title': card.get('title') or ''}
                   for where, card in cards if isinstance(card, dict) and card.get('item_id') not in shown]
    return screen


def describe_screen(screen: list[dict] | None) -> str:
    """The previous screen for a model prompt: one numbered line per card, titles JSON-encoded; '(none)' if empty."""
    lines = [f"{i}. {entry['where']}: {json.dumps(entry['title'])} (item_id {entry['item_id']})"
             for i, entry in enumerate(screen or [], 1)]
    return '\n'.join(lines) or '(none)'
