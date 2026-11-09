"""
Personalized Search history and item feedback (tech spec "API Contracts"; chunks 28 and 30, MVP).

- ``GET sessions/?enterprise_customer_uuid=&page=``: the learner's sessions in this enterprise that have
  at least one turn, newest first, 20 per page.
- ``GET sessions/{uuid}/?enterprise_customer_uuid=``: one session with its turns' stored parts, for replay.
  ``upstream_conversation_id`` is never returned. Another learner's or enterprise's session is 404.

Every query filters on the requesting learner's ``lms_user_id`` and the enterprise, on top of the
enterprise-scoped learner permission. All endpoints 404 while the feature flag is off. Reads and
writes go through the history and feedback stores (``personalized_search.adapters.django_store``).
"""
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions, serializers
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from enterprise_access.apps.api.utils import get_or_fetch_enterprise_uuid_for_bff_request
from enterprise_access.apps.core.constants import PERSONALIZED_SEARCH_LEARNER_PERMISSION
from enterprise_access.apps.personalized_search.adapters.django_store import DjangoHistoryStore
from enterprise_access.apps.personalized_search.core.errors import SessionNotFound
from enterprise_access.toggles import personalized_search_enabled


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
