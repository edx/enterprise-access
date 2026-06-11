"""Tests for academy sync helpers."""

import importlib
from unittest import mock
from uuid import uuid4

from django.test import SimpleTestCase, TestCase

from enterprise_access.apps.customer_billing.academy_sync import (
    _get_enterprise_academy_model,
    _normalize_catalog_academy,
    _normalize_catalog_query_uuid,
    sync_enterprise_academies_from_enterprise_catalog
)
from enterprise_access.apps.customer_billing.apps import CustomerBillingConfig


class FakeAcademy:
    """Simple in-memory academy row used by sync tests."""

    def __init__(self, **kwargs):
        self.name = kwargs.get('name', '')
        self.long_name = kwargs.get('long_name', '')
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

    def filter(self, **kwargs):
        """Filter fake records by supported lookup keys."""
        records = self.records
        name_iexact = kwargs.get('name__iexact')
        is_active = kwargs.get('is_active')

        if name_iexact is not None:
            expected = name_iexact.lower()
            records = [record for record in records if record.name.lower() == expected]

        if is_active is not None:
            records = [record for record in records if record.is_active == is_active]

        return FakeAcademyQuerySet(records)

    def bulk_create(self, objs, update_conflicts=False, unique_fields=None, update_fields=None):
        """Simulate bulk_create upsert behavior used by academy sync."""
        if not update_conflicts:
            self.records.extend(objs)
            return objs

        unique_fields = unique_fields or []
        update_fields = update_fields or []
        if unique_fields != ['name']:
            raise AssertionError('Tests expect unique_fields=[\'name\']')

        for obj in objs:
            existing = next((record for record in self.records if record.name == obj.name), None)
            if existing is None:
                self.records.append(obj)
                continue

            for field_name in update_fields:
                setattr(existing, field_name, getattr(obj, field_name))

        return objs


class TestCustomerBillingConfig(SimpleTestCase):
    """Verify app startup wiring behavior."""

    def test_ready_imports_signals(self):
        """Calling ready should import customer_billing signal handlers."""
        app_module = importlib.import_module('enterprise_access.apps.customer_billing')
        config = CustomerBillingConfig(CustomerBillingConfig.name, app_module)

        with mock.patch('builtins.__import__', wraps=__import__) as mock_import:
            config.ready()

        signal_import_calls = [
            call
            for call in mock_import.call_args_list
            if call.args and call.args[0] == 'enterprise_access.apps.customer_billing.signals'
        ]

        self.assertEqual(len(signal_import_calls), 1)


class FakeEnterpriseAcademyModel:
    """Model-like wrapper exposing objects manager for sync tests."""

    objects = FakeAcademyManager()

    def __init__(self, **kwargs):
        self.name = kwargs.get('name', '')
        self.long_name = kwargs.get('long_name', '')
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


class TestAcademySyncHelpers(TestCase):
    """Tests for academy sync helper functions."""

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.apps.get_model')
    def test_get_enterprise_academy_model(self, mock_get_model):
        model = object()
        mock_get_model.return_value = model

        resolved = _get_enterprise_academy_model()

        self.assertIs(resolved, model)
        mock_get_model.assert_called_once_with('customer_billing', 'EnterpriseAcademy')

    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.apps.get_model')
    def test_get_enterprise_academy_model_raises_runtime_error_for_lookup_error(self, mock_get_model):
        mock_get_model.side_effect = LookupError('not found')

        with self.assertRaises(RuntimeError) as exc_info:
            _get_enterprise_academy_model()

        self.assertIn('Unable to resolve customer_billing.EnterpriseAcademy', str(exc_info.exception))

    def test_normalize_catalog_query_uuid_accepts_valid_string(self):
        value_uuid = str(uuid4())
        self.assertEqual(_normalize_catalog_query_uuid(value_uuid), value_uuid)

    def test_normalize_catalog_query_uuid_accepts_uuid_object(self):
        """Test that UUID objects are converted to strings."""
        test_uuid = uuid4()
        result = _normalize_catalog_query_uuid(test_uuid)
        self.assertEqual(result, str(test_uuid))

    def test_normalize_catalog_query_uuid_rejects_empty_strings(self):
        """Test that empty and whitespace-only strings return None."""
        self.assertIsNone(_normalize_catalog_query_uuid(''))
        self.assertIsNone(_normalize_catalog_query_uuid('   '))
        self.assertIsNone(_normalize_catalog_query_uuid('\t\n'))

    def test_normalize_catalog_query_uuid_rejects_invalid_values(self):
        self.assertIsNone(_normalize_catalog_query_uuid('not-a-uuid'))
        self.assertIsNone(_normalize_catalog_query_uuid(123))
        self.assertIsNone(_normalize_catalog_query_uuid(None))


class TestNormalizeCatalogAcademy(TestCase):
    """Tests for strict payload normalization."""

    def _make_item(self, **overrides):
        """Build a baseline payload with optional per-test overrides."""
        item = {
            'name': 'AI Academy',
            'long_name': 'AI Academy Full',
            'description': 'Academy description',
            'marketing_url': 'https://example.com/academy',
            'thumbnail_url': 'https://example.com/image.png',
            'tags': ['ai'],
            'stripe_product_id': 'prod_123',
            'stripe_price_lookup_key': 'essentials_ai_academy_yearly',
            'catalog_query_uuid': '00000000-0000-0000-0000-000000000111',
            'product_key': 'ai-academy',
            'slug': 'ai-academy',
            'is_active': True,
            'display_order': 0,
        }
        item.update(overrides)
        return item

    def test_normalize_catalog_academy_returns_expected_fields(self):
        result = _normalize_catalog_academy(self._make_item())
        self.assertEqual(result['name'], 'AI Academy')
        self.assertEqual(result['product_key'], 'ai-academy')
        self.assertEqual(result['slug'], 'ai-academy')
        self.assertEqual(result['stripe_price_lookup_key'], 'essentials_ai_academy_yearly')
        self.assertEqual(result['catalog_query_uuid'], '00000000-0000-0000-0000-000000000111')

    def test_normalize_catalog_academy_returns_none_for_missing_required_fields(self):
        self.assertIsNone(_normalize_catalog_academy(self._make_item(name='')))
        self.assertIsNone(_normalize_catalog_academy(self._make_item(product_key='')))
        self.assertIsNone(_normalize_catalog_academy(self._make_item(slug='')))
        self.assertIsNone(_normalize_catalog_academy(self._make_item(stripe_price_lookup_key='')))

    def test_normalize_catalog_academy_returns_none_for_non_dict_input(self):
        """Test that non-dict payloads are rejected."""
        self.assertIsNone(_normalize_catalog_academy('not a dict'))
        self.assertIsNone(_normalize_catalog_academy(None))
        self.assertIsNone(_normalize_catalog_academy([]))

    def test_normalize_catalog_academy_defaults_optional_values(self):
        result = _normalize_catalog_academy(
            self._make_item(tags='invalid', long_name=None, description=None, display_order=None)
        )
        self.assertEqual(result['tags'], [])
        self.assertEqual(result['long_name'], '')
        self.assertEqual(result['description'], '')
        self.assertEqual(result['display_order'], 0)


class TestFakeAcademyManager(TestCase):
    """Sanity checks for the in-memory manager used by sync tests."""

    def test_bulk_create_without_update_conflicts_appends_objects(self):
        manager = FakeAcademyManager()
        created = manager.bulk_create([FakeAcademy(name='One')], update_conflicts=False)

        self.assertEqual(len(created), 1)
        self.assertEqual(len(manager.records), 1)
        self.assertEqual(manager.records[0].name, 'One')

    def test_bulk_create_raises_when_unique_fields_are_unexpected(self):
        manager = FakeAcademyManager()
        with self.assertRaises(AssertionError):
            manager.bulk_create(
                [FakeAcademy(name='One')],
                update_conflicts=True,
                unique_fields=['slug'],
                update_fields=['slug'],
            )


class TestSyncEnterpriseAcademies(TestCase):
    """Tests for syncing academy rows."""

    def setUp(self):
        super().setUp()
        FakeEnterpriseAcademyModel.objects = FakeAcademyManager()

    def _add_existing(self, **kwargs):
        record = FakeAcademy(**kwargs)
        FakeEnterpriseAcademyModel.objects.records.append(record)
        return record

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_raises_attribute_error_when_get_academies_not_callable(self, mock_client_cls, _mock_model):
        """Test that missing get_academies method raises AttributeError."""
        mock_client_cls.return_value.get_academies = 'not-callable'
        with self.assertRaises(AttributeError):
            sync_enterprise_academies_from_enterprise_catalog()

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_handles_non_list_payload(self, mock_client_cls, _mock_model):
        """Test that non-list payloads are treated as empty list."""
        mock_client_cls.return_value.get_academies.return_value = None
        result = sync_enterprise_academies_from_enterprise_catalog()
        self.assertEqual(result.created, 0)
        self.assertEqual(result.errors, 0)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_creates_new_rows(self, mock_client_cls, _mock_model):
        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'New Academy',
                'product_key': 'new-academy',
                'slug': 'new-academy',
                'stripe_price_lookup_key': 'new_lookup',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.created, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(len(FakeEnterpriseAcademyModel.objects.records), 1)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_updates_existing_row_case_insensitive(self, mock_client_cls, _mock_model):
        academy = self._add_existing(
            name='Data Academy',
            product_key='data-academy',
            slug='data-academy',
            stripe_price_lookup_key='data_lookup',
            catalog_query_uuid='00000000-0000-0000-0000-000000000101',
        )

        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'data academy',
                'product_key': 'data-academy',
                'slug': 'data-academy',
                'stripe_price_lookup_key': 'data_lookup',
                'catalog_query_uuid': '00000000-0000-0000-0000-000000000202',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.updated, 1)
        self.assertEqual(len(FakeEnterpriseAcademyModel.objects.records), 1)
        self.assertEqual(academy.catalog_query_uuid, '00000000-0000-0000-0000-000000000202')

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_counts_unchanged_when_payload_matches_existing(self, mock_client_cls, _mock_model):
        self._add_existing(
            name='Unchanged Academy',
            product_key='unchanged-academy',
            slug='unchanged-academy',
            stripe_price_lookup_key='unchanged_lookup',
            is_active=True,
            display_order=0,
        )

        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'Unchanged Academy',
                'product_key': 'unchanged-academy',
                'slug': 'unchanged-academy',
                'stripe_price_lookup_key': 'unchanged_lookup',
                'is_active': True,
                'display_order': 0,
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog()
        self.assertEqual(result.unchanged, 1)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_dry_run_counts_without_persisting(self, mock_client_cls, _mock_model):
        self._add_existing(
            name='Dry Run Academy',
            product_key='dry-run-academy',
            slug='dry-run-academy',
            stripe_price_lookup_key='dry_run_lookup',
            catalog_query_uuid='00000000-0000-0000-0000-000000000301',
        )

        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'Dry Run Academy',
                'product_key': 'dry-run-academy',
                'slug': 'dry-run-academy',
                'stripe_price_lookup_key': 'dry_run_lookup',
                'catalog_query_uuid': '00000000-0000-0000-0000-000000000302',
            },
            {
                'name': 'Would Create',
                'product_key': 'would-create',
                'slug': 'would-create',
                'stripe_price_lookup_key': 'would_create_lookup',
            },
        ]

        result = sync_enterprise_academies_from_enterprise_catalog(dry_run=True)

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.created, 1)
        self.assertEqual(len(FakeEnterpriseAcademyModel.objects.records), 1)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_preserves_existing_catalog_query_uuid_when_payload_omits_it(self, mock_client_cls, _mock_model):
        academy = self._add_existing(
            name='Preserve UUID Academy',
            product_key='preserve-uuid-academy',
            slug='preserve-uuid-academy',
            stripe_price_lookup_key='preserve_uuid_lookup',
            catalog_query_uuid='00000000-0000-0000-0000-000000000999',
            is_active=True,
        )

        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'Preserve UUID Academy',
                'product_key': 'preserve-uuid-academy',
                'slug': 'new-slug',
                'stripe_price_lookup_key': 'preserve_uuid_lookup',
                'is_active': True,
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.updated, 1)
        self.assertEqual(academy.catalog_query_uuid, '00000000-0000-0000-0000-000000000999')

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_deactivate_missing_marks_stale_records(self, mock_client_cls, _mock_model):
        self._add_existing(
            name='Active Academy',
            product_key='active-academy',
            slug='active-academy',
            stripe_price_lookup_key='active_lookup',
            is_active=True,
        )
        stale = self._add_existing(
            name='Stale Academy',
            product_key='stale-academy',
            slug='stale-academy',
            stripe_price_lookup_key='stale_lookup',
            is_active=True,
        )

        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'Active Academy',
                'product_key': 'active-academy',
                'slug': 'active-academy',
                'stripe_price_lookup_key': 'active_lookup',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog(deactivate_missing=True)

        self.assertEqual(result.deactivated, 1)
        self.assertFalse(stale.is_active)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_deactivate_missing_with_dry_run_does_not_persist(self, mock_client_cls, _mock_model):
        self._add_existing(
            name='Stay Active',
            product_key='stay-active',
            slug='stay-active',
            stripe_price_lookup_key='stay_active_lookup',
            is_active=True,
        )
        stale = self._add_existing(
            name='Stale Academy',
            product_key='stale-academy',
            slug='stale-academy',
            stripe_price_lookup_key='stale_lookup',
            is_active=True,
        )

        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'Stay Active',
                'product_key': 'stay-active',
                'slug': 'stay-active',
                'stripe_price_lookup_key': 'stay_active_lookup',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog(deactivate_missing=True, dry_run=True)

        self.assertEqual(result.deactivated, 1)
        self.assertTrue(stale.is_active)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_counts_skipped_for_invalid_payload_item(self, mock_client_cls, _mock_model):
        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'Missing Required Fields',
                'slug': 'missing-required-fields',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog()
        self.assertEqual(result.skipped, 1)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_counts_errors_when_normalization_raises(self, mock_client_cls, _mock_model):
        """Test that exceptions during normalization are caught and counted."""
        mock_client_cls.return_value.get_academies.return_value = [
            {'name': 'Bad Item', 'product_key': 'bad', 'slug': 'bad', 'stripe_price_lookup_key': 'bad_lookup'},
            {
                'name': 'Good Academy', 'product_key': 'good-academy',
                'slug': 'good-academy', 'stripe_price_lookup_key': 'good_lookup',
            },
        ]
        with mock.patch(
            'enterprise_access.apps.customer_billing.academy_sync._normalize_catalog_academy',
            side_effect=[
                RuntimeError('boom'),
                {
                    'name': 'Good Academy',
                    'product_key': 'good-academy',
                    'slug': 'good-academy',
                    'stripe_price_lookup_key': 'good_lookup',
                    'catalog_query_uuid': None,
                    'long_name': '', 'description': '', 'marketing_url': '', 'thumbnail_url': '',
                    'tags': [], 'stripe_product_id': '', 'is_active': True, 'display_order': 0,
                }
            ]
        ):
            result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.errors, 1)
        self.assertEqual(result.created, 1)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_counts_errors_when_bulk_upsert_raises(self, mock_client_cls, _mock_model):
        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'Broken Academy',
                'product_key': 'broken-academy',
                'slug': 'broken-academy',
                'stripe_price_lookup_key': 'broken_lookup',
            }
        ]

        with mock.patch.object(
            FakeEnterpriseAcademyModel.objects,
            'bulk_create',
            side_effect=RuntimeError('boom'),
        ):
            result = sync_enterprise_academies_from_enterprise_catalog()

        self.assertEqual(result.errors, 1)

    @mock.patch(
        'enterprise_access.apps.customer_billing.academy_sync._get_enterprise_academy_model',
        return_value=FakeEnterpriseAcademyModel,
    )
    @mock.patch('enterprise_access.apps.customer_billing.academy_sync.EnterpriseCatalogApiClient')
    def test_deactivate_missing_uses_db_name_casing_to_avoid_false_deactivation(self, mock_client_cls, _mock_model):
        self._add_existing(
            name='Data Academy',
            product_key='data-academy',
            slug='data-academy',
            stripe_price_lookup_key='data_lookup',
            is_active=True,
        )

        mock_client_cls.return_value.get_academies.return_value = [
            {
                'name': 'data academy',
                'product_key': 'data-academy',
                'slug': 'data-academy',
                'stripe_price_lookup_key': 'data_lookup_updated',
            }
        ]

        result = sync_enterprise_academies_from_enterprise_catalog(deactivate_missing=True)

        self.assertEqual(result.updated, 1)
        self.assertEqual(len(FakeEnterpriseAcademyModel.objects.records), 1)
        self.assertEqual(result.deactivated, 0)
