"""
Model backends for the pathway pipeline, and the registry that selects one.

Callers ask for a backend by configuration, never by import:

    backend = get_model_backend(prompt_type=PromptType.CANDIDATE_RERANK)
    response = backend.complete(system_prompt=..., user_content=..., trace_id=...)

That indirection is the point of the chunk. A step that imported ``ClaudeBackend``
directly would make "which model produced this?" a code question rather than a
configuration one, and switching backends for a comparison run would need a deploy.
"""
from django.conf import settings

from enterprise_access.apps.pathways.model_backends.base import (
    ModelBackend,
    ModelBackendConfigurationError,
    ModelBackendError,
    ModelBackendRequestError,
    ModelResponse,
    ModelResponseParseError
)
from enterprise_access.apps.pathways.model_backends.claude import BACKEND_NAME as CLAUDE_BACKEND
from enterprise_access.apps.pathways.model_backends.claude import ClaudeBackend
from enterprise_access.apps.pathways.model_backends.xpert import BACKEND_NAME as XPERT_BACKEND
from enterprise_access.apps.pathways.model_backends.xpert import XpertBackend

__all__ = [
    'CLAUDE_BACKEND',
    'XPERT_BACKEND',
    'ClaudeBackend',
    'ModelBackend',
    'ModelBackendConfigurationError',
    'ModelBackendError',
    'ModelBackendRequestError',
    'ModelResponse',
    'ModelResponseParseError',
    'XpertBackend',
    'get_model_backend',
]

BACKEND_NAMES = (XPERT_BACKEND, CLAUDE_BACKEND)


def get_model_backend(*, prompt_type: str, backend_name: str | None = None) -> ModelBackend:
    """
    Return the configured model backend.

    Args:
        prompt_type: Which stored prompt the Xpert backend should use. Required even when
            the Claude backend is selected, so that flipping the setting does not change
            the call signature at every call site.
        backend_name: Overrides ``settings.PATHWAYS_MODEL_BACKEND``. Intended for a
            comparison run that wants both backends in one process.

    Raises:
        ModelBackendConfigurationError: If the name does not match a known backend. An
            unknown name is a typo in configuration, and falling back to a default would
            hide it -- while silently sending traffic to a *paid* backend nobody chose.
    """
    name = (backend_name or settings.PATHWAYS_MODEL_BACKEND or '').strip().lower()

    if name == XPERT_BACKEND:
        return XpertBackend(prompt_type=prompt_type)
    if name == CLAUDE_BACKEND:
        return ClaudeBackend()

    raise ModelBackendConfigurationError(
        f'{name!r} is not a known model backend. Expected one of {", ".join(BACKEND_NAMES)}.'
    )
