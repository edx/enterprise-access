"""
Personalized Search debug model menu (the portal's ``?debug=true`` panel).

GET ``/api/v1/personalized-search/debug/`` lists the allowed models with prices, efforts, defaults and
the catalog label. 404 unless PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED, so it doesn't exist outside dev.
"""
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import permissions
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from enterprise_access.apps.api.utils import get_or_fetch_enterprise_uuid_for_bff_request
from enterprise_access.apps.api.v1.views.personalized_search import EFFORTS
from enterprise_access.apps.core.constants import PERSONALIZED_SEARCH_LEARNER_PERMISSION
from enterprise_access.apps.personalized_search import wiring
from enterprise_access.toggles import personalized_search_enabled


def _menu_entry(registry, model: str) -> dict:
    """One model for the panel: its prices (when the backend knows them) and the efforts it accepts."""
    backend = registry.backend_for(model)
    prices = getattr(backend, 'prices', lambda _m: None)(model)
    label = getattr(backend, 'label', None) or model
    efforts = [e for e in EFFORTS if backend.resolve_effort(model, e) == e] or [None]
    return {'id': model, 'label': label, 'price_in': prices[0] if prices else None,
            'price_cached_in': prices[1] if prices else None, 'price_out': prices[2] if prices else None,
            'efforts': efforts}


class PersonalizedSearchDebugConfigView(APIView):
    """GET the debug model menu."""

    authentication_classes = (JwtAuthentication,)
    permission_classes = (permissions.IsAuthenticated,)

    @permission_required(PERSONALIZED_SEARCH_LEARNER_PERMISSION, fn=get_or_fetch_enterprise_uuid_for_bff_request)
    def get(self, request):
        """The allowed models, efforts and defaults for the debug panel."""
        if not wiring.debug_overrides_enabled() or not personalized_search_enabled():
            raise NotFound()
        registry = wiring.model_registry()
        intent_default, compose_default = wiring.default_models()
        dev_scope = wiring.dev_catalog_scope()
        return Response({
            'models': [_menu_entry(registry, model) for model in registry.available_models()],
            'defaults': {
                'intent_model': intent_default.model, 'intent_effort': intent_default.effort,
                'compose_model': compose_default.model, 'compose_effort': compose_default.effort,
            },
            'catalog': (dev_scope.label if dev_scope else None) or 'Learner\'s enterprise catalog (secured key)',
        })
