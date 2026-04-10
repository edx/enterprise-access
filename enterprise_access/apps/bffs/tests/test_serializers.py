"""
Unit tests for BFF serializers in enterprise_access.apps.bffs.serializers
"""
from types import SimpleNamespace

import pytest
from rest_framework import serializers

from enterprise_access.apps.bffs import serializers as bffs_serializers
from enterprise_access.apps.bffs.api import secured_algolia_api_key_cache_key


# Example test for BaseBffSerializer
class DummySerializer(bffs_serializers.BaseBffSerializer):
    foo = serializers.CharField()


def test_base_bff_serializer_valid():
    data = {'foo': 'bar'}
    serializer = DummySerializer(data=data)
    assert serializer.is_valid()
    assert serializer.validated_data['foo'] == 'bar'


def test_base_bff_serializer_invalid():
    data = {}
    serializer = DummySerializer(data=data)
    assert not serializer.is_valid()
    assert 'foo' in serializer.errors


# Add more tests for each serializer as needed
@pytest.mark.parametrize("serializer_cls,fields,valid_data", [
    (
        bffs_serializers.ErrorSerializer,
        ['developer_message', 'user_message'],
        {'developer_message': 'error details', 'user_message': 'error'},
    ),
    (
        bffs_serializers.WarningSerializer,
        ['developer_message', 'user_message'],
        {'developer_message': 'warning details', 'user_message': 'warn'},
    ),
    (
        bffs_serializers.EnterpriseCustomerSiteSerializer,
        ['domain', 'name'],
        {'domain': 'example.com', 'name': 'Example Site'},
    ),
])
def test_serializer_fields_and_validation(serializer_cls, fields, valid_data):
    serializer = serializer_cls(data=valid_data)
    assert serializer.is_valid(), serializer.errors
    for field in fields:
        assert field in serializer.validated_data


# ---------------------------------------------------------------------------
# SubscriptionsSerializer — flag-gated field exclusion
# ---------------------------------------------------------------------------

_BASE_SUBSCRIPTIONS_DATA = {
    'customer_agreement': None,
    'subscription_licenses': [],
    'subscription_licenses_by_status': {},
    'subscription_license': None,
    'subscription_plan': None,
    'show_expiration_notifications': False,
}


def test_subscriptions_serializer_omits_licenses_by_catalog_when_not_in_source():
    """
    When the source dict does not include `licenses_by_catalog` (flag OFF),
    the serialized output must not contain the field.
    """
    serializer = bffs_serializers.SubscriptionsSerializer(_BASE_SUBSCRIPTIONS_DATA)
    output = serializer.data
    assert 'licenses_by_catalog' not in output


def test_subscriptions_serializer_omits_license_schema_version_always():
    """
    `license_schema_version` has been removed from the serializer entirely.
    It must never appear in the output regardless of source data.
    """
    serializer = bffs_serializers.SubscriptionsSerializer(_BASE_SUBSCRIPTIONS_DATA)
    assert 'license_schema_version' not in serializer.data

    with_extra = {**_BASE_SUBSCRIPTIONS_DATA, 'license_schema_version': 'v2'}
    serializer2 = bffs_serializers.SubscriptionsSerializer(with_extra)
    assert 'license_schema_version' not in serializer2.data


def test_subscriptions_serializer_includes_licenses_by_catalog_when_present():
    """
    When the handler includes `licenses_by_catalog` in the source dict (flag ON),
    it must appear in the serialized output.
    """
    source = {
        **_BASE_SUBSCRIPTIONS_DATA,
        'licenses_by_catalog': {'cat-a': []},
    }
    serializer = bffs_serializers.SubscriptionsSerializer(source)
    output = serializer.data
    assert 'licenses_by_catalog' in output
    assert 'cat-a' in output['licenses_by_catalog']


def test_subscriptions_serializer_handles_non_dict_instance():
    """Serializer supports object instances (non-dict) without dropping provided fields."""
    instance = SimpleNamespace(
        **_BASE_SUBSCRIPTIONS_DATA,
        licenses_by_catalog={'cat-a': []},
    )
    serializer = bffs_serializers.SubscriptionsSerializer(instance)
    output = serializer.data

    assert 'licenses_by_catalog' in output
    assert 'cat-a' in output['licenses_by_catalog']


# ---------------------------------------------------------------------------
# secured_algolia_api_key_cache_key
# ---------------------------------------------------------------------------

def test_cache_key_no_catalogs_is_stable_and_differs_from_scoped():
    """
    When catalog_uuids is None the key is stable and distinct from a catalog-scoped key.
    (versioned_cache_key hashes all parts, so the literal 'all' won't appear in output.)
    """
    key_unscoped = secured_algolia_api_key_cache_key('ent-uuid', 42)
    key_scoped = secured_algolia_api_key_cache_key('ent-uuid', 42, catalog_uuids=['cat-a'])
    assert key_unscoped == secured_algolia_api_key_cache_key('ent-uuid', 42)
    assert key_unscoped != key_scoped


def test_cache_key_with_large_catalogs_is_order_insensitive_and_scope_specific():
    """
    Large catalog UUID lists must still affect the cache key correctly:
    the same UUID set in a different order yields the same key, while a
    different UUID set yields a different key.
    """
    many_uuids = [f'cat-{i:04d}' for i in range(100)]
    reversed_uuids = list(reversed(many_uuids))
    different_uuids = many_uuids[:-1] + ['cat-9999']

    key1 = secured_algolia_api_key_cache_key('ent-uuid', 42, catalog_uuids=many_uuids)
    key2 = secured_algolia_api_key_cache_key('ent-uuid', 42, catalog_uuids=reversed_uuids)
    key3 = secured_algolia_api_key_cache_key('ent-uuid', 42, catalog_uuids=different_uuids)

    assert key1 == key2
    assert key1 != key3


def test_cache_key_with_catalogs_is_stable():
    """Same UUID set in different order must produce identical cache key."""
    uuids = ['cat-b', 'cat-a', 'cat-c']
    key1 = secured_algolia_api_key_cache_key('ent-uuid', 42, catalog_uuids=uuids)
    key2 = secured_algolia_api_key_cache_key('ent-uuid', 42, catalog_uuids=reversed(uuids))
    assert key1 == key2


def test_cache_key_different_catalogs_differ():
    """Different catalog UUID sets must produce distinct cache keys."""
    key_a = secured_algolia_api_key_cache_key('ent-uuid', 42, catalog_uuids=['cat-a'])
    key_b = secured_algolia_api_key_cache_key('ent-uuid', 42, catalog_uuids=['cat-b'])
    assert key_a != key_b
