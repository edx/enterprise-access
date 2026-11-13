"""
Personalized Search (Chat) turn endpoint (tech spec "API Contracts", "Turn pipeline").

POST ``/api/v1/personalized-search/turns/`` streams one turn as AI SDK UI Message Stream v1 over
SSE, or returns ``{"parts": [...]}`` with ``?mode=buffered`` (KD-4 fallback).

The turn itself runs in the portable core (``personalized_search/core``); this view only does HTTP:
auth, the learner permission, the feature flag, validation, the session lock and the response.
``personalized_search.wiring`` builds the models, catalog and store from settings.
"""
import json
import logging
import time

from django.http import JsonResponse, StreamingHttpResponse
from edx_django_utils.monitoring import set_custom_attribute
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from enterprise_access.apps.api.utils import get_enterprise_uuid_from_request_data
from enterprise_access.apps.core.constants import PERSONALIZED_SEARCH_LEARNER_PERMISSION
from enterprise_access.apps.personalized_search import events, wiring
from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.errors import SessionNotFound, TurnInProgress
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, TurnContext, platform_of, run_turn
from enterprise_access.apps.personalized_search.core.stream import STREAM_PROTOCOL_HEADER, sse_frames
from enterprise_access.apps.personalized_search.renderers import EventStreamRenderer
from enterprise_access.toggles import personalized_search_enabled

logger = logging.getLogger(__name__)

EFFORTS = ('none', 'minimal', 'low', 'medium', 'high')


class DebugOverridesSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Per-turn model overrides. Honoured only for users allowed debug mode (``wiring.debug_allowed``)."""

    intent_model = serializers.CharField(required=False)
    intent_effort = serializers.ChoiceField(choices=EFFORTS, required=False, allow_null=True)
    compose_model = serializers.CharField(required=False)
    compose_effort = serializers.ChoiceField(choices=EFFORTS, required=False, allow_null=True)

    def validate(self, attrs):
        allowed = wiring.model_registry().available_models()
        for key in ('intent_model', 'compose_model'):
            if attrs.get(key) and attrs[key] not in allowed:
                raise serializers.ValidationError({key: 'Not an allowed model.'})
        return attrs


class TurnRequestSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """POST body for one turn."""

    enterprise_customer_uuid = serializers.UUIDField()
    session_uuid = serializers.UUIDField(required=False, allow_null=True)
    message = serializers.CharField(max_length=constants.MAX_MESSAGE_CHARS, trim_whitespace=True)
    # The portal sets this on the first message after reopening a past search (usage events, KD-12).
    resumed_from_history = serializers.BooleanField(required=False, default=False)
    # The learner's portal language (e.g. "es"); results and model text follow it when it's supported.
    locale = serializers.CharField(required=False, allow_blank=True, max_length=16, default='')
    debug = DebugOverridesSerializer(required=False, allow_null=True)


def body_enterprise_uuid(request, *args, **kwargs):  # pylint: disable=unused-argument
    """
    The enterprise the permission is checked against: the request body's, the same one the turn runs under.
    (Reading the query string first would let the two disagree.)
    """
    return get_enterprise_uuid_from_request_data(request)


def _error(code: str, http_status: int, detail: str = '') -> JsonResponse:
    return JsonResponse({'code': code, 'detail': detail}, status=http_status)


def _model_choices(overrides: dict, allowed: bool = False) -> tuple[ModelChoice, ModelChoice, bool]:
    """
    The configured models, or the debug overrides when this user may use debug mode (``allowed``), and whether
    this turn applied overrides (the usage events' ``is_debug``).
    """
    intent_default, compose_default = wiring.default_models()
    if overrides and not allowed:
        logger.info('Ignoring Personalized Search debug overrides: debug mode is not allowed for this user here.')
        overrides = {}
    return (ModelChoice(overrides.get('intent_model') or intent_default.model,
                        overrides.get('intent_effort', intent_default.effort)),
            ModelChoice(overrides.get('compose_model') or compose_default.model,
                        overrides.get('compose_effort', compose_default.effort)),
            bool(overrides))


def _record_pre_stream(ctx: TurnContext, enterprise_customer_uuid: str, started: float) -> None:
    """
    Chunk 21: monitoring attributes and one timing line, set before the response is returned, because
    nothing inside the streaming generator runs in the request's monitoring context. No learner text.
    """
    backend = platform_of(ctx)
    set_custom_attribute('personalized_search_enterprise_uuid', enterprise_customer_uuid)
    set_custom_attribute('personalized_search_backend', backend)
    set_custom_attribute('personalized_search_degraded', False)
    logger.info('personalized_search.pre_stream %s', json.dumps({
        'session_uuid': ctx.session['uuid'], 'sequence': ctx.session['turn_count'] + 1, 'backend': backend,
        'pre_stream_ms': round((time.monotonic() - started) * 1000),
    }, sort_keys=True))


class PersonalizedSearchTurnView(APIView):
    """Stream one Personalized Search turn."""

    authentication_classes = (JwtAuthentication,)
    permission_classes = (permissions.IsAuthenticated,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'personalized_search_turn'
    renderer_classes = (JSONRenderer, EventStreamRenderer)

    @permission_required(PERSONALIZED_SEARCH_LEARNER_PERMISSION, fn=body_enterprise_uuid)
    def post(self, request):
        """Validate, lock the session, then stream the turn (or return it buffered)."""
        started = time.monotonic()
        if not personalized_search_enabled():
            raise NotFound()
        body = TurnRequestSerializer(data=request.data)
        if not body.is_valid():
            return Response({'code': 'invalid_request', 'errors': body.errors}, status=status.HTTP_400_BAD_REQUEST)
        data = body.validated_data
        enterprise_customer_uuid = str(data['enterprise_customer_uuid'])

        registry = wiring.model_registry()
        # Debug mode is checked against the user's role on the server; the portal's ?debug=true only asks for it.
        debug_allowed = wiring.debug_allowed(request.user)
        intent_choice, compose_choice, overridden = _model_choices(data.get('debug') or {}, debug_allowed)
        if not all(registry.is_configured(choice.model) for choice in (intent_choice, compose_choice)):
            return _error('backend_unsupported', status.HTTP_503_SERVICE_UNAVAILABLE, 'No model backend is configured.')

        try:
            # A ``debug`` object, even an empty one, marks a ``?debug=true`` turn (debug catalog, if configured).
            scope = wiring.catalog_scope(request, enterprise_customer_uuid, data.get('locale'),
                                         debug=debug_allowed and data.get('debug') is not None)
        except Exception:  # pylint: disable=broad-exception-caught  # enterprise-catalog failure or no secured key
            logger.exception('Personalized Search could not scope the catalog for %s.', enterprise_customer_uuid)
            return _error('catalog_unavailable', status.HTTP_503_SERVICE_UNAVAILABLE)

        lms_user_id = getattr(request.user, 'lms_user_id', None)
        if not lms_user_id:  # sessions are owned by the LMS user id; the Django pk is a different id space
            return _error('forbidden', status.HTTP_403_FORBIDDEN)
        store = wiring.session_store()
        session_uuid = str(data['session_uuid']) if data.get('session_uuid') else None
        try:
            session = store.load_or_create(
                session_uuid=session_uuid, lms_user_id=lms_user_id,
                enterprise_customer_uuid=enterprise_customer_uuid, message=data['message'],
            )
            store.lock(session)
        except SessionNotFound as exc:
            raise NotFound() from exc
        except TurnInProgress:
            return _error('turn_in_progress', status.HTTP_409_CONFLICT)

        def release():
            store.unlock(session)

        try:
            ctx = wiring.turn_context(session=session, message=data['message'], intent_model=intent_choice,
                                      compose_model=compose_choice, scope=scope,
                                      debug=debug_allowed,
                                      debug_overrides=overridden or scope.is_debug,
                                      registry=registry)
            _record_pre_stream(ctx, enterprise_customer_uuid, started)
            if data.get('resumed_from_history') and session_uuid:  # only a past search can be resumed
                events.send(lms_user_id, events.SESSION_RESUMED, conversation_id=session['uuid'],
                            enterprise_customer_uuid=enterprise_customer_uuid, turn_index=session['turn_count'] + 1)
            parts = run_turn(ctx, wiring.steps(registry=registry, store=store))
        except Exception:  # pylint: disable=broad-exception-caught
            release()  # don't leave the session locked for the lock window
            raise

        if request.query_params.get('mode') == 'buffered':
            try:
                return Response({'parts': list(parts)})
            finally:
                release()

        response = StreamingHttpResponse(sse_frames(parts, release), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        response[STREAM_PROTOCOL_HEADER[0]] = STREAM_PROTOCOL_HEADER[1]
        return response
