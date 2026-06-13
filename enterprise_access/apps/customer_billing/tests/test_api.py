"""
Unit tests for the ``enterprise_access.apps.customer_billing.api`` module.
"""
from datetime import timedelta
from unittest import mock

import ddt
import requests
import stripe
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing import api as customer_billing_api
from enterprise_access.apps.customer_billing import stripe_api
from enterprise_access.apps.customer_billing.constants import CheckoutIntentState
from enterprise_access.apps.customer_billing.models import CheckoutIntent, SspProduct

User = get_user_model()


def raise_404_error(*args, **kwargs):
    mock_404_response = requests.Response()
    mock_404_response.status_code = 404
    mock_404_response.raise_for_status()


QUARTERLY_PRICE_ID = 'price_test_quarterly'

MOCK_SSP_PRODUCTS = {
    'quarterly_license_plan': {
        'stripe_price_id': QUARTERLY_PRICE_ID,  # DEPRECATED: Use lookup_key instead
        'lookup_key': 'price_quarterly_0002',
        'quantity_range': (5, 30),
    },
    'yearly_license_plan': {
        'stripe_price_id': 'price_test_yearly',  # DEPRECATED: Use lookup_key instead
        'lookup_key': 'price_yearly_0001',
        'quantity_range': (5, 30),
    },
}

MOCK_SSP_PRICING_DATA = {
    'quarterly_license_plan': {
        'id': QUARTERLY_PRICE_ID,
        'lookup_key': 'price_quarterly_0002',
        'quantity_range': (5, 30),
        'unit_amount': 3300,
        'unit_amount_decimal': 33.00,
        'currency': 'usd',
        'ssp_product_key': 'quarterly_license_plan',
    },
    'yearly_license_plan': {
        'id': 'price_test_yearly',
        'lookup_key': 'price_yearly_0001',
        'quantity_range': (5, 30),
        'unit_amount': 36000,
        'unit_amount_decimal': 360.00,
        'currency': 'usd',
        'ssp_product_key': 'yearly_license_plan',
    },
}


@override_settings(
    SSP_PRODUCTS=MOCK_SSP_PRODUCTS,
    SSP_TRIAL_PERIOD_DAYS=14,
)
@ddt.ddt
class TestCreateFreeTrialCheckoutSession(TestCase):
    """
    Tests for the ``create_free_trial_checkout_session()`` function.
    """
    def setUp(self):
        self.user = UserFactory()
        self.other_user = UserFactory()

    def tearDown(self):
        # Clean up any intents created during tests
        CheckoutIntent.objects.all().delete()

    @staticmethod
    def _create_teams_yearly_product():
        return SspProduct.objects.get_or_create(
            slug='teams-yearly',
            defaults={
                'stripe_price_lookup_key': 'teams_yearly_price',
                'catalog_query_uuid': '00000000-0000-0000-0000-000000000000',
                'is_active': True,
            },
        )[0]

    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value=MOCK_SSP_PRICING_DATA,
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_create_free_trial_checkout_session_success(
        self, mock_stripe, mock_lms_client_class, mock_get_ssp_pricing,  # pylint: disable=unused-argument
    ):
        """
        Happy path for ``create_free_trial_checkout_session()`` with checkout intent creation.
        """
        # Setup mocks library methods.
        mock_lms_client = mock_lms_client_class.return_value
        mock_lms_client.get_lms_user_account.return_value = [{'id': self.user.lms_user_id}]
        mock_lms_client.get_enterprise_customer_data.side_effect = raise_404_error
        mock_stripe_session = mock.Mock()
        mock_stripe_session.to_dict.return_value = {
            'id': 'test-stripe-checkout-session',
            'customer': 'cust-123',
        }
        mock_stripe.checkout.Session.create.return_value = mock_stripe_session
        mock_stripe.Customer.search.return_value.data = []

        # Actually call the API under test.
        result = customer_billing_api.create_free_trial_checkout_session(
            user=self.user,
            admin_email=self.user.email,
            enterprise_slug='my-sluggy',
            company_name='My Cool Company',
            quantity=20,
            stripe_price_id=QUARTERLY_PRICE_ID,
        )

        # Assert API response.
        self.assertEqual(
            result,
            {'id': 'test-stripe-checkout-session', 'customer': 'cust-123'},
        )

        # Assert that a CheckoutIntent was created
        intent = CheckoutIntent.objects.get(user=self.user)
        self.assertEqual(intent.state, CheckoutIntentState.CREATED)
        self.assertEqual(intent.enterprise_slug, 'my-sluggy')
        self.assertEqual(intent.enterprise_name, 'My Cool Company')
        self.assertEqual(intent.stripe_checkout_session_id, 'test-stripe-checkout-session')
        self.assertEqual(intent.stripe_customer_id, 'cust-123')
        self.assertFalse(intent.is_expired())

        # Assert library methods were called correctly.
        mock_lms_client.get_lms_user_account.assert_called_once_with(
            email=self.user.email,
        )
        mock_lms_client.get_enterprise_customer_data.assert_has_calls([
            mock.call(enterprise_customer_slug='my-sluggy'),
            mock.call(enterprise_customer_name='My Cool Company'),
        ])

        # Check that customer slug and user data is in Stripe metadata
        call_args = mock_stripe.checkout.Session.create.call_args
        metadata = call_args[1]['subscription_data']['metadata']
        self.assertEqual(metadata['enterprise_customer_slug'], 'my-sluggy')
        self.assertEqual(metadata['lms_user_id'], str(self.user.lms_user_id))

    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value=MOCK_SSP_PRICING_DATA,
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_create_free_trial_checkout_session_success_without_user(
        self, mock_stripe, mock_lms_client_class, mock_get_ssp_pricing,  # pylint: disable=unused-argument
    ):
        """
        Test that checkout session creation works without user (backwards compatibility).
        """
        # Setup mocks library methods.
        mock_lms_client = mock_lms_client_class.return_value
        mock_lms_client.get_lms_user_account.return_value = [{'id': 9876}]
        mock_lms_client.get_enterprise_customer_data.side_effect = raise_404_error
        mock_stripe.checkout.Session.create.return_value = {'id': 'test-stripe-checkout-session'}
        mock_stripe.Customer.search.return_value.data = []

        # Call without user parameter
        with self.assertRaises(customer_billing_api.CreateCheckoutSessionValidationError) as cm:
            customer_billing_api.create_free_trial_checkout_session(
                user=None,
                admin_email='test@example.com',
                enterprise_slug='my-sluggy',
                company_name='My Cool Company',
                quantity=20,
                stripe_price_id=QUARTERLY_PRICE_ID,
            )
            # Should get slug reserved error
            validation_errors = cm.exception.validation_errors_by_field
            self.assertIn('user', validation_errors)

    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value=MOCK_SSP_PRICING_DATA,
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_create_free_trial_checkout_session_replaces_user_intent(
        self, mock_stripe, mock_lms_client_class, mock_get_ssp_pricing,  # pylint: disable=unused-argument
    ):
        """
        Test that creating a new checkout session replaces the user's existing intent.
        """
        # Create an existing intent for the user
        CheckoutIntent.create_intent(
            user=self.user, quantity=10,
            slug='old-slug',
            name='Old Comapny',
            terms_metadata={'version': '1.0'}
        )

        # Setup mocks
        mock_lms_client = mock_lms_client_class.return_value
        mock_lms_client.get_lms_user_account.return_value = [{'id': 9876}]
        mock_lms_client.get_enterprise_customer_data.side_effect = raise_404_error
        mock_stripe_session = mock.Mock()
        mock_stripe_session.to_dict.return_value = {'id': 'new-stripe-session', 'customer': None}
        mock_stripe.checkout.Session.create.return_value = mock_stripe_session
        mock_stripe.Customer.search.return_value.data = []

        # Create new checkout session with different slug
        result = customer_billing_api.create_free_trial_checkout_session(
            user=self.user,
            admin_email='test@example.com',
            enterprise_slug='new-sluggy',
            company_name='New Company',
            quantity=20,
            stripe_price_id=QUARTERLY_PRICE_ID,
        )

        # Should succeed and replace the old intent
        self.assertEqual(result, {'id': 'new-stripe-session', 'customer': None})

        # Assert that a CheckoutIntent was updated
        intent = CheckoutIntent.objects.get(user=self.user)
        self.assertEqual(intent.state, CheckoutIntentState.CREATED)
        self.assertEqual(intent.enterprise_slug, 'new-sluggy')
        self.assertEqual(intent.enterprise_name, 'New Company')
        self.assertEqual(intent.stripe_checkout_session_id, 'new-stripe-session')
        self.assertFalse(intent.is_expired())

    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value=MOCK_SSP_PRICING_DATA,
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_slug_reservation_conflict(
        self, mock_stripe, mock_lms_client_class, mock_get_ssp_pricing,   # pylint: disable=unused-argument
    ):
        """
        Test that slug reservation prevents conflicts between different users.
        """
        # User 1 reserves a slug
        CheckoutIntent.create_intent(
            user=self.other_user,
            quantity=10,
            slug='conflicting-slug',
            name='My company',
            terms_metadata={'version': '1.0'},
        )

        # Setup mocks
        mock_lms_client = mock_lms_client_class.return_value
        mock_lms_client.get_lms_user_account.return_value = [{'id': 9876}]
        mock_lms_client.get_enterprise_customer_data.side_effect = raise_404_error

        # User 2 tries to use the same slug - should fail
        with self.assertRaises(customer_billing_api.CreateCheckoutSessionValidationError) as cm:
            customer_billing_api.create_free_trial_checkout_session(
                user=self.user,
                admin_email='test@example.com',
                enterprise_slug='conflicting-slug',
                company_name='doesnt matter',
                quantity=20,
                stripe_price_id=QUARTERLY_PRICE_ID,
            )

            # Should get slug reserved error
            validation_errors = cm.exception.validation_errors_by_field
            self.assertIn('enterprise_slug', validation_errors)
            self.assertEqual(validation_errors['enterprise_slug']['error_code'], 'slug_reserved')

    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value=MOCK_SSP_PRICING_DATA,
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_name_reservation_conflict(
        self, mock_stripe, mock_lms_client_class, mock_get_ssp_pricing,  # pylint: disable=unused-argument
    ):
        """
        Test that comapny name reservation prevents conflicts between different users.
        """
        # User 1 reserves a slug
        CheckoutIntent.create_intent(
            user=self.other_user,
            quantity=10,
            slug='ok-slug',
            name='Conflicting company',
            terms_metadata={'version': '1.0'},
        )

        # Setup mocks
        mock_lms_client = mock_lms_client_class.return_value
        mock_lms_client.get_lms_user_account.return_value = [{'id': 9876}]
        mock_lms_client.get_enterprise_customer_data.side_effect = raise_404_error

        # User 2 tries to use the same name - should fail
        with self.assertRaises(customer_billing_api.CreateCheckoutSessionValidationError) as cm:
            customer_billing_api.create_free_trial_checkout_session(
                user=self.user,
                admin_email='test@example.com',
                enterprise_slug='different-slug',
                company_name='Conflicting company',
                quantity=20,
                stripe_price_id=QUARTERLY_PRICE_ID,
            )

            # Should get slug reserved error
            validation_errors = cm.exception.validation_errors_by_field
            self.assertIn('company_name', validation_errors)
            self.assertEqual(validation_errors['company_name']['error_code'], 'existing_enterprise_customer')

    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value=MOCK_SSP_PRICING_DATA,
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_expired_intent_allows_reuse(
        self, mock_stripe, mock_lms_client_class, mock_get_ssp_pricing,  # pylint: disable=unused-argument
    ):
        """
        Test that expired intents don't block new intents.
        """
        # Create an expired intent
        expired_time = timezone.now() - timedelta(minutes=5)
        CheckoutIntent.objects.create(
            user=self.other_user,
            enterprise_slug='expired-slug',
            enterprise_name='Expired Company',
            state=CheckoutIntentState.EXPIRED,
            expires_at=expired_time,
            quantity=10,
            country='US',
            terms_metadata={'version': '1.0', 'expired': True}
        )

        # Setup mocks
        mock_lms_client = mock_lms_client_class.return_value
        mock_lms_client.get_lms_user_account.return_value = [{'id': 9876}]
        mock_lms_client.get_enterprise_customer_data.side_effect = raise_404_error
        mock_stripe_session = mock.Mock()
        mock_stripe_session.to_dict.return_value = {'id': 'test-session', 'customer': None}
        mock_stripe.checkout.Session.create.return_value = mock_stripe_session
        mock_stripe.Customer.search.return_value.data = []

        # Should be able to reserve the expired slug
        result = customer_billing_api.create_free_trial_checkout_session(
            user=self.user,
            admin_email='test@example.com',
            enterprise_slug='expired-slug',
            company_name='anything',
            quantity=20,
            stripe_price_id=QUARTERLY_PRICE_ID,
        )

        # Should succeed
        self.assertEqual(result, {'id': 'test-session', 'customer': None})

        # Assert that a CheckoutIntent was created
        intent = CheckoutIntent.objects.get(user=self.user)
        self.assertEqual(intent.state, CheckoutIntentState.CREATED)
        self.assertEqual(intent.enterprise_slug, 'expired-slug')
        self.assertEqual(intent.enterprise_name, 'anything')
        self.assertEqual(intent.stripe_checkout_session_id, 'test-session')
        self.assertFalse(intent.is_expired())

    @ddt.data(
        {
            'email_registered': False,
            'expected_validation_errors': {
                'admin_email': {
                    'error_code': 'not_registered',
                    'developer_message': 'Given email address does not correspond to an existing user.',
                }
            }
        },
        {
            'request_enterprise_slug': 'weird#slug',
            'expected_validation_errors': {
                'enterprise_slug': {
                    'error_code': 'invalid_format',
                    'developer_message': 'Invalid format for given slug.',
                }
            }
        },
        {
            'customer_exists': True,
            'is_admin_for_existing_customer': False,
            'expected_validation_errors': {
                'enterprise_slug': {
                    'error_code': 'existing_enterprise_customer',
                    'developer_message': 'The slug conflicts with an existing customer.',
                }
            }
        },
        {
            'customer_exists': True,
            'is_admin_for_existing_customer': True,
            'expected_validation_errors': {
                'enterprise_slug': {
                    'error_code': 'existing_enterprise_customer',
                    'developer_message': 'The slug conflicts with an existing customer.',
                }
            }
        },
        {
            'request_quantity': -1,
            'expected_validation_errors': {
                'quantity': {
                    'error_code': 'invalid_format',
                    'developer_message': 'Must be a positive integer.',
                }
            }
        },
        {
            'request_quantity': "foo",
            'expected_validation_errors': {
                'quantity': {
                    'error_code': 'invalid_format',
                    'developer_message': 'Must be a positive integer.',
                }
            }
        },
        {
            'request_quantity': 100,
            'expected_validation_errors': {
                'quantity': {
                    'error_code': 'range_exceeded',
                    'developer_message': 'Exceeded allowed range for given stripe_price_id.',
                }
            }
        },
        {
            'request_stripe_price_id': 'price_not-configured',
            'expected_validation_errors': {
                'stripe_price_id': {
                    'error_code': 'does_not_exist',
                    'developer_message': 'This stripe_price_id has not been configured.',
                },
                'quantity': {
                    'developer_message': 'Not enough parameters were given.',
                    'error_code': 'incomplete_data',
                },
            }
        },
    )
    @ddt.unpack
    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value=MOCK_SSP_PRICING_DATA,
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_create_free_trial_checkout_session_errors(
        self,
        mock_stripe,
        mock_lms_client_class,
        mock_get_ssp_pricing,  # pylint: disable=unused-argument
        email_registered=True,
        customer_exists=False,
        is_admin_for_existing_customer=False,
        request_enterprise_slug='my-sluggy',
        request_quantity=15,
        request_stripe_price_id=QUARTERLY_PRICE_ID,
        expected_validation_errors=None,
    ):
        """
        Error cases for ``create_free_trial_checkout_session()``.
        """
        # Setup mocks library methods.
        mock_lms_client = mock_lms_client_class.return_value
        if email_registered:
            mock_lms_client.get_lms_user_account.return_value = [{'id': 9876}]
        else:
            mock_lms_client.get_lms_user_account.side_effect = raise_404_error
        if customer_exists:
            if is_admin_for_existing_customer:
                mock_lms_client.get_enterprise_customer_data.return_value = {
                    'admin_users': [{'email': 'test@example.com'}]
                }
            else:
                mock_lms_client.get_enterprise_customer_data.return_value = {
                    'admin_users': []
                }
        else:
            mock_lms_client.get_enterprise_customer_data.side_effect = raise_404_error
        mock_stripe.checkout.Session.create.return_value = {'id': 'test-stripe-checkout-session'}
        mock_stripe.Customer.search.return_value.data = []

        # Actually call the API under test.
        with self.assertRaises(customer_billing_api.CreateCheckoutSessionValidationError) as cm:
            customer_billing_api.create_free_trial_checkout_session(
                user=self.user,  # Include user in error cases too
                admin_email='test@example.com',
                enterprise_slug=request_enterprise_slug,
                quantity=request_quantity,
                stripe_price_id=request_stripe_price_id,
            )

        actual_validation_errors = cm.exception.validation_errors_by_field
        assert actual_validation_errors == expected_validation_errors

    @mock.patch.object(customer_billing_api.CheckoutSessionInputValidator, 'validate', return_value={})
    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        side_effect=Exception('Pricing API unavailable'),
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_create_free_trial_checkout_session_pricing_error_uses_fallback(
        self, mock_stripe, _mock_lms_client_class, _mock_get_pricing, _mock_validate,
    ):
        """
        When pricing API fails during SSP product lookup, should fall back to teams-yearly.
        """
        self._create_teams_yearly_product()
        mock_session = mock.Mock()
        mock_session.to_dict.return_value = {'id': 'test-session-id', 'customer': 'cus_test'}
        mock_stripe.checkout.Session.create.return_value = mock_session
        mock_stripe.Customer.search.return_value.data = []

        result = customer_billing_api.create_free_trial_checkout_session(
            user=self.user,
            admin_email='test@example.com',
            enterprise_slug='test-slug',
            company_name='Test Company',
            quantity=10,
            stripe_price_id='price_any',
        )

        assert result['id'] == 'test-session-id'
        intent = CheckoutIntent.objects.get(user=self.user)
        assert intent.ssp_product.slug == 'teams-yearly'

    @mock.patch.object(customer_billing_api.CheckoutSessionInputValidator, 'validate', return_value={})
    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value={'product': {'id': 'price_test', 'lookup_key': 'lookup_unmapped'}},
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_create_free_trial_checkout_session_lookup_not_in_db_uses_fallback(
        self, mock_stripe, _mock_lms_client_class, _mock_get_pricing, _mock_validate,
    ):
        """
        When lookup_key doesn't map to an SspProduct, should fall back to teams-yearly.
        """
        self._create_teams_yearly_product()
        mock_session = mock.Mock()
        mock_session.to_dict.return_value = {'id': 'test-session-id', 'customer': 'cus_test'}
        mock_stripe.checkout.Session.create.return_value = mock_session
        mock_stripe.Customer.search.return_value.data = []

        result = customer_billing_api.create_free_trial_checkout_session(
            user=self.user,
            admin_email='test@example.com',
            enterprise_slug='test-slug',
            company_name='Test Company',
            quantity=10,
            stripe_price_id='price_test',
        )

        assert result['id'] == 'test-session-id'
        intent = CheckoutIntent.objects.get(user=self.user)
        assert intent.ssp_product.slug == 'teams-yearly'

    @mock.patch.object(customer_billing_api.CheckoutSessionInputValidator, 'validate', return_value={})
    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value={
            'product': {
                'id': QUARTERLY_PRICE_ID,
                # Missing lookup_key - should trigger fallback
            }
        },
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_create_free_trial_checkout_session_missing_lookup_key_uses_fallback(
        self, mock_stripe, _mock_lms_client_class, _mock_get_pricing, _mock_validate,
    ):
        """
        When matching_price has no lookup_key, should fall back to teams-yearly.
        """
        self._create_teams_yearly_product()
        mock_session = mock.Mock()
        mock_session.to_dict.return_value = {'id': 'test-session-id', 'customer': 'cus_test'}
        mock_stripe.checkout.Session.create.return_value = mock_session
        mock_stripe.Customer.search.return_value.data = []

        result = customer_billing_api.create_free_trial_checkout_session(
            user=self.user,
            admin_email='test@example.com',
            enterprise_slug='test-slug',
            company_name='Test Company',
            quantity=10,
            stripe_price_id=QUARTERLY_PRICE_ID,
        )

        assert result['id'] == 'test-session-id'
        intent = CheckoutIntent.objects.get(user=self.user)
        assert intent.ssp_product.slug == 'teams-yearly'

    @mock.patch.object(customer_billing_api.CheckoutSessionInputValidator, 'validate', return_value={})
    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        return_value=MOCK_SSP_PRICING_DATA,
    )
    @mock.patch.object(customer_billing_api, 'LmsApiClient', autospec=True)
    @mock.patch.object(stripe_api, 'stripe')
    def test_create_free_trial_checkout_session_matching_product_resolved(
        self, mock_stripe, _mock_lms_client_class, _mock_get_pricing, _mock_validate,
    ):
        """
        When lookup_key maps to an SspProduct in DB, use that product directly.
        """
        # Create product with the lookup_key from MOCK_SSP_PRICING_DATA
        quarterly_product = SspProduct.objects.get_or_create(
            slug='quarterly-plan',
            defaults={
                'stripe_price_lookup_key': 'price_quarterly_0002',
                'catalog_query_uuid': '00000000-0000-0000-0000-000000000001',
                'is_active': True,
            },
        )[0]
        mock_session = mock.Mock()
        mock_session.to_dict.return_value = {'id': 'test-session-id', 'customer': 'cus_test'}
        mock_stripe.checkout.Session.create.return_value = mock_session
        mock_stripe.Customer.search.return_value.data = []

        result = customer_billing_api.create_free_trial_checkout_session(
            user=self.user,
            admin_email='test@example.com',
            enterprise_slug='test-slug',
            company_name='Test Company',
            quantity=10,
            stripe_price_id=QUARTERLY_PRICE_ID,
        )

        assert result['id'] == 'test-session-id'
        intent = CheckoutIntent.objects.get(user=self.user)
        assert intent.ssp_product == quarterly_product

    @mock.patch.object(customer_billing_api.CheckoutSessionInputValidator, 'validate', return_value={})
    @mock.patch(
        'enterprise_access.apps.customer_billing.api.get_ssp_product_pricing',
        side_effect=Exception('Pricing API unavailable'),
    )
    def test_create_free_trial_checkout_session_pricing_error_no_fallback_raises(
        self, _mock_get_pricing, _mock_validate,
    ):
        """
        If pricing lookup fails and no teams-yearly fallback exists, raise validation error.
        """
        # Ensure no teams-yearly product exists
        SspProduct.objects.filter(slug='teams-yearly').delete()

        with self.assertRaises(customer_billing_api.CreateCheckoutSessionValidationError) as cm:
            customer_billing_api.create_free_trial_checkout_session(
                user=self.user,
                admin_email='test@example.com',
                enterprise_slug='test-slug',
                company_name='Test Company',
                quantity=10,
                stripe_price_id='price_any',
            )

        # Check the validation error mentions missing SspProduct mapping
        msg = str(cm.exception.validation_errors_by_field)
        self.assertIn('No SspProduct configured', msg)

    def test_create_stripe_billing_portal_session_missing_customer_raises(self):
        """create_stripe_billing_portal_session should raise ValueError without stripe_customer_id."""
        intent = CheckoutIntent.create_intent(
            user=self.user,
            slug='portal-slug',
            name='Portal Company',
            quantity=1,
        )

        with self.assertRaises(ValueError):
            customer_billing_api.create_stripe_billing_portal_session(intent, return_url='https://example.com')

    @mock.patch('enterprise_access.apps.customer_billing.api.stripe.billing_portal.Session.create')
    def test_create_stripe_billing_portal_session_stripeerror_propagates(self, mock_create):
        """If Stripe raises StripeError, it should propagate from billing portal creation."""
        # Create intent and set a stripe_customer_id
        intent = CheckoutIntent.create_intent(
            user=self.user,
            slug='portal-slug-2',
            name='Portal Company 2',
            quantity=1,
        )
        intent.stripe_customer_id = 'cus_test_123'
        intent.save()

        # Make Stripe raise a StripeError
        mock_create.side_effect = stripe.error.StripeError('boom')

        with self.assertRaises(stripe.error.StripeError):
            customer_billing_api.create_stripe_billing_portal_session(intent, return_url='https://example.com')
