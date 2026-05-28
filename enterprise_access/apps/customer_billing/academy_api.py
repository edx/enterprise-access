"""
Helpers for fetching and caching Academy metadata from enterprise-catalog.

Academy display data (title, description, etc.) is NOT stored locally.
Use these helpers to fetch it on demand with TieredCache backing.
"""
import logging

from django.conf import settings
from edx_django_utils.cache import TieredCache

from enterprise_access.apps.api_client.enterprise_catalog_client import EnterpriseCatalogApiClient
from enterprise_access.cache_utils import versioned_cache_key

logger = logging.getLogger(__name__)

ACADEMY_DATA_CACHE_TIMEOUT = getattr(settings, 'ACADEMY_DATA_CACHE_TIMEOUT', 300)


def get_cached_academy_data(academy_uuid, timeout=None):
    """
    Fetch and cache Academy metadata from enterprise-catalog for the given UUID.

    Arguments:
        academy_uuid (str|UUID): The academy UUID to fetch.
        timeout (int, optional): Cache TTL in seconds. Defaults to ACADEMY_DATA_CACHE_TIMEOUT.

    Returns:
        dict: Academy data from enterprise-catalog, or None if academy_uuid is falsy.
    """
    if not academy_uuid:
        return None

    cache_key = versioned_cache_key('academy_data', str(academy_uuid))
    cached = TieredCache.get_cached_response(cache_key)
    if cached.is_found:
        logger.info('Cache hit for academy %s', academy_uuid)
        return cached.value

    data = EnterpriseCatalogApiClient().get_academy(academy_uuid)
    TieredCache.set_all_tiers(
        cache_key,
        data,
        django_cache_timeout=timeout if timeout is not None else ACADEMY_DATA_CACHE_TIMEOUT,
    )
    logger.info('Cached academy data for %s', academy_uuid)
    return data
