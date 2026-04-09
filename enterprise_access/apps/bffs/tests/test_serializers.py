"""
Unit tests for BFF serializers in enterprise_access.apps.bffs.serializers
"""
import pytest
from rest_framework import serializers
from enterprise_access.apps.bffs import serializers as bffs_serializers

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
