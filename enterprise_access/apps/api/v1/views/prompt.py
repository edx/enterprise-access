"""
Reusable base viewset for prompt-backed Xpert requests.
"""
import json
import logging
import uuid as uuid_module
from collections.abc import Sequence
from typing import TypeAlias, cast

from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework.request import Request
from rest_framework.viewsets import ViewSet

from enterprise_access.apps.api_client.base_user import get_request_id
from enterprise_access.apps.prompts.api_client import XpertAPIClient, XpertAPIError
from enterprise_access.apps.prompts.models import BaseSystemPrompt

logger = logging.getLogger(__name__)

JSONValue: TypeAlias = (
    str |
    int |
    float |
    bool |
    None |
    list["JSONValue"] |
    dict[str, "JSONValue"]
)

ValidatedData: TypeAlias = dict[str, object]
XpertMessage: TypeAlias = dict[str, str]
XpertResponse: TypeAlias = dict[str, object]
SystemPromptModel: TypeAlias = type[BaseSystemPrompt]

_CONVERSATION_ID_PREFIX = 'enterprise-access'
_X_REQUEST_ID_HEADER = 'X-Request-ID'
_SCHEMA_SEPARATOR = '\n\nEXPECTED OUTPUT SCHEMA:\n'


class PromptRequestException(APIException):
    """
    Raised when a prompt-backed Xpert request fails internally.

    The underlying error message is exposed through both DRF's ``detail``
    attribute and ``exception.args[0]`` for logging and error monitoring.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail = 'Prompt request failed.'
    default_code = 'prompt_request_error'

    def __init__(self, message: str) -> None:
        super().__init__(
            detail=message,
            code=self.default_code,
        )
        self.args = (message,)


class BasePromptViewSet(ViewSet):
    """
    Reusable helper methods for prompt-backed Xpert requests.

    Concrete viewsets compose these helpers inside their individual actions.
    This base class intentionally defines no actions, routes, authentication
    classes, or permission policies.
    """

    def _validate_request(
        self,
        request: Request,
        serializer_class: type[serializers.Serializer],
    ) -> ValidatedData:
        """
        Validate request data and return the serializer's validated payload.

        Invalid request data follows standard DRF validation behavior and
        produces an HTTP 400 response.
        """
        serializer = serializer_class(
            data=request.data,
            context={
                'request': request,
                'format': None,
                'view': self,
            },
        )
        serializer.is_valid(raise_exception=True)

        return serializer.validated_data

    def _get_current_prompt(
        self,
        *,
        prompt_model: type[SystemPromptModel],
        prompt_type: str,
    ) -> BaseSystemPrompt:
        """
        Resolve the current prompt for the exact supplied prompt type.
        """
        prompt = prompt_model.get_current(
            prompt_type=prompt_type,
        )
        if prompt is None:
            raise PromptRequestException(
                f'No active prompt found for prompt_type={prompt_type!r}.'
            )

        return prompt

    def _build_system_prompt(
        self,
        prompt: BaseSystemPrompt,
    ) -> str:
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

    def _build_messages(
        self,
        validated_data: ValidatedData,
    ) -> list[XpertMessage]:
        """
        Build the default Xpert message list.

        The complete validated request payload is encoded as compact JSON in
        a single user message.
        """
        return [
            {
                'role': 'user',
                'content': json.dumps(
                    validated_data,
                    separators=(',', ':'),
                ),
            },
        ]

    def _get_conversation_id(
        self,
        request: Request,
    ) -> str:
        """
        Construct a traceable, non-blank Xpert conversation ID.

        The repository-level request-ID helper is the primary source. Reading
        directly from the supplied request is retained as a fallback for tests
        and execution contexts where CRUM has no current request.
        """
        request_id = get_request_id()

        if not request_id:
            request_id = request.headers.get(_X_REQUEST_ID_HEADER)

        if not request_id:
            request_id = str(uuid_module.uuid4())

        return f'{_CONVERSATION_ID_PREFIX}:{request_id}'

    def _send_xpert_message(
        self,
        *,
        system_prompt: str,
        messages: list[XpertMessage],
        conversation_id: str,
        tags: Sequence[str] | None = None,
        prompt_type: str | None = None,
    ) -> XpertResponse:
        """
        Send one prompt-backed request through the existing Xpert client.

        Xpert client failures are logged with tracking metadata and converted
        to HTTP 500 prompt request failures. Prompt text, request payloads, and
        raw model responses are not logged.
        """
        normalized_tags = list(tags) if tags else None

        try:
            response = XpertAPIClient().send_message(
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
            raise PromptRequestException(str(exc)) from exc

        return response

    def _extract_xpert_content(
        self,
        xpert_response: XpertResponse,
    ) -> str:
        """
        Extract the raw content string from the normalized Xpert response.
        """
        content = xpert_response.get('content')

        if content is None:
            raise PromptRequestException(
                'Xpert response is missing the "content" field.'
            )

        return content

    def _parse_json_content(
        self,
        content: str,
    ) -> JSONValue:
        """
        Parse and return the complete JSON value produced by Xpert.

        The content must be directly parseable as JSON after surrounding
        whitespace is removed. Markdown fencing, repair prompts, retries,
        fallback parsing, field mapping, and response normalization are
        intentionally unsupported.
        """
        try:
            parsed_content = json.loads(content.strip())
        except json.JSONDecodeError as exc:
            raise PromptRequestException(
                f'Failed to parse Xpert response content as JSON: {exc}'
            ) from exc

        return cast(JSONValue, parsed_content)
