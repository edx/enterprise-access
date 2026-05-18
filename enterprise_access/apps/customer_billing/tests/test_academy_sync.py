"""Tests for academy sync helpers."""

from unittest import mock

from django.test import TestCase

from enterprise_access.apps.customer_billing.academy_sync import (
    _normalize_catalog_academy,
    sync_enterprise_academies_from_enterprise_catalog
)
from enterprise_access.apps.customer_billing.models import EnterpriseAcademy


class TestNormalizeCatalogAcademy(TestCase):
    """Tests for catalog query UUID extraction during normalization."""

    def _make_item(self, **overrides):
        """Build a baseline academy payload and allow field overrides for tests."""
        item = {
            'name': 'AI Academy',
            'title': 'AI Academy',
            'product_key': 'ai-academy',
            'slug': 'ai-academy',
            'stripe_product_id': 'prod_ai',
            'stripe_price_lookup_key': 'essentials_ai_academy_yearly',
            'catalog_query_uuid': None,
            'metadata': {},
            'tags': [],
            'is_active': True,
            'display_order': 0,
        }
        item.update(overrides)
        return item

    def test_catalog_query_uuid_from_top_level(self):
        query_uuid = '00000000-0000-0000-0000-000000000042'
        result = _normalize_catalog_academy(self._make_item(catalog_query_uuid=query_uuid))
        self.assertEqual(result['catalog_query_uuid'], query_uuid)

    def test_catalog_query_uuid_from_metadata(self):
        query_uuid = '00000000-0000-0000-0000-000000000052'
        result = _normalize_catalog_academy(
            self._make_item(metadata={'catalog_query_uuid': query_uuid}, catalog_query_uuid=None)
        )
        self.assertEqual(result['catalog_query_uuid'], query_uuid)

    def test_ignores_legacy_integer_catalog_query_values(self):
        result = _normalize_catalog_academy(self._make_item(catalog_query_id=42))
        self.assertIsNone(result['catalog_query_uuid'])


class TestSyncEnterpriseAcademies(TestCase):
    """Tests for syncing academy rows with catalog query UUID behavior."""

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    def test_updates_existing_row_case_insensitive_name_match(self, mock_fetch):
        academy = EnterpriseAcademy.objects.create(
            name='Data Academy',
            slug='data-academy',
            product_key='data-academy',
            stripe_price_lookup_key='data_lookup',
            catalog_query_uuid='00000000-0000-0000-0000-000000000101',
        )

        mock_fetch.return_value = [
            {
                'name': 'data academy',
                'slug': 'data-academy',
                'product_key': 'data-academy',
                'catalog_queries': [{'uuid': '00000000-0000-0000-0000-000000000202'}],
                'stripe_price_lookup_key': 'data_lookup',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.updated, 1)
        academy.refresh_from_db()
        self.assertEqual(str(academy.catalog_query_uuid), '00000000-0000-0000-0000-000000000202')
