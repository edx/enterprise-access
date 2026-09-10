"""
REST API viewsets for the server-side learner pathways pipeline.

Distinct from ``views/prompt.py``, which owns the two live single-shot prompt endpoints.
These endpoints run a persisted, multi-step workflow instead, so every request leaves a
trace that can be inspected -- or re-serialized -- afterwards without re-running it.
"""
import logging

from django.conf import settings
from drf_spectacular.utils import extend_schema
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.viewsets import ViewSet

from enterprise_access.apps.api import serializers as api_serializers
from enterprise_access.apps.api.serializers.learner_pathways import LEARNER_PATHWAYS_API_TAG
from enterprise_access.apps.core import constants
from enterprise_access.apps.pathways.models import CareerDiscoveryWorkflow, PathwayAssemblyWorkflow
from enterprise_access.apps.workflow.exceptions import UnitOfWorkException

logger = logging.getLogger(__name__)


class CareerDiscoveryException(APIException):
    """
    Raised when the career discovery workflow fails.

    Deliberately carries no partial results: a failed run's step records hold the input,
    output and failure of every stage, so the diagnosis lives in the trace rather than in
    a half-populated response body a client might render.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail = 'Career discovery failed.'
    default_code = 'career_discovery_error'


class CareerDiscoveryViewSet(ViewSet):
    """
    Endpoint for deriving career candidates from a learner's intake.

    Registered alongside ``LearnerPathwaysViewSet`` under the same ``learner-pathways``
    prefix. Each action defines its own authentication, permissions and throttling
    explicitly; nothing is configured at the class level.
    """

    # DRF's ``as_view()`` rejects an ``@action`` initkwarg that is not also a class
    # attribute, so the per-action ``throttle_scope`` needs this declaration to exist.
    throttle_scope: str | None = None

    @extend_schema(
        tags=[LEARNER_PATHWAYS_API_TAG],
        summary='Derive career candidates from learner intake.',
        description=(
            'Runs the server-side career discovery workflow: derives learning intent from the '
            'learner\'s intake via Xpert, then searches the careers taxonomy for matching roles. '
            'Every execution persists a workflow record and one record per executed step, so the '
            'returned workflow_uuid can be used to inspect the full trace afterwards.'
        ),
        request=api_serializers.CareerDiscoveryRequestSerializer,
        responses={
            status.HTTP_200_OK: api_serializers.CareerDiscoveryResponseSerializer,
            status.HTTP_400_BAD_REQUEST: None,
            status.HTTP_401_UNAUTHORIZED: None,
            status.HTTP_403_FORBIDDEN: None,
            status.HTTP_404_NOT_FOUND: None,
            status.HTTP_429_TOO_MANY_REQUESTS: None,
            status.HTTP_500_INTERNAL_SERVER_ERROR: None,
        },
    )
    @permission_required(constants.LEARNER_PATHWAYS_CAREER_DISCOVERY_PERMISSION)
    @action(
        detail=False,
        methods=['post'],
        url_path='careers',
        url_name='careers',
        authentication_classes=(JwtAuthentication,),
        permission_classes=(permissions.IsAuthenticated,),
        throttle_classes=(ScopedRateThrottle,),
        throttle_scope='learner_pathways_careers',
    )
    def careers(self, request: Request) -> Response:
        """
        Derive career candidates from the learner's stated goals, free text and context.

        Returns HTTP 404 when the server-side pipeline is disabled.
        Returns HTTP 400 for invalid request input.
        Returns HTTP 401/403 when the caller is unauthenticated or not an enterprise learner.
        Returns HTTP 429 when the per-endpoint rate limit is exceeded.
        Returns HTTP 500 when any step of the workflow fails, with no partial results.
        """
        # Checked before validation so a disabled pipeline is indistinguishable from an
        # endpoint that does not exist, whatever the payload.
        if not settings.LEARNER_PATHWAYS_SERVER_PIPELINE_ENABLED:
            raise NotFound()

        request_serializer = api_serializers.CareerDiscoveryRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)

        workflow = CareerDiscoveryWorkflow.objects.create(
            input_data=CareerDiscoveryWorkflow.generate_input_dict(request_serializer.validated_data),
        )
        logger.info('Created CareerDiscoveryWorkflow (uuid=%s)', workflow.uuid)

        try:
            workflow.execute()
        except UnitOfWorkException as exc:
            logger.exception('CareerDiscoveryWorkflow (uuid=%s) failed: %s', workflow.uuid, exc)
            raise CareerDiscoveryException(
                detail=f'Error in career discovery workflow: {exc}',
            ) from exc

        response_serializer = api_serializers.CareerDiscoveryResponseSerializer({
            'workflow_uuid': workflow.uuid,
            'careers': workflow.career_candidates(),
        })
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class PathwayAssemblyException(APIException):
    """
    Raised when the pathway assembly workflow fails.

    Carries no partial results, for the same reason ``CareerDiscoveryException`` does not:
    the step records hold every stage's input, output and failure, so a client is better
    served by a clean error and a workflow_uuid than by half a pathway it might render.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail = 'Pathway assembly failed.'
    default_code = 'pathway_assembly_error'


class PathwayViewSet(ViewSet):
    """
    Endpoint for assembling a pathway for a career the learner has chosen.

    A separate request from career discovery rather than a continuation of it: the
    learner's choice sits between the two, and only the client knows which career was
    picked.
    """

    # See CareerDiscoveryViewSet -- DRF requires the class attribute to exist for the
    # per-action throttle_scope initkwarg to be accepted.
    throttle_scope: str | None = None

    @extend_schema(
        tags=[LEARNER_PATHWAYS_API_TAG],
        summary='Assemble a course pathway for a selected career.',
        description=(
            'Runs the server-side pathway assembly workflow: reads the catalog\'s skill '
            'vocabulary, translates the career\'s skills into it, retrieves a candidate '
            'window, optionally re-ranks it with a model, and selects five courses that '
            'span difficulty levels without over-representing one provider. Returns HTTP '
            '200 with an empty course list when the catalog has no pathway for the career.'
        ),
        request=api_serializers.PathwayRequestSerializer,
        responses={
            status.HTTP_200_OK: api_serializers.PathwayResponseSerializer,
            status.HTTP_400_BAD_REQUEST: None,
            status.HTTP_401_UNAUTHORIZED: None,
            status.HTTP_403_FORBIDDEN: None,
            status.HTTP_404_NOT_FOUND: None,
            status.HTTP_429_TOO_MANY_REQUESTS: None,
            status.HTTP_500_INTERNAL_SERVER_ERROR: None,
        },
    )
    @permission_required(constants.LEARNER_PATHWAYS_PATHWAY_PERMISSION)
    @action(
        detail=False,
        methods=['post'],
        url_path='pathway',
        url_name='pathway',
        authentication_classes=(JwtAuthentication,),
        permission_classes=(permissions.IsAuthenticated,),
        throttle_classes=(ScopedRateThrottle,),
        throttle_scope='learner_pathways_pathway',
    )
    def pathway(self, request: Request) -> Response:
        """
        Assemble a five-course pathway for the selected career.

        Returns HTTP 404 when the server-side pipeline is disabled.
        Returns HTTP 400 for invalid request input.
        Returns HTTP 401/403 when the caller is unauthenticated or not an enterprise learner.
        Returns HTTP 429 when the per-endpoint rate limit is exceeded.
        Returns HTTP 500 when any step of the workflow fails, with no partial results.

        A career with no catalog coverage is **not** an error: it returns 200 with an
        empty ``courses`` list. That distinction matters because "we could not build this"
        and "something broke" lead to different client behaviour, and the catalog
        genuinely has careers with no matching courses.
        """
        if not settings.LEARNER_PATHWAYS_SERVER_PIPELINE_ENABLED:
            raise NotFound()

        request_serializer = api_serializers.PathwayRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        validated = request_serializer.validated_data

        workflow = PathwayAssemblyWorkflow.objects.create(
            input_data=PathwayAssemblyWorkflow.generate_input_dict(
                career_name=validated['career_name'],
                career_skills=validated['career_skills'],
                skills_required=validated.get('skills_required') or [],
                skills_preferred=validated.get('skills_preferred') or [],
                learner_profile=validated.get('learner_profile') or {},
            ),
        )
        logger.info('Created PathwayAssemblyWorkflow (uuid=%s)', workflow.uuid)

        try:
            workflow.execute()
        except UnitOfWorkException as exc:
            logger.exception('PathwayAssemblyWorkflow (uuid=%s) failed: %s', workflow.uuid, exc)
            raise PathwayAssemblyException(
                detail=f'Error in pathway assembly workflow: {exc}',
            ) from exc

        assembled = workflow.pathway() or {}
        response_serializer = api_serializers.PathwayResponseSerializer({
            'workflow_uuid': workflow.uuid,
            'courses': assembled.get('courses') or [],
            'unfilled_rungs': assembled.get('unfilled_rungs') or [],
        })
        return Response(response_serializer.data, status=status.HTTP_200_OK)
