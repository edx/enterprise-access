"""Tests for academy_api caching helpers."""
from unittest import mock
from uuid import uuid4

from django.test import TestCase
from requests.exceptions import ConnectionError as RequestsConnectionError

from enterprise_access.apps.customer_billing.academy_api import (
    COURSE_COUNT_FAILURE_CACHE_TIMEOUT,
    get_cached_academy_data,
    get_cached_course_count
)


class TestGetCachedAcademyData(TestCase):
    """Tests for get_cached_academy_data()."""

    def setUp(self):
        self.academy_uuid = uuid4()
        self.academy_data = {
            'uuid': str(self.academy_uuid),
            'title': 'AI Academy',
            'description': 'Learn AI skills',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'https://example.com/ai.png',
            'tags': ['ai', 'ml'],
        }

    @mock.patch('enterprise_access.apps.customer_billing.academy_api.TieredCache')
    @mock.patch('enterprise_access.apps.customer_billing.academy_api.EnterpriseCatalogApiClient')
    def test_cache_miss_fetches_and_caches(self, mock_client_class, mock_cache):
        mock_cache.get_cached_response.return_value.is_found = False
        mock_client_class.return_value.get_academy.return_value = self.academy_data

        result = get_cached_academy_data(self.academy_uuid)

        self.assertEqual(result, self.academy_data)
        mock_client_class.return_value.get_academy.assert_called_once_with(self.academy_uuid)
        mock_cache.set_all_tiers.assert_called_once()

    @mock.patch('enterprise_access.apps.customer_billing.academy_api.TieredCache')
    @mock.patch('enterprise_access.apps.customer_billing.academy_api.EnterpriseCatalogApiClient')
    def test_cache_hit_skips_fetch(self, mock_client_class, mock_cache):
        mock_cache.get_cached_response.return_value.is_found = True
        mock_cache.get_cached_response.return_value.value = self.academy_data

        result = get_cached_academy_data(self.academy_uuid)

        self.assertEqual(result, self.academy_data)
        mock_client_class.return_value.get_academy.assert_not_called()
        mock_cache.set_all_tiers.assert_not_called()

    def test_none_uuid_returns_none(self):
        result = get_cached_academy_data(None)
        self.assertIsNone(result)

    def test_empty_string_uuid_returns_none(self):
        result = get_cached_academy_data('')
        self.assertIsNone(result)


class TestGetCachedCourseCount(TestCase):
    """Tests for get_cached_course_count()."""

    def setUp(self):
        self.catalog_query_uuid = uuid4()

    @mock.patch('enterprise_access.apps.customer_billing.academy_api.TieredCache')
    @mock.patch('enterprise_access.apps.customer_billing.academy_api.EnterpriseCatalogApiClient')
    def test_cache_miss_fetches_and_caches(self, mock_client_class, mock_cache):
        mock_cache.get_cached_response.return_value.is_found = False
        mock_client_class.return_value.get_catalog_query_course_count.return_value = 16

        result = get_cached_course_count(self.catalog_query_uuid)

        self.assertEqual(result, 16)
        mock_client_class.return_value.get_catalog_query_course_count.assert_called_once_with(self.catalog_query_uuid)
        mock_cache.set_all_tiers.assert_called_once()

    @mock.patch('enterprise_access.apps.customer_billing.academy_api.TieredCache')
    @mock.patch('enterprise_access.apps.customer_billing.academy_api.EnterpriseCatalogApiClient')
    def test_cache_hit_skips_fetch(self, mock_client_class, mock_cache):
        mock_cache.get_cached_response.return_value.is_found = True
        mock_cache.get_cached_response.return_value.value = 16

        result = get_cached_course_count(self.catalog_query_uuid)

        self.assertEqual(result, 16)
        mock_client_class.return_value.get_catalog_query_course_count.assert_not_called()
        mock_cache.set_all_tiers.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.academy_api.TieredCache')
    @mock.patch('enterprise_access.apps.customer_billing.academy_api.EnterpriseCatalogApiClient')
    def test_request_failure_returns_none_and_caches_briefly(self, mock_client_class, mock_cache):
        """Catalog request failures return None and are cached with a short TTL."""
        mock_cache.get_cached_response.return_value.is_found = False
        mock_get_count = mock_client_class.return_value.get_catalog_query_course_count
        mock_get_count.side_effect = RequestsConnectionError('catalog unavailable')

        result = get_cached_course_count(self.catalog_query_uuid)

        self.assertIsNone(result)
        mock_cache.set_all_tiers.assert_called_once_with(
            mock.ANY, None, django_cache_timeout=COURSE_COUNT_FAILURE_CACHE_TIMEOUT,
        )

    @mock.patch('enterprise_access.apps.customer_billing.academy_api.TieredCache')
    @mock.patch('enterprise_access.apps.customer_billing.academy_api.EnterpriseCatalogApiClient')
    def test_unexpected_exception_propagates(self, mock_client_class, mock_cache):
        """Errors other than catalog request failures are not swallowed or cached."""
        mock_cache.get_cached_response.return_value.is_found = False
        mock_client_class.return_value.get_catalog_query_course_count.side_effect = ValueError('unexpected')

        with self.assertRaises(ValueError):
            get_cached_course_count(self.catalog_query_uuid)
        mock_cache.set_all_tiers.assert_not_called()

        mock_cache.set_all_tiers.assert_not_called()

    def test_none_uuid_returns_none(self):
        result = get_cached_course_count(None)
        self.assertIsNone(result)

    def test_empty_string_uuid_returns_none(self):
        result = get_cached_course_count('')
        self.assertIsNone(result)
