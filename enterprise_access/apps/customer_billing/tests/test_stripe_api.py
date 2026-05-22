"""
Unit tests for interacting with stripe via ``stripe_api.api``.
"""
from unittest import mock

import stripe
from django.test import TestCase
from edx_django_utils.cache import TieredCache

from enterprise_access.apps.customer_billing.stripe_api import (
    create_subscription_checkout_session,
    get_academy_stripe_prices,
    get_academy_stripe_product_by_key,
    get_academy_stripe_products,
    get_stripe_checkout_session,
    get_stripe_invoice,
    get_stripe_payment_intent,
    get_stripe_payment_method,
    get_stripe_trialing_subscription,
    stripe_cache
)


class StripeApiFunctionsTests(TestCase):
    """Tests for Stripe API functions with caching."""

    def setUp(self):
        """Set up test case."""
        # Clear cache before each test
        TieredCache.dangerous_clear_all_tiers()

        # Sample test data
        self.session_id = "cs_test_123456789"
        self.payment_intent_id = "pi_test_123456789"
        self.invoice_id = "in_test_123456789"
        self.payment_method_id = "pm_test_123456789"

        # Sample response objects
        self.session_response = {"id": self.session_id, "object": "checkout.session"}
        self.payment_intent_response = {"id": self.payment_intent_id, "object": "payment_intent"}
        self.invoice_response = {"id": self.invoice_id, "object": "invoice"}
        self.payment_method_response = {"id": self.payment_method_id, "object": "payment_method"}


class TestStripeCheckoutSession(StripeApiFunctionsTests):
    """Tests for get_stripe_checkout_session function."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.retrieve')
    def test_get_stripe_checkout_session_success(self, mock_retrieve):
        """Test successful retrieval of checkout session."""
        mock_retrieve.return_value = self.session_response

        # First call should hit the API
        result = get_stripe_checkout_session(self.session_id)

        mock_retrieve.assert_called_once_with(self.session_id)
        self.assertEqual(result, self.session_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.retrieve')
    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_get_stripe_checkout_session_cache_hit(self, mock_set, mock_get, mock_retrieve):
        """Test cache hit for checkout session."""
        # Setup cache hit
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = True
        mock_cached_response.value = self.session_response
        mock_get.return_value = mock_cached_response

        # Call function
        result = get_stripe_checkout_session(self.session_id)

        # Verify behavior
        mock_get.assert_called_once()
        mock_retrieve.assert_not_called()
        mock_set.assert_not_called()
        self.assertEqual(result, self.session_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.retrieve')
    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_get_stripe_checkout_session_cache_miss(self, mock_set, mock_get, mock_retrieve):
        """Test cache miss for checkout session."""
        # Setup cache miss
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = False
        mock_get.return_value = mock_cached_response

        # Setup API response
        mock_retrieve.return_value = self.session_response

        # Call function
        result = get_stripe_checkout_session(self.session_id)

        # Verify behavior
        mock_get.assert_called_once()
        mock_retrieve.assert_called_once_with(self.session_id)
        mock_set.assert_called_once()
        self.assertEqual(result, self.session_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.retrieve')
    def test_get_stripe_checkout_session_api_error(self, mock_retrieve):
        """Test API error handling for checkout session."""
        # Setup API error
        mock_retrieve.side_effect = stripe.StripeError("API Error")

        # Call function and verify exception is raised
        with self.assertRaises(stripe.StripeError):
            get_stripe_checkout_session(self.session_id)


class TestCreateSubscriptionCheckoutSession(StripeApiFunctionsTests):
    """Tests for create_subscription_checkout_session customer vs customer_email selection."""

    def _base_input(self, admin_email='admin@example.com'):
        # Minimal inputs used by create_subscription_checkout_session
        return {
            'admin_email': admin_email,
            'company_name': 'Acme Co',
            'enterprise_slug': 'acme',
            'stripe_price_id': 'price_123',
            'quantity': 3,
        }

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.create')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Customer.search')
    def test_sets_customer_email_when_no_existing_customer(self, mock_customer_search, mock_session_create):
        """When no Stripe customer exists for admin_email, pass customer_email and not customer."""
        mock_customer_search.return_value = mock.MagicMock(data=[])
        mock_stripe_session = mock.Mock()
        mock_stripe_session.to_dict.return_value = {'id': 'cs_test_abc'}
        mock_session_create.return_value = mock_stripe_session

        input_data = self._base_input(admin_email='new-admin@example.com')
        checkout_intent = mock.MagicMock()
        checkout_intent.id = 'chk_123'

        create_subscription_checkout_session(input_data, lms_user_id=1, checkout_intent=checkout_intent)

        # Inspect kwargs passed to Session.create
        _, kwargs = mock_session_create.call_args
        self.assertEqual(kwargs.get('customer_email'), 'new-admin@example.com')
        self.assertNotIn('customer', kwargs)
        self.assertEqual(kwargs.get('ui_mode'), 'elements')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.create')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Customer.search')
    def test_sets_customer_when_existing_customer_found(self, mock_customer_search, mock_session_create):
        """When a Stripe customer exists for admin_email, pass customer and not customer_email."""
        mock_customer_search.return_value = mock.MagicMock(data=[{'id': 'cus_12345'}])
        mock_stripe_session = mock.Mock()
        mock_stripe_session.to_dict.return_value = {'id': 'cs_test_def'}
        mock_session_create.return_value = mock_stripe_session

        input_data = self._base_input(admin_email='existing-admin@example.com')
        checkout_intent = mock.MagicMock()
        checkout_intent.id = 'chk_456'

        create_subscription_checkout_session(input_data, lms_user_id=2, checkout_intent=checkout_intent)

        # Inspect kwargs passed to Session.create
        _, kwargs = mock_session_create.call_args
        self.assertEqual(kwargs.get('customer'), 'cus_12345')
        self.assertNotIn('customer_email', kwargs)
        self.assertEqual(kwargs.get('ui_mode'), 'elements')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.create')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Customer.search')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_includes_product_metadata_when_checkout_intent_has_stripe_product_id(
        self,
        mock_product_retrieve,
        mock_customer_search,
        mock_session_create,
    ):
        """When a checkout intent has a Stripe product, enrich subscription metadata from it."""
        mock_customer_search.return_value = mock.MagicMock(data=[])
        mock_product_retrieve.return_value = mock.MagicMock(
            metadata={'name': 'AI Academy', 'product_type': 'essential_academy'}
        )
        mock_stripe_session = mock.Mock()
        mock_stripe_session.to_dict.return_value = {'id': 'cs_test_metadata'}
        mock_session_create.return_value = mock_stripe_session

        input_data = self._base_input(admin_email='metadata-admin@example.com')
        checkout_intent = mock.MagicMock()
        checkout_intent.id = 'chk_metadata'
        checkout_intent.uuid = 'uuid_chk_metadata'
        checkout_intent.stripe_product_id = 'prod_ai_123'

        create_subscription_checkout_session(input_data, lms_user_id=99, checkout_intent=checkout_intent)

        _, kwargs = mock_session_create.call_args
        metadata = kwargs['subscription_data']['metadata']
        self.assertEqual(metadata['name'], 'AI Academy')
        self.assertEqual(metadata['product_type'], 'essential_academy')
        mock_product_retrieve.assert_called_once_with('prod_ai_123')


class TestAcademyStripeHelpers(TestCase):
    """Tests for academy Stripe product helpers."""

    def setUp(self):
        TieredCache.dangerous_clear_all_tiers()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_get_academy_stripe_products(self, mock_search):
        mock_product = mock.MagicMock()
        mock_product.id = 'prod_ai'
        mock_search.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([mock_product]))
        )

        result = get_academy_stripe_products()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 'prod_ai')
        query = mock_search.call_args.kwargs['query']
        self.assertIn("edx_product_type", query)
        self.assertIn("essential_academy", query)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_get_academy_stripe_product_by_key(self, mock_search):
        mock_product = mock.MagicMock()
        mock_product.id = 'prod_ai'
        mock_search.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([mock_product]))
        )

        result = get_academy_stripe_product_by_key('essentials_ai')

        self.assertEqual(result.id, 'prod_ai')
        query = mock_search.call_args.kwargs['query']
        self.assertIn("product_key", query)
        self.assertIn("essentials_ai", query)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_academy_stripe_products')
    def test_get_academy_stripe_prices(self, mock_get_products, mock_price_list):
        mock_product = mock.MagicMock()
        mock_product.id = 'prod_ai'
        mock_get_products.return_value = [mock_product]

        mock_price = mock.MagicMock()
        mock_price.id = 'price_ai_year'
        mock_price_list.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([mock_price]))
        )

        result = get_academy_stripe_prices()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 'price_ai_year')
        mock_price_list.assert_called_once_with(product='prod_ai', active=True)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_academy_stripe_products')
    def test_get_academy_stripe_prices_returns_empty_when_no_products(self, mock_get_products):
        mock_get_products.return_value = []

        self.assertEqual(get_academy_stripe_prices(), [])


class TestStripeTrialingSubscription(StripeApiFunctionsTests):
    """Tests for get_stripe_trialing_subscription."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Subscription.list')
    def test_returns_first_subscription_when_found(self, mock_list):
        mock_list.return_value = mock.MagicMock(data=[{'id': 'sub_trial'}])

        result = get_stripe_trialing_subscription('cus_123')

        self.assertEqual(result, {'id': 'sub_trial'})
        mock_list.assert_called_once_with(customer='cus_123', status='trialing', limit=1)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Subscription.list')
    def test_returns_none_when_no_matching_subscription(self, mock_list):
        mock_list.return_value = mock.MagicMock(data=[])

        result = get_stripe_trialing_subscription('cus_123', status='active')

        self.assertIsNone(result)
        mock_list.assert_called_once_with(customer='cus_123', status='active', limit=1)


class TestStripePaymentIntent(StripeApiFunctionsTests):
    """Tests for get_stripe_payment_intent function."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.PaymentIntent.retrieve')
    def test_get_stripe_payment_intent_success(self, mock_retrieve):
        """Test successful retrieval of payment intent."""
        mock_retrieve.return_value = self.payment_intent_response

        # First call should hit the API
        result = get_stripe_payment_intent(self.payment_intent_id)

        mock_retrieve.assert_called_once_with(self.payment_intent_id)
        self.assertEqual(result, self.payment_intent_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.PaymentIntent.retrieve')
    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_get_stripe_payment_intent_cache_hit(self, mock_set, mock_get, mock_retrieve):
        """Test cache hit for payment intent."""
        # Setup cache hit
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = True
        mock_cached_response.value = self.payment_intent_response
        mock_get.return_value = mock_cached_response

        # Call function
        result = get_stripe_payment_intent(self.payment_intent_id)

        # Verify behavior
        mock_get.assert_called_once()
        mock_retrieve.assert_not_called()
        mock_set.assert_not_called()
        self.assertEqual(result, self.payment_intent_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.PaymentIntent.retrieve')
    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_get_stripe_payment_intent_cache_miss(self, mock_set, mock_get, mock_retrieve):
        """Test cache miss for payment intent."""
        # Setup cache miss
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = False
        mock_get.return_value = mock_cached_response

        # Setup API response
        mock_retrieve.return_value = self.payment_intent_response

        # Call function
        result = get_stripe_payment_intent(self.payment_intent_id)

        # Verify behavior
        mock_get.assert_called_once()
        mock_retrieve.assert_called_once_with(self.payment_intent_id)
        mock_set.assert_called_once()
        self.assertEqual(result, self.payment_intent_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.PaymentIntent.retrieve')
    def test_get_stripe_payment_intent_api_error(self, mock_retrieve):
        """Test API error handling for payment intent."""
        # Setup API error
        mock_retrieve.side_effect = stripe.StripeError("API Error")

        # Call function and verify exception is raised
        with self.assertRaises(stripe.StripeError):
            get_stripe_payment_intent(self.payment_intent_id)


class TestStripeInvoice(StripeApiFunctionsTests):
    """Tests for get_stripe_invoice function."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Invoice.retrieve')
    def test_get_stripe_invoice_success(self, mock_retrieve):
        """Test successful retrieval of invoice."""
        mock_retrieve.return_value = self.invoice_response

        # First call should hit the API
        result = get_stripe_invoice(self.invoice_id)

        mock_retrieve.assert_called_once_with(self.invoice_id)
        self.assertEqual(result, self.invoice_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Invoice.retrieve')
    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_get_stripe_invoice_cache_hit(self, mock_set, mock_get, mock_retrieve):
        """Test cache hit for invoice."""
        # Setup cache hit
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = True
        mock_cached_response.value = self.invoice_response
        mock_get.return_value = mock_cached_response

        # Call function
        result = get_stripe_invoice(self.invoice_id)

        # Verify behavior
        mock_get.assert_called_once()
        mock_retrieve.assert_not_called()
        mock_set.assert_not_called()
        self.assertEqual(result, self.invoice_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Invoice.retrieve')
    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_get_stripe_invoice_cache_miss(self, mock_set, mock_get, mock_retrieve):
        """Test cache miss for invoice."""
        # Setup cache miss
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = False
        mock_get.return_value = mock_cached_response

        # Setup API response
        mock_retrieve.return_value = self.invoice_response

        # Call function
        result = get_stripe_invoice(self.invoice_id)

        # Verify behavior
        mock_get.assert_called_once()
        mock_retrieve.assert_called_once_with(self.invoice_id)
        mock_set.assert_called_once()
        self.assertEqual(result, self.invoice_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Invoice.retrieve')
    def test_get_stripe_invoice_api_error(self, mock_retrieve):
        """Test API error handling for invoice."""
        # Setup API error
        mock_retrieve.side_effect = stripe.StripeError("API Error")

        # Call function and verify exception is raised
        with self.assertRaises(stripe.StripeError):
            get_stripe_invoice(self.invoice_id)


class TestStripePaymentMethod(StripeApiFunctionsTests):
    """Tests for get_stripe_payment_method function."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.PaymentMethod.retrieve')
    def test_get_stripe_payment_method_success(self, mock_retrieve):
        """Test successful retrieval of payment method."""
        mock_retrieve.return_value = self.payment_method_response

        # First call should hit the API
        result = get_stripe_payment_method(self.payment_method_id)

        mock_retrieve.assert_called_once_with(self.payment_method_id)
        self.assertEqual(result, self.payment_method_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.PaymentMethod.retrieve')
    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_get_stripe_payment_method_cache_hit(self, mock_set, mock_get, mock_retrieve):
        """Test cache hit for payment method."""
        # Setup cache hit
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = True
        mock_cached_response.value = self.payment_method_response
        mock_get.return_value = mock_cached_response

        # Call function
        result = get_stripe_payment_method(self.payment_method_id)

        # Verify behavior
        mock_get.assert_called_once()
        mock_retrieve.assert_not_called()
        mock_set.assert_not_called()
        self.assertEqual(result, self.payment_method_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.PaymentMethod.retrieve')
    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_get_stripe_payment_method_cache_miss(self, mock_set, mock_get, mock_retrieve):
        """Test cache miss for payment method."""
        # Setup cache miss
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = False
        mock_get.return_value = mock_cached_response

        # Setup API response
        mock_retrieve.return_value = self.payment_method_response

        # Call function
        result = get_stripe_payment_method(self.payment_method_id)

        # Verify behavior
        mock_get.assert_called_once()
        mock_retrieve.assert_called_once_with(self.payment_method_id)
        mock_set.assert_called_once()
        self.assertEqual(result, self.payment_method_response)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.PaymentMethod.retrieve')
    def test_get_stripe_payment_method_api_error(self, mock_retrieve):
        """Test API error handling for payment method."""
        # Setup API error
        mock_retrieve.side_effect = stripe.StripeError("API Error")

        # Call function and verify exception is raised
        with self.assertRaises(stripe.StripeError):
            get_stripe_payment_method(self.payment_method_id)


class TestStripeCacheDecorator(TestCase):
    """Tests for the stripe_cache decorator itself."""

    def setUp(self):
        """Set up test case."""
        TieredCache.dangerous_clear_all_tiers()

    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response', autospec=True)
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers', autospec=True)
    def test_stripe_cache_decorator_different_keys(self, mock_set, mock_get):
        """Test that different resource IDs create different cache keys."""
        # Setup cache miss for all calls
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = False
        mock_get.return_value = mock_cached_response

        # Mock the stripe API call
        with mock.patch('stripe.checkout.Session.retrieve') as mock_retrieve:
            mock_retrieve.return_value = {"id": "test1"}

            # Call with first ID
            get_stripe_checkout_session("test1")

            # Call with second ID
            get_stripe_checkout_session("test2")

        # Check that we got two different cache keys
        self.assertEqual(mock_get.call_count, 2)
        self.assertNotEqual(
            mock_get.call_args_list[0][0][0],  # First call's cache key
            mock_get.call_args_list[1][0][0],  # Second call's cache key
        )
        mock_set.assert_has_calls([
            mock.call('stripe_get_stripe_checkout_session_test1', {'id': 'test1'}, django_cache_timeout=60),
            mock.call('stripe_get_stripe_checkout_session_test2', {'id': 'test1'}, django_cache_timeout=60),
        ])

    @mock.patch('edx_django_utils.cache.TieredCache.get_cached_response')
    @mock.patch('edx_django_utils.cache.TieredCache.set_all_tiers')
    def test_stripe_cache_decorator_custom_timeout(self, mock_set, mock_get):
        """Test that the timeout parameter is passed correctly."""
        # Setup cache miss
        mock_cached_response = mock.MagicMock()
        mock_cached_response.is_found = False
        mock_get.return_value = mock_cached_response

        # Define a test function with custom timeout
        @stripe_cache(timeout=120)
        def test_function(resource_id):
            return {"id": resource_id}

        # Call the function
        test_function("test_id")

        # Check that set_all_tiers was called with the correct timeout
        mock_set.assert_called_once()

        # Third argument to set_all_tiers should be the timeout
        call_kwargs = mock_set.call_args[1]
        self.assertEqual(call_kwargs, {'django_cache_timeout': 120})
