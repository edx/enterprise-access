"""Tests for academy sync helpers."""

from unittest import mock
from uuid import uuid4

from django.test import TestCase

from enterprise_access.apps.customer_billing.academy_sync import (
    _default_stripe_lookup_key,
    _extract_catalog_query_uuid,
    _extract_payload_list,
    _first_non_empty,
    _normalize_catalog_academy,
    _to_lookup_token,
    _to_slug,
    fetch_enterprise_catalog_academies,
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

    def test_returns_none_when_name_missing(self):
        self.assertIsNone(_normalize_catalog_academy({'title': '', 'name': ''}))

    def test_catalog_query_uuid_from_nested_catalog_queries(self):
        query_uuid = '00000000-0000-0000-0000-000000000062'
        result = _normalize_catalog_academy(
            self._make_item(
                catalog_queries=[{'id': query_uuid}],
                catalog_query_uuid=None,
            )
        )
        self.assertEqual(result['catalog_query_uuid'], query_uuid)


class TestAcademySyncHelpers(TestCase):
    """Tests for helper utilities used by academy sync."""

    def test_first_non_empty_prefers_rendered_dict_and_strips(self):
        value = _first_non_empty(None, {'rendered': '  hello  '}, 'fallback')
        self.assertEqual(value, 'hello')

    def test_to_slug_uses_fallback_when_slugify_empty(self):
        self.assertEqual(_to_slug('***', 'academy', 123), 'academy-123')

    def test_to_lookup_token_and_default_lookup_key(self):
        self.assertEqual(_to_lookup_token('AI & Data / Intro'), 'ai_and_data_intro')
        self.assertEqual(
            _default_stripe_lookup_key('AI Academy', ''),
            'essentials_ai_academy_academy_yearly',
        )

    def test_extract_payload_list_supports_wrappers(self):
        self.assertEqual(_extract_payload_list([{'a': 1}]), [{'a': 1}])
        self.assertEqual(_extract_payload_list({'results': [{'a': 2}]}), [{'a': 2}])
        self.assertEqual(_extract_payload_list({'items': [{'a': 3}]}), [{'a': 3}])
        self.assertEqual(_extract_payload_list('bad-payload'), [])

    def test_extract_catalog_query_uuid_handles_supported_types(self):
        value_uuid = str(uuid4())
        object_uuid = uuid4()
        self.assertEqual(_extract_catalog_query_uuid(value_uuid), value_uuid)
        self.assertEqual(_extract_catalog_query_uuid(object_uuid), str(object_uuid))

    def test_extract_catalog_query_uuid_ignores_invalid_and_legacy_int(self):
        value_uuid = str(uuid4())
        extracted = _extract_catalog_query_uuid(
            42,
            {'catalog_query_id': 99},
            {'catalog_queries': [{'uuid': value_uuid}]},
        )
        self.assertIsNone(extracted)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_fetch_enterprise_catalog_academies(self, mock_client_cls):
        mock_client_cls.return_value.get_academies.return_value = {'results': [{'name': 'A'}]}
        payload = fetch_enterprise_catalog_academies(academy_uuid='abc')
        self.assertEqual(payload, [{'name': 'A'}])
        mock_client_cls.return_value.get_academies.assert_called_once_with(academy_uuid='abc')


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

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    def test_dry_run_does_not_persist_creates_or_updates(self, mock_fetch):
        EnterpriseAcademy.objects.create(
            name='Dry Run Academy',
            slug='dry-run-academy',
            product_key='dry-run-academy',
            stripe_price_lookup_key='dry_run_lookup',
            catalog_query_uuid='00000000-0000-0000-0000-000000000301',
        )
        mock_fetch.return_value = [
            {
                'name': 'Dry Run Academy',
                'slug': 'dry-run-academy',
                'product_key': 'dry-run-academy',
                'catalog_query_uuid': '00000000-0000-0000-0000-000000000302',
                'stripe_price_lookup_key': 'dry_run_lookup',
            },
            {
                'name': 'Would Create',
                'slug': 'would-create',
                'product_key': 'would-create',
                'stripe_price_lookup_key': 'would_create_lookup',
            },
        ]

        result = sync_enterprise_academies_from_enterprise_catalog(dry_run=True)

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.created, 1)
        self.assertFalse(EnterpriseAcademy.objects.filter(name='Would Create').exists())
        existing = EnterpriseAcademy.objects.get(name='Dry Run Academy')
        self.assertEqual(existing.catalog_query_uuid, '00000000-0000-0000-0000-000000000301')

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    def test_deactivate_missing_marks_stale_records(self, mock_fetch):
        EnterpriseAcademy.objects.create(
            name='Active Academy',
            slug='active-academy',
            product_key='active-academy',
            stripe_price_lookup_key='active_lookup',
            is_active=True,
        )
        stale = EnterpriseAcademy.objects.create(
            name='Stale Academy',
            slug='stale-academy',
            product_key='stale-academy',
            stripe_price_lookup_key='stale_lookup',
            is_active=True,
        )
        mock_fetch.return_value = [
            {
                'name': 'Active Academy',
                'slug': 'active-academy',
                'product_key': 'active-academy',
                'stripe_price_lookup_key': 'active_lookup',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog(deactivate_missing=True)

        self.assertEqual(result.deactivated, 1)
        stale.refresh_from_db()
        self.assertFalse(stale.is_active)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseAcademy.objects.create')
    def test_sync_counts_errors_when_create_raises(self, mock_create, mock_fetch):
        mock_fetch.return_value = [
            {
                'name': 'Broken Academy',
                'slug': 'broken-academy',
                'product_key': 'broken-academy',
                'stripe_price_lookup_key': 'broken_lookup',
            }
        ]
        mock_create.side_effect = RuntimeError('boom')

        result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.errors, 1)
