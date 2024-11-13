"""
Text for the BFF context
"""
import ddt
from django.test import RequestFactory, TestCase
from faker import Faker
from rest_framework.exceptions import ValidationError
from unittest import mock

from enterprise_access.apps.bffs.context import HandlerContext
from enterprise_access.apps.core.tests.factories import UserFactory


@ddt.ddt
class TestHandlerContext(TestCase):
    """
    Test the HandlerContext class
    """

    def setUp(self):
        super().setUp()
        self.maxDiff = None
        self.factory = RequestFactory()
        self.mock_user = UserFactory()
        self.mock_staff_user = UserFactory(is_staff=True)
        self.faker = Faker()

        self.mock_enterprise_customer_uuid = self.faker.uuid4()
        self.mock_enterprise_customer_slug = 'mock-slug'
        self.mock_enterprise_customer_uuid_2 = self.faker.uuid4()
        self.mock_enterprise_customer_slug_2 = 'mock-slug-2'

        # Mock request
        self.request = self.factory.get('sample/api/call')
        self.request.user = self.mock_user
        self.request.query_params = {
            'enterprise_customer_uuid': self.mock_enterprise_customer_uuid
        }
        self.request.data = {}

        # Mock data
        self.mock_enterprise_learner_response_data = {
            'results': [
                {
                    'active': True,
                    'enterprise_customer': {
                        'uuid': self.mock_enterprise_customer_uuid,
                        'slug': self.mock_enterprise_customer_slug,
                        'enable_learner_portal': True,
                    },
                },
                {
                    'active': False,
                    'enterprise_customer': {
                        'uuid': self.mock_enterprise_customer_uuid_2,
                        'slug': self.mock_enterprise_customer_slug_2,
                        'enable_learner_portal': True,
                    },
                },
            ],
            'enterprise_features': {'feature_flag': True}
        }
        self.mock_transformed_enterprise_customer = {
            'uuid': self.mock_enterprise_customer_uuid,
            'slug': self.mock_enterprise_customer_slug,
            'disable_search': False,
            'enable_learner_portal': True,
            'show_integration_warning': False,
        }
        self.mock_transformed_enterprise_customer_2 = {
            'uuid': self.mock_enterprise_customer_uuid_2,
            'slug': self.mock_enterprise_customer_slug_2,
            'disable_search': False,
            'enable_learner_portal': True,
            'show_integration_warning': False,
        }

    @ddt.data(
        {'raises_exception': False},
        {'raises_exception': True},
    )
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @ddt.unpack
    def test_handler_context_init(self, mock_get_enterprise_customers_for_user, raises_exception):
        if raises_exception:
            mock_get_enterprise_customers_for_user.side_effect = Exception('Mock exception')
        else:
            mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data

        context = HandlerContext(self.request)

        self.assertEqual(context.request, self.request)
        self.assertEqual(context.user, self.mock_user)

        expected_data = {}
        if not raises_exception:
            expected_data = {
                'enterprise_customer': self.mock_transformed_enterprise_customer,
                'active_enterprise_customer': self.mock_transformed_enterprise_customer,
                'staff_enterprise_customer': None,
                'all_linked_enterprise_customer_users': [
                    {
                        **self.mock_enterprise_learner_response_data['results'][0],
                        'enterprise_customer': self.mock_transformed_enterprise_customer,
                    },
                    {
                        **self.mock_enterprise_learner_response_data['results'][1],
                        'enterprise_customer': self.mock_transformed_enterprise_customer_2,
                    }
                ],
            }

        self.assertEqual(context.data, expected_data)
        if raises_exception:
            self.assertEqual(context.errors, [
                {
                    'developer_message': 'Mock exception',
                    'user_message': 'Error retrieving linked enterprise customers'
                 }
            ])
            self.assertEqual(context.enterprise_customer_slug, None)  # No enterprise customer data resolved from uuid
            self.assertEqual(context.enterprise_features, {})
        else:
            self.assertEqual(context.errors, [])
            self.assertEqual(context.enterprise_customer_slug, self.mock_enterprise_customer_slug)
            self.assertEqual(
                context.enterprise_features,
                self.mock_enterprise_learner_response_data['enterprise_features']
            )

        self.assertEqual(context.warnings, [])
        self.assertEqual(context.enterprise_customer_uuid, self.mock_enterprise_customer_uuid)
        self.assertEqual(context.lms_user_id, self.mock_user.lms_user_id)

    @ddt.data(
        # No enterprise customer uuid/slug in the request; returns active enterprise customer user
        {
            'has_query_params': False,
            'has_payload_data': False,
            'has_enterprise_customer_uuid_param': False,
            'has_enterprise_customer_slug_param': False,
        },
        # Enterprise customer uuid in the request; returns enterprise customer user with that uuid
        {
            'has_query_params': True,
            'has_payload_data': False,
            'has_enterprise_customer_uuid_param': True,
            'has_enterprise_customer_slug_param': False,
        },
        {
            'has_query_params': False,
            'has_payload_data': True,
            'has_enterprise_customer_uuid_param': True,
            'has_enterprise_customer_slug_param': False,
        },
        {
            'has_query_params': True,
            'has_payload_data': True,
            'has_enterprise_customer_uuid_param': True,
            'has_enterprise_customer_slug_param': False,
        },
        # Enterprise customer slug in the request; returns enterprise customer user with that slug
        {
            'has_query_params': True,
            'has_payload_data': False,
            'has_enterprise_customer_uuid_param': False,
            'has_enterprise_customer_slug_param': True,
        },
        {
            'has_query_params': False,
            'has_payload_data': True,
            'has_enterprise_customer_uuid_param': False,
            'has_enterprise_customer_slug_param': True,
        },
        {
            'has_query_params': True,
            'has_payload_data': True,
            'has_enterprise_customer_uuid_param': False,
            'has_enterprise_customer_slug_param': True,
        },
        # Both enterprise customer uuid and slug in the request; returns enterprise customer user with that uuid
        {
            'has_query_params': True,
            'has_payload_data': False,
            'has_enterprise_customer_uuid_param': True,
            'has_enterprise_customer_slug_param': True,
        },
        {
            'has_query_params': False,
            'has_payload_data': True,
            'has_enterprise_customer_uuid_param': True,
            'has_enterprise_customer_slug_param': True,
        },
        {
            'has_query_params': True,
            'has_payload_data': True,
            'has_enterprise_customer_uuid_param': True,
            'has_enterprise_customer_slug_param': True,
        },
    )
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @ddt.unpack
    def test_handler_context_enterprise_customer_params(self,
        mock_get_enterprise_customers_for_user,
        has_query_params,
        has_payload_data,
        has_enterprise_customer_uuid_param,
        has_enterprise_customer_slug_param,
    ):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        request = self.request

        query_params = {}
        if has_query_params:
            if has_enterprise_customer_uuid_param:
                query_params['enterprise_customer_uuid'] = self.mock_enterprise_customer_uuid_2
            if has_enterprise_customer_slug_param:
                query_params['enterprise_customer_slug'] = self.mock_enterprise_customer_slug_2

        if has_payload_data:
            # Switch to a POST request
            request = self.factory.post('sample/api/call')
            request.user = self.mock_user
            request.data = {}
            if has_enterprise_customer_uuid_param:
                request.data['enterprise_customer_uuid'] = self.mock_enterprise_customer_uuid_2
            if has_enterprise_customer_slug_param:
                request.data['enterprise_customer_slug'] = self.mock_enterprise_customer_slug_2

        # Set the query params, if any.
        request.query_params = query_params

        context = HandlerContext(request)

        if has_enterprise_customer_slug_param or has_enterprise_customer_uuid_param:
            self.assertEqual(context.enterprise_customer_uuid, self.mock_enterprise_customer_uuid_2)
            self.assertEqual(context.enterprise_customer_slug, self.mock_enterprise_customer_slug_2)
        else:
            self.assertEqual(context.enterprise_customer_uuid, self.mock_enterprise_customer_uuid)
            self.assertEqual(context.enterprise_customer_slug, self.mock_enterprise_customer_slug)


    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_handler_context_add_error_serializer(self, mock_get_enterprise_customers_for_user):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        context = HandlerContext(self.request)

        expected_output = {
            "developer_message": "No enterprise uuid associated to the user mock-id",
            "user_message": "You may not be associated with the enterprise.",
        }
        # Define kwargs for add_error
        arguments = {
            **expected_output,
            "status": 403  # Add an attribute that is not explicitly defined in the serializer to verify
        }
        context.add_error(
            **arguments
        )
        self.assertEqual(expected_output, context.errors[0])

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_handler_context_add_error_serializer_is_valid(self, mock_get_enterprise_customers_for_user):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        context = HandlerContext(self.request)

        malformed_output = {
            "developer_message": "No enterprise uuid associated to the user mock-id",
        }
        with self.assertRaises(ValidationError):
            context.add_error(**malformed_output)

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_handler_context_add_warning_serializer(self, mock_get_enterprise_customers_for_user):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        context = HandlerContext(self.request)
        expected_output = {
            "developer_message": "Heuristic Expiration",
            "user_message": "The data received might be out-dated",
        }
        # Define kwargs for add_warning
        arguments = {
            **expected_output,
            "status": 113  # Add an attribute that is not explicitly defined in the serializer to verify
        }
        context.add_warning(
            **arguments
        )
        self.assertEqual(expected_output, context.warnings[0])

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_handler_context_add_warning_serializer_is_valid(self, mock_get_enterprise_customers_for_user):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        context = HandlerContext(self.request)
        malformed_output = {
            "user_message": "The data received might be out-dated",
        }
        with self.assertRaises(ValidationError):
            context.add_error(**malformed_output)
