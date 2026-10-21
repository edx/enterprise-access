"""
Decides how a request to the catalog index should be authorized: a request-scoped
secured key when one can be vended for a known enterprise customer, or an explicit
unscoped fallback.

Deliberately takes no request/crum dependency here -- the caller resolves that and passes
in a plain callable, which keeps this trivially testable (a fake ``vend_secured_key``, no
request mocking needed).

Does NOT independently decide whether unscoped access is globally permitted
(``ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH``) -- that rule already belongs to
``AlgoliaSearchClient._resolve_catalog_api_key``, and duplicating it here would be a
second place for it to be wrong. ``AlgoliaSearchClient`` still makes the final refusal if
this resolves to no key and no unscoped permission.
"""
import logging
from dataclasses import dataclass

import requests

from enterprise_access.apps.api_client.algolia_client import SecuredAlgoliaKey

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogAccess:
    """What a catalog search got: either a secured key, or (possibly) unscoped permission."""

    secured_key: SecuredAlgoliaKey | None = None
    allow_unscoped: bool = False


def resolve_catalog_access(*, enterprise_customer_uuid, vend_secured_key, allow_unscoped_fallback=False):
    """
    Resolve the credential a catalog search should be issued with.

    Args:
        enterprise_customer_uuid: The customer to vend a secured key for, or empty/None.
        vend_secured_key: A callable of one argument (``enterprise_customer_uuid``)
            returning a ``SecuredAlgoliaKey``, or ``None`` if no request context is
            available to vend one from (e.g. the offline evaluation harness). Never called
            if ``enterprise_customer_uuid`` is falsy.
        allow_unscoped_fallback: Passed through unchanged as the fallback's
            ``allow_unscoped`` value when no secured key was vended -- mirrors the step's
            own input flag.

    Returns:
        A ``CatalogAccess``. If a secured key can't be vended, falls back to
        ``CatalogAccess(allow_unscoped=allow_unscoped_fallback)`` -- ``AlgoliaSearchClient``
        itself refuses the call if that's not actually permitted by settings.
    """
    if enterprise_customer_uuid and vend_secured_key is not None:
        try:
            return CatalogAccess(secured_key=vend_secured_key(enterprise_customer_uuid))
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.warning(
                'Could not vend a secured catalog key for enterprise_customer_uuid=%s: %s',
                enterprise_customer_uuid, exc,
            )

    return CatalogAccess(allow_unscoped=allow_unscoped_fallback)
