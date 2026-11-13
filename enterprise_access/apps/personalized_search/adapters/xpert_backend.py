"""
Xpert platform model backend for Personalized Search, so OpenAI and Xpert can be compared side by side.

Prompts go to Xpert's raw ``/v1/message`` API as the caller's ``system_message`` (spike 2: the raw API
honours it; only the pathways ``XpertBackend`` swaps in stored prompts). Both backends therefore get
identical prompts, and only the model differs. Xpert fixes its model (``gpt-4o-mini``), has no
``response_format`` and reports no token counts, so cost is unknown.

``stream: true`` framing (spike 2, ``personalized-search/docs/plans/spikes/spike2-xpert-stream.md``):
``text/event-stream`` in name only; each unit is a one-element JSON array whose ``content`` is
cumulative; units are concatenated with no delimiter; a read can hold several; there is no completion
marker. ``XpertStreamDecoder`` buffers, decodes complete units from the front, and emits the suffix.

Each call uses a fresh server-generated ``conversation_id`` so the intent and compose calls (and turns)
can't share Xpert-side history; the caller already sends the history it wants in the message.

Configuration comes in through the constructor (``wiring.py`` reads settings).
"""
import codecs
import json
import time
import uuid
from typing import Iterator

import requests

from enterprise_access.apps.personalized_search.core.errors import ModelError, model_error_for_status
from enterprise_access.apps.personalized_search.core.ports import Usage

XPERT_MODEL = 'xpert'
CONNECT_TIMEOUT_S = 5


def _message_content(unit) -> str | None:
    """The text of an Xpert response unit (a one-element list of message dicts), if any."""
    message = unit[0] if isinstance(unit, list) and unit else unit
    content = message.get('content') if isinstance(message, dict) else None
    return content if isinstance(content, str) else None


def _request_error(exc: Exception, what: str) -> ModelError:
    """The port error for a failed request, by the HTTP status of its response (none for timeouts or connections)."""
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    return model_error_for_status(status if isinstance(status, int) else None,
                                  f'Xpert {what} failed ({type(exc).__name__}).')


class XpertStreamDecoder:
    """Turns raw ``stream: true`` bytes into text deltas (cumulative units → new suffix)."""

    def __init__(self):
        self._bytes = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self._json = json.JSONDecoder()
        self._buffer = ''
        self.text = ''
        self.units = 0

    def feed(self, chunk: bytes) -> list[str]:
        """Consume raw bytes; return the deltas they complete."""
        self._buffer += self._bytes.decode(chunk)
        deltas = []
        while True:
            self._buffer = self._buffer.lstrip()
            if not self._buffer:
                break
            try:
                unit, end = self._json.raw_decode(self._buffer)
            except ValueError:
                break  # a partial unit: wait for more bytes
            self._buffer = self._buffer[end:]
            self.units += 1
            content = _message_content(unit)
            if content is None or not content.startswith(self.text):
                continue  # null first unit, or a unit that doesn't extend the text: nothing new to emit
            delta, self.text = content[len(self.text):], content
            if delta:
                deltas.append(delta)
        return deltas


class XpertBackend:
    """``ModelBackend`` for the Xpert platform's raw ``/v1/message`` API, serving the one model id ``xpert``."""

    platform = 'xpert'
    label = 'Xpert platform'

    def __init__(self, *, base_url: str, client_id: str, request_timeout_s: float | None = None,
                 session: requests.Session | None = None):
        self._base_url = base_url or ''
        self._client_id = client_id or ''
        # Xpert is slower than the core's default step timeouts (spike 2); wiring uses this instead.
        self.request_timeout_s = request_timeout_s
        self._session = session or requests.Session()

    def models(self) -> list[str]:
        """``xpert``, once the base URL and client id are set."""
        return [XPERT_MODEL] if self.is_configured() else []

    def is_configured(self) -> bool:
        """Whether the Xpert base URL and client id are set."""
        return bool(self._base_url and self._client_id)

    def resolve_effort(self, model: str, effort: str | None) -> str | None:  # pylint: disable=unused-argument
        """None: Xpert fixes its own model and takes no reasoning effort."""
        return None

    def _endpoint(self) -> str:
        return self._base_url.rstrip('/') + '/v1/message'

    def _payload(self, system: str, user: str, *, stream: bool) -> dict:
        return {
            'client_id': self._client_id,
            'system_message': system,
            'messages': [{'role': 'user', 'content': user}],
            'conversation_id': f'ps-{uuid.uuid4()}',
            'stream': stream,
        }

    def complete_json(self, *, model: str, effort: str | None, system: str, user: str,
                      max_tokens: int, timeout_s: float) -> tuple[str, Usage]:
        """One non-streamed Xpert message; the prompt must ask for JSON (Xpert has no JSON mode)."""
        # pylint: disable=unused-argument  # Xpert fixes the model and has no effort or token cap
        usage = Usage(model=model, effort=None)
        started = time.monotonic()
        try:
            response = self._session.post(self._endpoint(), json=self._payload(system, user, stream=False),
                                          timeout=(CONNECT_TIMEOUT_S, timeout_s))
            response.raise_for_status()
            content = _message_content(response.json())
        except (requests.RequestException, ValueError) as exc:
            raise _request_error(exc, 'request') from exc
        finally:
            usage.elapsed_ms = round((time.monotonic() - started) * 1000)
        if content is None:
            raise ModelError('Xpert response had no message content.')
        return content, usage

    def stream_text(self, *, model: str, effort: str | None, system: str, user: str,
                    max_tokens: int, timeout_s: float, usage: Usage) -> Iterator[str]:
        """Stream an Xpert message's text deltas. Closing the generator closes the HTTP response."""
        # pylint: disable=unused-argument  # Xpert fixes the model and has no effort or token cap
        started = time.monotonic()
        try:
            response = self._session.post(self._endpoint(), json=self._payload(system, user, stream=True),
                                          stream=True, timeout=(CONNECT_TIMEOUT_S, timeout_s))
            response.raise_for_status()
        except requests.RequestException as exc:
            raise _request_error(exc, 'request') from exc
        decoder = XpertStreamDecoder()
        try:
            for chunk in response.iter_content(chunk_size=None):
                for delta in decoder.feed(chunk):
                    if usage.first_token_ms is None:
                        usage.first_token_ms = round((time.monotonic() - started) * 1000)
                    yield delta
        except requests.RequestException as exc:
            raise _request_error(exc, 'stream') from exc
        finally:
            usage.elapsed_ms = round((time.monotonic() - started) * 1000)
            usage.extra['xpert_units'] = decoder.units
            response.close()
