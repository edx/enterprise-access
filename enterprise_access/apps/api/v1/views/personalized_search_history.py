"""
Personalized Search history and item feedback (tech spec "API Contracts"; chunks 28 and 30, MVP).

- ``GET sessions/?enterprise_customer_uuid=&page=``: the learner's sessions in this enterprise that have
  at least one turn, newest first, 20 per page.
- ``GET sessions/{uuid}/?enterprise_customer_uuid=``: one session with its turns' stored parts, for replay.
  ``upstream_conversation_id`` is never returned. Another learner's or enterprise's session is 404.
- ``GET item-feedback/?enterprise_customer_uuid=&kind=saved``, ``POST item-feedback/`` (idempotent:
  201, then 200 for the same item and kind) and ``DELETE item-feedback/?enterprise_customer_uuid=&item_id=&kind=``.

Every query filters on the requesting learner's ``lms_user_id`` and the enterprise, on top of the
enterprise-scoped learner permission. All endpoints 404 while the feature flag is off. Reads and
writes go through the history and feedback stores (``personalized_search.adapters.django_store``).
"""
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from enterprise_access.apps.api.utils import get_or_fetch_enterprise_uuid_for_bff_request
from enterprise_access.apps.core.constants import PERSONALIZED_SEARCH_LEARNER_PERMISSION
from enterprise_access.apps.personalized_search.adapters.django_store import DjangoFeedbackStore, DjangoHistoryStore
from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.errors import SessionNotFound
from enterprise_access.toggles import personalized_search_enabled

FEEDBACK_KINDS = DjangoFeedbackStore.kinds


def enterprise_uuid_for_request(request, *args, **kwargs):
    """``get_or_fetch_enterprise_uuid_for_bff_request`` for views whose URL has its own kwargs (``session_uuid``)."""
    return get_or_fetch_enterprise_uuid_for_bff_request(request)


def learner_id(request) -> int:
    """The requesting learner's LMS user id (the key every session and feedback row is scoped by)."""
    return getattr(request.user, 'lms_user_id', None) or request.user.id


class _LearnerView(APIView):
    """Shared auth: cookie JWT, authenticated, flag on."""

    authentication_classes = (JwtAuthentication,)
    permission_classes = (permissions.IsAuthenticated,)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not personalized_search_enabled():
            raise NotFound()


class EnterpriseQuerySerializer(serializers.Serializer):  # pylint: disable=abstract-method
    enterprise_customer_uuid = serializers.UUIDField()


class SessionListQuerySerializer(EnterpriseQuerySerializer):  # pylint: disable=abstract-method
    page = serializers.IntegerField(min_value=1, required=False, default=1)


def _validated(serializer_class, data):
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class PersonalizedSearchSessionListView(_LearnerView):
    """The learner's sessions in this enterprise, newest first."""

    @permission_required(PERSONALIZED_SEARCH_LEARNER_PERMISSION, fn=enterprise_uuid_for_request)
    def get(self, request):
        """List sessions with at least one turn, 20 per page."""
        query = _validated(SessionListQuerySerializer, request.query_params)
        enterprise = query['enterprise_customer_uuid']
        page = DjangoHistoryStore().list_sessions(lms_user_id=learner_id(request), enterprise_customer_uuid=enterprise,
                                                  page=query['page'])
        if page is None:
            raise NotFound()
        base = request.build_absolute_uri(request.path)

        def link(number):
            return f'{base}?enterprise_customer_uuid={enterprise}&page={number}' if number else None

        return Response({'count': page['count'], 'next': link(page['next_page']),
                         'previous': link(page['previous_page']), 'results': page['results']})


class PersonalizedSearchSessionDetailView(_LearnerView):
    """One of the learner's sessions with its stored turns."""

    @permission_required(PERSONALIZED_SEARCH_LEARNER_PERMISSION, fn=enterprise_uuid_for_request)
    def get(self, request, session_uuid):
        """The session and its turns' stored parts, in sequence order."""
        query = _validated(EnterpriseQuerySerializer, request.query_params)
        try:
            return Response(DjangoHistoryStore().get_session(
                session_uuid=session_uuid, lms_user_id=learner_id(request),
                enterprise_customer_uuid=query['enterprise_customer_uuid'],
            ))
        except SessionNotFound as exc:
            raise NotFound() from exc


class ItemFeedbackWriteSerializer(EnterpriseQuerySerializer):  # pylint: disable=abstract-method
    """POST body for item feedback."""

    item_id = serializers.CharField(max_length=255)
    item_type = serializers.ChoiceField(choices=constants.ITEM_TYPES)
    kind = serializers.ChoiceField(choices=list(FEEDBACK_KINDS))
    session_uuid = serializers.UUIDField(required=False, allow_null=True)
    card_snapshot = serializers.JSONField(required=False, allow_null=True)


class ItemFeedbackListQuerySerializer(EnterpriseQuerySerializer):  # pylint: disable=abstract-method
    kind = serializers.ChoiceField(choices=list(FEEDBACK_KINDS), default=FEEDBACK_KINDS[0])


class ItemFeedbackDeleteQuerySerializer(EnterpriseQuerySerializer):  # pylint: disable=abstract-method
    item_id = serializers.CharField(max_length=255)
    kind = serializers.ChoiceField(choices=list(FEEDBACK_KINDS))


class PersonalizedSearchItemFeedbackView(_LearnerView):
    """Save / not-interested items, and undo (REQ-009)."""

    @permission_required(PERSONALIZED_SEARCH_LEARNER_PERMISSION, fn=enterprise_uuid_for_request)
    def get(self, request):
        """The learner's feedback of one kind in this enterprise, newest first."""
        query = _validated(ItemFeedbackListQuerySerializer, request.query_params)
        return Response({'results': DjangoFeedbackStore().list(
            lms_user_id=learner_id(request), enterprise_customer_uuid=query['enterprise_customer_uuid'],
            kind=query['kind'],
        )})

    @permission_required(PERSONALIZED_SEARCH_LEARNER_PERMISSION, fn=enterprise_uuid_for_request)
    def post(self, request):
        """Record feedback; an existing row for the same item and kind is returned as-is (200)."""
        data = _validated(ItemFeedbackWriteSerializer, request.data)
        row, created = DjangoFeedbackStore().add(
            lms_user_id=learner_id(request), enterprise_customer_uuid=data['enterprise_customer_uuid'],
            item_id=data['item_id'], item_type=data['item_type'], kind=data['kind'],
            session_uuid=data.get('session_uuid'), card_snapshot=data.get('card_snapshot'),
        )
        return Response(row, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @permission_required(PERSONALIZED_SEARCH_LEARNER_PERMISSION, fn=enterprise_uuid_for_request)
    def delete(self, request):
        """Undo: remove the learner's feedback row for this item and kind (204 even if it was already gone)."""
        query = _validated(ItemFeedbackDeleteQuerySerializer, request.query_params)
        DjangoFeedbackStore().remove(
            lms_user_id=learner_id(request), enterprise_customer_uuid=query['enterprise_customer_uuid'],
            item_id=query['item_id'], kind=query['kind'],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
