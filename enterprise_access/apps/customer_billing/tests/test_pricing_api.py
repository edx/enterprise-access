"""
Unit tests for the pricing_api module.
"""
import uuid
from decimal import Decimal
from unittest import mock

import ddt
from django.test import TestCase, override_settings
from edx_django_utils.cache import TieredCache
from stripe import InvalidRequestError

from enterprise_access.apps.customer_billing import pricing_api
from enterprise_access.apps.customer_billing.models import SspProduct

MOCK_SSP_PRODUCTS = {
    'quarterly_license_plan': {
        'stripe_price_id': 'price_test_quarterly',  # DEPRECATED: Use lookup_key instead
        'lookup_key': 'price_quarterly_0002',
        'quantity_range': [5, 50],
    },
    'yearly_license_plan': {
        'stripe_price_id': 'price_test_yearly',  # DEPRECATED: Use lookup_key instead
        'lookup_key': 'price_yearly_0001',
        'quantity_range': [5, 50],
    },
}


@override_settings(
    SSP_PRODUCTS=MOCK_SSP_PRODUCTS
)
@ddt.ddt
class TestStripePricingAPI(TestCase):
    """
    Tests for the Stripe pricing API functions.
    """

    def setUp(self):
        # Clear cache before each test
        TieredCache.dangerous_clear_all_tiers()
        SspProduct.objects.create(
            slug='quarterly_license_plan',
            stripe_price_lookup_key=MOCK_SSP_PRODUCTS['quarterly_license_plan']['lookup_key'],
            academy_uuid=None,
            catalog_query_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )
        SspProduct.objects.create(
            slug='yearly_license_plan',
            stripe_price_lookup_key=MOCK_SSP_PRODUCTS['yearly_license_plan']['lookup_key'],
            academy_uuid=None,
            catalog_query_uuid='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )

    def tearDown(self):
        # Clear cache after each test
        TieredCache.dangerous_clear_all_tiers()

    def _create_mock_stripe_price(
        self,
        price_id='price_123',
        unit_amount=10000,
        currency='usd',
        product_id='prod_123',
        product_name='Test Product',
        recurring=None,
        lookup_key=None,
    ):
        """Helper to create mock Stripe price object."""
        mock_product = mock.MagicMock()
        mock_product.id = product_id
        mock_product.name = product_name
        mock_product.description = 'Test product description'
        mock_product.metadata = {'test': 'value'}

        mock_price = mock.MagicMock()
        mock_price.id = price_id or MOCK_SSP_PRODUCTS['quarterly_license_plan']['stripe_price_id']
        mock_price.unit_amount = unit_amount
        mock_price.currency = currency
        mock_price.product = mock_product
        mock_price.recurring = recurring
        mock_price.lookup_key = lookup_key or MOCK_SSP_PRODUCTS['quarterly_license_plan']['lookup_key']
        mock_price.billing_scheme = 'per_unit'
        mock_price.type = 'recurring'

        return mock_price

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_stripe_price_data_basic_format(self, mock_stripe):
        """Test fetching price data in basic format."""
        price_id = MOCK_SSP_PRODUCTS['quarterly_license_plan']['stripe_price_id']
        lookup_key = MOCK_SSP_PRODUCTS['quarterly_license_plan']['lookup_key']
        mock_price = self._create_mock_stripe_price(price_id=price_id, lookup_key=lookup_key)
        mock_stripe.Price.retrieve.return_value = mock_price

        result = pricing_api.get_stripe_price_data(price_id)

        expected = {
            'id': price_id,
            'unit_amount_decimal': Decimal(100.0),
            'unit_amount': 10000,
            'currency': 'usd',
            'lookup_key': lookup_key,
            'product': {
                'id': 'prod_123',
                'name': 'Test Product',
                'description': 'Test product description',
                'metadata': {'test': 'value'},
            }
        }

        # Only assert expected keys to remain resilient to optional fields like `ssp_product_slug`
        for k, v in expected.items():
            self.assertEqual(result.get(k), v)
        mock_stripe.Price.retrieve.assert_called_once_with(price_id, expand=['product'])

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_stripe_price_data_with_recurring(self, mock_stripe):
        """Test fetching price data with recurring billing info."""
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = 'year'
        mock_recurring.interval_count = 1
        mock_recurring.usage_type = 'licensed'

        mock_price = self._create_mock_stripe_price(recurring=mock_recurring)
        mock_stripe.Price.retrieve.return_value = mock_price

        result = pricing_api.get_stripe_price_data('price_123')

        self.assertIn('recurring', result)
        self.assertEqual(result['recurring']['interval'], 'year')
        self.assertEqual(result['recurring']['interval_count'], 1)
        self.assertEqual(result['recurring']['usage_type'], 'licensed')

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_stripe_price_data_caching(self, mock_stripe):
        """Test that price data is properly cached."""
        mock_price = self._create_mock_stripe_price()
        mock_stripe.Price.retrieve.return_value = mock_price

        # First call should hit Stripe
        result1 = pricing_api.get_stripe_price_data('price_123')

        # Second call should hit cache
        result2 = pricing_api.get_stripe_price_data('price_123')

        self.assertEqual(result1, result2)
        # Stripe should only be called once
        mock_stripe.Price.retrieve.assert_called_once()

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price')
    def test_get_stripe_price_data_stripe_error(self, mock_stripe_price):
        """Test handling of Stripe API errors."""
        mock_stripe_price.retrieve.side_effect = InvalidRequestError(
            'No such price', 'price_123'
        )

        with self.assertRaises(pricing_api.StripePricingError):
            pricing_api.get_stripe_price_data('price_123')

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_ssp_product_pricing(self, mock_stripe):
        """Test fetching SSP product pricing."""
        # Ensure we exercise the settings-backed path for quantity_range
        SspProduct.objects.all().delete()

        SspProduct.objects.create(
            slug='quarterly_license_plan',
            stripe_price_lookup_key=MOCK_SSP_PRODUCTS['quarterly_license_plan']['lookup_key'],
            is_active=True,
            catalog_query_uuid=uuid.uuid4(),
        )
        SspProduct.objects.create(
            slug='yearly_license_plan',
            stripe_price_lookup_key=MOCK_SSP_PRODUCTS['yearly_license_plan']['lookup_key'],
            is_active=True,
            catalog_query_uuid=uuid.uuid4(),
        )

        quarterly_price = self._create_mock_stripe_price()
        yearly_price = self._create_mock_stripe_price(
            price_id=MOCK_SSP_PRODUCTS['yearly_license_plan']['stripe_price_id'],
            lookup_key=MOCK_SSP_PRODUCTS['yearly_license_plan']['lookup_key'],
        )
        mock_stripe.Price.list().auto_paging_iter.return_value = [quarterly_price, yearly_price]

        result = pricing_api.get_ssp_product_pricing()

        # Should have entries for configured SSP products (from settings)
        self.assertIn('quarterly_license_plan', result)
        self.assertIn('yearly_license_plan', result)

        # Check that SSP-specific metadata is added and quantity_range is sourced from settings
        quarterly_data = result['quarterly_license_plan']
        self.assertEqual(quarterly_data['ssp_product_key'], 'quarterly_license_plan')
        self.assertEqual(quarterly_data.get('quantity_range'), [5, 50])

    def test_calculate_subtotal_basic_format(self):
        """Test subtotal calculation with basic format."""
        price_data = {
            'unit_amount_decimal': Decimal(100.0),
            'unit_amount': 10000,
            'currency': 'usd',
            'recurring': {
                'interval': 'year',
                'interval_count': 1,
            }
        }

        result = pricing_api.calculate_subtotal(price_data, 5)

        expected = {
            'subtotal_cents': 50000,
            'subtotal_decimal': 500.0,
            'currency': 'usd',
            'quantity': 5,
            'unit_amount_cents': 10000,
            'unit_amount_decimal': 100.0,
            'billing_period': {
                'interval': 'year',
                'interval_count': 1,
            }
        }

        self.assertEqual(result, expected)

    def test_calculate_subtotal_non_usd_currency_data(self):
        """Test subtotal calculation with non-usd currency data."""
        price_data = {
            'unit_amount_decimal': Decimal(85.0),
            'unit_amount': 8500,
            'currency': 'eur',
        }

        result = pricing_api.calculate_subtotal(price_data, 3)

        expected = {
            'subtotal_cents': 8500 * 3,
            'subtotal_decimal': Decimal('255.00'),
            'currency': 'eur',
            'quantity': 3,
            'unit_amount_cents': 8500,
            'unit_amount_decimal': Decimal(85.0),
        }
        self.assertEqual(expected, result)

    def test_format_price_display_basic_format(self):
        """Test price display formatting with basic format."""
        price_data = {
            'unit_amount_decimal': Decimal(100.0),
            'unit_amount': 10000,
            'currency': 'usd',
            'recurring': {
                'interval': 'year',
                'interval_count': 1,
            }
        }

        result = pricing_api.format_price_display(price_data)
        self.assertEqual(result, '$100.00/year')

    def test_format_price_display_without_currency_symbol(self):
        """Test price display formatting without currency symbol."""
        price_data = {
            'unit_amount_decimal': Decimal(100.0),
            'unit_amount': 10000,
            'currency': 'usd',
        }

        result = pricing_api.format_price_display(price_data, include_currency_symbol=False)
        self.assertEqual(result, '100.00 USD')

    def test_format_price_display_multi_interval(self):
        """Test price display with multi-interval recurring."""
        price_data = {
            'unit_amount_decimal': Decimal(100.0),
            'unit_amount': 10000,
            'currency': 'usd',
            'recurring': {
                'interval': 'month',
                'interval_count': 3,
            }
        }

        result = pricing_api.format_price_display(price_data)
        self.assertEqual(result, '$100.00/every 3 months')

    def test_format_price_display_non_usd_currency(self):
        """Test price display with non-USD currency data."""
        price_data = {
            'unit_amount_decimal': Decimal(42.31),
            'unit_amount': 4231,
            'currency': 'eur',
        }

        result = pricing_api.format_price_display(price_data, currency='eur', include_currency_symbol=False)
        self.assertEqual(result, '42.31 EUR')

    def test_format_price_display_mismatched_currency(self):
        """Test price display with mismatched currency data."""
        price_data = {
            'unit_amount_decimal': Decimal(42.31),
            'unit_amount': 4231,
            'currency': 'eur',
        }

        # Price data in EUR, but we request USD
        result = pricing_api.format_price_display(price_data, currency='usd')
        self.assertEqual(result, 'Price unavailable')

    def test_serialize_basic_format_edge_cases(self):
        """Test serialization edge cases for basic format."""
        # Test with zero amount
        mock_price = self._create_mock_stripe_price(unit_amount=0)
        result = pricing_api._serialize_basic_format(mock_price)  # pylint: disable=protected-access

        self.assertEqual(result['unit_amount_decimal'], Decimal(0.0))
        self.assertEqual(result['unit_amount'], 0)

    def test_serialize_basic_format_no_product(self):
        """Test serialization when product is not expanded."""
        mock_price = self._create_mock_stripe_price()
        mock_price.product = None  # No expanded product data

        result = pricing_api._serialize_basic_format(mock_price)  # pylint: disable=protected-access

        self.assertNotIn('product', result)
        self.assertEqual(result['unit_amount_decimal'], Decimal(100.0))
        self.assertEqual(result['unit_amount'], 10000)

    def test_validate_stripe_price_schema_missing_field(self):
        """Test schema validation with missing required field."""
        mock_price = self._create_mock_stripe_price()
        # Remove required field
        del mock_price.currency

        with mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price') as mock_stripe_price:
            mock_stripe_price.retrieve.return_value = mock_price

            with self.assertRaises(pricing_api.StripePricingError) as cm:
                pricing_api.get_stripe_price_data('price_123')

            self.assertIn('Missing required field', str(cm.exception))

    def test_validate_stripe_price_schema_invalid_type(self):
        """Test schema validation with invalid field type."""
        mock_price = self._create_mock_stripe_price()
        # Set invalid type for unit_amount
        mock_price.unit_amount = "invalid"

        with mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price') as mock_stripe_price:
            mock_stripe_price.retrieve.return_value = mock_price

            with self.assertRaises(pricing_api.StripePricingError) as cm:
                pricing_api.get_stripe_price_data('price_123')

            self.assertIn('Invalid unit_amount type', str(cm.exception))

    def test_validate_stripe_price_schema_invalid_recurring(self):
        """Test schema validation with invalid recurring data."""
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = None  # Invalid - should be a string
        mock_recurring.interval_count = 1

        mock_price = self._create_mock_stripe_price(recurring=mock_recurring)

        with mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price') as mock_stripe_price:
            mock_stripe_price.retrieve.return_value = mock_price

            with self.assertRaises(pricing_api.StripePricingError) as cm:
                pricing_api.get_stripe_price_data('price_123')

            self.assertIn('Recurring price missing interval', str(cm.exception))

    @ddt.data(
        # All valid
        {
            "active": True,
            "billing_scheme": "per_unit",
            "type_": "recurring",
            "recurring_usage_type": "licensed",
            "expect_error": None,
        },
        # inactive price
        {
            "active": False,
            "billing_scheme": "per_unit",
            "type_": "recurring",
            "recurring_usage_type": "licensed",
            "expect_error": "Stripe price must be active",
        },
        # wrong billing_scheme
        {
            "active": True,
            "billing_scheme": "tiered",
            "type_": "recurring",
            "recurring_usage_type": "licensed",
            "expect_error": "Only per_unit billing_scheme is supported, got tiered",
        },
        # wrong type
        {
            "active": True,
            "billing_scheme": "per_unit",
            "type_": "one_time",
            "recurring_usage_type": "licensed",
            "expect_error": "Only recurring price type is supported, got one_time",
        },
        # wrong recurring.usage_type
        {
            "active": True,
            "billing_scheme": "per_unit",
            "type_": "recurring",
            "recurring_usage_type": "metered",
            "expect_error": "Only licensed recurring prices are supported, got metered",
        },
    )
    @ddt.unpack
    def test_validate_stripe_price_schema_variants(
        self,
        active,
        billing_scheme,
        type_,
        recurring_usage_type,
        expect_error,
    ):
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = "month"
        mock_recurring.interval_count = 1
        mock_recurring.usage_type = recurring_usage_type

        mock_price = self._create_mock_stripe_price()
        mock_price.active = active
        mock_price.billing_scheme = billing_scheme
        mock_price.type = type_
        mock_price.recurring = mock_recurring

        # pylint: disable=protected-access
        if expect_error is None:
            pricing_api._validate_stripe_price_schema(mock_price)
        else:
            with self.assertRaises(pricing_api.StripePricingError) as cm:
                pricing_api._validate_stripe_price_schema(mock_price)
            self.assertIn(expect_error, str(cm.exception))

    def test_serialize_basic_format_with_product_metadata_ssp_slug(self):
        """Product metadata with ssp_product_slug should be preferred."""
        mock_price = self._create_mock_stripe_price()
        mock_price.product.metadata = {'ssp_product_slug': 'meta-slug'}

        result = pricing_api._serialize_basic_format(mock_price)  # pylint: disable=protected-access

        self.assertIn('ssp_product_slug', result)
        self.assertEqual(result['ssp_product_slug'], 'meta-slug')

    def test_serialize_basic_format_model_fallback_for_ssp_slug(self):
        """When product metadata lacks ssp_product_slug, fallback to SspProduct lookup_key."""
        # Create a model-backed SSP product to be discovered by lookup_key
        SspProduct.objects.create(
            slug='fallback_slug',
            stripe_price_lookup_key='lookup_fallback',
            is_active=True,
            catalog_query_uuid=uuid.uuid4(),
        )

        mock_price = self._create_mock_stripe_price(lookup_key='lookup_fallback')
        mock_price.product.metadata = {}

        result = pricing_api._serialize_basic_format(mock_price)  # pylint: disable=protected-access

        self.assertIn('ssp_product_slug', result)
        self.assertEqual(result['ssp_product_slug'], 'fallback_slug')

    def test_get_ssp_product_pricing_raises_on_missing_lookup_key(self):
        """If an active SspProduct is missing lookup_key, raise StripePricingError."""
        # Add a product missing a lookup key
        SspProduct.objects.create(
            slug='bad_product',
            stripe_price_lookup_key='',
            is_active=True,
            catalog_query_uuid=uuid.uuid4(),
        )

        with mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_stripe_prices') as mock_all:
            mock_all.return_value = {}
            with self.assertRaises(pricing_api.StripePricingError):
                pricing_api.get_ssp_product_pricing()

    def test_get_ssp_product_pricing_raises_when_lookup_key_not_found(self):
        """If lookup_key for a product isn't present in Stripe prices, raise StripePricingError."""
        SspProduct.objects.create(
            slug='missing_lookup',
            stripe_price_lookup_key='no_such_lookup',
            is_active=True,
            catalog_query_uuid=uuid.uuid4(),
        )

        with mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_stripe_prices') as mock_all:
            mock_all.return_value = {}
            with self.assertRaises(pricing_api.StripePricingError):
                pricing_api.get_ssp_product_pricing()

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price')
    def test_get_stripe_price_data_non_stripe_exception(self, mock_stripe_price):
        """General (non-Stripe) exceptions should be wrapped in StripePricingError."""
        mock_stripe_price.retrieve.side_effect = RuntimeError('connection reset')

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api.get_stripe_price_data('price_999')

        self.assertIn('Unexpected error', str(cm.exception))

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_all_stripe_prices_basic(self, mock_stripe):
        """Directly test get_all_stripe_prices returns a lookup_key mapping."""
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = 'year'
        mock_recurring.interval_count = 1
        mock_recurring.usage_type = 'licensed'

        price = self._create_mock_stripe_price(
            price_id='price_all_1', lookup_key='lk_all_1', recurring=mock_recurring,
        )
        mock_stripe.Price.list.return_value.auto_paging_iter.return_value = [price]

        result = pricing_api.get_all_stripe_prices()

        self.assertIn('lk_all_1', result)
        self.assertEqual(result['lk_all_1']['unit_amount'], 10000)
        self.assertEqual(result['lk_all_1']['currency'], 'usd')

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_all_stripe_prices_caching(self, mock_stripe):
        """Second call to get_all_stripe_prices should return cached result."""
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = 'year'
        mock_recurring.interval_count = 1
        mock_recurring.usage_type = 'licensed'

        price = self._create_mock_stripe_price(
            price_id='price_cache', lookup_key='lk_cache', recurring=mock_recurring,
        )
        mock_stripe.Price.list.return_value.auto_paging_iter.return_value = [price]

        result1 = pricing_api.get_all_stripe_prices()

        # Reset the mock so we can verify it is NOT called again
        mock_stripe.Price.list.reset_mock()

        result2 = pricing_api.get_all_stripe_prices()

        self.assertEqual(result1, result2)
        # Stripe should not be called again; the cache should serve the result
        mock_stripe.Price.list.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_all_stripe_prices_skips_non_recurring(self, mock_stripe):
        """Non-recurring (one_time) prices should be skipped."""
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = 'year'
        mock_recurring.interval_count = 1
        mock_recurring.usage_type = 'licensed'

        recurring_price = self._create_mock_stripe_price(
            price_id='price_rec', lookup_key='lk_rec', recurring=mock_recurring,
        )

        one_time_price = self._create_mock_stripe_price(
            price_id='price_ot', lookup_key='lk_ot',
        )
        one_time_price.type = 'one_time'

        mock_stripe.Price.list.return_value.auto_paging_iter.return_value = [
            recurring_price, one_time_price,
        ]

        result = pricing_api.get_all_stripe_prices()

        self.assertIn('lk_rec', result)
        self.assertNotIn('lk_ot', result)

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_all_stripe_prices_skips_missing_lookup_key(self, mock_stripe):
        """Prices without a lookup_key should be skipped with a warning."""
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = 'year'
        mock_recurring.interval_count = 1
        mock_recurring.usage_type = 'licensed'

        price_with_key = self._create_mock_stripe_price(
            price_id='price_wk', lookup_key='lk_wk', recurring=mock_recurring,
        )

        price_no_key = self._create_mock_stripe_price(
            price_id='price_nk', recurring=mock_recurring,
        )
        price_no_key.lookup_key = None

        mock_stripe.Price.list.return_value.auto_paging_iter.return_value = [
            price_with_key, price_no_key,
        ]

        result = pricing_api.get_all_stripe_prices()

        self.assertIn('lk_wk', result)
        self.assertEqual(len(result), 1)

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price')
    def test_get_all_stripe_prices_stripe_error(self, mock_stripe_price):
        """StripeError during Price.list should raise StripePricingError."""
        mock_stripe_price.list.side_effect = InvalidRequestError('bad request', 'param')

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api.get_all_stripe_prices()

        self.assertIn('Failed to fetch all prices', str(cm.exception))

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price')
    def test_get_all_stripe_prices_general_exception(self, mock_stripe_price):
        """General exception during Price.list should raise StripePricingError."""
        mock_stripe_price.list.side_effect = RuntimeError('unexpected failure')

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api.get_all_stripe_prices()

        self.assertIn('Unexpected error', str(cm.exception))

    def test_calculate_subtotal_returns_none_on_error(self):
        """Missing keys in price_data should cause calculate_subtotal to return None."""
        # Empty dict triggers KeyError for 'unit_amount'
        result = pricing_api.calculate_subtotal({}, 5)
        self.assertIsNone(result)

    def test_format_price_display_returns_unavailable_on_exception(self):
        """Missing keys inside the try block should return 'Price unavailable'."""
        # currency matches so we enter the try block, but unit_amount_decimal is missing
        price_data = {'currency': 'usd'}

        result = pricing_api.format_price_display(price_data)
        self.assertEqual(result, 'Price unavailable')

    def test_validate_stripe_price_schema_invalid_currency_type(self):
        """Non-string currency should raise StripePricingError."""
        mock_price = self._create_mock_stripe_price()
        mock_price.currency = 12345  # not a string

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api._validate_stripe_price_schema(mock_price)  # pylint: disable=protected-access

        self.assertIn('Invalid currency type', str(cm.exception))

    def test_validate_stripe_price_schema_missing_interval_count(self):
        """Recurring price with missing interval_count should raise StripePricingError."""
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = 'month'
        mock_recurring.interval_count = None  # missing
        mock_recurring.usage_type = 'licensed'

        mock_price = self._create_mock_stripe_price(recurring=mock_recurring)

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api._validate_stripe_price_schema(mock_price)  # pylint: disable=protected-access

        self.assertIn('Recurring price missing interval_count', str(cm.exception))

    def test_get_ssp_product_pricing_raises_on_missing_lookup_key_slug_format(self):
        """Ensure missing lookup_key exception correctly formats using the product slug."""
        SspProduct.objects.all().delete()
        SspProduct.objects.create(
            slug='bad_product_slug_test',
            stripe_price_lookup_key='',
            is_active=True,
            catalog_query_uuid=uuid.uuid4(),
        )

        with mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_stripe_prices') as mock_all:
            mock_all.return_value = {}
            with self.assertRaises(pricing_api.StripePricingError) as cm:
                pricing_api.get_ssp_product_pricing()

            self.assertIn('SSP product bad_product_slug_test missing lookup_key', str(cm.exception))

    def test_serialize_basic_format_metadata_exception_handled(self):
        """Ensure exceptions raised during metadata access fallback safely to ssp_slug = None."""
        mock_price = self._create_mock_stripe_price()
        mock_metadata = mock.MagicMock()
        mock_metadata.get.side_effect = RuntimeError("Metadata completely broken")
        mock_price.product.metadata = mock_metadata

        # pylint: disable=protected-access
        result = pricing_api._serialize_basic_format(mock_price)
        self.assertEqual(result.get('ssp_product_slug'), 'quarterly_license_plan')

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_ssp_product_pricing_skips_invalid_settings_config(self, mock_stripe):
        """Ensure settings blocks missing lookup_key or quantity_range are skipped gracefully."""
        SspProduct.objects.all().delete()

        # Create a valid active DB product pointing to the complete settings block
        SspProduct.objects.create(
            slug='complete_plan',
            stripe_price_lookup_key='valid_lk_range',
            is_active=True,
            catalog_query_uuid=uuid.uuid4(),
        )

        mock_price = self._create_mock_stripe_price(lookup_key='valid_lk_range')
        mock_stripe.Price.list().auto_paging_iter.return_value = [mock_price]

        result = pricing_api.get_ssp_product_pricing()

        # The complete plan should process properly
        self.assertIn('complete_plan', result)
        self.assertEqual(result['complete_plan'].get('quantity_range'), [5, 50])

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_ssp_product_pricing_ignores_inactive_ssp_products(self, mock_stripe):
        """Ensure that SspProduct database filtering strictly targets is_active=True objects."""
        SspProduct.objects.all().delete()

        # Create an inactive product that matches valid settings
        SspProduct.objects.create(
            slug='quarterly_license_plan',
            stripe_price_lookup_key=MOCK_SSP_PRODUCTS['quarterly_license_plan']['lookup_key'],
            is_active=False,
            catalog_query_uuid=uuid.uuid4(),
        )

        mock_price = self._create_mock_stripe_price()
        mock_stripe.Price.list().auto_paging_iter.return_value = [mock_price]

        result = pricing_api.get_ssp_product_pricing()

        self.assertEqual(len(result), 0)
