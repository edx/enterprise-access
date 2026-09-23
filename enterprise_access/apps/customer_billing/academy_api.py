"""
Helpers for fetching and caching Academy metadata from enterprise-catalog.

Academy display data (title, description, etc.) is NOT stored locally.
Use these helpers to fetch it on demand with TieredCache backing.
"""
from django.conf import settings
from edx_django_utils.cache import TieredCache

from enterprise_access.apps.api_client.enterprise_catalog_client import EnterpriseCatalogApiClient
from enterprise_access.cache_utils import versioned_cache_key

ACADEMY_CACHE_KEY_PREFIX = 'academy_data'
COURSE_COUNT_CACHE_KEY_PREFIX = 'academy_course_count'


def get_cached_academy_data(academy_uuid, timeout=None):
    """
    Fetch and cache Academy metadata from enterprise-catalog for the given UUID.

    Arguments:
        academy_uuid (str|UUID): The academy UUID to fetch.
        timeout (int, optional): Cache TTL in seconds. Defaults to settings.ACADEMY_DATA_CACHE_TIMEOUT.

    Returns:
        dict: Academy data from enterprise-catalog, or None if academy_uuid is falsy.
    """
    if not academy_uuid:
        return None

    cache_key = versioned_cache_key(ACADEMY_CACHE_KEY_PREFIX, str(academy_uuid))
    cached = TieredCache.get_cached_response(cache_key)
    if cached.is_found:
        return cached.value

    data = EnterpriseCatalogApiClient().get_academy(academy_uuid)

    cache_timeout_value = timeout if timeout is not None else settings.ACADEMY_DATA_CACHE_TIMEOUT
    TieredCache.set_all_tiers(cache_key, data, django_cache_timeout=cache_timeout_value)
    return data


def get_cached_course_count(catalog_query_uuid, timeout=None):
    """
    Fetch and cache the course count for a CatalogQuery from enterprise-catalog.

    Arguments:
        catalog_query_uuid (str|UUID): The catalog query UUID to fetch.
        timeout (int, optional): Cache TTL in seconds. Defaults to settings.ACADEMY_DATA_CACHE_TIMEOUT.

    Returns:
        int: The course count for the catalog query, or None if catalog_query_uuid is falsy.
    """
    if not catalog_query_uuid:
        return None

    cache_key = versioned_cache_key(COURSE_COUNT_CACHE_KEY_PREFIX, str(catalog_query_uuid))
    cached = TieredCache.get_cached_response(cache_key)
    if cached.is_found:
        return cached.value

    data = EnterpriseCatalogApiClient().get_catalog_query(catalog_query_uuid)
    course_count = data.get('course_count')

    cache_timeout_value = timeout if timeout is not None else settings.ACADEMY_DATA_CACHE_TIMEOUT
    TieredCache.set_all_tiers(cache_key, course_count, django_cache_timeout=cache_timeout_value)
    return course_count
