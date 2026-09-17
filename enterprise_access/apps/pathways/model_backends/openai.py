"""
The OpenAI backend: a direct metered call, for evaluating prompt and model variants.

Same role as ``claude.py`` — a caller-supplied system prompt, reported token counts, and no
database row — so the two are interchangeable and comparable. Whether a model-class
difference exists is a question the harness should answer from persisted traces rather than
one anybody should assert, and that needs at least two metered backends to compare.

``openai`` is imported lazily, inside the method, for the same two reasons ``anthropic``
is: it is an optional paid dependency a deployment using only Xpert should not need
installed, and a module-scope import would make every import of this app fail when it is
absent — including the tests for the other backends.

One deliberate difference from the Claude backend: this one asks for
``response_format={'type': 'json_object'}``. Every prompt in this pipeline requires JSON,
and OpenAI can enforce that server-side, which removes an entire failure mode rather than
handling it. Note it requires the word "JSON" to appear in the prompt; ours do.
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

BACKEND_NAME = 'openai'

# Generous but bounded, matching the Claude backend. A re-rank returns an ordered list of
# at most 20 keys with short rationales, so a run wanting more has gone wrong not long.
DEFAULT_MAX_TOKENS = 4096


class OpenAIBackend(ModelBackend):
    """
    Issues completions directly against OpenAI's chat completions API.

    The system prompt is the caller's, not a stored row — this backend is for evaluating
    variants that are not yet worth persisting as admin-editable configuration.
    """

    name = BACKEND_NAME

    def __init__(self, *, model: str | None = None, api_key: str | None = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS, client=None):
        self.model = model or settings.PATHWAYS_OPENAI_MODEL
        self.max_tokens = max_tokens
        self._api_key = api_key or settings.OPENAI_API_KEY
        # Injected in tests so nothing here needs the package or the network.
        self._client = client

    def _get_client(self):
        """Build the OpenAI client, or explain precisely what is missing."""
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ModelBackendConfigurationError(
                'OPENAI_API_KEY is not configured, so the openai backend cannot be used. '
                'Set it, or select another backend via PATHWAYS_MODEL_BACKEND.'
            )
        try:
            import openai  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            raise ModelBackendConfigurationError(
                'The openai package is not installed, so the openai backend cannot be '
                'used. Install it, or select another backend via PATHWAYS_MODEL_BACKEND.'
            ) from exc

        self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    def _complete(self, *, system_prompt: str, user_content: str, trace_id: str) -> ModelResponse:
        """Issue one chat-completions request."""
        client = self._get_client()

        try:
            completion = client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                response_format={'type': 'json_object'},
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_content},
                ],
            )
        except ModelBackendConfigurationError:
            raise
        except Exception as exc:
            # Deliberately broad, and reporting the exception *type* rather than its text:
            # the SDK's message can echo the request body, which holds the learner's own
            # intake. Taking the SDK's exception hierarchy as a dependency would also make
            # this module care about a detail no caller distinguishes.
            raise ModelBackendRequestError(
                f'OpenAI request failed ({type(exc).__name__}) for model {self.model!r}.'
            ) from exc

        return ModelResponse(
            content=_first_message_content(completion),
            backend=self.name,
            model=getattr(completion, 'model', self.model) or self.model,
            input_tokens=_usage_value(completion, 'prompt_tokens'),
            output_tokens=_usage_value(completion, 'completion_tokens'),
            metadata={'finish_reason': _first_finish_reason(completion)},
        )


def _first_choice(completion):
    """The first choice, or ``None`` when the response carries none."""
    choices = getattr(completion, 'choices', None) or []
    return choices[0] if choices else None


def _first_message_content(completion) -> str:
    """
    The first choice's message content, or an empty string.

    Empty rather than raising: the caller already degrades an unusable response to
    retrieval order, and a missing choice is that same case rather than a new one.
    """
    choice = _first_choice(completion)
    message = getattr(choice, 'message', None) if choice else None
    return getattr(message, 'content', None) or ''


def _first_finish_reason(completion):
    """Why generation stopped — ``length`` here means the response was truncated."""
    choice = _first_choice(completion)
    return getattr(choice, 'finish_reason', None) if choice else None


def _usage_value(completion, attribute: str) -> int | None:
    """
    Read one token count, returning ``None`` when the response omits usage.

    ``None`` rather than ``0``, per the ``ModelResponse`` contract: zero is a measurement,
    and a cost report that silently treats unknown as free is worse than one that says it
    does not know.
    """
    usage = getattr(completion, 'usage', None)
    if usage is None:
        return None
    value = getattr(usage, attribute, None)
    return value if isinstance(value, int) else None
