"""
Unit tests for interacting with stripe via ``stripe_api.api``.
"""
from unittest import mock

import stripe
from django.test import TestCase
from edx_django_utils.cache import TieredCache

from enterprise_access.apps.customer_billing.constants import (
    STRIPE_PRODUCT_KEY_METADATA_KEY,
    STRIPE_PRODUCT_TYPE_ESSENTIAL_ACADEMY,
    STRIPE_PRODUCT_TYPE_METADATA_KEY
)
from enterprise_access.apps.customer_billing.stripe_api import (
    _get_subscription_product_metadata,
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
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_sets_customer_email_when_no_existing_customer(
        self,
        mock_product_retrieve,
        mock_customer_search,
        mock_session_create,
    ):
        """When no Stripe customer exists for admin_email, pass customer_email and not customer."""
        mock_product_retrieve.return_value = {'metadata': {}}
        mock_customer_search.return_value = mock.MagicMock(data=[])
        mock_stripe_session = mock.Mock()
        mock_stripe_session.to_dict.return_value = {'id': 'cs_test_abc'}
        mock_session_create.return_value = mock_stripe_session

        input_data = self._base_input(admin_email='new-admin@example.com')
        checkout_intent = mock.MagicMock()
        checkout_intent.id = 'chk_123'
        checkout_intent.uuid = 'uuid_chk_123'
        checkout_intent.stripe_product_id = None

        create_subscription_checkout_session(input_data, lms_user_id=1, checkout_intent=checkout_intent)

        # Inspect kwargs passed to Session.create
        _, kwargs = mock_session_create.call_args
        self.assertEqual(kwargs.get('customer_email'), 'new-admin@example.com')
        self.assertNotIn('customer', kwargs)
        self.assertEqual(kwargs.get('ui_mode'), 'elements')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.create')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Customer.search')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_sets_customer_when_existing_customer_found(
        self,
        mock_product_retrieve,
        mock_customer_search,
        mock_session_create,
    ):
        """When a Stripe customer exists for admin_email, pass customer and not customer_email."""
        mock_product_retrieve.return_value = {'metadata': {}}
        mock_customer_search.return_value = mock.MagicMock(data=[{'id': 'cus_12345'}])
        mock_stripe_session = mock.Mock()
        mock_stripe_session.to_dict.return_value = {'id': 'cs_test_def'}
        mock_session_create.return_value = mock_stripe_session

        input_data = self._base_input(admin_email='existing-admin@example.com')
        checkout_intent = mock.MagicMock()
        checkout_intent.id = 'chk_456'
        checkout_intent.uuid = 'uuid_chk_456'
        checkout_intent.stripe_product_id = None

        create_subscription_checkout_session(input_data, lms_user_id=2, checkout_intent=checkout_intent)

        # Inspect kwargs passed to Session.create
        _, kwargs = mock_session_create.call_args
        self.assertEqual(kwargs.get('customer'), 'cus_12345')
        self.assertNotIn('customer_email', kwargs)
        self.assertEqual(kwargs.get('ui_mode'), 'elements')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.create')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Customer.search')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_includes_name_and_product_type_in_subscription_metadata(
        self,
        mock_product_retrieve,
        mock_customer_search,
        mock_session_create,
    ):
        """Layer 6: include academy name/product_type in subscription metadata when product metadata has them."""
        mock_customer_search.return_value = mock.MagicMock(data=[{'id': 'cus_12345'}])
        mock_product_retrieve.return_value = {
            'id': 'prod_academy_123',
            'metadata': {'name': 'Data Science', 'product_type': 'essentials'},
        }
        mock_session_create.return_value = {'id': 'cs_test_layer6'}

        input_data = self._base_input(admin_email='existing-admin@example.com')
        checkout_intent = mock.MagicMock()
        checkout_intent.id = 'chk_789'
        checkout_intent.uuid = 'uuid_chk_789'
        checkout_intent.stripe_product_id = 'prod_academy_123'

        create_subscription_checkout_session(input_data, lms_user_id=7, checkout_intent=checkout_intent)

        _, kwargs = mock_session_create.call_args
        metadata = kwargs['subscription_data']['metadata']
        self.assertEqual(metadata['name'], 'Data Science')
        self.assertEqual(metadata['product_type'], 'essentials')
        mock_product_retrieve.assert_called_once_with('prod_academy_123')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.create')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Customer.search')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_returns_to_dict_recursive_when_to_dict_missing(
        self,
        mock_product_retrieve,
        mock_customer_search,
        mock_session_create,
    ):
        """Use to_dict_recursive when to_dict is not available on Stripe session object."""
        mock_product_retrieve.return_value = {'metadata': {}}
        mock_customer_search.return_value = mock.MagicMock(data=[])
        mock_stripe_session = mock.MagicMock(spec=['to_dict_recursive'])
        mock_stripe_session.to_dict_recursive.return_value = {'id': 'cs_recursive'}
        mock_session_create.return_value = mock_stripe_session

        checkout_intent = mock.MagicMock(
            id='chk_r',
            uuid='uuid_chk_r',
            stripe_product_id=None,
        )
        result = create_subscription_checkout_session(
            self._base_input(),
            lms_user_id=1,
            checkout_intent=checkout_intent,
        )

        self.assertEqual(result, {'id': 'cs_recursive'})

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.checkout.Session.create')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Customer.search')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_returns_plain_dict_session_as_is(
        self,
        mock_product_retrieve,
        mock_customer_search,
        mock_session_create,
    ):
        """Return plain dict sessions directly."""
        mock_product_retrieve.return_value = {'metadata': {}}
        mock_customer_search.return_value = mock.MagicMock(data=[])
        mock_session_create.return_value = {'id': 'cs_plain_dict'}

        checkout_intent = mock.MagicMock(
            id='chk_d',
            uuid='uuid_chk_d',
            stripe_product_id=None,
        )
        result = create_subscription_checkout_session(
            self._base_input(),
            lms_user_id=1,
            checkout_intent=checkout_intent,
        )

        self.assertEqual(result, {'id': 'cs_plain_dict'})


class TestGetStripeTrialingSubscription(TestCase):
    """Tests for get_stripe_trialing_subscription."""

    def setUp(self):
        TieredCache.dangerous_clear_all_tiers()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Subscription.list')
    def test_returns_first_subscription_when_found(self, mock_list):
        subscription = {'id': 'sub_trial'}
        mock_list.return_value = mock.MagicMock(data=[subscription])

        result = get_stripe_trialing_subscription('cus_123')

        self.assertEqual(result, subscription)
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


class TestGetAcademyStripeProducts(TestCase):
    """Tests for get_academy_stripe_products."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_returns_products_on_success(self, mock_search):
        mock_product = mock.MagicMock()
        mock_product.id = 'prod_academy_1'
        mock_search.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([mock_product]))
        )

        result = get_academy_stripe_products()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 'prod_academy_1')
        mock_search.assert_called_once()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_returns_empty_list_when_no_products(self, mock_search):
        mock_search.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([]))
        )

        result = get_academy_stripe_products()

        self.assertEqual(result, [])

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_raises_stripe_error(self, mock_search):
        mock_search.side_effect = stripe.StripeError('Stripe unavailable')

        with self.assertRaises(stripe.StripeError):
            get_academy_stripe_products()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_raises_generic_exception(self, mock_search):
        mock_search.side_effect = Exception('Unexpected error')

        with self.assertRaises(Exception):
            get_academy_stripe_products()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_search_query_filters_by_product_type_metadata(self, mock_search):
        mock_search.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([]))
        )

        get_academy_stripe_products()

        query_arg = mock_search.call_args[1].get('query') or mock_search.call_args[0][0]
        self.assertIn(STRIPE_PRODUCT_TYPE_METADATA_KEY, query_arg)
        self.assertIn(STRIPE_PRODUCT_TYPE_ESSENTIAL_ACADEMY, query_arg)


class TestGetAcademyStripeProductByKey(TestCase):
    """Tests for get_academy_stripe_product_by_key."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_returns_product_when_found(self, mock_search):
        mock_product = mock.MagicMock()
        mock_product.id = 'prod_ai'
        mock_search.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([mock_product]))
        )

        result = get_academy_stripe_product_by_key('essentials_ai')

        self.assertEqual(result.id, 'prod_ai')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_returns_none_when_not_found(self, mock_search):
        mock_search.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([]))
        )

        result = get_academy_stripe_product_by_key('nonexistent_key')

        self.assertIsNone(result)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_raises_stripe_error(self, mock_search):
        mock_search.side_effect = stripe.StripeError('Stripe down')

        with self.assertRaises(stripe.StripeError):
            get_academy_stripe_product_by_key('essentials_ai')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_search_query_includes_product_key(self, mock_search):
        mock_search.return_value = mock.MagicMock(
            auto_paging_iter=mock.MagicMock(return_value=iter([]))
        )

        get_academy_stripe_product_by_key('essentials_data')

        query_arg = mock_search.call_args[1].get('query') or mock_search.call_args[0][0]
        self.assertIn(STRIPE_PRODUCT_KEY_METADATA_KEY, query_arg)
        self.assertIn('essentials_data', query_arg)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.search')
    def test_raises_unexpected_exception(self, mock_search):
        mock_search.side_effect = RuntimeError('Unexpected error')

        with self.assertRaises(RuntimeError):
            get_academy_stripe_product_by_key('essentials_ai')


class TestGetAcademyStripePrices(TestCase):
    """Tests for get_academy_stripe_prices."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_academy_stripe_products')
    def test_returns_prices_for_all_products(self, mock_get_products, mock_price_list):
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

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_academy_stripe_products')
    def test_returns_empty_when_no_products(self, mock_get_products):
        mock_get_products.return_value = []

        result = get_academy_stripe_prices()

        self.assertEqual(result, [])

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_academy_stripe_products')
    def test_continues_when_one_product_price_fetch_fails(self, mock_get_products, mock_price_list):
        prod1 = mock.MagicMock()
        prod1.id = 'prod_1'
        prod2 = mock.MagicMock()
        prod2.id = 'prod_2'
        mock_get_products.return_value = [prod1, prod2]

        def price_list_side_effect(*, product, active):
            self.assertTrue(active)
            if product == 'prod_1':
                raise stripe.StripeError('Error for prod_1')
            mock_price = mock.MagicMock()
            mock_price.id = 'price_prod_2'
            return mock.MagicMock(auto_paging_iter=mock.MagicMock(return_value=iter([mock_price])))

        mock_price_list.side_effect = price_list_side_effect

        result = get_academy_stripe_prices()

        # Should still return prices for prod_2 even though prod_1 failed
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 'price_prod_2')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_academy_stripe_products')
    def test_raises_when_products_fetch_raises_stripe_error(self, mock_get_products):
        mock_get_products.side_effect = stripe.StripeError('Cannot fetch products')

        with self.assertRaises(stripe.StripeError):
            get_academy_stripe_prices()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_academy_stripe_products')
    def test_aggregates_prices_across_multiple_products(self, mock_get_products, mock_price_list):
        products = [mock.MagicMock(id=f'prod_{i}') for i in range(3)]
        mock_get_products.return_value = products

        def price_list_side_effect(*, product, active):
            self.assertTrue(active)
            price = mock.MagicMock()
            price.id = f'price_for_{product}'
            return mock.MagicMock(auto_paging_iter=mock.MagicMock(return_value=iter([price])))

        mock_price_list.side_effect = price_list_side_effect

        result = get_academy_stripe_prices()

        self.assertEqual(len(result), 3)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_academy_stripe_products')
    def test_raises_when_products_fetch_raises_unexpected_error(self, mock_get_products):
        mock_get_products.side_effect = RuntimeError('Unexpected failure')

        with self.assertRaises(RuntimeError):
            get_academy_stripe_prices()


class TestGetSubscriptionProductMetadata(TestCase):
    """Tests for _get_subscription_product_metadata (via create_subscription_checkout_session)."""

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_returns_empty_dict_when_no_stripe_product_id(self, mock_retrieve):
        intent = mock.MagicMock(spec=[])  # no stripe_product_id attr

        result = _get_subscription_product_metadata(intent)

        mock_retrieve.assert_not_called()
        self.assertEqual(result, {})

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_returns_empty_dict_when_stripe_product_id_is_none(self, mock_retrieve):
        intent = mock.MagicMock(stripe_product_id=None)

        result = _get_subscription_product_metadata(intent)

        mock_retrieve.assert_not_called()
        self.assertEqual(result, {})

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_returns_name_and_product_type_from_metadata(self, mock_retrieve):
        mock_retrieve.return_value = {
            'metadata': {'name': 'AI Academy', 'product_type': 'essential_academy'}
        }
        intent = mock.MagicMock(stripe_product_id='prod_ai', uuid='uuid-123')

        result = _get_subscription_product_metadata(intent)

        self.assertEqual(result['name'], 'AI Academy')
        self.assertEqual(result['product_type'], 'essential_academy')

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_returns_empty_dict_on_stripe_error(self, mock_retrieve):
        mock_retrieve.side_effect = stripe.StripeError('Product not found')
        intent = mock.MagicMock(stripe_product_id='prod_missing', uuid='uuid-456')

        result = _get_subscription_product_metadata(intent)

        self.assertEqual(result, {})

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.stripe.Product.retrieve')
    def test_omits_keys_not_in_metadata(self, mock_retrieve):
        mock_retrieve.return_value = {'metadata': {'other_key': 'other_value'}}
        intent = mock.MagicMock(stripe_product_id='prod_test', uuid='uuid-789')

        result = _get_subscription_product_metadata(intent)

        self.assertNotIn('name', result)
        self.assertNotIn('product_type', result)
        self.assertEqual(result, {})
