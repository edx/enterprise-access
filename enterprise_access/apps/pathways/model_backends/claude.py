"""
The Claude backend: a direct reasoning-model call, metered.

Exists so re-ranking can be evaluated against a model that reports token counts and takes
a caller-supplied system prompt. Xpert does neither, which makes cost comparison and
prompt iteration awkward on that path alone.

``anthropic`` is imported lazily, inside the method. Two reasons, and the second is the
load-bearing one: the package is an optional paid dependency that a deployment which only
uses Xpert should not need installed, and importing at module scope would make every
import of this app fail when it is absent -- including the tests for the Xpert path.
"""
import logging

from django.conf import settings

from enterprise_access.apps.pathways.model_backends.base import (
    ModelBackend,
    ModelBackendConfigurationError,
    ModelBackendRequestError,
    ModelResponse
)

logger = logging.getLogger(__name__)

BACKEND_NAME = 'claude'

# Generous but bounded. A re-rank returns an ordered list of at most 20 keys with short
# rationales, so a run that wants more than this has gone wrong rather than gone long.
DEFAULT_MAX_TOKENS = 4096


class ClaudeBackend(ModelBackend):
    """
    Issues completions directly against the Anthropic Messages API.

    The system prompt is the caller's, not a stored row -- this backend is for evaluating
    prompt variants that are not yet worth persisting as admin-editable configuration.
    """

    name = BACKEND_NAME

    def __init__(self, *, model: str | None = None, api_key: str | None = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS, client=None):
        self.model = model or settings.PATHWAYS_CLAUDE_MODEL
        self.max_tokens = max_tokens
        self._api_key = api_key or settings.ANTHROPIC_API_KEY
        # Injected in tests so nothing here needs the package or the network.
        self._client = client

    def _get_client(self):
        """Build the Anthropic client, or explain precisely what is missing."""
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ModelBackendConfigurationError(
                'ANTHROPIC_API_KEY is not configured, so the claude backend cannot be used. '
                'Set it, or select the xpert backend via PATHWAYS_MODEL_BACKEND.'
            )
        try:
            import anthropic  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            raise ModelBackendConfigurationError(
                'The anthropic package is not installed, so the claude backend cannot be '
                'used. Install it, or select the xpert backend via PATHWAYS_MODEL_BACKEND.'
            ) from exc

        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def _complete(self, *, system_prompt: str, user_content: str, trace_id: str) -> ModelResponse:
        """Issue one Messages API request."""
        client = self._get_client()

        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_content}],
            )
        except ModelBackendConfigurationError:
            raise
        except Exception as exc:
            # Deliberately broad: the SDK's exception hierarchy is not a dependency this
            # module should take, and every failure here means the same thing to a caller.
            # The message is the exception *type*, not its text, which can echo the
            # request body back.
            raise ModelBackendRequestError(
                f'Anthropic request failed ({type(exc).__name__}) for model {self.model!r}.'
            ) from exc

        return ModelResponse(
            content=_extract_text(message),
            backend=self.name,
            model=getattr(message, 'model', self.model) or self.model,
            input_tokens=_usage_value(message, 'input_tokens'),
            output_tokens=_usage_value(message, 'output_tokens'),
            metadata={'stop_reason': getattr(message, 'stop_reason', None)},
        )


def _extract_text(message) -> str:
    """
    Concatenate the text blocks of a Messages API response.

    Content is a list of typed blocks; non-text blocks are skipped rather than
    stringified, so a future block type cannot silently corrupt a JSON payload.
    """
    parts = []
    for block in getattr(message, 'content', None) or []:
        text = getattr(block, 'text', None)
        if text:
            parts.append(text)
    return ''.join(parts)


def _usage_value(message, attribute: str) -> int | None:
    """Read one token count, returning ``None`` when the response omits usage."""
    usage = getattr(message, 'usage', None)
    if usage is None:
        return None
    value = getattr(usage, attribute, None)
    return value if isinstance(value, int) else None
