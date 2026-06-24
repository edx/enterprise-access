"""
REST API viewsets for prompt-backed Xpert requests.
"""
import logging
import uuid as uuid_module

from django.conf import settings
from drf_spectacular.utils import extend_schema
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.viewsets import ViewSet

from enterprise_access.apps.api import serializers as api_serializers
from enterprise_access.apps.api.serializers.learner_pathways import LEARNER_PATHWAYS_API_TAG
from enterprise_access.apps.api_client.base_user import get_request_id
from enterprise_access.apps.prompts import api as prompts_api
from enterprise_access.apps.prompts.models import PromptType, XpertLearnerPathwaysSystemPrompt

logger = logging.getLogger(__name__)

ValidatedData = dict[str, object]

_CONVERSATION_ID_PREFIX = 'enterprise-access'
_X_REQUEST_ID_HEADER = 'X-Request-ID'


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

    This base class provides HTTP-layer utilities: request validation
    and conversation ID generation. Domain logic is delegated to
    enterprise_access.apps.prompts.api.

    Concrete viewsets compose these helpers inside their individual actions.
    This class intentionally defines no actions, routes, authentication
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


class LearnerPathwaysViewSet(BasePromptViewSet):
    """
    Endpoints for the Learner Pathways Xpert-backed feature.

    Each action defines its own authentication, permissions, and throttle configuration
    explicitly.  No shared authentication, permissions, or throttle classes are defined
    at the class level.
    """

    model_type = XpertLearnerPathwaysSystemPrompt
    throttle_scope: str | None = None

    @extend_schema(
        tags=[LEARNER_PATHWAYS_API_TAG],
        summary='Derive learning intent from learner input.',
        description=(
            'Calls Xpert with the learner\'s stated goals, free-text input, and known context '
            'to derive skills and a search query.  Returns the raw JSON produced by Xpert.'
        ),
        request=api_serializers.LearningIntentRequestSerializer,
        responses={
            status.HTTP_200_OK: api_serializers.LearningIntentResponseSerializer,
            status.HTTP_400_BAD_REQUEST: None,
            status.HTTP_401_UNAUTHORIZED: None,
            status.HTTP_403_FORBIDDEN: None,
            status.HTTP_429_TOO_MANY_REQUESTS: None,
            status.HTTP_500_INTERNAL_SERVER_ERROR: None,
        },
    )
    @action(
        detail=False,
        methods=['post'],
        url_path='learning-intent',
        url_name='learning-intent',
        authentication_classes=(JwtAuthentication,),
        permission_classes=(permissions.IsAuthenticated,),
        throttle_classes=(ScopedRateThrottle,),
        throttle_scope='learner_pathways_learning_intent',
    )
    def learning_intent(self, request: Request) -> Response:
        """
        Derive learning intent from the learner's stated goals, free-text input, and known context.

        Returns HTTP 400 for invalid request input.
        Returns HTTP 401/403 when the caller is unauthenticated or not an enterprise learner.
        Returns HTTP 429 when the per-endpoint rate limit is exceeded.
        Returns HTTP 500 when the prompt is missing, the Xpert call fails, or the response
        cannot be parsed as JSON.
        """
        validated_data = self._validate_request(
            request,
            api_serializers.LearningIntentRequestSerializer,
        )

        conversation_id = self._get_conversation_id(request)

        try:
            prompt = prompts_api.get_current_prompt(
                prompt_model=self.model_type,
                prompt_type=PromptType.LEARNER_INTENT,
            )
            system_prompt = prompts_api.build_system_prompt(prompt)
            messages = prompts_api.build_messages(validated_data)

            xpert_response = prompts_api.send_xpert_message(
                system_prompt=system_prompt,
                messages=messages,
                conversation_id=conversation_id,
                tags=settings.XPERT_LEARNER_PATHWAYS_RAG_TAGS,
                prompt_type=PromptType.LEARNER_INTENT,
            )

            content = prompts_api.extract_xpert_content(xpert_response)
            response_data = prompts_api.parse_json_content(content)
        except prompts_api.PromptError as exc:
            raise PromptRequestException(str(exc)) from exc

        return Response(response_data, status=status.HTTP_200_OK)
