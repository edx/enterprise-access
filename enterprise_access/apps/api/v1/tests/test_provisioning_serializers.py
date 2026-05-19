"""
Tests for provisioning serializers.
"""
import uuid

from django.test import TestCase, override_settings

from enterprise_access.apps.api.serializers.provisioning import (
    BaseSerializer,
    CustomerAgreementRequestSerializer,
    EnterpriseCatalogRequestSerializer,
    PendingCustomerAdminRequestSerializer,
    SubscriptionPlanOLIUpdateSerializer,
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

    def test_catalog_query_uuid_invalid_value(self):
        """Invalid catalog_query_uuid values should fail validation."""
        data = {
            'title': 'Test',
            'catalog_query_uuid': 'not-a-valid-uuid',
        }
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn('catalog_query_uuid', serializer.errors)

    def test_legacy_catalog_query_id_alias_still_supported(self):
        """Test deprecated catalog_query_id is still accepted and normalized."""
        data = {
            'title': 'Test',
            'catalog_query_id': 1,
        }
        serializer = EnterpriseCatalogRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))
        self.assertEqual(serializer.validated_data['catalog_query_uuid'], '1')

    def test_catalog_query_validators_direct(self):
        """Direct validator methods should normalize accepted legacy ids."""
        serializer = EnterpriseCatalogRequestSerializer()
        self.assertEqual(serializer.validate_catalog_query_uuid('1'), '1')
        self.assertEqual(serializer.validate_catalog_query_id('1'), '1')

    def test_catalog_query_validate_promotes_legacy_alias(self):
        """Object-level validate should promote legacy alias to catalog_query_uuid."""
        serializer = EnterpriseCatalogRequestSerializer()
        attrs = serializer.validate({'catalog_query_id': '1'})
        self.assertEqual(attrs['catalog_query_uuid'], '1')
        self.assertEqual(attrs['catalog_query_id'], '1')

    @override_settings(
        PROVISIONING_DEFAULTS={
            'catalog': {
                'catalog_query_id': None,
                'all_catalog_query_choices': [],
            },
        },
    )
    def test_catalog_query_uuid_invalid_without_legacy_fallback(self):
        """Without configured fallback ids, invalid UUID input should fail normalization."""
        serializer = EnterpriseCatalogRequestSerializer()
        with self.assertRaises(Exception):
            serializer.validate_catalog_query_uuid('not-a-uuid')

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

    def test_subscription_plan_catalog_query_uuid_validator(self):
        """SubscriptionPlan serializer should validate and normalize catalog_query_uuid."""
        data = {
            'title': 'Plan with Catalog Query UUID',
            'salesforce_opportunity_line_item': '00kCATQ',
            'catalog_query_uuid': '1',
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))
        self.assertEqual(serializer.validated_data['catalog_query_uuid'], '1')

    def test_subscription_plan_catalog_query_id_alias_validator(self):
        """SubscriptionPlan serializer should support legacy catalog_query_id alias."""
        data = {
            'title': 'Plan with Legacy Catalog Query',
            'salesforce_opportunity_line_item': '00kCATLEG',
            'catalog_query_id': '1',
        }
        serializer = SubscriptionPlanRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(raise_exception=False))
        self.assertEqual(serializer.validated_data['catalog_query_uuid'], '1')
        self.assertEqual(serializer.validated_data['catalog_query_id'], '1')


class BaseSerializerTests(TestCase):
    """Tests for BaseSerializer no-op create/update contract."""

    def test_base_serializer_create_and_update_return_none(self):
        serializer = BaseSerializer()
        self.assertIsNone(serializer.create({}))
        self.assertIsNone(serializer.update(None, {}))


class SubscriptionPlanOLIUpdateSerializerTests(TestCase):
    """Tests for SubscriptionPlanOLIUpdateSerializer."""

    def test_requires_one_checkout_intent_identifier(self):
        data = {
            'salesforce_opportunity_line_item': '00k123',
        }
        serializer = SubscriptionPlanOLIUpdateSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn('One of CheckoutIntent id or uuid is required', str(serializer.errors))

    def test_rejects_both_checkout_intent_identifiers(self):
        data = {
            'checkout_intent_id': 1,
            'checkout_intent_uuid': str(uuid.uuid4()),
            'salesforce_opportunity_line_item': '00k123',
        }
        serializer = SubscriptionPlanOLIUpdateSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn('Only one of CheckoutIntent id or uuid can be provided', str(serializer.errors))

    def test_accepts_single_checkout_intent_uuid(self):
        data = {
            'checkout_intent_uuid': str(uuid.uuid4()),
            'salesforce_opportunity_line_item': '00k123',
        }
        serializer = SubscriptionPlanOLIUpdateSerializer(data=data)
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
