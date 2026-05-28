"""Tests for academy_api caching helpers."""
from unittest import mock
from uuid import uuid4

from django.test import TestCase

from enterprise_access.apps.customer_billing.academy_api import get_cached_academy_data


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
