"""
The model-backend contract: one interface over Xpert and a direct reasoning model.

Production code, not harness code. The point is that "which model produced this pathway,
how many tokens did it cost, and how long did it take" becomes a query over persisted step
records rather than something a bespoke comparison rig has to instrument. So every backend
returns the same ``ModelResponse``, and the step that calls one records it verbatim.

Nothing here logs prompt text or response bodies. The learner's intake is user-authored
content and a response can quote it back, so both stay out of logs at this layer -- the
same rule ``prompts/api.py`` already follows. Token counts and elapsed time are safe to
log and are the only things worth logging anyway.
"""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class ModelBackendError(Exception):
    """
    Base class for model-backend failures.

    Catch this to handle any backend error without caring which backend produced it,
    which is the whole point of the adapter.
    """


class ModelBackendConfigurationError(ModelBackendError):
    """
    Raised when a backend cannot be used as configured.

    Never transient: no request was sent, and retrying changes nothing. Kept distinct
    from ``ModelBackendRequestError`` so a caller does not retry a missing API key.
    """


class ModelBackendRequestError(ModelBackendError):
    """Raised when a backend was reachable but the request failed."""


class ModelResponseParseError(ModelBackendError):
    """Raised when a response could not be parsed into the shape the caller asked for."""


@dataclass(frozen=True)
class ModelResponse:
    """
    One completion, normalised across backends.

    ``input_tokens`` and ``output_tokens`` are ``None`` rather than ``0`` when a backend
    does not report them -- Xpert does not -- because zero is a measurement and ``None``
    is the absence of one, and a cost report that silently treats unknown as free is worse
    than one that says it does not know.
    """

    content: str
    backend: str
    model: str = ''
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_ms: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        """Combined token count, or ``None`` if either side is unreported."""
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    def as_json(self):
        """
        Parse ``content`` as JSON.

        Raises:
            ModelResponseParseError: If the content is not JSON. The offending text is
                deliberately not included -- it can quote the learner's own intake back.
        """
        import json  # pylint: disable=import-outside-toplevel

        try:
            return json.loads(self.content.strip())
        except json.JSONDecodeError as exc:
            raise ModelResponseParseError(
                f'{self.backend} response was not valid JSON: {exc.msg} '
                f'(at position {exc.pos} of {len(self.content)} characters).'
            ) from exc

    def to_trace_dict(self) -> dict:
        """
        The subset safe to persist on a step record and to log.

        Excludes ``content``: a step stores its own parsed output, and keeping the raw
        body out of the trace keeps user-authored text out of a second place.
        """
        return {
            'backend': self.backend,
            'model': self.model,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'elapsed_ms': self.elapsed_ms,
        }


class ModelBackend(ABC):
    """
    One way of issuing a completion.

    Subclasses implement ``_complete``; ``complete`` wraps it with timing so elapsed
    milliseconds are measured identically for every backend rather than each one being
    trusted to do its own arithmetic.
    """

    #: Short stable name, used in settings and persisted on traces.
    name: str = ''

    @abstractmethod
    def _complete(self, *, system_prompt: str, user_content: str, trace_id: str) -> ModelResponse:
        """Issue the request. Timing is applied by ``complete``."""

    def complete(self, *, system_prompt: str, user_content: str, trace_id: str) -> ModelResponse:
        """
        Issue one completion and return a normalised response.

        Args:
            system_prompt: The system instruction.
            user_content: The user message. May contain learner-authored text, so it is
                never logged.
            trace_id: Identifier tying this call to a persisted step record.

        Raises:
            ModelBackendConfigurationError: The backend is misconfigured; no request sent.
            ModelBackendRequestError: The request was attempted and failed.
        """
        started = time.monotonic()
        try:
            response = self._complete(
                system_prompt=system_prompt,
                user_content=user_content,
                trace_id=trace_id,
            )
        except ModelBackendError:
            # Already typed by the backend. Logged with the trace id only -- no prompt,
            # no response body, and no exception message that might embed either.
            logger.warning(
                'Model backend %r failed for trace_id=%s.', self.name, trace_id,
            )
            raise

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            'Model backend %r completed trace_id=%s in %dms (tokens in/out: %s/%s).',
            self.name, trace_id, elapsed_ms, response.input_tokens, response.output_tokens,
        )
        # Backends do not set their own timing; ``complete`` owns it so the number means
        # the same thing everywhere.
        return ModelResponse(
            content=response.content,
            backend=response.backend,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            elapsed_ms=elapsed_ms,
            metadata=response.metadata,
        )
