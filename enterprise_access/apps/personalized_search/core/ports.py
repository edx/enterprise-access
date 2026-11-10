"""
The core's ports: what the turn pipeline needs from the outside world, as ``typing.Protocol``s.

Adapters implement them (``adapters/``): Algolia for catalog search, OpenAI and the Xpert platform for
models, the Django ORM or memory for sessions. Anything with the same methods works, so the pipeline
can run in another service, a notebook or a test with no Django, network or database.
"""
from dataclasses import dataclass, field
from typing import Iterator, Protocol, runtime_checkable


@dataclass
class Usage:
    """Token counts, timing and (when the backend knows its prices) cost for one model call."""

    model: str
    effort: str | None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    elapsed_ms: int | None = None
    first_token_ms: int | None = None
    # (input, cached input, output) USD per million tokens, set by the backend; None when unknown.
    prices: tuple[float, float, float] | None = None
    extra: dict = field(default_factory=dict)

    def cost_usd(self) -> float | None:
        """Standard-tier cost of this call, or None when the prices or token counts are unknown."""
        if not self.prices or self.input_tokens is None or self.output_tokens is None:
            return None
        price_in, price_cached, price_out = self.prices
        cached = self.cached_input_tokens or 0
        uncached = self.input_tokens - cached
        return (uncached * price_in + cached * price_cached + self.output_tokens * price_out) / 1_000_000

    def as_dict(self) -> dict:
        """The debug read-out for this call."""
        cost = self.cost_usd()
        return {
            'model': self.model,
            'effort': self.effort,
            'input_tokens': self.input_tokens,
            'cached_input_tokens': self.cached_input_tokens,
            'output_tokens': self.output_tokens,
            'reasoning_tokens': self.reasoning_tokens,
            'elapsed_ms': self.elapsed_ms,
            'first_token_ms': self.first_token_ms,
            'cost_usd': round(cost, 6) if cost is not None else None,
            'finish_reason': self.extra.get('finish_reason'),
        }


@runtime_checkable
class ModelBackend(Protocol):
    """One model platform. ``platform`` names it in logs and timing ('openai', 'xpert', ...)."""

    platform: str

    def models(self) -> list[str]:
        """The model ids this backend serves (its allowlist). A backend may list none until configured."""

    def is_configured(self) -> bool:
        """Whether it has what it needs (keys, URLs) to be called."""

    def resolve_effort(self, model: str, effort: str | None) -> str | None:
        """The reasoning effort actually sent for ``model``, or None when the model takes none."""

    def complete_json(self, *, model: str, effort: str | None, system: str, user: str,
                      max_tokens: int, timeout_s: float) -> tuple[str, Usage]:
        """One JSON completion. Raises ``ModelError``."""

    def stream_text(self, *, model: str, effort: str | None, system: str, user: str,
                    max_tokens: int, timeout_s: float, usage: Usage) -> Iterator[str]:
        """Stream text deltas, filling ``usage``. Closing the iterator closes the call."""


@runtime_checkable
class CatalogSearch(Protocol):
    """Runs several catalog sub-requests in one round trip; ``scope`` is the adapter's own key/filter."""

    def search(self, queries: list[dict], *, scope, timeout_s: float) -> list[dict]:
        """One result dict (with ``hits``) per query, in order. Raises on failure."""


@runtime_checkable
class SessionStore(Protocol):
    """Where sessions, their turns and the learner's dismissed items live."""

    def load_or_create(self, *, session_uuid: str | None, lms_user_id, enterprise_customer_uuid: str,
                       message: str) -> dict:
        """The learner's session as a dict, or a new unsaved one. Raises ``SessionNotFound``."""

    def lock(self, session: dict) -> None:
        """Take the turn lock (saving a new session). Raises ``TurnInProgress``."""

    def unlock(self, session_uuid: str) -> None:
        """Release the turn lock."""

    def persist_turn(self, session: dict, turn: dict) -> None:
        """Write a finished turn and the session's new state atomically."""

    def not_interested_ids(self, lms_user_id, enterprise_customer_uuid) -> list[str]:
        """Items this learner dismissed in this enterprise."""
