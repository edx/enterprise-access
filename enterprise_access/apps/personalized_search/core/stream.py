"""
Encoding for the AI SDK UI Message Stream v1 (tech spec KD-4): one SSE ``data:`` frame per part, and
the parts kept for replay. The JSON Schemas for the parts and blocks are in ``schemas/``.
"""
import json
from typing import Callable, Iterable, Iterator

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
