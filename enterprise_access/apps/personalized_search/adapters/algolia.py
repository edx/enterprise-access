"""``CatalogSearch`` over enterprise-access's Algolia client (one ``multiple_queries`` round trip)."""
from enterprise_access.apps.api_client.algolia_client import AlgoliaClientError, AlgoliaSearchClient, SecuredAlgoliaKey
from enterprise_access.apps.personalized_search.core.errors import CatalogSearchError


class AlgoliaCatalog:
    """
    Runs the core's sub-requests through ``AlgoliaSearchClient.multi_search_catalog_index``. The scope's
    ``secured_key`` is a ``SecuredAlgoliaKey``; the client refuses an unscoped search unless both the
    scope and ``ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH`` allow it. A scope's ``client_config`` (the debug
    catalog) names another Algolia app, key or index to search instead of the configured ones.
    """

    def __init__(self, client: AlgoliaSearchClient | None = None):
        self._client = client

    def search(self, queries: list[dict], *, scope, timeout_s: float) -> list[dict]:
        """The client's results; its search and configuration errors become the port's ``CatalogSearchError``."""
        try:
            config = getattr(scope, 'client_config', None) or {}
            client = self._client or AlgoliaSearchClient(app_id=config.get('app_id'),
                                                         search_api_key=config.get('search_api_key'))
            extra = {'index_name': config['index_name']} if config.get('index_name') else {}
            return client.multi_search_catalog_index(queries, secured_key=scope.secured_key,
                                                     allow_unscoped=scope.allow_unscoped, timeout_s=timeout_s, **extra)
        except AlgoliaClientError as exc:
            # The type only: the client's message can name the index, and Algolia's can echo the filters.
            raise CatalogSearchError(type(exc).__name__) from exc


def secured_key_from_payload(payload: dict) -> SecuredAlgoliaKey:
    """The ``SecuredAlgoliaKey`` from enterprise-catalog's key-vending response."""
    return SecuredAlgoliaKey.from_response_payload(payload)
