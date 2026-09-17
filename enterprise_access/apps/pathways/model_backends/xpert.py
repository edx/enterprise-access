"""
The Xpert backend: the same path the live prompt endpoints already use.

Wraps ``prompts/api.py`` rather than reimplementing it, so a prompt stays admin-editable
and versioned, and this backend cannot drift from the endpoints that share its prompts.

Xpert reports no token counts, so ``input_tokens`` and ``output_tokens`` are ``None``.
That is a real limitation of comparing this backend against a metered one on cost, and it
is recorded as absence rather than papered over with zeros.
"""
import logging

from django.conf import settings

from enterprise_access.apps.pathways.api import prompt_revision
from enterprise_access.apps.pathways.model_backends.base import (
    ModelBackend,
    ModelBackendConfigurationError,
    ModelBackendRequestError,
    ModelResponse
)
from enterprise_access.apps.prompts import api as prompts_api
from enterprise_access.apps.prompts.api_client import XpertAPIError
from enterprise_access.apps.prompts.models import XpertLearnerPathwaysSystemPrompt

logger = logging.getLogger(__name__)

BACKEND_NAME = 'xpert'


class XpertBackend(ModelBackend):
    """
    Issues completions through Xpert, using a stored prompt.

    Unlike a direct model backend, the system prompt here is *not* supplied by the caller
    -- it comes from the ``prompts`` app row for ``prompt_type``. A caller that passes
    ``system_prompt`` gets it appended as context rather than replacing the stored prompt,
    because silently overriding an admin-editable prompt would make the admin UI a lie.
    """

    name = BACKEND_NAME

    def __init__(self, prompt_type: str, *, tags=None):
        self.prompt_type = prompt_type
        self.tags = tags if tags is not None else settings.XPERT_LEARNER_PATHWAYS_RAG_TAGS

    def _complete(self, *, system_prompt: str, user_content: str, trace_id: str) -> ModelResponse:
        """Send one message through Xpert with the stored prompt for ``prompt_type``."""
        try:
            prompt = prompts_api.get_current_prompt(
                prompt_model=XpertLearnerPathwaysSystemPrompt,
                prompt_type=self.prompt_type,
            )
        except prompts_api.PromptError as exc:
            # No prompt row configured. Not transient -- no request was sent.
            raise ModelBackendConfigurationError(
                f'No active Xpert prompt for prompt_type={self.prompt_type!r}.'
            ) from exc

        messages = [
            prompts_api.XpertRequestMessage(role='user', content=user_content),
        ]

        try:
            response = prompts_api.send_xpert_message(
                prompt=prompt,
                messages=messages,
                conversation_id=trace_id,
                tags=self.tags,
                prompt_type=self.prompt_type,
            )
        except (prompts_api.PromptError, XpertAPIError) as exc:
            raise ModelBackendRequestError(
                f'Xpert request failed for prompt_type={self.prompt_type!r}.'
            ) from exc

        return ModelResponse(
            content=response.content,
            backend=self.name,
            model=self.prompt_type,
            metadata={'prompt_revision': prompt_revision(prompt)},
        )
