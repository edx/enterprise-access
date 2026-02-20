"""
Tests for customer billing API endpoints.
"""
import json
import uuid
from datetime import timedelta
from unittest import mock

import stripe
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_ADMIN_ROLE, SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import CheckoutIntentState
from enterprise_access.apps.customer_billing.models import CheckoutIntent
from test_utils import APITest


class CustomerBillingPortalSessionTests(APITest):
    """
    Tests for CustomerBillingPortalSession endpoints.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())
        self.stripe_customer_id = 'cus_test_123'

        # Create a checkout intent for testing
        self.checkout_intent = CheckoutIntent.objects.create(
            user=self.user,
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            stripe_customer_id=self.stripe_customer_id,
            state=CheckoutIntentState.PAID,
            quantity=10,
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def tearDown(self):
        CheckoutIntent.objects.all().delete()
        super().tearDown()

    def test_create_enterprise_admin_portal_session_success(self):
        """
        Successful creation of enterprise admin portal session.
        """
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,  # implicit access to this enterprise
        }])

        url = reverse('api:v1:customer-billing-create-enterprise-admin-portal-session')

        mock_session = {
            'id': 'bps_test_123',
            'url': 'https://billing.stripe.com/session/test_123',
            'customer': self.stripe_customer_id,
        }

        with mock.patch('stripe.billing_portal.Session.create') as mock_create:
            mock_create.return_value = mock_session

            response = self.client.get(
                url,
                {'enterprise_customer_uuid': self.enterprise_uuid},
                HTTP_ORIGIN='https://admin.example.com'
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, mock_session)

        # Implementation uses /{enterprise_slug} for Admin portal return URL.
        mock_create.assert_called_once_with(
            customer=self.stripe_customer_id,
            return_url='https://admin.example.com/test-enterprise',
        )

    def test_create_enterprise_admin_portal_session_missing_uuid(self):
        """
        Without enterprise_customer_uuid, RBAC blocks at the decorator -> 403.
        """
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

        url = reverse('api:v1:customer-billing-create-enterprise-admin-portal-session')

        response = self.client.get(url)

        # Permission layer rejects because fn(...) yields None context.
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_enterprise_admin_portal_session_no_checkout_intent(self):
        """
        RBAC passes (user has implicit access to provided UUID), view returns 404 when no intent exists.
        """
        non_existent_uuid = str(uuid.uuid4())
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': non_existent_uuid,
        }])

        url = reverse('api:v1:customer-billing-create-enterprise-admin-portal-session')

        response = self.client.get(
            url,
            {'enterprise_customer_uuid': non_existent_uuid}
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_enterprise_admin_portal_session_no_stripe_customer(self):
        """
        If the CheckoutIntent has no Stripe customer ID, Stripe call will error → 422.
        """
        other_user = UserFactory()
        checkout_intent_no_stripe = CheckoutIntent.objects.create(
            user=other_user,
            enterprise_uuid=str(uuid.uuid4()),
            enterprise_name='Test Enterprise 2',
            enterprise_slug='test-enterprise-2',
            stripe_customer_id=None,
            state=CheckoutIntentState.CREATED,
            quantity=5,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': checkout_intent_no_stripe.enterprise_uuid,
        }])

        url = reverse('api:v1:customer-billing-create-enterprise-admin-portal-session')

        with mock.patch('stripe.billing_portal.Session.create') as mock_create:
            mock_create.side_effect = stripe.InvalidRequestError(
                'Customer does not exist',
                'customer'
            )
            response = self.client.get(
                url,
                {'enterprise_customer_uuid': checkout_intent_no_stripe.enterprise_uuid},
                HTTP_ORIGIN='https://admin.example.com'
            )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_create_enterprise_admin_portal_session_stripe_error(self):
        """
        Stripe API returns an error → 422.
        """
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

        url = reverse('api:v1:customer-billing-create-enterprise-admin-portal-session')

        with mock.patch('stripe.billing_portal.Session.create') as mock_create:
            mock_create.side_effect = stripe.InvalidRequestError(
                'Customer does not exist',
                'customer'
            )

            response = self.client.get(
                url,
                {'enterprise_customer_uuid': self.enterprise_uuid},
                HTTP_ORIGIN='https://admin.example.com'
            )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_create_enterprise_admin_portal_session_authentication_required(self):
        """
        Authentication required for enterprise admin portal session.
        """
        url = reverse('api:v1:customer-billing-create-enterprise-admin-portal-session')

        response = self.client.get(
            url,
            {'enterprise_customer_uuid': self.enterprise_uuid}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_enterprise_admin_portal_session_permission_required(self):
        """
        User with learner role only should be forbidden by RBAC.
        """
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }])

        url = reverse('api:v1:customer-billing-create-enterprise-admin-portal-session')

        response = self.client.get(
            url,
            {'enterprise_customer_uuid': self.enterprise_uuid}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_checkout_portal_session_success(self):
        """
        Successful creation of checkout portal session.
        """
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

        url = reverse('api:v1:customer-billing-create-checkout-portal-session',
                      kwargs={'pk': self.checkout_intent.id})

        mock_session = {
            'id': 'bps_test_456',
            'url': 'https://billing.stripe.com/session/test_456',
            'customer': self.stripe_customer_id,
        }

        with mock.patch('stripe.billing_portal.Session.create') as mock_create:
            mock_create.return_value = mock_session

            response = self.client.get(
                url,
                HTTP_ORIGIN='https://checkout.example.com'
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, mock_session)

        mock_create.assert_called_once_with(
            customer=self.stripe_customer_id,
            return_url='https://checkout.example.com/billing-details/success',
        )

    def test_create_checkout_portal_session_wrong_user(self):
        """
        Wrong user (permission class denies) → 403.
        """
        other_user = UserFactory()
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }], user=other_user)

        url = reverse('api:v1:customer-billing-create-checkout-portal-session',
                      kwargs={'pk': self.checkout_intent.id})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_checkout_portal_session_nonexistent_intent(self):
        """
        Permission class denies before view (no intent for pk) → 403.
        """
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

        url = reverse('api:v1:customer-billing-create-checkout-portal-session',
                      kwargs={'pk': 99999})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_checkout_portal_session_no_stripe_customer(self):
        """
        No Stripe customer on the CheckoutIntent → 404 (from view).
        """
        other_user = UserFactory()
        checkout_intent_no_stripe = CheckoutIntent.objects.create(
            user=other_user,
            enterprise_uuid=str(uuid.uuid4()),
            enterprise_name='Test Enterprise 3',
            enterprise_slug='test-enterprise-3',
            stripe_customer_id=None,
            state=CheckoutIntentState.CREATED,
            quantity=5,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }], user=other_user)

        url = reverse('api:v1:customer-billing-create-checkout-portal-session',
                      kwargs={'pk': checkout_intent_no_stripe.id})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_checkout_portal_session_stripe_error(self):
        """
        Stripe API error → 422.
        """
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

        url = reverse('api:v1:customer-billing-create-checkout-portal-session',
                      kwargs={'pk': self.checkout_intent.id})

        with mock.patch('stripe.billing_portal.Session.create') as mock_create:
            mock_create.side_effect = stripe.AuthenticationError('Invalid API key')

            response = self.client.get(
                url,
                HTTP_ORIGIN='https://checkout.example.com'
            )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_create_checkout_portal_session_authentication_required(self):
        """
        Authentication required for checkout portal session.
        """
        url = reverse('api:v1:customer-billing-create-checkout-portal-session',
                      kwargs={'pk': self.checkout_intent.id})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class StripeWebhookTests(APITest):
    """
    Tests for Stripe webhook endpoint with new authentication.
    """

    def setUp(self):
        super().setUp()
        self.url = reverse('api:v1:customer-billing-stripe-webhook')
        self.valid_event_payload = json.dumps({
            'id': 'evt_test_webhook',
            'object': 'event',
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_test_123',
                }
            }
        })

    def _post_webhook_with_signature(self, payload, signature):
        """Helper to POST webhook data with signature header."""
        return self.client.post(
            self.url,
            data=payload,
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE=signature,
        )

    @override_settings(STRIPE_WEBHOOK_ENDPOINT_SECRET='whsec_test_secret')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.StripeEventHandler.dispatch')
    @mock.patch('stripe.Webhook.construct_event')
    def test_webhook_success_with_valid_signature(self, mock_construct_event, mock_dispatch):
        """
        Test webhook endpoint succeeds with valid Stripe signature.
        """
        mock_event = {'id': 'evt_test', 'type': 'checkout.session.completed'}
        mock_construct_event.return_value = mock_event

        response = self._post_webhook_with_signature(
            self.valid_event_payload,
            't=1234567890,v1=valid_signature'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_dispatch.assert_called_once_with(mock_event)

    @override_settings(STRIPE_WEBHOOK_ENDPOINT_SECRET='whsec_test_secret')
    @mock.patch('stripe.Webhook.construct_event')
    def test_webhook_fails_with_invalid_signature(self, mock_construct_event):
        """
        Test webhook endpoint fails with invalid signature.
        """
        mock_construct_event.side_effect = stripe.SignatureVerificationError(
            'Invalid signature',
            'sig_header'
        )

        response = self._post_webhook_with_signature(
            self.valid_event_payload,
            't=1234567890,v1=invalid_signature'
        )

        # Authentication failure returns 403
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_webhook_fails_without_signature_header(self):
        """
        Test webhook endpoint fails when signature header is missing.
        """
        response = self.client.post(
            self.url,
            data=self.valid_event_payload,
            content_type='application/json',
        )

        # Missing signature header causes authentication failure (403)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(STRIPE_WEBHOOK_ENDPOINT_SECRET=None)
    def test_webhook_fails_without_secret_configured(self):
        """
        Test webhook endpoint fails when secret is not configured.
        """
        response = self._post_webhook_with_signature(
            self.valid_event_payload,
            't=1234567890,v1=signature'
        )

        # Missing configuration causes authentication failure (403)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(STRIPE_WEBHOOK_ENDPOINT_SECRET='whsec_test_secret')
    @mock.patch('stripe.Webhook.construct_event')
    def test_webhook_fails_with_invalid_payload(self, mock_construct_event):
        """
        With authentication parsing the event, an invalid payload results in auth failing.
        Expect a 403 Forbidden with an appropriate error message.
        """
        # Authentication layer raises due to invalid payload
        mock_construct_event.side_effect = ValueError('Invalid payload')

        response = self._post_webhook_with_signature(
            'invalid payload',
            't=1234567890,v1=valid_signature'
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('invalid', str(response.data).lower())

    @override_settings(STRIPE_WEBHOOK_ENDPOINT_SECRET='whsec_test_secret')
    @mock.patch('enterprise_access.apps.customer_billing.stripe_event_handlers.StripeEventHandler.dispatch')
    @mock.patch('stripe.Webhook.construct_event')
    def test_webhook_propagates_handler_exceptions(self, mock_construct_event, mock_dispatch):
        """
        Test that exceptions from event handler are propagated (trigger Stripe retry).
        """
        mock_event = {'id': 'evt_test', 'type': 'checkout.session.completed'}
        mock_construct_event.return_value = mock_event
        mock_dispatch.side_effect = Exception('Handler failed')

        with self.assertRaises(Exception) as context:
            self._post_webhook_with_signature(
                self.valid_event_payload,
                't=1234567890,v1=valid_signature'
            )

        self.assertIn('Handler failed', str(context.exception))


class BillingManagementAPITests(APITest):
    """
    Tests for the billing management API endpoints.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())

    def test_billing_management_api_endpoint_available(self):
        """
        Test that the billing management API health-check endpoint is available and requires authentication.
        """
        # The endpoint should be available when flag is enabled in settings
        url = reverse('api:v1:billing-management-health-check')
        response = self.client.get(url)
        # Should return 200 OK because we have authentication set up by APITest
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {'status': 'healthy'})

    def test_billing_management_requires_authentication(self):
        """
        Test that the billing management API endpoint requires authentication.
        """
        # Remove authentication
        self.client.cookies.clear()

        url = reverse('api:v1:billing-management-health-check')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class BillingManagementAddressEndpointTests(APITest):
    """
    Tests for the billing management address endpoint.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())
        self.stripe_customer_id = 'cus_test_address_123'

        # Create a checkout intent for testing
        self.checkout_intent = CheckoutIntent.objects.create(
            user=self.user,
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            stripe_customer_id=self.stripe_customer_id,
            state=CheckoutIntentState.PAID,
            quantity=10,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        # Set JWT cookie with appropriate permissions
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

    def tearDown(self):
        CheckoutIntent.objects.all().delete()
        super().tearDown()

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_stripe_customer')
    def test_get_address_success(self, mock_get_stripe_customer):
        """
        Test successful retrieval of billing address.
        """
        mock_stripe_customer = {
            'id': self.stripe_customer_id,
            'name': 'John Doe',
            'email': 'john@example.com',
            'phone': '+1234567890',
            'address': {
                'line1': '123 Main St',
                'line2': 'Suite 100',
                'city': 'San Francisco',
                'state': 'CA',
                'postal_code': '94105',
                'country': 'US',
            },
        }
        mock_get_stripe_customer.return_value = mock_stripe_customer

        url = reverse('api:v1:billing-management-address')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(response_data['name'], 'John Doe')
        self.assertEqual(response_data['email'], 'john@example.com')
        self.assertEqual(response_data['phone'], '+1234567890')
        self.assertEqual(response_data['address_line_1'], '123 Main St')
        self.assertEqual(response_data['address_line_2'], 'Suite 100')
        self.assertEqual(response_data['city'], 'San Francisco')
        self.assertEqual(response_data['state'], 'CA')
        self.assertEqual(response_data['postal_code'], '94105')
        self.assertEqual(response_data['country'], 'US')

    def test_get_address_missing_enterprise_uuid(self):
        """
        Test that missing enterprise_customer_uuid returns 400.
        """
        url = reverse('api:v1:billing-management-address')
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.json())

    def test_get_address_nonexistent_enterprise(self):
        """
        Test that non-existent enterprise returns 404.
        """
        nonexistent_uuid = str(uuid.uuid4())
        url = reverse('api:v1:billing-management-address')
        response = self.client.get(url, {'enterprise_customer_uuid': nonexistent_uuid})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('error', response.json())

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_stripe_customer')
    def test_get_address_stripe_error(self, mock_get_stripe_customer):
        """
        Test that Stripe API errors are handled gracefully.
        """
        mock_get_stripe_customer.side_effect = stripe.error.StripeError('Stripe API Error')

        url = reverse('api:v1:billing-management-address')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn('error', response.json())

    def test_get_address_requires_permission(self):
        """
        Test that the endpoint requires BILLING_MANAGEMENT_ACCESS_PERMISSION.
        """
        # Create a user without billing management permission
        from enterprise_access.apps.core.tests.factories import UserFactory
        unprivileged_user = UserFactory()
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }], user=unprivileged_user)

        url = reverse('api:v1:billing-management-address')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch('enterprise_access.apps.customer_billing.stripe_api.get_stripe_customer')
    def test_get_address_with_partial_address_data(self, mock_get_stripe_customer):
        """
        Test that endpoint handles Stripe customers with partial address data.
        """
        mock_stripe_customer = {
            'id': self.stripe_customer_id,
            'name': 'Jane Doe',
            'email': 'jane@example.com',
            'phone': None,
            'address': None,
        }
        mock_get_stripe_customer.return_value = mock_stripe_customer

        url = reverse('api:v1:billing-management-address')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(response_data['name'], 'Jane Doe')
        self.assertEqual(response_data['email'], 'jane@example.com')
        self.assertIsNone(response_data['phone'])
        self.assertIsNone(response_data.get('address_line_1'))
        self.assertIsNone(response_data.get('city'))


class BillingManagementAddressUpdateTests(APITest):
    """
    Tests for the billing management address update endpoint.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())
        self.stripe_customer_id = 'cus_test_update_123'

        # Create a checkout intent for testing
        self.checkout_intent = CheckoutIntent.objects.create(
            user=self.user,
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            stripe_customer_id=self.stripe_customer_id,
            state=CheckoutIntentState.PAID,
            quantity=10,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        # Set JWT cookie with appropriate permissions
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

    def tearDown(self):
        CheckoutIntent.objects.all().delete()
        super().tearDown()

    @mock.patch('stripe.Customer.modify')
    def test_update_address_success(self, mock_customer_modify):
        """
        Test successful update of billing address.
        """
        updated_customer = {
            'id': self.stripe_customer_id,
            'name': 'Jane Smith',
            'email': 'jane.smith@example.com',
            'phone': '+14155551234',
            'address': {
                'line1': '456 Oak Ave',
                'line2': 'Floor 2',
                'city': 'New York',
                'state': 'NY',
                'postal_code': '10001',
                'country': 'US',
            },
        }
        mock_customer_modify.return_value = updated_customer

        url = reverse('api:v1:billing-management-address')
        request_data = {
            'name': 'Jane Smith',
            'email': 'jane.smith@example.com',
            'phone': '+14155551234',
            'address_line_1': '456 Oak Ave',
            'address_line_2': 'Floor 2',
            'city': 'New York',
            'state': 'NY',
            'postal_code': '10001',
            'country': 'US',
        }
        response = self.client.post(
            url,
            request_data,
            {'enterprise_customer_uuid': str(self.enterprise_uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(response_data['name'], 'Jane Smith')
        self.assertEqual(response_data['email'], 'jane.smith@example.com')
        self.assertEqual(response_data['phone'], '+14155551234')
        self.assertEqual(response_data['address_line_1'], '456 Oak Ave')
        self.assertEqual(response_data['address_line_2'], 'Floor 2')
        self.assertEqual(response_data['city'], 'New York')
        self.assertEqual(response_data['state'], 'NY')
        self.assertEqual(response_data['postal_code'], '10001')
        self.assertEqual(response_data['country'], 'US')

        # Verify Stripe API was called correctly
        mock_customer_modify.assert_called_once_with(
            self.stripe_customer_id,
            name='Jane Smith',
            email='jane.smith@example.com',
            phone='+14155551234',
            address={
                'line1': '456 Oak Ave',
                'line2': 'Floor 2',
                'city': 'New York',
                'state': 'NY',
                'postal_code': '10001',
                'country': 'US',
            },
        )

    def test_update_address_missing_enterprise_uuid(self):
        """
        Test that missing enterprise_customer_uuid returns 400.
        """
        url = reverse('api:v1:billing-management-address')
        request_data = {
            'name': 'Jane Smith',
            'email': 'jane@example.com',
            'country': 'US',
            'address_line_1': '123 Main St',
            'city': 'San Francisco',
            'state': 'CA',
            'postal_code': '94105',
        }
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.json())

    def test_update_address_missing_required_fields(self):
        """
        Test that missing required fields returns 400 with validation errors.
        """
        url = reverse('api:v1:billing-management-address')
        request_data = {
            'name': 'Jane Smith',
            # Missing required fields: email, country, address_line_1, city, state, postal_code
        }
        response = self.client.post(
            url,
            request_data,
            {'enterprise_customer_uuid': str(self.enterprise_uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn('email', response_data)
        self.assertIn('country', response_data)

    def test_update_address_invalid_country_code(self):
        """
        Test that invalid country code is rejected.
        """
        url = reverse('api:v1:billing-management-address')
        request_data = {
            'name': 'Jane Smith',
            'email': 'jane@example.com',
            'country': 'USA',  # Invalid - should be 2 letters
            'address_line_1': '123 Main St',
            'city': 'San Francisco',
            'state': 'CA',
            'postal_code': '94105',
        }
        response = self.client.post(
            url,
            request_data,
            {'enterprise_customer_uuid': str(self.enterprise_uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn('country', response_data)

    def test_update_address_nonexistent_enterprise(self):
        """
        Test that non-existent enterprise returns 404.
        """
        nonexistent_uuid = str(uuid.uuid4())
        url = reverse('api:v1:billing-management-address')
        request_data = {
            'name': 'Jane Smith',
            'email': 'jane@example.com',
            'country': 'US',
            'address_line_1': '123 Main St',
            'city': 'San Francisco',
            'state': 'CA',
            'postal_code': '94105',
        }
        response = self.client.post(
            url,
            request_data,
            {'enterprise_customer_uuid': nonexistent_uuid},
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('error', response.json())

    @mock.patch('stripe.Customer.modify')
    def test_update_address_stripe_error(self, mock_customer_modify):
        """
        Test that Stripe API errors are handled gracefully.
        """
        mock_customer_modify.side_effect = stripe.error.StripeError('Stripe API Error')

        url = reverse('api:v1:billing-management-address')
        request_data = {
            'name': 'Jane Smith',
            'email': 'jane@example.com',
            'country': 'US',
            'address_line_1': '123 Main St',
            'city': 'San Francisco',
            'state': 'CA',
            'postal_code': '94105',
        }
        response = self.client.post(
            url,
            request_data,
            {'enterprise_customer_uuid': str(self.enterprise_uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn('error', response.json())

    def test_update_address_requires_permission(self):
        """
        Test that the endpoint requires BILLING_MANAGEMENT_ACCESS_PERMISSION.
        """
        # Create a user without billing management permission
        from enterprise_access.apps.core.tests.factories import UserFactory
        unprivileged_user = UserFactory()
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }], user=unprivileged_user)

        url = reverse('api:v1:billing-management-address')
        request_data = {
            'name': 'Jane Smith',
            'email': 'jane@example.com',
            'country': 'US',
            'address_line_1': '123 Main St',
            'city': 'San Francisco',
            'state': 'CA',
            'postal_code': '94105',
        }
        response = self.client.post(
            url,
            request_data,
            {'enterprise_customer_uuid': str(self.enterprise_uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch('stripe.Customer.modify')
    def test_update_address_with_optional_fields_omitted(self, mock_customer_modify):
        """
        Test that update works when optional fields are omitted.
        """
        updated_customer = {
            'id': self.stripe_customer_id,
            'name': 'John Doe',
            'email': 'john@example.com',
            'phone': None,
            'address': {
                'line1': '789 Pine St',
                'line2': None,
                'city': 'Los Angeles',
                'state': 'CA',
                'postal_code': '90001',
                'country': 'US',
            },
        }
        mock_customer_modify.return_value = updated_customer

        url = reverse('api:v1:billing-management-address')
        request_data = {
            'name': 'John Doe',
            'email': 'john@example.com',
            'country': 'US',
            'address_line_1': '789 Pine St',
            # Omit optional: address_line_2, phone
            'city': 'Los Angeles',
            'state': 'CA',
            'postal_code': '90001',
        }
        response = self.client.post(
            url,
            request_data,
            {'enterprise_customer_uuid': str(self.enterprise_uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(response_data['name'], 'John Doe')
        self.assertEqual(response_data['email'], 'john@example.com')


class BillingManagementPaymentMethodsTests(APITest):
    """
    Tests for the billing management payment methods endpoint.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())
        self.stripe_customer_id = 'cus_test_payment_123'

        # Create a checkout intent for testing
        self.checkout_intent = CheckoutIntent.objects.create(
            user=self.user,
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            stripe_customer_id=self.stripe_customer_id,
            state=CheckoutIntentState.PAID,
            quantity=10,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        # Set JWT cookie with appropriate permissions
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

    def tearDown(self):
        CheckoutIntent.objects.all().delete()
        super().tearDown()

    @mock.patch('stripe.Customer.retrieve')
    @mock.patch('stripe.PaymentMethod.list')
    def test_list_payment_methods_success(self, mock_payment_method_list, mock_customer_retrieve):
        """
        Test successful retrieval of payment methods.
        """
        mock_customer = {'invoice_settings': {'default_payment_method': 'pm_card_visa'}}
        mock_customer_retrieve.return_value = mock_customer

        mock_payment_methods = [
            {
                'id': 'pm_card_visa',
                'type': 'card',
                'card': {
                    'last4': '4242',
                    'brand': 'visa',
                    'exp_month': 12,
                    'exp_year': 2025,
                }
            },
            {
                'id': 'pm_card_mastercard',
                'type': 'card',
                'card': {
                    'last4': '5555',
                    'brand': 'mastercard',
                    'exp_month': 6,
                    'exp_year': 2026,
                }
            }
        ]
        mock_payment_method_list.return_value = mock.Mock(data=mock_payment_methods)

        url = reverse('api:v1:billing-management-payment-methods')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data['payment_methods']), 2)

        # Check first payment method (the default)
        first_method = response_data['payment_methods'][0]
        self.assertEqual(first_method['id'], 'pm_card_visa')
        self.assertEqual(first_method['type'], 'card')
        self.assertEqual(first_method['last4'], '4242')
        self.assertEqual(first_method['brand'], 'visa')
        self.assertEqual(first_method['exp_month'], 12)
        self.assertEqual(first_method['exp_year'], 2025)
        self.assertTrue(first_method['is_default'])

        # Check second payment method (not default)
        second_method = response_data['payment_methods'][1]
        self.assertEqual(second_method['id'], 'pm_card_mastercard')
        self.assertEqual(second_method['type'], 'card')
        self.assertEqual(second_method['last4'], '5555')
        self.assertEqual(second_method['brand'], 'mastercard')
        self.assertFalse(second_method['is_default'])

    @mock.patch('stripe.Customer.retrieve')
    @mock.patch('stripe.PaymentMethod.list')
    def test_list_payment_methods_empty(self, mock_payment_method_list, mock_customer_retrieve):
        """
        Test that empty payment methods list returns successfully.
        """
        mock_customer = {'invoice_settings': {}}
        mock_customer_retrieve.return_value = mock_customer
        mock_payment_method_list.return_value = mock.Mock(data=[])

        url = reverse('api:v1:billing-management-payment-methods')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data['payment_methods']), 0)

    def test_list_payment_methods_missing_enterprise_uuid(self):
        """
        Test that missing enterprise_customer_uuid returns 400.
        """
        url = reverse('api:v1:billing-management-payment-methods')
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.json())

    def test_list_payment_methods_nonexistent_enterprise(self):
        """
        Test that non-existent enterprise returns 404.
        """
        nonexistent_uuid = str(uuid.uuid4())
        url = reverse('api:v1:billing-management-payment-methods')
        response = self.client.get(url, {'enterprise_customer_uuid': nonexistent_uuid})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('error', response.json())

    @mock.patch('stripe.Customer.retrieve')
    def test_list_payment_methods_stripe_error(self, mock_customer_retrieve):
        """
        Test that Stripe API errors are handled gracefully.
        """
        mock_customer_retrieve.side_effect = stripe.error.StripeError('Stripe API Error')

        url = reverse('api:v1:billing-management-payment-methods')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn('error', response.json())

    def test_list_payment_methods_requires_permission(self):
        """
        Test that the endpoint requires BILLING_MANAGEMENT_ACCESS_PERMISSION.
        """
        # Create a user without billing management permission
        from enterprise_access.apps.core.tests.factories import UserFactory
        unprivileged_user = UserFactory()
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }], user=unprivileged_user)

        url = reverse('api:v1:billing-management-payment-methods')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch('stripe.Customer.retrieve')
    @mock.patch('stripe.PaymentMethod.list')
    def test_list_payment_methods_with_bank_account(self, mock_payment_method_list, mock_customer_retrieve):
        """
        Test that payment methods include bank account details when present.
        """
        mock_customer = {'invoice_settings': {'default_payment_method': 'pm_bank_account'}}
        mock_customer_retrieve.return_value = mock_customer

        mock_payment_methods = [
            {
                'id': 'pm_bank_account',
                'type': 'us_bank_account',
                'us_bank_account': {
                    'last4': '6789',
                }
            }
        ]
        mock_payment_method_list.return_value = mock.Mock(data=mock_payment_methods)

        url = reverse('api:v1:billing-management-payment-methods')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data['payment_methods']), 1)

        method = response_data['payment_methods'][0]
        self.assertEqual(method['id'], 'pm_bank_account')
        self.assertEqual(method['type'], 'us_bank_account')
        self.assertEqual(method['last4'], '6789')
        self.assertTrue(method['is_default'])



class BillingManagementDeletePaymentMethodTests(APITest):
    """
    Tests for the delete payment method endpoint.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())
        self.stripe_customer_id = 'cus_test_delete_123'

        # Create a checkout intent for testing
        self.checkout_intent = CheckoutIntent.objects.create(
            user=self.user,
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            stripe_customer_id=self.stripe_customer_id,
            state=CheckoutIntentState.PAID,
            quantity=10,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        # Set JWT cookie with appropriate permissions
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

    def tearDown(self):
        CheckoutIntent.objects.all().delete()
        super().tearDown()

    @mock.patch('stripe.PaymentMethod.detach')
    @mock.patch('stripe.PaymentMethod.list')
    @mock.patch('stripe.Customer.retrieve')
    @mock.patch('stripe.PaymentMethod.retrieve')
    def test_delete_payment_method_success(self, mock_retrieve_pm, mock_retrieve_cust, mock_list_pm, mock_detach):
        """
        Test successfully deleting a non-default payment method when others exist.
        """
        mock_payment_method = mock.Mock()
        mock_payment_method.get.side_effect = lambda key, default=None: {
            'id': 'pm_test123',
            'customer': self.stripe_customer_id,
        }.get(key, default)
        mock_retrieve_pm.return_value = mock_payment_method

        # Mock customer with different default
        mock_customer = mock.Mock()
        mock_customer.get.side_effect = lambda key, default=None: {
            'id': self.stripe_customer_id,
            'invoice_settings': {'default_payment_method': 'pm_default'},
        }.get(key, default)
        mock_retrieve_cust.return_value = mock_customer

        # Mock multiple payment methods
        mock_payment_methods = [
            mock.Mock(get=lambda key, default=None: {'id': 'pm_default'}.get(key, default)),
            mock.Mock(get=lambda key, default=None: {'id': 'pm_test123'}.get(key, default)),
        ]
        mock_list_pm.return_value = mock.Mock(data=mock_payment_methods)

        url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_test123'})
        response = self.client.delete(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertIn('message', response_data)
        self.assertIn('deleted successfully', response_data['message'])
        mock_detach.assert_called_once_with('pm_test123')

    @mock.patch('stripe.PaymentMethod.list')
    @mock.patch('stripe.Customer.retrieve')
    @mock.patch('stripe.PaymentMethod.retrieve')
    def test_delete_only_payment_method_fails(self, mock_retrieve_pm, mock_retrieve_cust, mock_list_pm):
        """
        Test that deleting the only payment method returns 409 conflict.
        """
        mock_payment_method = mock.Mock()
        mock_payment_method.get.side_effect = lambda key, default=None: {
            'id': 'pm_only',
            'customer': self.stripe_customer_id,
        }.get(key, default)
        mock_retrieve_pm.return_value = mock_payment_method

        # Mock customer
        mock_customer = mock.Mock()
        mock_customer.get.side_effect = lambda key, default=None: {
            'id': self.stripe_customer_id,
            'invoice_settings': {'default_payment_method': 'pm_only'},
        }.get(key, default)
        mock_retrieve_cust.return_value = mock_customer

        # Mock only one payment method
        mock_payment_methods = [
            mock.Mock(get=lambda key, default=None: {'id': 'pm_only'}.get(key, default)),
        ]
        mock_list_pm.return_value = mock.Mock(data=mock_payment_methods)

        url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_only'})
        response = self.client.delete(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('only payment method', response_data['error'])

    @mock.patch('stripe.PaymentMethod.list')
    @mock.patch('stripe.Customer.retrieve')
    @mock.patch('stripe.PaymentMethod.retrieve')
    def test_delete_default_payment_method_with_others_fails(self, mock_retrieve_pm, mock_retrieve_cust, mock_list_pm):
        """
        Test that deleting the default payment method when others exist returns 409 conflict.
        """
        mock_payment_method = mock.Mock()
        mock_payment_method.get.side_effect = lambda key, default=None: {
            'id': 'pm_default',
            'customer': self.stripe_customer_id,
        }.get(key, default)
        mock_retrieve_pm.return_value = mock_payment_method

        # Mock customer with this method as default
        mock_customer = mock.Mock()
        mock_customer.get.side_effect = lambda key, default=None: {
            'id': self.stripe_customer_id,
            'invoice_settings': {'default_payment_method': 'pm_default'},
        }.get(key, default)
        mock_retrieve_cust.return_value = mock_customer

        # Mock multiple payment methods
        mock_payment_methods = [
            mock.Mock(get=lambda key, default=None: {'id': 'pm_default'}.get(key, default)),
            mock.Mock(get=lambda key, default=None: {'id': 'pm_other'}.get(key, default)),
        ]
        mock_list_pm.return_value = mock.Mock(data=mock_payment_methods)

        url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_default'})
        response = self.client.delete(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Set another method as default', response_data['error'])

    def test_delete_payment_method_missing_uuid(self):
        """
        Test that endpoint requires enterprise_customer_uuid query parameter.
        """
        url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_test123'})
        response = self.client.delete(url, {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('enterprise_customer_uuid', response_data['error'])

    @mock.patch('stripe.PaymentMethod.retrieve')
    def test_delete_payment_method_payment_method_not_found(self, mock_retrieve_pm):
        """
        Test that endpoint returns 404 when payment method doesn't exist.
        """
        mock_retrieve_pm.side_effect = stripe.error.InvalidRequestError('No such payment method')

        url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_invalid'})
        response = self.client.delete(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_data = response.json()
        self.assertIn('error', response_data)

    @mock.patch('stripe.PaymentMethod.retrieve')
    def test_delete_payment_method_belongs_to_different_customer(self, mock_retrieve_pm):
        """
        Test that endpoint returns 404 when payment method belongs to different customer.
        """
        mock_payment_method = mock.Mock()
        mock_payment_method.get.side_effect = lambda key, default=None: {
            'id': 'pm_test123',
            'customer': 'cus_different_customer',
        }.get(key, default)
        mock_retrieve_pm.return_value = mock_payment_method

        url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_test123'})
        response = self.client.delete(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('does not belong to this customer', response_data['error'])

    def test_delete_payment_method_non_existent_enterprise(self):
        """
        Test that endpoint returns 404 when enterprise is not found.
        """
        non_existent_uuid = str(uuid.uuid4())

        url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_test123'})
        response = self.client.delete(url, {'enterprise_customer_uuid': non_existent_uuid})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Stripe customer not found', response_data['error'])

    @mock.patch('stripe.PaymentMethod.list')
    @mock.patch('stripe.Customer.retrieve')
    @mock.patch('stripe.PaymentMethod.retrieve')
    def test_delete_payment_method_stripe_api_error(self, mock_retrieve_pm, mock_retrieve_cust, mock_list_pm):
        """
        Test that endpoint handles Stripe API errors gracefully.
        """
        mock_payment_method = mock.Mock()
        mock_payment_method.get.side_effect = lambda key, default=None: {
            'id': 'pm_test123',
            'customer': self.stripe_customer_id,
        }.get(key, default)
        mock_retrieve_pm.return_value = mock_payment_method

        # Mock customer
        mock_customer = mock.Mock()
        mock_customer.get.side_effect = lambda key, default=None: {
            'id': self.stripe_customer_id,
            'invoice_settings': {'default_payment_method': 'pm_default'},
        }.get(key, default)
        mock_retrieve_cust.return_value = mock_customer

        # Mock multiple payment methods but detach fails
        mock_payment_methods = [
            mock.Mock(get=lambda key, default=None: {'id': 'pm_default'}.get(key, default)),
            mock.Mock(get=lambda key, default=None: {'id': 'pm_test123'}.get(key, default)),
        ]
        mock_list_pm.return_value = mock.Mock(data=mock_payment_methods)

        # Mock detach to fail
        with mock.patch('stripe.PaymentMethod.detach', side_effect=stripe.error.StripeError('Connection error')):
            url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_test123'})
            response = self.client.delete(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Stripe API error', response_data['error'])

    def test_delete_payment_method_requires_permission(self):
        """
        Test that endpoint requires BILLING_MANAGEMENT_ACCESS_PERMISSION.
        """
        # Set JWT cookie without required permission
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }])

        url = reverse('api:v1:billing-management-delete-payment-method', kwargs={'payment_method_id': 'pm_test123'})
        response = self.client.delete(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

class BillingManagementTransactionsTests(APITest):
    """
    Tests for the list transactions endpoint.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())
        self.stripe_customer_id = 'cus_test_transactions_123'

        # Create a checkout intent for testing
        self.checkout_intent = CheckoutIntent.objects.create(
            user=self.user,
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            stripe_customer_id=self.stripe_customer_id,
            state=CheckoutIntentState.PAID,
            quantity=10,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        # Set JWT cookie with appropriate permissions
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

    def tearDown(self):
        CheckoutIntent.objects.all().delete()
        super().tearDown()

    @mock.patch('stripe.Charge.retrieve')
    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_success(self, mock_invoice_list, mock_charge_retrieve):
        """
        Test successfully retrieving transaction list.
        """
        # Mock invoice response
        mock_invoices = [
            {
                'id': 'in_test123',
                'created': 1640000000,
                'amount_paid': 9900,
                'currency': 'USD',
                'status': 'paid',
                'description': 'Test Invoice 1',
                'hosted_invoice_url': 'https://stripe.com/invoice/1',
                'charge': 'ch_test123',
            },
            {
                'id': 'in_test456',
                'created': 1639900000,
                'amount_paid': 5000,
                'currency': 'USD',
                'status': 'open',
                'description': 'Test Invoice 2',
                'hosted_invoice_url': 'https://stripe.com/invoice/2',
                'charge': None,
            },
        ]
        mock_invoice_list.return_value = mock.Mock(
            data=mock_invoices,
            has_more=False,
        )

        # Mock charge response
        mock_charge = {
            'id': 'ch_test123',
            'receipt_url': 'https://stripe.com/receipt/ch_test123',
        }
        mock_charge_retrieve.return_value = mock_charge

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        self.assertIn('transactions', response_data)
        self.assertEqual(len(response_data['transactions']), 2)
        
        # Check first transaction
        tx = response_data['transactions'][0]
        self.assertEqual(tx['id'], 'in_test123')
        self.assertEqual(tx['amount'], 9900)
        self.assertEqual(tx['currency'], 'usd')
        self.assertEqual(tx['status'], 'paid')
        self.assertEqual(tx['description'], 'Test Invoice 1')
        self.assertIsNone(response_data.get('next_page_token'))

    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_with_pagination(self, mock_invoice_list):
        """
        Test pagination with next_page_token.
        """
        # Mock invoice response with has_more=True
        mock_invoices = [
            {
                'id': 'in_test123',
                'created': 1640000000,
                'amount_paid': 9900,
                'currency': 'USD',
                'status': 'paid',
                'description': 'Test Invoice 1',
                'hosted_invoice_url': 'https://stripe.com/invoice/1',
                'charge': None,
            },
        ]
        mock_invoice_list.return_value = mock.Mock(
            data=mock_invoices,
            has_more=True,
        )

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        self.assertIn('next_page_token', response_data)
        self.assertIsNotNone(response_data['next_page_token'])
        self.assertEqual(response_data['next_page_token'], 'in_test123')

    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_with_limit_parameter(self, mock_invoice_list):
        """
        Test limit parameter is passed to Stripe API and capped at 25.
        """
        mock_invoices = []
        mock_invoice_list.return_value = mock.Mock(data=mock_invoices, has_more=False)

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {
            'enterprise_customer_uuid': str(self.enterprise_uuid),
            'limit': '15'
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_invoice_list.assert_called_once()
        call_kwargs = mock_invoice_list.call_args[1]
        self.assertEqual(call_kwargs['limit'], 15)

    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_limit_capped_at_25(self, mock_invoice_list):
        """
        Test that limit is capped at max of 25.
        """
        mock_invoices = []
        mock_invoice_list.return_value = mock.Mock(data=mock_invoices, has_more=False)

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {
            'enterprise_customer_uuid': str(self.enterprise_uuid),
            'limit': '100'
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        call_kwargs = mock_invoice_list.call_args[1]
        self.assertEqual(call_kwargs['limit'], 10)  # Falls back to default when > 25

    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_missing_uuid(self, mock_invoice_list):
        """
        Test that endpoint requires enterprise_customer_uuid query parameter.
        """
        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('enterprise_customer_uuid', response_data['error'])

    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_non_existent_enterprise(self, mock_invoice_list):
        """
        Test that endpoint returns 404 when enterprise is not found.
        """
        non_existent_uuid = str(uuid.uuid4())

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {'enterprise_customer_uuid': non_existent_uuid})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Stripe customer not found', response_data['error'])

    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_stripe_api_error(self, mock_invoice_list):
        """
        Test that endpoint handles Stripe API errors gracefully.
        """
        mock_invoice_list.side_effect = stripe.error.StripeError('Connection error')

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Stripe API error', response_data['error'])

    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_requires_permission(self, mock_invoice_list):
        """
        Test that endpoint requires BILLING_MANAGEMENT_ACCESS_PERMISSION.
        """
        # Set JWT cookie without required permission
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }])

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch('stripe.Charge.retrieve')
    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_empty_list(self, mock_invoice_list, mock_charge_retrieve):
        """
        Test returning empty transaction list.
        """
        mock_invoice_list.return_value = mock.Mock(data=[], has_more=False)

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        self.assertIn('transactions', response_data)
        self.assertEqual(len(response_data['transactions']), 0)
        self.assertIsNone(response_data.get('next_page_token'))

    @mock.patch('stripe.Charge.retrieve')
    @mock.patch('stripe.Invoice.list')
    def test_list_transactions_normalizes_status(self, mock_invoice_list, mock_charge_retrieve):
        """
        Test that invoice statuses are normalized correctly.
        """
        # Mock invoices with various Stripe statuses
        mock_invoices = [
            {
                'id': 'in_paid',
                'created': 1640000000,
                'amount_paid': 1000,
                'currency': 'USD',
                'status': 'paid',
                'description': 'Paid invoice',
                'hosted_invoice_url': 'https://stripe.com/invoice/1',
                'charge': None,
            },
            {
                'id': 'in_draft',
                'created': 1640000000,
                'amount_paid': 0,
                'currency': 'USD',
                'status': 'draft',
                'description': 'Draft invoice',
                'hosted_invoice_url': 'https://stripe.com/invoice/2',
                'charge': None,
            },
            {
                'id': 'in_void',
                'created': 1640000000,
                'amount_paid': 0,
                'currency': 'USD',
                'status': 'void',
                'description': 'Void invoice',
                'hosted_invoice_url': 'https://stripe.com/invoice/3',
                'charge': None,
            },
        ]
        mock_invoice_list.return_value = mock.Mock(data=mock_invoices, has_more=False)

        url = reverse('api:v1:billing-management-transactions')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        transactions = response_data['transactions']
        self.assertEqual(transactions[0]['status'], 'paid')
        self.assertEqual(transactions[1]['status'], 'open')  # Draft normalized to open
        self.assertEqual(transactions[2]['status'], 'void')

class BillingManagementSubscriptionTests(APITest):
    """
    Tests for the get subscription status endpoint.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())
        self.stripe_customer_id = 'cus_test_subscription_123'

        # Create a checkout intent for testing
        self.checkout_intent = CheckoutIntent.objects.create(
            user=self.user,
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            stripe_customer_id=self.stripe_customer_id,
            state=CheckoutIntentState.PAID,
            quantity=10,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        # Set JWT cookie with appropriate permissions
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

    def tearDown(self):
        CheckoutIntent.objects.all().delete()
        super().tearDown()

    @mock.patch('stripe.Product.retrieve')
    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_success(self, mock_sub_list, mock_price_retrieve, mock_product_retrieve):
        """
        Test successfully retrieving subscription with plan_type from metadata.
        """
        # Mock price
        mock_price = {
            'id': 'price_test123',
            'unit_amount': 50000,  # $500/year
            'recurring': {'interval': 'year'},
            'product': 'prod_test123',
            'metadata': {'plan_type': 'Teams'},
        }
        mock_price_retrieve.return_value = mock_price

        # Mock subscription
        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 5,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        url = reverse('api:v1:billing-management-subscription')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        self.assertIn('subscription', response_data)
        sub = response_data['subscription']
        self.assertEqual(sub['id'], 'sub_test123')
        self.assertEqual(sub['status'], 'active')
        self.assertEqual(sub['plan_type'], 'Teams')
        self.assertFalse(sub['cancel_at_period_end'])
        self.assertEqual(sub['yearly_amount'], 50000)
        self.assertEqual(sub['license_count'], 5)

    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_no_active_subscription(self, mock_sub_list):
        """
        Test returning null subscription when no active subscription exists.
        """
        mock_sub_list.return_value = mock.Mock(data=[])

        url = reverse('api:v1:billing-management-subscription')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        self.assertIn('subscription', response_data)
        self.assertIsNone(response_data['subscription'])

    @mock.patch('stripe.Product.retrieve')
    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_plan_type_from_product_metadata(self, mock_sub_list, mock_price_retrieve, mock_product_retrieve):
        """
        Test deriving plan_type from product metadata when not in price metadata.
        """
        # Mock price without plan_type
        mock_price = {
            'id': 'price_test123',
            'unit_amount': 50000,
            'recurring': {'interval': 'year'},
            'product': 'prod_test123',
            'metadata': {},
        }
        mock_price_retrieve.return_value = mock_price

        # Mock product with plan_type
        mock_product = {
            'id': 'prod_test123',
            'metadata': {'plan_type': 'Essentials'},
        }
        mock_product_retrieve.return_value = mock_product

        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 1,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        url = reverse('api:v1:billing-management-subscription')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        sub = response_data['subscription']
        self.assertEqual(sub['plan_type'], 'Essentials')

    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_plan_type_defaults_to_other(self, mock_sub_list, mock_price_retrieve):
        """
        Test that plan_type defaults to 'Other' when no metadata is found.
        """
        # Mock price without plan_type
        mock_price = {
            'id': 'price_test123',
            'unit_amount': 50000,
            'recurring': {'interval': 'year'},
            'product': 'prod_test123',
            'metadata': {},
        }
        mock_price_retrieve.return_value = mock_price

        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 1,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        with mock.patch('stripe.Product.retrieve', side_effect=stripe.error.StripeError('Not found')):
            url = reverse('api:v1:billing-management-subscription')
            response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        sub = response_data['subscription']
        self.assertEqual(sub['plan_type'], 'Other')

    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_missing_uuid(self, mock_sub_list):
        """
        Test that endpoint requires enterprise_customer_uuid query parameter.
        """
        url = reverse('api:v1:billing-management-subscription')
        response = self.client.get(url, {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('enterprise_customer_uuid', response_data['error'])

    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_non_existent_enterprise(self, mock_sub_list):
        """
        Test that endpoint returns 404 when enterprise is not found.
        """
        non_existent_uuid = str(uuid.uuid4())

        url = reverse('api:v1:billing-management-subscription')
        response = self.client.get(url, {'enterprise_customer_uuid': non_existent_uuid})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Stripe customer not found', response_data['error'])

    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_stripe_api_error(self, mock_sub_list):
        """
        Test that endpoint handles Stripe API errors gracefully.
        """
        mock_sub_list.side_effect = stripe.error.StripeError('Connection error')

        url = reverse('api:v1:billing-management-subscription')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Stripe API error', response_data['error'])

    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_requires_permission(self, mock_sub_list):
        """
        Test that endpoint requires BILLING_MANAGEMENT_ACCESS_PERMISSION.
        """
        # Set JWT cookie without required permission
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }])

        url = reverse('api:v1:billing-management-subscription')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.list')
    def test_get_subscription_yearly_amount_calculation(self, mock_sub_list, mock_price_retrieve):
        """
        Test yearly amount calculation for different billing periods.
        """
        # Mock monthly billing
        mock_price = {
            'id': 'price_test123',
            'unit_amount': 5000,  # $50/month
            'recurring': {'interval': 'month'},
            'product': 'prod_test123',
            'metadata': {'plan_type': 'Teams'},
        }
        mock_price_retrieve.return_value = mock_price

        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 1,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        url = reverse('api:v1:billing-management-subscription')
        response = self.client.get(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        sub = response_data['subscription']
        # Monthly: 5000 * 12 = 60000
        self.assertEqual(sub['yearly_amount'], 60000)

class BillingManagementCancelSubscriptionTests(APITest):
    """
    Tests for the cancel subscription endpoint.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = str(uuid.uuid4())
        self.stripe_customer_id = 'cus_test_cancel_123'

        # Create a checkout intent for testing
        self.checkout_intent = CheckoutIntent.objects.create(
            user=self.user,
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            enterprise_slug='test-enterprise',
            stripe_customer_id=self.stripe_customer_id,
            state=CheckoutIntentState.PAID,
            quantity=10,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        # Set JWT cookie with appropriate permissions
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

    def tearDown(self):
        CheckoutIntent.objects.all().delete()
        super().tearDown()

    @mock.patch('stripe.Product.retrieve')
    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.modify')
    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_teams_plan_success(self, mock_sub_list, mock_sub_modify, mock_price_retrieve, mock_product_retrieve):
        """
        Test successfully cancelling a Teams plan subscription.
        """
        # Mock price with Teams plan_type
        mock_price = {
            'id': 'price_test123',
            'product': 'prod_test123',
            'metadata': {'plan_type': 'Teams'},
        }
        mock_price_retrieve.return_value = mock_price

        # Mock subscription
        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 5,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        # Mock modified subscription
        mock_modified_subscription = mock_subscription.copy()
        mock_modified_subscription['cancel_at_period_end'] = True
        mock_sub_modify.return_value = mock_modified_subscription

        url = reverse('api:v1:billing-management-cancel-subscription')
        response = self.client.post(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        
        self.assertIn('subscription', response_data)
        sub = response_data['subscription']
        self.assertEqual(sub['id'], 'sub_test123')
        self.assertTrue(sub['cancel_at_period_end'])
        mock_sub_modify.assert_called_once_with('sub_test123', cancel_at_period_end=True)

    @mock.patch('stripe.Product.retrieve')
    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.modify')
    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_essentials_plan_success(self, mock_sub_list, mock_sub_modify, mock_price_retrieve, mock_product_retrieve):
        """
        Test successfully cancelling an Essentials plan subscription.
        """
        # Mock price with Essentials plan_type
        mock_price = {
            'id': 'price_test123',
            'product': 'prod_test123',
            'metadata': {'plan_type': 'Essentials'},
        }
        mock_price_retrieve.return_value = mock_price

        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 1,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        mock_modified_subscription = mock_subscription.copy()
        mock_modified_subscription['cancel_at_period_end'] = True
        mock_sub_modify.return_value = mock_modified_subscription

        url = reverse('api:v1:billing-management-cancel-subscription')
        response = self.client.post(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        sub = response_data['subscription']
        self.assertTrue(sub['cancel_at_period_end'])

    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_learner_credit_plan_fails(self, mock_sub_list, mock_price_retrieve):
        """
        Test that cancelling LearnerCredit plan returns 403.
        """
        # Mock price with LearnerCredit plan_type
        mock_price = {
            'id': 'price_test123',
            'product': 'prod_test123',
            'metadata': {'plan_type': 'LearnerCredit'},
        }
        mock_price_retrieve.return_value = mock_price

        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 1,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        url = reverse('api:v1:billing-management-cancel-subscription')
        response = self.client.post(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('plan type', response_data['error'])

    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_other_plan_fails(self, mock_sub_list, mock_price_retrieve):
        """
        Test that cancelling Other plan returns 403.
        """
        # Mock price without plan_type metadata
        mock_price = {
            'id': 'price_test123',
            'product': 'prod_test123',
            'metadata': {},
        }
        mock_price_retrieve.return_value = mock_price

        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 1,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        with mock.patch('stripe.Product.retrieve', side_effect=stripe.error.StripeError('Not found')):
            url = reverse('api:v1:billing-management-cancel-subscription')
            response = self.client.post(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('plan type', response_data['error'])

    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_already_cancelling(self, mock_sub_list):
        """
        Test that cancelling an already-cancelling subscription returns 409.
        """
        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': True,  # Already cancelling
            'current_period_end': 1640000000,
            'items': {
                'data': []
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        url = reverse('api:v1:billing-management-cancel-subscription')
        response = self.client.post(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('already scheduled', response_data['error'])

    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_no_active_subscription(self, mock_sub_list):
        """
        Test that cancelling with no active subscription returns 404.
        """
        mock_sub_list.return_value = mock.Mock(data=[])

        url = reverse('api:v1:billing-management-cancel-subscription')
        response = self.client.post(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('No active subscription', response_data['error'])

    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_missing_uuid(self, mock_sub_list):
        """
        Test that endpoint requires enterprise_customer_uuid query parameter.
        """
        url = reverse('api:v1:billing-management-cancel-subscription')
        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('enterprise_customer_uuid', response_data['error'])

    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_non_existent_enterprise(self, mock_sub_list):
        """
        Test that endpoint returns 404 when enterprise is not found.
        """
        non_existent_uuid = str(uuid.uuid4())

        url = reverse('api:v1:billing-management-cancel-subscription')
        response = self.client.post(url, {'enterprise_customer_uuid': non_existent_uuid})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Stripe customer not found', response_data['error'])

    @mock.patch('stripe.Price.retrieve')
    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_stripe_api_error(self, mock_sub_list, mock_price_retrieve):
        """
        Test that endpoint handles Stripe API errors gracefully.
        """
        mock_price = {
            'id': 'price_test123',
            'product': 'prod_test123',
            'metadata': {'plan_type': 'Teams'},
        }
        mock_price_retrieve.return_value = mock_price

        mock_subscription = {
            'id': 'sub_test123',
            'status': 'active',
            'cancel_at_period_end': False,
            'current_period_end': 1640000000,
            'items': {
                'data': [
                    {
                        'price': {'id': 'price_test123'},
                        'quantity': 1,
                    }
                ]
            },
        }
        mock_sub_list.return_value = mock.Mock(data=[mock_subscription])

        # Mock modify to fail
        with mock.patch('stripe.Subscription.modify', side_effect=stripe.error.StripeError('Connection error')):
            url = reverse('api:v1:billing-management-cancel-subscription')
            response = self.client.post(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        response_data = response.json()
        self.assertIn('error', response_data)
        self.assertIn('Stripe API error', response_data['error'])

    @mock.patch('stripe.Subscription.list')
    def test_cancel_subscription_requires_permission(self, mock_sub_list):
        """
        Test that endpoint requires BILLING_MANAGEMENT_ACCESS_PERMISSION.
        """
        # Set JWT cookie without required permission
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }])

        url = reverse('api:v1:billing-management-cancel-subscription')
        response = self.client.post(url, {'enterprise_customer_uuid': str(self.enterprise_uuid)})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
