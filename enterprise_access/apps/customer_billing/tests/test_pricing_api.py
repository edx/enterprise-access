"""
Unit tests for the pricing_api module.
"""
from decimal import Decimal
from unittest import mock

import ddt
from django.test import TestCase, override_settings
from edx_django_utils.cache import TieredCache
from stripe import InvalidRequestError

from enterprise_access.apps.customer_billing import pricing_api

MOCK_SSP_PRODUCTS = {
    'quarterly_license_plan': {
        'stripe_price_id': 'price_test_quarterly',  # DEPRECATED: Use lookup_key instead
        'lookup_key': 'price_quarterly_0002',
        'quantity_range': (5, 30),
    },
    'yearly_license_plan': {
        'stripe_price_id': 'price_test_yearly',  # DEPRECATED: Use lookup_key instead
        'lookup_key': 'price_yearly_0001',
        'quantity_range': (5, 30),
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

        self.assertEqual(result, expected)
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
        quarterly_price = self._create_mock_stripe_price()
        yearly_price = self._create_mock_stripe_price(
            price_id=MOCK_SSP_PRODUCTS['yearly_license_plan']['stripe_price_id'],
            lookup_key=MOCK_SSP_PRODUCTS['yearly_license_plan']['lookup_key'],
        )
        mock_stripe.Price.list().auto_paging_iter.return_value = [quarterly_price, yearly_price]

        result = pricing_api.get_ssp_product_pricing()

        # Should have entries for configured SSP products
        self.assertIn('quarterly_license_plan', result)
        self.assertIn('yearly_license_plan', result)

        # Check that SSP-specific metadata is added
        quarterly_data = result['quarterly_license_plan']
        self.assertEqual(quarterly_data['ssp_product_key'], 'quarterly_license_plan')
        self.assertEqual(quarterly_data['quantity_range'], (5, 30))

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

    def test_validate_stripe_price_schema_invalid_currency_type(self):
        """Test schema validation with invalid currency type."""
        mock_price = self._create_mock_stripe_price()
        mock_price.currency = 123

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api._validate_stripe_price_schema(mock_price)  # pylint: disable=protected-access

        self.assertIn('Invalid currency type', str(cm.exception))

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

    def test_validate_stripe_price_schema_missing_interval_count(self):
        """Test schema validation with missing recurring interval_count."""
        mock_recurring = mock.MagicMock()
        mock_recurring.interval = 'month'
        mock_recurring.interval_count = None
        mock_recurring.usage_type = 'licensed'

        mock_price = self._create_mock_stripe_price(recurring=mock_recurring)

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api._validate_stripe_price_schema(mock_price)  # pylint: disable=protected-access

        self.assertIn('Recurring price missing interval_count', str(cm.exception))

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

    def test_calculate_subtotal_missing_keys(self):
        """Test subtotal calculation when price_data lacks required keys."""
        incomplete_price_data = {
            'currency': 'usd',
            # missing unit_amount_decimal and unit_amount
        }

        result = pricing_api.calculate_subtotal(incomplete_price_data, 5)
        self.assertIsNone(result)

    def test_calculate_subtotal_invalid_quantity(self):
        """Test subtotal calculation with invalid quantity type."""
        price_data = {
            'unit_amount_decimal': Decimal(100.0),
            'unit_amount': 10000,
            'currency': 'usd',
        }

        # Invalid quantity type should cause exception to be caught
        result = pricing_api.calculate_subtotal(price_data, "invalid")
        self.assertIsNone(result)

    def test_format_price_display_invalid_data(self):
        """Test price display with invalid price data."""
        invalid_price_data = {
            'currency': 'usd',
            # missing unit_amount_decimal
        }

        result = pricing_api.format_price_display(invalid_price_data)
        self.assertEqual(result, 'Price unavailable')

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price.list')
    def test_get_all_active_stripe_prices_stripe_error(self, mock_price_list):
        """Test handling of Stripe errors in get_all_active_stripe_prices."""
        mock_price_list.side_effect = InvalidRequestError('API Error', 'request')

        with self.assertRaises(pricing_api.StripePricingError):
            pricing_api.get_all_active_stripe_prices()

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe')
    def test_get_all_active_stripe_prices_filters_one_time(self, mock_stripe):
        """Test that one_time prices are filtered out."""
        recurring_price = self._create_mock_stripe_price()
        one_time_price = self._create_mock_stripe_price()
        one_time_price.type = 'one_time'  # Should be filtered

        mock_stripe.Price.list().auto_paging_iter.return_value = [recurring_price, one_time_price]

        result = pricing_api.get_all_active_stripe_prices()

        # Should only include recurring prices
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], recurring_price.id)

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_stripe_prices')
    @override_settings(SSP_PRODUCTS={
        'test_product': {
            'lookup_key': 'nonexistent_key',
            'quantity_range': (5, 30),
        }
    })
    def test_get_ssp_product_pricing_missing_lookup_key(self, mock_get_all_stripe_prices):
        """Test error when SSP product lookup_key is not found in Stripe prices."""
        mock_get_all_stripe_prices.return_value = {}

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api.get_ssp_product_pricing()

        self.assertIn('not found in active Stripe prices', str(cm.exception))

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_stripe_prices')
    @override_settings(SSP_PRODUCTS={
        'test_product': {
            # missing lookup_key
            'quantity_range': (5, 30),
        }
    })
    def test_get_ssp_product_pricing_missing_config_lookup_key(self, mock_get_all_stripe_prices):
        """Test error when SSP product config lacks lookup_key."""
        mock_get_all_stripe_prices.return_value = {}

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api.get_ssp_product_pricing()

        self.assertIn('missing lookup_key', str(cm.exception))

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_active_stripe_prices')
    def test_get_all_stripe_prices_by_lookup_key(self, mock_get_all_active_stripe_prices):
        """Test fetching all Stripe prices and mapping by lookup_key."""
        mock_get_all_active_stripe_prices.return_value = [
            pricing_api._serialize_basic_format(self._create_mock_stripe_price()),  # pylint: disable=protected-access
        ]

        result = pricing_api.get_all_stripe_prices()

        # Should be mapped by lookup_key
        self.assertIn(MOCK_SSP_PRODUCTS['quarterly_license_plan']['lookup_key'], result)

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_active_stripe_prices')
    def test_get_all_stripe_prices_skips_missing_lookup_key(self, mock_get_all_active_stripe_prices):
        """Test that prices without lookup_key are skipped."""
        price_with_key = pricing_api._serialize_basic_format(  # pylint: disable=protected-access
            self._create_mock_stripe_price()
        )
        price_without_key = pricing_api._serialize_basic_format(  # pylint: disable=protected-access
            self._create_mock_stripe_price()
        )
        price_without_key['lookup_key'] = None

        mock_get_all_active_stripe_prices.return_value = [price_with_key, price_without_key]

        result = pricing_api.get_all_stripe_prices()

        # Should only include prices with lookup_key
        self.assertEqual(len(result), 1)

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_active_stripe_prices')
    def test_get_all_stripe_prices_stripe_error(self, mock_get_all_active_stripe_prices):
        """Test handling of Stripe errors in get_all_stripe_prices."""
        mock_get_all_active_stripe_prices.side_effect = InvalidRequestError('API Error', 'request')

        with self.assertRaises(pricing_api.StripePricingError):
            pricing_api.get_all_stripe_prices()

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price.retrieve')
    def test_get_stripe_price_data_general_exception(self, mock_price_retrieve):
        """Test handling of general exceptions in get_stripe_price_data."""
        mock_price_retrieve.side_effect = Exception('Unexpected error')

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api.get_stripe_price_data('price_123')

        self.assertIn('Unexpected error', str(cm.exception))

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.stripe.Price.list')
    def test_get_all_active_stripe_prices_general_exception(self, mock_price_list):
        """Test handling of general exceptions in get_all_active_stripe_prices."""
        mock_price_list.side_effect = Exception('Unexpected error')

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api.get_all_active_stripe_prices()

        self.assertIn('Unexpected error', str(cm.exception))

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.get_all_active_stripe_prices')
    def test_get_all_stripe_prices_general_exception(self, mock_get_all_active_stripe_prices):
        """Test handling of general exceptions in get_all_stripe_prices."""
        mock_get_all_active_stripe_prices.side_effect = Exception('Unexpected error')

        with self.assertRaises(pricing_api.StripePricingError) as cm:
            pricing_api.get_all_stripe_prices()

        self.assertIn('Unexpected error', str(cm.exception))

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.TieredCache.get_cached_response')
    def test_get_all_stripe_prices_cache_hit(self, mock_get_cached_response):
        """Test get_all_stripe_prices returns cached value without recomputing."""
        cached_value = {'price_quarterly_0002': {'id': 'price_123'}}
        mock_get_cached_response.return_value = mock.Mock(is_found=True, value=cached_value)

        result = pricing_api.get_all_stripe_prices()

        self.assertEqual(result, cached_value)

    @mock.patch('enterprise_access.apps.customer_billing.pricing_api.TieredCache.get_cached_response')
    def test_get_all_active_stripe_prices_cache_hit(self, mock_get_cached_response):
        """Test get_all_active_stripe_prices returns cached value without Stripe calls."""
        cached_value = [{'id': 'price_123'}]
        mock_get_cached_response.return_value = mock.Mock(is_found=True, value=cached_value)

        result = pricing_api.get_all_active_stripe_prices()

        self.assertEqual(result, cached_value)
