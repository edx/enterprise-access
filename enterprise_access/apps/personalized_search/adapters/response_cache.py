"""
An exact-match cache for Personalized Search model calls (tech spec KD-13).

A model call's whole input (system prompt, user content with the intent, recent turns, message and
candidates, model and effort) determines its output, so an identical call can replay a stored output.
Starter-chip first turns in one enterprise repeat byte for byte; later turns hit only on identical
conversations. Only the model text is cached: the parser still runs on it, so cards, statuses,
placements and the profile are rebuilt for the current learner.

Keys are scoped to the enterprise, so no output is shared across customers. A stopped, failed or
timed-out call is never stored. The original call's token counts are kept, so a hit reports what it saved.
"""
import hashlib
import logging
from collections.abc import Callable, Iterator

from edx_django_utils.cache import TieredCache

from enterprise_access.apps.personalized_search.core.ports import Usage

logger = logging.getLogger(__name__)

# Bump when the cached value's format changes. Prompt changes need no bump: the prompts are in the key.
CACHE_FORMAT = 1
REPLAY_CHUNK = 200
TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens')


def _tokens(usage: Usage) -> dict:
    return {name: getattr(usage, name) for name in TOKEN_FIELDS}


def _mark_hit(usage: Usage, stored_usage: dict) -> None:
    """A replayed call costs nothing; ``saved`` is what the original call used."""
    for name in TOKEN_FIELDS:
        setattr(usage, name, 0)
    usage.elapsed_ms = 0
    usage.first_token_ms = 0
    usage.extra.update(cache='hit', saved={name: stored_usage.get(name) or 0 for name in TOKEN_FIELDS})


class ResponseCache:
    """Wraps the intent and compose step callables (``wiring.steps``) with a TieredCache lookup."""

    def __init__(self, *, scope: str, seconds: int, cache=TieredCache):
        self.scope = scope
        self.seconds = seconds
        self.cache = cache

    def key(self, *, step: str, model: str, effort: str | None, system: str, user: str) -> str:
        """The cache key: every input to the call, plus the enterprise and the step."""
        digest = hashlib.sha256()
        for value in (CACHE_FORMAT, self.scope, step, model, effort or '', system, user):
            digest.update(str(value).encode('utf-8'))
            digest.update(b'\x00')
        return f'ps:resp:{digest.hexdigest()}'

    def _get(self, key: str) -> dict | None:
        """The stored ``{text, usage}``, or None on a miss or when the cache can't be read."""
        try:
            found = self.cache.get_cached_response(key)
        except Exception:  # pylint: disable=broad-exception-caught  # a cache outage must not fail a turn
            logger.warning('Personalized Search response cache read failed.', exc_info=True)
            return None
        return found.value if found.is_found and isinstance(found.value, dict) else None

    def _set(self, key: str, text: str, usage: Usage) -> None:
        try:
            self.cache.set_all_tiers(key, {'text': text, 'usage': _tokens(usage)}, self.seconds)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning('Personalized Search response cache write failed.', exc_info=True)

    def intent(self, call: Callable) -> Callable:
        """The intent step: a hit returns the stored text with a zero-token usage."""
        def cached(*, system, user, model, effort, timeout_s):
            key = self.key(step='intent', model=model, effort=effort, system=system, user=user)
            stored = self._get(key)
            if stored is not None:
                usage = Usage(model=model, effort=effort)
                _mark_hit(usage, stored.get('usage') or {})
                return stored['text'], usage
            text, usage = call(system=system, user=user, model=model, effort=effort, timeout_s=timeout_s)
            usage.extra['cache'] = 'miss'
            self._set(key, text, usage)
            return text, usage
        return cached

    def compose(self, call: Callable) -> Callable:
        """The compose step: a hit streams the stored text; a miss is stored once it streamed to the end."""
        def cached(*, system, user, model, effort, timeout_s, usage: Usage) -> Iterator[str]:
            key = self.key(step='compose', model=model, effort=effort, system=system, user=user)
            stored = self._get(key)
            if stored is not None:
                _mark_hit(usage, stored.get('usage') or {})
                text = stored['text']
                for i in range(0, len(text), REPLAY_CHUNK):
                    yield text[i:i + REPLAY_CHUNK]
                return
            usage.extra['cache'] = 'miss'
            deltas = []
            # Reached only when the stream ends normally: a stop (close) or an error skips the store.
            for delta in call(system=system, user=user, model=model, effort=effort, timeout_s=timeout_s,
                              usage=usage):
                deltas.append(delta)
                yield delta
            self._set(key, ''.join(deltas), usage)
        return cached
