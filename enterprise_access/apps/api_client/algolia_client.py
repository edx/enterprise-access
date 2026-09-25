"""
Search-only Algolia client for enterprise-access.

enterprise-catalog owns an ``AlgoliaSearchClient`` too, but it is an *indexing and
administration* client built on the write-scoped ``ALGOLIA.API_KEY`` and it exposes no
``search()``. That key must never reach this service, so this module is a separate,
deliberately read-only client rather than a reuse of that one.

Two indexes, two credential paths, and they are not interchangeable:

* **Catalog index** — queried with an *enterprise-scoped secured key* vended by
  enterprise-catalog. That scoping is the whole point: it is what keeps results inside
  the learner's catalog without this service reimplementing catalog filtering.
* **Jobs (Lightcast taxonomy) index** — queried with the plain search key. Secured keys
  set ``restrictIndices`` to the catalog index and its replicas only
  (``enterprise-catalog``'s ``generate_secured_api_key``), so a secured key *cannot*
  read the jobs index. The learner portal MFE encodes the same constraint as
  ``unsupportedSecuredAlgoliaIndices = [ALGOLIA_INDEX_NAME_JOBS]``.

Sending a secured key to the jobs index therefore fails at Algolia with an opaque error.
``search_jobs_index()`` refuses before issuing the request instead.
"""
import base64
import binascii
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from algoliasearch.exceptions import AlgoliaException, AlgoliaUnreachableHostException, RequestException
from algoliasearch.search_client import SearchClient
from django.conf import settings
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)


class AlgoliaClientError(Exception):
    """Base class for every error raised by this module."""


class AlgoliaConfigurationError(AlgoliaClientError):
    """
    Raised when a search cannot be issued safely: missing settings, or a credential
    that is wrong for the requested index.

    Distinct from ``AlgoliaSearchError`` because it is never transient — retrying will
    not help, and no request was sent.
    """


class AlgoliaSearchError(AlgoliaClientError):
    """
    Raised when a search request fails in transit.

    Wraps ``AlgoliaException`` and friends so callers never have to import
    ``algoliasearch`` to handle a failure.
    """


def looks_like_secured_api_key(api_key: str) -> bool:
    """
    Whether ``api_key`` is an Algolia *secured* key rather than a plain search key.

    A secured key is ``base64(hmac_sha256_hex + urlencoded_restrictions)``, so decoding
    one yields a querystring carrying ``restrictIndices`` and/or ``validUntil``. A plain
    search key is a 32-character hex string that does not base64-decode to anything of
    that shape.

    Used to catch a misconfiguration — a secured key handed to the client as its plain
    search key — before it reaches the jobs index, where ``restrictIndices`` guarantees
    it will fail with an opaque Algolia error.
    """
    if not api_key:
        return False
    try:
        decoded = base64.b64decode(api_key, validate=True).decode('utf-8', errors='replace')
    except (binascii.Error, ValueError):
        return False
    return 'restrictIndices=' in decoded or 'validUntil=' in decoded


@dataclass(frozen=True)
class SecuredAlgoliaKey:
    """
    An enterprise-scoped secured Algolia API key and the instant it stops working.

    Built from enterprise-catalog's ``secured-algolia-api-key`` response, whose
    ``valid_until`` is an ISO-8601 UTC timestamp.
    """

    api_key: str
    valid_until: datetime | None

    @classmethod
    def from_response_payload(cls, payload: dict[str, Any]) -> 'SecuredAlgoliaKey':
        """
        Build a key from an enterprise-catalog ``get_secured_algolia_api_key()`` payload.

        Raises:
            AlgoliaConfigurationError: If the payload carries no secured key.
        """
        algolia_payload = (payload or {}).get('algolia') or {}
        # The BFF serializes this field as ``secured_algolia_api_key``; the raw
        # enterprise-catalog response uses ``secured_api_key``. Accept either.
        api_key = (
            algolia_payload.get('secured_api_key') or
            algolia_payload.get('secured_algolia_api_key')
        )
        if not api_key:
            raise AlgoliaConfigurationError(
                'enterprise-catalog returned no secured Algolia API key; '
                'refusing to fall back to an unscoped search key.'
            )

        raw_valid_until = algolia_payload.get('valid_until')
        valid_until = parse_datetime(raw_valid_until) if raw_valid_until else None
        if valid_until is not None and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)

        return cls(api_key=api_key, valid_until=valid_until)

    def is_expired(self, now: datetime | None = None) -> bool:
        """
        Whether this key is past ``valid_until``.

        A key with no ``valid_until`` is treated as expired: an unknown expiry is not
        evidence of validity, and the safe failure here is to re-vend.
        """
        if self.valid_until is None:
            return True
        return (now or datetime.now(timezone.utc)) >= self.valid_until


class AlgoliaSearchClient:
    """
    Issues read-only queries against the catalog and jobs Algolia indexes.

    One instance holds no credentials of its own beyond the configured application ID
    and plain search key. The per-enterprise secured key is passed in per call, because
    it is vended per user by enterprise-catalog and expires.

    This class deliberately exposes only ``search``-shaped methods. It has no write,
    index-management, or key-management surface.
    """

    def __init__(self, app_id: str | None = None, search_api_key: str | None = None):
        self._app_id = app_id or settings.ALGOLIA_APP_ID
        self._search_api_key = search_api_key or settings.ALGOLIA_SEARCH_API_KEY

        if not self._app_id:
            raise AlgoliaConfigurationError('ALGOLIA_APP_ID is not configured.')

    @property
    def catalog_index_name(self) -> str:
        return settings.ALGOLIA_CATALOG_INDEX_NAME

    @property
    def jobs_index_name(self) -> str:
        return settings.ALGOLIA_JOBS_INDEX_NAME

    def _search(self, index_name: str, api_key: str, query: str, search_params: dict[str, Any]) -> dict[str, Any]:
        """
        Issue one search and return the raw Algolia response body.

        Mirrors the calling shape already used in enterprise-catalog's CSV export view:
        ``index.search(query, {facetFilters, attributesToRetrieve, hitsPerPage, page})``.

        Raises:
            AlgoliaConfigurationError: If ``index_name`` or ``api_key`` is missing.
            AlgoliaSearchError: On any transport or API failure.
        """
        if not index_name:
            raise AlgoliaConfigurationError('Cannot search Algolia without an index name.')
        if not api_key:
            raise AlgoliaConfigurationError(f'Cannot search Algolia index {index_name!r} without an API key.')

        client = SearchClient.create(self._app_id, api_key)
        try:
            return client.init_index(index_name).search(query, search_params)
        except (AlgoliaException, AlgoliaUnreachableHostException, RequestException) as exc:
            # The query text is safe to log (it is derived from learner intent, not
            # credentials); the API key is never logged.
            logger.exception(
                'Algolia search failed for index=%r, query=%r.',
                index_name, query,
            )
            raise AlgoliaSearchError(f'Algolia search failed for index {index_name!r}: {exc}') from exc
        finally:
            client.close()

    def search_catalog_index(
        self,
        query: str,
        *,
        secured_key: SecuredAlgoliaKey | None = None,
        allow_unscoped: bool = False,
        index_name: str | None = None,
        **search_params: Any,
    ) -> dict[str, Any]:
        """
        Search the enterprise catalog index.

        Normally requires a live enterprise-scoped ``secured_key``; that is what makes
        results catalog-correct for the learner.

        ``allow_unscoped=True`` opts out of scoping and searches the whole index with the
        plain search key. It exists for the offline retrieval diagnostic, which runs as a
        management command with no request and therefore no per-user secured key to vend.
        It is gated on ``settings.ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH`` so it cannot be
        reached in an environment that has not deliberately enabled it, and it is never a
        fallback — a missing or expired secured key raises rather than silently widening
        the search.

        Raises:
            AlgoliaConfigurationError: If no usable secured key is supplied, or if
                ``allow_unscoped`` is requested but not enabled by settings.
            AlgoliaSearchError: On any transport or API failure.
        """
        resolved_index_name = index_name or self.catalog_index_name
        api_key = self._resolve_catalog_api_key(secured_key, allow_unscoped, resolved_index_name)
        return self._search(resolved_index_name, api_key, query, search_params)

    def _resolve_catalog_api_key(
        self,
        secured_key: SecuredAlgoliaKey | None,
        allow_unscoped: bool,
        index_name: str,
    ) -> str:
        """
        Decide which key may be used for a catalog read, or refuse.

        Shared by every catalog-reading method so the scoping rules exist in exactly one
        place — a second copy of this logic is a second place for it to be wrong.
        """
        if allow_unscoped:
            if not getattr(settings, 'ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH', False):
                raise AlgoliaConfigurationError(
                    'Unscoped catalog search requires ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH to be enabled.'
                )
            logger.warning(
                'Issuing an UNSCOPED Algolia catalog search against index=%r. '
                'Results are not restricted to any enterprise catalog.',
                index_name,
            )
            return self._search_api_key

        if secured_key is None:
            raise AlgoliaConfigurationError(
                'A secured Algolia API key is required to search the catalog index. '
                'Pass allow_unscoped=True only for offline diagnostics.'
            )
        if secured_key.is_expired():
            raise AlgoliaConfigurationError(
                'The supplied secured Algolia API key is expired; re-vend it before searching. '
                'Refusing to issue an unscoped query.'
            )

        return secured_key.api_key

    def search_facet_values(
        self,
        facet_name: str,
        facet_query: str,
        *,
        secured_key: SecuredAlgoliaKey | None = None,
        allow_unscoped: bool = False,
        index_name: str | None = None,
        max_facet_hits: int = 20,
        **search_params: Any,
    ) -> list[dict[str, Any]]:
        """
        Search the *values* of one facet on the catalog index.

        Answers "what does this index actually call this?" — e.g. a ``skill_names``
        facet query for ``Python`` returns ``Python (Programming Language)``. That is the
        cheap, always-current alternative to a hand-maintained alias map for grounding a
        skill name against the catalog's Lightcast-canonical vocabulary.

        Returns a list of ``{'value': str, 'count': int, 'highlighted': str}``.

        **The counts are not course counts.** Catalog records are duplicated per customer
        group (``objectID`` looks like ``course-<uuid>-customer-uuids-9``) and this
        endpoint is not restricted to ``content_type:course``, so a count here can be two
        orders of magnitude above the number of matching courses. Treat a value as
        *candidate vocabulary* and confirm it against a scoped facet snapshot before
        relying on it.

        Raises:
            AlgoliaConfigurationError: Same credential rules as ``search_catalog_index``.
            AlgoliaSearchError: On any transport or API failure.
        """
        resolved_index_name = index_name or self.catalog_index_name
        api_key = self._resolve_catalog_api_key(secured_key, allow_unscoped, resolved_index_name)

        if not resolved_index_name:
            raise AlgoliaConfigurationError('Cannot search Algolia facets without an index name.')

        client = SearchClient.create(self._app_id, api_key)
        try:
            response = client.init_index(resolved_index_name).search_for_facet_values(
                facet_name,
                facet_query,
                {'maxFacetHits': max_facet_hits, **search_params},
            )
        except (AlgoliaException, AlgoliaUnreachableHostException, RequestException) as exc:
            logger.exception(
                'Algolia facet search failed for index=%r, facet=%r.',
                resolved_index_name, facet_name,
            )
            raise AlgoliaSearchError(
                f'Algolia facet search failed for {facet_name!r} on {resolved_index_name!r}: {exc}'
            ) from exc
        finally:
            client.close()

        return response.get('facetHits', [])

    def search_jobs_index(
        self,
        query: str,
        *,
        index_name: str | None = None,
        **search_params: Any,
    ) -> dict[str, Any]:
        """
        Search the Lightcast jobs/taxonomy index with the plain search key.

        There is no secured-key variant. Secured keys restrict themselves to the catalog
        index and its replicas, so one would fail here; the parameter is absent rather
        than validated so that no caller can construct that request at all.

        Raises:
            AlgoliaConfigurationError: If the jobs index or plain search key is unset.
            AlgoliaSearchError: On any transport or API failure.
        """
        resolved_index_name = index_name or self.jobs_index_name
        if resolved_index_name and resolved_index_name == self.catalog_index_name:
            raise AlgoliaConfigurationError(
                f'Refusing to search index {resolved_index_name!r} as a jobs index: '
                'it is the configured catalog index, which requires a secured key.'
            )
        if looks_like_secured_api_key(self._search_api_key):
            raise AlgoliaConfigurationError(
                'The configured Algolia search key is a secured key. Secured keys are '
                f'restricted to the catalog index and cannot read {resolved_index_name!r}.'
            )
        return self._search(resolved_index_name, self._search_api_key, query, search_params)
