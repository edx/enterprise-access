"""
REST API viewsets for prompt-backed Xpert requests.
"""
import logging
import uuid as uuid_module

from django.conf import settings
from drf_spectacular.utils import extend_schema
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.viewsets import ViewSet

from enterprise_access.apps.api import serializers as api_serializers
from enterprise_access.apps.api.serializers.learner_pathways import LEARNER_PATHWAYS_API_TAG
from enterprise_access.apps.api_client.base_user import get_request_id
from enterprise_access.apps.core import constants
from enterprise_access.apps.prompts import api as prompts_api
from enterprise_access.apps.prompts.api_client import XpertAPIError
from enterprise_access.apps.prompts.models import PromptType, XpertLearnerPathwaysSystemPrompt

logger = logging.getLogger(__name__)

_CONVERSATION_ID_PREFIX = 'enterprise-access'


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

    This base class provides HTTP-layer utilities for conversation ID generation
    and the shared validate/execute/parse lifecycle for prompt-backed actions.
    Domain logic is delegated to enterprise_access.apps.prompts.api.

    Concrete viewsets compose these helpers inside their individual actions.
    This class intentionally defines no actions, routes, authentication
    classes, or permission policies.
    """

    # Subclasses must override this with their concrete system-prompt model.
    model_type: type[XpertLearnerPathwaysSystemPrompt] | None = None

    def _get_conversation_id(
        self,
        request: Request,
    ) -> str:
        """
        Construct a traceable, non-blank Xpert conversation ID.

        The repository-level request-ID helper (backed by CRUM's current request)
        is the sole source for tracing. If no request ID is available, a freshly
        generated UUID4 is used as the fallback instead.
        """
        request_id = get_request_id()

        if not request_id:
            request_id = str(uuid_module.uuid4())

        return f'{_CONVERSATION_ID_PREFIX}:{request_id}'

    def _execute_prompt_workflow(
        self,
        request: Request,
        *,
        request_serializer_class: type[Serializer],
        response_serializer_class: type[Serializer],
        prompt_type: str,
    ) -> Response:
        """
        Execute the shared validate/execute/parse lifecycle for a prompt-backed action.

        Validates the request, looks up and executes the configured prompt via
        Xpert, and validates the parsed response.  Xpert and parsing failures are
        mapped to PromptRequestException (HTTP 500).

        Returns HTTP 400 for invalid request input via standard DRF validation.
        """
        serializer = request_serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated_data = serializer.validated_data

        conversation_id = self._get_conversation_id(request)

        try:
            prompt = prompts_api.get_current_prompt(
                prompt_model=self.model_type,
                prompt_type=prompt_type,
            )
            messages = prompts_api.build_messages(validated_data)

            xpert_response = prompts_api.send_xpert_message(
                prompt=prompt,
                messages=messages,
                conversation_id=conversation_id,
                tags=settings.XPERT_LEARNER_PATHWAYS_RAG_TAGS,
                prompt_type=prompt_type,
            )

            response_data = xpert_response.as_json()
        except (prompts_api.PromptError, XpertAPIError) as exc:
            raise PromptRequestException(str(exc)) from exc

        response_serializer = response_serializer_class(data=response_data)
        try:
            response_serializer.is_valid(raise_exception=True)
        except ValidationError as exc:
            raise PromptRequestException(f'Invalid Xpert response: {exc.detail}') from exc
        return Response(response_serializer.data, status=status.HTTP_200_OK)


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
            'to derive skills and a search query.  Returns a validated response shape; unknown '
            'top-level fields from Xpert are ignored.'
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
    @permission_required(constants.LEARNER_PATHWAYS_LEARNING_INTENT_PERMISSION)
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
        return self._execute_prompt_workflow(
            request,
            request_serializer_class=api_serializers.LearningIntentRequestSerializer,
            response_serializer_class=api_serializers.LearningIntentResponseSerializer,
            prompt_type=PromptType.LEARNER_INTENT,
        )

    @extend_schema(
        tags=[LEARNER_PATHWAYS_API_TAG],
        summary='Provide feedback on pathway recommendations.',
        description=(
            'Calls Xpert with the learner\'s selected career, course keys, and learner profile '
            'to generate reasoning for the recommendations.  Returns a validated response shape; '
            'unknown top-level fields from Xpert are ignored.'
        ),
        request=api_serializers.RecommendationFeedbackRequestSerializer,
        responses={
            status.HTTP_200_OK: api_serializers.RecommendationFeedbackResponseSerializer,
            status.HTTP_400_BAD_REQUEST: None,
            status.HTTP_401_UNAUTHORIZED: None,
            status.HTTP_403_FORBIDDEN: None,
            status.HTTP_429_TOO_MANY_REQUESTS: None,
            status.HTTP_500_INTERNAL_SERVER_ERROR: None,
        },
    )
    @permission_required(constants.LEARNER_PATHWAYS_RECOMMENDATION_FEEDBACK_PERMISSION)
    @action(
        detail=False,
        methods=['post'],
        url_path='recommendation-feedback',
        url_name='recommendation-feedback',
        authentication_classes=(JwtAuthentication,),
        permission_classes=(permissions.IsAuthenticated,),
        throttle_classes=(ScopedRateThrottle,),
        throttle_scope='learner_pathways_recommendation_feedback',
    )
    def recommendation_feedback(self, request: Request) -> Response:
        """
        Generate reasoning for pathway recommendations based on learner profile and selected career.

        Returns HTTP 400 for invalid request input.
        Returns HTTP 401/403 when the caller is unauthenticated or not an enterprise learner.
        Returns HTTP 429 when the per-endpoint rate limit is exceeded.
        Returns HTTP 500 when the prompt is missing, the Xpert call fails, or the response
        cannot be parsed as JSON.
        """
        return self._execute_prompt_workflow(
            request,
            request_serializer_class=api_serializers.RecommendationFeedbackRequestSerializer,
            response_serializer_class=api_serializers.RecommendationFeedbackResponseSerializer,
            prompt_type=PromptType.RECOMMENDATIONS_FEEDBACK,
        )
