"""
Domain-layer API for Xpert prompt-backed workflows.

This module handles the orchestration of prompt-based Xpert AI interactions,
including prompt retrieval, message construction, and response parsing.
No HTTP or DRF machinery — suitable for testing in isolation.
"""
import json
import logging
from collections.abc import Sequence
from typing import Any, TypeAlias

from enterprise_access.apps.prompts.api_client import (
    XpertAPIClient,
    XpertAPIError,
    XpertRequestMessage,
    XpertResponseMessage
)
from enterprise_access.apps.prompts.models import BaseSystemPrompt

logger = logging.getLogger(__name__)

ValidatedData: TypeAlias = dict[str, Any]
XpertMessage: TypeAlias = XpertRequestMessage
SystemPromptModel: TypeAlias = type[BaseSystemPrompt]

_SCHEMA_SEPARATOR = '\n\nEXPECTED OUTPUT SCHEMA:\n'


class PromptError(Exception):
    """
    Raised when a prompt-backed Xpert request fails at the domain layer.

    These errors are translated by the view layer into HTTP 500 responses.
    """


def get_current_prompt(
    prompt_model: SystemPromptModel,
    prompt_type: str,
) -> BaseSystemPrompt:
    """
    Resolve the current prompt for the supplied prompt type.

    Raises:
        PromptError: If no active prompt exists for the given prompt_type.
    """
    prompt = prompt_model.get_current(prompt_type=prompt_type)
    if prompt is None:
        raise PromptError(
            f'No active prompt found for prompt_type={prompt_type!r}.'
        )
    return prompt


def build_system_prompt(prompt: BaseSystemPrompt) -> str:
    """
    Build the complete system prompt sent to Xpert.

    The configured prompt text is stripped of surrounding whitespace.
    A non-empty output schema is appended as formatted JSON.
    """
    system_prompt = prompt.system_prompt.strip()
    output_schema = prompt.output_schema

    if output_schema:
        system_prompt += _SCHEMA_SEPARATOR + json.dumps(
            output_schema,
            indent=2,
            sort_keys=True,
        )

    return system_prompt


def build_messages(validated_data: ValidatedData) -> list[XpertMessage]:
    """
    Build the default Xpert message list.

    The complete validated request payload is encoded as compact JSON in
    a single user message.
    """
    return [
        XpertRequestMessage(
            role='user',
            content=json.dumps(
                validated_data,
                separators=(',', ':'),
            ),
        ),
    ]


def send_xpert_message(
    *,
    prompt: BaseSystemPrompt,
    messages: list[XpertMessage],
    conversation_id: str,
    tags: Sequence[str] | None = None,
    prompt_type: str | None = None,
) -> XpertResponseMessage:
    """
    Send one prompt-backed request through the Xpert client.

    Xpert client failures are logged with tracking metadata and converted
    to PromptError. Prompt text, request payloads, and raw model responses
    are not logged.

    Args:
        prompt: System prompt domain model; used to build the Xpert system message.
        messages: List of messages for Xpert (user role + content).
        conversation_id: Unique conversation ID for tracing.
        tags: Optional list of tags for Xpert (e.g. RAG tags).
        prompt_type: Optional prompt type for logging/tracking.

    Raises:
        PromptError: If the Xpert API call fails.

    Returns:
        The Xpert response message.
    """
    system_prompt = build_system_prompt(prompt)
    normalized_tags = list(tags) if tags else None

    try:
        return XpertAPIClient().send_message(
            system_prompt=system_prompt,
            messages=messages,
            conversation_id=conversation_id,
            tags=normalized_tags,
        )
    except XpertAPIError as exc:
        logger.exception(
            'Xpert request failed for prompt_type=%r, conversation_id=%r.',
            prompt_type,
            conversation_id,
        )
        raise PromptError(str(exc)) from exc
