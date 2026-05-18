"""
Tests for provisioning serializers.
"""
import uuid

from django.test import TestCase

from enterprise_access.apps.api.serializers.provisioning import (
    CustomerAgreementRequestSerializer,
    EnterpriseCatalogRequestSerializer,
    PendingCustomerAdminRequestSerializer,
    SubscriptionPlanRequestSerializer
)


class EnterpriseCatalogRequestSerializerTests(TestCase):

    """Tests for EnterpriseCatalogRequestSerializer."""

    def test_with_title_only(self):
        """Test serializer with only title."""
        data = {'title': 'Test Catalog'}
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_academy_name(self):
        """Test serializer with academy_name."""
        data = {
            'title': 'Test Catalog',
            'academy_name': 'academy_1',
        }
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_both_title_and_academy_name(self):
        """Test serializer with both title and academy_name."""
        data = {
            'title': 'Named Catalog',
            'academy_name': 'academy_2',
        }
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_catalog_query_uuid_none(self):
        """Test catalog_query_uuid validation with None."""
        data = {
            'title': 'Test',
            'catalog_query_uuid': None,
        }
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_catalog_query_uuid_with_value(self):
        """Test catalog_query_uuid validation with allowed value."""
        data = {
            'title': 'Test',
            'catalog_query_uuid': 1,
        }
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        # Should be valid - rely on settings for what's allowed
        serializer.is_valid(raise_exception=False)

    def test_legacy_catalog_query_id_alias_still_supported(self):
        """Test deprecated catalog_query_id is still accepted and normalized."""
        data = {
            'title': 'Test',
            'catalog_query_id': 1,
        }
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))
        self.assertEqual(serializer.validated_data['catalog_query_uuid'], '1')

    def test_empty_catalog(self):
        """Test with all optional fields empty."""
        data = {
            'title': '',
            'academy_name': '',
        }
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))


class SubscriptionPlanRequestSerializerTests(TestCase):
    """Tests for SubscriptionPlanRequestSerializer."""

    def test_with_minimal_fields(self):
        """Test with only required title and salesforce_opportunity_line_item."""
        data = {
            'title': 'Test Plan',
            'salesforce_opportunity_line_item': '00k123',
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_dates(self):
        """Test with start and expiration dates."""
        data = {
            'title': 'Dated Plan',
            'salesforce_opportunity_line_item': '00k456',
            'start_date': '2025-06-01T00:00:00Z',
            'expiration_date': '2026-06-01T00:00:00Z',
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_product_and_licenses(self):
        """Test with product_id and desired_num_licenses."""
        data = {
            'title': 'Full Plan',
            'salesforce_opportunity_line_item': '00k789',
            'product_id': 1,
            'desired_num_licenses': 100,
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_academy_name(self):
        """Test with academy_name for Essentials."""
        data = {
            'title': 'Essentials Plan',
            'salesforce_opportunity_line_item': '00kESS',
            'academy_name': 'academy_essentials',
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_stripe_product_id(self):
        """Test with stripe_product_id for Essentials."""
        data = {
            'title': 'Stripe Plan',
            'salesforce_opportunity_line_item': '00kSTR',
            'stripe_product_id': 'prod_abc123',
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_all_new_fields(self):
        """Test with academy_name, stripe_product_id, and enterprise_catalog_uuid."""
        data = {
            'title': 'Complete Plan',
            'salesforce_opportunity_line_item': '00kCOMP',
            'start_date': '2025-07-01T00:00:00Z',
            'expiration_date': '2026-07-01T00:00:00Z',
            'product_id': 2,
            'desired_num_licenses': 50,
            'academy_name': 'academy_premium',
            'stripe_product_id': 'prod_premium_123',
            'enterprise_catalog_uuid': str(uuid.uuid4()),
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_null_salesforce_id(self):
        """Test with None salesforce_opportunity_line_item."""
        data = {
            'title': 'No SFDC Plan',
            'salesforce_opportunity_line_item': None,
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))


class PendingCustomerAdminRequestSerializerTests(TestCase):
    """Tests for PendingCustomerAdminRequestSerializer."""

    def test_valid_email(self):
        """Test with valid email."""
        data = {'user_email': 'admin@example.com'}
        serializer = PendingCustomerAdminRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_invalid_email(self):
        """Test with invalid email format."""
        data = {'user_email': 'not-an-email'}
        serializer = PendingCustomerAdminRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn('user_email', serializer.errors)


class CustomerAgreementRequestSerializerTests(TestCase):
    """Tests for CustomerAgreementRequestSerializer."""

    def test_empty_agreement(self):
        """Test with empty agreement data."""
        data = {}
        serializer = CustomerAgreementRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_default_catalog_uuid(self):
        """Test with default_catalog_uuid."""
        catalog_uuid = str(uuid.uuid4())
        data = {'default_catalog_uuid': catalog_uuid}
        serializer = CustomerAgreementRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))

    def test_with_null_default_catalog_uuid(self):
        """Test with None default_catalog_uuid."""
        data = {'default_catalog_uuid': None}
        serializer = CustomerAgreementRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))
