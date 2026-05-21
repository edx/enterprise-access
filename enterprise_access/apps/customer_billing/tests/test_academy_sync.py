"""Tests for academy sync helpers."""

from unittest import mock
from uuid import uuid4

from django.test import TestCase

from enterprise_access.apps.customer_billing.academy_sync import (
    _default_stripe_lookup_key,
    _extract_catalog_query_uuid,
    _extract_payload_list,
    _first_non_empty,
    _get_enterprise_academy_model,
    _normalize_catalog_academy,
    _to_lookup_token,
    _to_slug,
    fetch_enterprise_catalog_academies,
    sync_enterprise_academies_from_enterprise_catalog
)


class FakeAcademy:
    """Simple in-memory academy row used by sync tests."""

    def __init__(self, **kwargs):
        self.name = kwargs.get('name', '')
        self.long_name = kwargs.get('long_name', self.name)
        self.description = kwargs.get('description', '')
        self.marketing_url = kwargs.get('marketing_url', '')
        self.thumbnail_url = kwargs.get('thumbnail_url', '')
        self.tags = kwargs.get('tags', [])
        self.stripe_product_id = kwargs.get('stripe_product_id', '')
        self.stripe_price_lookup_key = kwargs.get('stripe_price_lookup_key', '')
        self.catalog_query_uuid = kwargs.get('catalog_query_uuid', None)
        self.product_key = kwargs.get('product_key', '')
        self.slug = kwargs.get('slug', '')
        self.is_active = kwargs.get('is_active', True)
        self.display_order = kwargs.get('display_order', 0)

    def save(self):
        """No-op save used for update path compatibility."""


class FakeAcademyQuerySet:
    """Minimal queryset-like helper for filtering and updates."""

    def __init__(self, records):
        self._records = records

    def first(self):
        return self._records[0] if self._records else None

    def exclude(self, **kwargs):
        excluded_names = set(kwargs.get('name__in', []))
        return FakeAcademyQuerySet([record for record in self._records if record.name not in excluded_names])

    def count(self):
        return len(self._records)

    def update(self, **kwargs):
        for record in self._records:
            for key, value in kwargs.items():
                setattr(record, key, value)


class FakeAcademyManager:
    """In-memory manager implementing the subset used by academy_sync."""

    def __init__(self):
        self.records = []

    def create(self, **kwargs):
        """Create and store a fake academy record."""
        record = FakeAcademy(**kwargs)
        self.records.append(record)
        return record

    def filter(self, **kwargs):
        """Filter fake academy records using the subset of lookup keys used in tests."""
        records = self.records
        name_iexact = kwargs.get('name__iexact')
        is_active = kwargs.get('is_active')

        if name_iexact is not None:
            expected = name_iexact.lower()
            records = [record for record in records if record.name.lower() == expected]

        if is_active is not None:
            records = [record for record in records if record.is_active == is_active]

        return FakeAcademyQuerySet(records)


class FakeEnterpriseAcademyModel:
    """Model-like wrapper exposing objects manager for sync tests."""

    objects = FakeAcademyManager()


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

    def test_first_non_empty_falls_back_to_str_cast(self):
        self.assertEqual(_first_non_empty(None, 123), '123')

    def test_first_non_empty_returns_empty_when_nothing_usable(self):
        self.assertEqual(_first_non_empty(None, '', {'rendered': ''}), '')

    def test_to_slug_uses_fallback_when_slugify_empty(self):
        self.assertEqual(_to_slug('***', 'academy', 123), 'academy-123')

    def test_to_slug_returns_item_slug_when_prefix_is_not_slugifiable(self):
        self.assertEqual(_to_slug('***', '***', 'My Item'), 'my-item')

    def test_to_slug_returns_prefix_when_item_is_not_slugifiable(self):
        self.assertEqual(_to_slug('***', 'academy', '***'), 'academy')

    def test_to_slug_returns_item_literal_when_no_slug_parts_available(self):
        self.assertEqual(_to_slug('***', '***', '***'), 'item')

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
        self.assertEqual(_extract_payload_list({'academies': [{'a': 4}]}), [{'a': 4}])
        self.assertEqual(_extract_payload_list({'catalogs': [{'a': 5}]}), [{'a': 5}])
        self.assertEqual(_extract_payload_list({'other': []}), [])
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

    def test_extract_catalog_query_uuid_from_dict_id(self):
        value_uuid = str(uuid4())
        self.assertEqual(_extract_catalog_query_uuid({'id': value_uuid}), value_uuid)

    def test_extract_catalog_query_uuid_from_list_item(self):
        value_uuid = str(uuid4())
        self.assertEqual(_extract_catalog_query_uuid([{'uuid': value_uuid}]), value_uuid)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.apps.get_model')
    def test_get_enterprise_academy_model(self, mock_get_model):
        model = object()
        mock_get_model.return_value = model

        resolved = _get_enterprise_academy_model()

        self.assertIs(resolved, model)
        mock_get_model.assert_called_once_with('customer_billing', 'EnterpriseAcademy')

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_fetch_enterprise_catalog_academies(self, mock_client_cls):
        mock_client_cls.return_value.get_academies.return_value = {'results': [{'name': 'A'}]}
        payload = fetch_enterprise_catalog_academies(academy_uuid='abc')
        self.assertEqual(payload, [{'name': 'A'}])
        mock_client_cls.return_value.get_academies.assert_called_once_with(academy_uuid='abc')

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_fetch_enterprise_catalog_academies_raises_when_client_lacks_method(self, mock_client_cls):
        mock_client_cls.return_value = object()

        with self.assertRaises(AttributeError):
            fetch_enterprise_catalog_academies(academy_uuid='abc')


class TestSyncEnterpriseAcademies(TestCase):
    """Tests for syncing academy rows with catalog query UUID behavior."""

    def setUp(self):
        super().setUp()
        FakeEnterpriseAcademyModel.objects = FakeAcademyManager()

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_updates_existing_row_case_insensitive_name_match(self, _mock_model, mock_fetch):
        academy = FakeEnterpriseAcademyModel.objects.create(
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
        self.assertEqual(str(academy.catalog_query_uuid), '00000000-0000-0000-0000-000000000202')

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_dry_run_does_not_persist_creates_or_updates(self, _mock_model, mock_fetch):
        FakeEnterpriseAcademyModel.objects.create(
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
        created_names = {record.name for record in FakeEnterpriseAcademyModel.objects.records}
        self.assertNotIn('Would Create', created_names)
        existing = FakeEnterpriseAcademyModel.objects.filter(name__iexact='Dry Run Academy').first()
        self.assertEqual(existing.catalog_query_uuid, '00000000-0000-0000-0000-000000000301')

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_deactivate_missing_marks_stale_records(self, _mock_model, mock_fetch):
        FakeEnterpriseAcademyModel.objects.create(
            name='Active Academy',
            slug='active-academy',
            product_key='active-academy',
            stripe_price_lookup_key='active_lookup',
            is_active=True,
        )
        stale = FakeEnterpriseAcademyModel.objects.create(
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
        self.assertFalse(stale.is_active)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_sync_counts_errors_when_create_raises(self, _mock_model, mock_fetch):
        mock_fetch.return_value = [
            {
                'name': 'Broken Academy',
                'slug': 'broken-academy',
                'product_key': 'broken-academy',
                'stripe_price_lookup_key': 'broken_lookup',
            }
        ]

        with mock.patch.object(FakeEnterpriseAcademyModel.objects, 'create', side_effect=RuntimeError('boom')):
            result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.errors, 1)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_sync_counts_skipped_when_name_is_missing(self, _mock_model, mock_fetch):
        mock_fetch.return_value = [{'title': '', 'name': ''}]

        result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.skipped, 1)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_sync_counts_unchanged_when_payload_matches_existing(self, _mock_model, mock_fetch):
        FakeEnterpriseAcademyModel.objects.create(
            name='Unchanged Academy',
            slug='unchanged-academy',
            product_key='unchanged-academy',
            stripe_price_lookup_key='unchanged_lookup',
            is_active=True,
            display_order=0,
        )
        mock_fetch.return_value = [
            {
                'name': 'Unchanged Academy',
                'slug': 'unchanged-academy',
                'product_key': 'unchanged-academy',
                'stripe_price_lookup_key': 'unchanged_lookup',
                'is_active': True,
                'display_order': 0,
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.unchanged, 1)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_sync_deactivate_missing_ignored_when_no_seen_names(self, _mock_model, mock_fetch):
        FakeEnterpriseAcademyModel.objects.create(
            name='Existing Academy',
            slug='existing-academy',
            product_key='existing-academy',
            stripe_price_lookup_key='existing_lookup',
            is_active=True,
        )
        mock_fetch.return_value = []

        result = sync_enterprise_academies_from_enterprise_catalog(deactivate_missing=True)

        self.assertEqual(result.deactivated, 0)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_sync_preserves_existing_catalog_query_uuid_when_payload_omits_it(self, _mock_model, mock_fetch):
        """Verify that existing catalog_query_uuid is preserved when update payload doesn't include one."""
        original_uuid = '00000000-0000-0000-0000-000000000999'
        academy = FakeEnterpriseAcademyModel.objects.create(
            name='Preserve UUID Academy',
            slug='preserve-uuid-academy',
            product_key='preserve-uuid-academy',
            stripe_price_lookup_key='preserve_uuid_lookup',
            catalog_query_uuid=original_uuid,
            is_active=True,
        )

        mock_fetch.return_value = [
            {
                'name': 'Preserve UUID Academy',
                'slug': 'new-slug',  # Change to trigger update
                'product_key': 'preserve-uuid-academy',
                'stripe_price_lookup_key': 'preserve_uuid_lookup',
                'is_active': True,
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.updated, 1)
        self.assertEqual(academy.catalog_query_uuid, original_uuid)

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_sync_deactivate_missing_with_dry_run_does_not_persist(self, _mock_model, mock_fetch):
        """Verify deactivate_missing with dry_run=True counts but doesn't update."""
        FakeEnterpriseAcademyModel.objects.create(
            name='Stay Active',
            slug='stay-active',
            product_key='stay-active',
            stripe_price_lookup_key='stay_active_lookup',
            is_active=True,
        )
        stale_academy = FakeEnterpriseAcademyModel.objects.create(
            name='Stale Academy',
            slug='stale-academy',
            product_key='stale-academy',
            stripe_price_lookup_key='stale_lookup',
            is_active=True,
        )

        mock_fetch.return_value = [
            {
                'name': 'Stay Active',
                'slug': 'stay-active',
                'product_key': 'stay-active',
                'stripe_price_lookup_key': 'stay_active_lookup',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog(deactivate_missing=True, dry_run=True)

        self.assertEqual(result.deactivated, 1)
        self.assertTrue(stale_academy.is_active)  # Still active because dry_run=True

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.fetch_enterprise_catalog_academies')
    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    def test_sync_counts_errors_when_save_raises(self, _mock_model, mock_fetch):
        """Verify exception during save() is caught and counted."""
        existing_academy = FakeEnterpriseAcademyModel.objects.create(
            name='Broken Update',
            slug='broken-update',
            product_key='broken-update',
            stripe_price_lookup_key='broken_lookup',
        )

        mock_fetch.return_value = [
            {
                'name': 'Broken Update',
                'slug': 'new-slug',  # Change to trigger update path
                'product_key': 'broken-update',
                'stripe_price_lookup_key': 'broken_lookup',
            }
        ]

        with mock.patch.object(existing_academy, 'save', side_effect=RuntimeError('save failed')):
            result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.errors, 1)

    def test_first_non_empty_with_dict_rendered_empty_string(self):
        """Verify _first_non_empty skips dict with empty rendered field."""
        value = _first_non_empty(None, {'rendered': ''}, 'fallback')
        self.assertEqual(value, 'fallback')

    def test_first_non_empty_with_multiple_non_string_values(self):
        """Verify _first_non_empty converts multiple types in order, including 0."""
        value = _first_non_empty(None, {}, '', 'result')
        self.assertEqual(value, 'result')

        # 0 is converted to string '0', which is non-empty
        value2 = _first_non_empty(None, {}, 0)
        self.assertEqual(value2, '0')

    def test_to_slug_all_fallback_paths(self):
        """Verify _to_slug three-part fallback logic exhaustively."""
        # All parts empty -> 'item'
        self.assertEqual(_to_slug('***', '***', '***'), 'item')
        # Prefix + ID but main empty
        self.assertEqual(_to_slug('***', 'pre', 'id'), 'pre-id')
        # Only ID works
        self.assertEqual(_to_slug('***', '***', 'item-id'), 'item-id')
        # Only prefix works
        self.assertEqual(_to_slug('***', 'prefix', '***'), 'prefix')

    def test_extract_payload_list_unknown_dict_key(self):
        """Verify _extract_payload_list returns empty for unknown dict keys."""
        result = _extract_payload_list({'unknown_key': [1, 2, 3]})
        self.assertEqual(result, [])

    def test_extract_catalog_query_uuid_bool_and_invalid_string(self):
        """Verify _extract_catalog_query_uuid skips bool and invalid UUID strings."""
        result = _extract_catalog_query_uuid(True, False, 'not-a-uuid', 'also-not-valid')
        self.assertIsNone(result)
