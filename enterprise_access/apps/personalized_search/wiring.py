"""
Composition root for Personalized Search in enterprise-access: the only module that reads Django
settings and builds the adapters the portable core runs on (``core/ports.py``).

Views and management commands call these functions; another host would write its own wiring.
"""
from django.conf import settings

from enterprise_access.apps.bffs.api import get_and_cache_secured_algolia_search_keys
from enterprise_access.apps.personalized_search.adapters.algolia import AlgoliaCatalog, secured_key_from_payload
from enterprise_access.apps.personalized_search.adapters.django_store import DjangoSessionStore
from enterprise_access.apps.personalized_search.adapters.openai_backend import OpenAIBackend
from enterprise_access.apps.personalized_search.adapters.segment_sink import segment_emit
from enterprise_access.apps.personalized_search.adapters.xpert_backend import XpertBackend
from enterprise_access.apps.personalized_search.core import constants, retrieval
from enterprise_access.apps.personalized_search.core.errors import ModelConfigurationError
from enterprise_access.apps.personalized_search.core.model_registry import ModelRegistry
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, Steps, TurnContext
from enterprise_access.apps.personalized_search.core.retrieval import CatalogScope


def xpert_backend() -> XpertBackend:
    """The Xpert platform backend, from ``XPERT_API_*`` (unconfigured, it lists no models)."""
    return XpertBackend(base_url=getattr(settings, 'XPERT_API_BASE_URL', ''),
                        client_id=getattr(settings, 'XPERT_API_CLIENT_ID', ''),
                        request_timeout_s=float(getattr(settings, 'XPERT_REQUEST_TIMEOUT', 45)))


def model_registry() -> ModelRegistry:
    """OpenAI (the ``PERSONALIZED_SEARCH_OPENAI_MODELS`` allowlist), then Xpert when configured."""
    return ModelRegistry([
        OpenAIBackend(api_key=getattr(settings, 'OPENAI_API_KEY', '') or '',
                      models=settings.PERSONALIZED_SEARCH_OPENAI_MODELS),
        xpert_backend(),
    ])


def session_store() -> DjangoSessionStore:
    return DjangoSessionStore()


def default_models() -> tuple[ModelChoice, ModelChoice]:
    """The configured (intent, compose) models and efforts (OQ-8)."""
    return (ModelChoice(settings.PERSONALIZED_SEARCH_INTENT_MODEL, settings.PERSONALIZED_SEARCH_INTENT_EFFORT),
            ModelChoice(settings.PERSONALIZED_SEARCH_COMPOSE_MODEL, settings.PERSONALIZED_SEARCH_COMPOSE_EFFORT))


def steps(*, registry: ModelRegistry | None = None, catalog=None, store=None, persist: bool = True) -> Steps:
    """Model calls through the registry, catalog reads through Algolia, persistence through the store."""
    registry = registry or model_registry()
    catalog = catalog or AlgoliaCatalog()
    store = store or session_store()

    def intent(*, system, user, model, effort, timeout_s):
        return registry.complete_json(model=model, effort=effort, system=system, user=user,
                                      max_tokens=constants.INTENT_MAX_TOKENS, timeout_s=timeout_s)

    def compose(*, system, user, model, effort, timeout_s, usage):
        return registry.stream_text(model=model, effort=effort, system=system, user=user,
                                    max_tokens=constants.COMPOSE_MAX_TOKENS, timeout_s=timeout_s, usage=usage)

    def retrieve(merged, scope, excluded, timeout_s=constants.RETRIEVAL_TIMEOUT_S):
        return retrieval.search(merged, scope, excluded, catalog=catalog, timeout_s=timeout_s)

    return Steps(intent=intent, retrieve=retrieve, compose=compose, persist=store.persist_turn if persist else None,
                 emit=segment_emit if persist else None)


def _step_timeout(registry: ModelRegistry, model: str, default_s: float) -> float:
    """A backend that brings its own request timeout (Xpert) uses it; others use the setting."""
    try:
        own = getattr(registry.backend_for(model), 'request_timeout_s', None)
    except ModelConfigurationError:  # unknown model: the turn refuses it
        own = None
    return own or default_s


def turn_context(*, session: dict, message: str, intent_model: ModelChoice, compose_model: ModelChoice,
                 scope: CatalogScope, debug: bool = False, registry: ModelRegistry | None = None) -> TurnContext:
    """A ``TurnContext`` with the configured timeouts and the registry's platform names."""
    registry = registry or model_registry()
    return TurnContext(
        session=session, message=message, intent_model=intent_model, compose_model=compose_model, scope=scope,
        debug=debug, platform=registry.platform_of,
        analytics={'cohort': settings.PERSONALIZED_SEARCH_ANALYTICS_COHORT,
                   'engine': settings.PERSONALIZED_SEARCH_ENGINE_LABEL},
        intent_timeout_s=_step_timeout(registry, intent_model.model, settings.PERSONALIZED_SEARCH_INTENT_TIMEOUT_S),
        compose_timeout_s=_step_timeout(registry, compose_model.model,
                                        settings.PERSONALIZED_SEARCH_COMPOSE_READ_TIMEOUT_S),
    )


def dev_catalog_scope() -> CatalogScope | None:
    """
    Dev only: the plain key plus an explicit catalog filter (``PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE``).
    The Algolia client still refuses it unless ``ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH`` is on.
    """
    dev_scope = getattr(settings, 'PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE', None)
    if not dev_scope:
        return None
    return CatalogScope(filter=dev_scope['filter'], allow_unscoped=True, label=dev_scope.get('label'))


def catalog_scope(request, enterprise_customer_uuid: str) -> CatalogScope:
    """The learner's secured key and enterprise filter, or the dev catalog scope when configured."""
    dev_scope = dev_catalog_scope()
    if dev_scope:
        return dev_scope
    payload = get_and_cache_secured_algolia_search_keys(request, enterprise_customer_uuid)
    return CatalogScope(
        filter=f'enterprise_customer_uuids:"{enterprise_customer_uuid}"',
        secured_key=secured_key_from_payload(payload),
    )


def debug_overrides_enabled() -> bool:
    """Whether per-turn model overrides and the debug read-out are allowed in this environment."""
    return bool(getattr(settings, 'PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED', False))
