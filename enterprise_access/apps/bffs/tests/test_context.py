"""
Tests for the BFF context
"""

from unittest import mock

import ddt
from requests.exceptions import HTTPError
from rest_framework import status
from rest_framework.exceptions import ValidationError

from enterprise_access.apps.bffs.context import HandlerContext
from enterprise_access.apps.bffs.tests.utils import TestHandlerContextMixin


@ddt.ddt
class TestHandlerContext(TestHandlerContextMixin):
    """
    Test the HandlerContext class
    """

    @ddt.data(
        {'raises_exception': False},
        {'raises_exception': True},
    )
    @mock.patch(
        'enterprise_access.apps.api_client.enterprise_catalog_client'
        '.EnterpriseCatalogUserV1ApiClient.get_secured_algolia_api_key'
    )
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @ddt.unpack
    def test_handler_context_init(
            self,
            mock_get_enterprise_customers_for_user,
            mock_get_secured_algolia_api_key_for_user,
            raises_exception):
        if raises_exception:
            mock_get_enterprise_customers_for_user.side_effect = Exception('Mock exception')
        else:
            mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
            mock_get_secured_algolia_api_key_for_user.return_value = self.mock_secured_algolia_api_key_response

        context = HandlerContext(self.request)

        self.assertEqual(context.request, self.request)
        self.assertEqual(context.user, self.mock_user)

        expected_data = {}
        if not raises_exception:
            expected_data = {
                'enterprise_customer': self.mock_enterprise_customer,
                'active_enterprise_customer': self.mock_enterprise_customer,
                'staff_enterprise_customer': None,
                'catalog_uuids_to_catalog_query_uuids': self.mock_catalog_uuids_to_catalog_query_uuids,
                'algolia': self.mock_algolia_object,
                'all_linked_enterprise_customer_users': [
                    {
                        **self.mock_enterprise_learner_response_data['results'][0],
                        'enterprise_customer': self.mock_enterprise_customer,
                    },
                    {
                        **self.mock_enterprise_learner_response_data['results'][1],
                        'enterprise_customer': self.mock_enterprise_customer_2,
                    }
                ],
                'should_update_active_enterprise_customer_user': False,
            }
        self.assertEqual(context.data, expected_data)
        if raises_exception:
            self.assertEqual(context.enterprise_features, {})
        else:
            self.assertEqual(context.enterprise_customer_slug, self.mock_enterprise_customer_slug)
            self.assertEqual(
                context.enterprise_features,
                self.mock_enterprise_learner_response_data['enterprise_features']
            )

        expected_errors = (
            [
                {
                    'developer_message': 'Could not initialize enterprise customer users. Error: Mock exception',
                    'user_message': 'Error initializing enterprise customer users'
                }
            ] if raises_exception else []
        )
        self.assertEqual(context.errors, expected_errors)
        self.assertEqual(context.warnings, [])

        expected_status_code = (
            status.HTTP_500_INTERNAL_SERVER_ERROR
            if raises_exception
            else status.HTTP_200_OK
        )
        self.assertEqual(context.status_code, expected_status_code)

        self.assertEqual(context.enterprise_customer_uuid, self.mock_enterprise_customer_uuid)
        expected_slug = None if raises_exception else self.mock_enterprise_customer_slug
        self.assertEqual(context.enterprise_customer_slug, expected_slug)
        self.assertEqual(context.lms_user_id, self.mock_user.lms_user_id)
        expected_enterprise_customer = None if raises_exception else self.mock_enterprise_customer
        self.assertEqual(context.enterprise_customer, expected_enterprise_customer)
        expected_is_linked_user = False if raises_exception else True
        self.assertEqual(context.is_request_user_linked_to_enterprise_customer, expected_is_linked_user)
        if not raises_exception:
            self.assertEqual(
                context.secured_algolia_api_key,
                self.mock_algolia_object['secured_algolia_api_key'],
            )
            self.assertEqual(context.valid_until, self.mock_algolia_object['valid_until'])

    @ddt.data(
        {'raises_exception': False},
        {'raises_exception': True},
    )
    @mock.patch(
        'enterprise_access.apps.api_client.enterprise_catalog_client'
        '.EnterpriseCatalogUserV1ApiClient.get_secured_algolia_api_key'
    )
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsApiClient.get_enterprise_customer_data')
    @ddt.unpack
    def test_handler_context_init_staff_user_unlinked(
        self,
        mock_get_enterprise_customer_data,
        mock_get_enterprise_customers_for_user,
        mock_get_secured_algolia_api_key_for_user,
        raises_exception,
    ):
        mock_get_enterprise_customers_for_user.return_value = {
            **self.mock_enterprise_learner_response_data,
            'results': [],
        }

        if raises_exception:
            mock_get_enterprise_customer_data.side_effect = Exception('Mock exception')
            mock_get_secured_algolia_api_key_for_user.side_effect = Exception('Mock exception')
        else:
            mock_get_enterprise_customer_data.return_value = self.mock_enterprise_customer
            mock_get_secured_algolia_api_key_for_user.return_value = self.mock_secured_algolia_api_key_response

        request = self.request
        request.user = self.mock_staff_user
        context = HandlerContext(request)
        self.assertEqual(context.request, request)
        self.assertEqual(context.user, self.mock_staff_user)

        expected_data = {
            'enterprise_customer': self.mock_enterprise_customer,
            'active_enterprise_customer': None,
            'staff_enterprise_customer': self.mock_enterprise_customer,
            'catalog_uuids_to_catalog_query_uuids': self.mock_catalog_uuids_to_catalog_query_uuids,
            'algolia': self.mock_algolia_object,
            'all_linked_enterprise_customer_users': [],
            'should_update_active_enterprise_customer_user': False,
        }
        if raises_exception:
            expected_data = {
                'enterprise_customer': None,
                'active_enterprise_customer': None,
                'staff_enterprise_customer': None,
                'should_update_active_enterprise_customer_user': False,
                'all_linked_enterprise_customer_users': [],
            }
        self.assertEqual(context.data, expected_data)
        expected_errors = (
            [
                {
                    'user_message': 'No enterprise customer found',
                    'developer_message': (
                        f'No enterprise customer found for request user {context.lms_user_id} '
                        f'and enterprise uuid {context.enterprise_customer_uuid}, '
                        f'and/or enterprise slug {context.enterprise_customer_slug}'
                    ),
                }
            ] if raises_exception else []
        )
        self.assertEqual(context.errors, expected_errors)
        self.assertEqual(context.warnings, [])

        expected_status_code = (
            status.HTTP_404_NOT_FOUND
            if raises_exception
            else status.HTTP_200_OK
        )
        self.assertEqual(context.status_code, expected_status_code)

        self.assertEqual(context.enterprise_features, self.mock_enterprise_learner_response_data['enterprise_features'])
        self.assertEqual(context.enterprise_customer_uuid, self.mock_enterprise_customer_uuid)
        expected_slug = None if raises_exception else self.mock_enterprise_customer_slug
        self.assertEqual(context.enterprise_customer_slug, expected_slug)
        self.assertEqual(context.lms_user_id, self.mock_staff_user.lms_user_id)
        expected_enterprise_customer = None if raises_exception else self.mock_enterprise_customer
        self.assertEqual(context.enterprise_customer, expected_enterprise_customer)
        self.assertEqual(context.is_request_user_linked_to_enterprise_customer, False)

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
    def test_handler_context_enterprise_customer_params(
        self,
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
        self.assertEqual(expected_output, context.errors[-1])

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

    @mock.patch('enterprise_access.apps.bffs.context.transform_secured_algolia_api_key_response')
    @mock.patch('enterprise_access.apps.bffs.context.get_and_cache_secured_algolia_search_keys')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_handler_context_init_deferred_algolia_skips_upstream_call(
        self,
        mock_get_enterprise_customers_for_user,
        mock_get_and_cache_secured_algolia_search_keys,
        _mock_transform,
    ):
        """initialize_secured_algolia_api_keys=False skips the unscoped enterprise-catalog fetch."""
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data

        context = HandlerContext(self.request, initialize_secured_algolia_api_keys=False)

        mock_get_and_cache_secured_algolia_search_keys.assert_not_called()
        self.assertIsNone(context.data.get('algolia'))
        self.assertIsNone(context.data.get('catalog_uuids_to_catalog_query_uuids'))
        # Enterprise customer data is still fully initialised
        self.assertEqual(context.enterprise_customer, self.mock_enterprise_customer)

    @mock.patch('enterprise_access.apps.bffs.context.transform_secured_algolia_api_key_response')
    @mock.patch('enterprise_access.apps.bffs.context.get_and_cache_secured_algolia_search_keys')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_refresh_secured_algolia_api_keys_empty_catalogs_short_circuits(
        self,
        mock_get_enterprise_customers_for_user,
        mock_get_and_cache_secured_algolia_search_keys,
        mock_transform_secured_algolia_api_key_response,
    ):
        """Explicit empty catalog scope clears Algolia fields without upstream refresh call."""
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        mock_get_and_cache_secured_algolia_search_keys.return_value = self.mock_secured_algolia_api_key_response
        mock_transform_secured_algolia_api_key_response.return_value = (
            self.mock_algolia_object['secured_algolia_api_key'],
            self.mock_catalog_uuids_to_catalog_query_uuids,
            self.mock_algolia_object['valid_until'],
        )

        context = HandlerContext(self.request)
        mock_get_and_cache_secured_algolia_search_keys.reset_mock()
        mock_transform_secured_algolia_api_key_response.reset_mock()

        context.refresh_secured_algolia_api_keys(catalog_uuids=[])

        mock_get_and_cache_secured_algolia_search_keys.assert_not_called()
        mock_transform_secured_algolia_api_key_response.assert_not_called()
        self.assertEqual(context.data.get('catalog_uuids_to_catalog_query_uuids'), {})
        self.assertEqual(
            context.data.get('algolia'),
            {
                'secured_algolia_api_key': None,
                'valid_until': None,
            }
        )

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_handler_context_properties_with_deferred_algolia(
        self,
        mock_get_enterprise_customers_for_user,
    ):
        """
        Exercises HandlerContext properties not covered by other tests.

        Uses a deferred-init context (initialize_secured_algolia_api_keys=False)
        so that the algolia dict is absent from context.data.  This hits the
        ``return None`` branches of ``secured_algolia_api_key`` and ``valid_until``
        and covers ``algolia``, ``catalog_uuids_to_catalog_query_uuids``,
        ``active_enterprise_customer``, ``staff_enterprise_customer``, and
        ``should_update_active_enterprise_customer_user``.
        """
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data

        context = HandlerContext(self.request, initialize_secured_algolia_api_keys=False)

        # Algolia absent → None-branches covered
        self.assertIsNone(context.secured_algolia_api_key)
        self.assertIsNone(context.valid_until)
        self.assertIsNone(context.algolia)
        self.assertIsNone(context.catalog_uuids_to_catalog_query_uuids)

        # Standard enterprise-customer properties
        self.assertEqual(context.active_enterprise_customer, self.mock_enterprise_customer)
        self.assertIsNone(context.staff_enterprise_customer)
        self.assertFalse(context.should_update_active_enterprise_customer_user)

    @mock.patch('enterprise_access.apps.bffs.context.HTTPError', new=HTTPError)
    @mock.patch('enterprise_access.apps.bffs.context.get_and_cache_secured_algolia_search_keys')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_handler_context_http_error_during_algolia_init(
        self,
        mock_get_enterprise_customers_for_user,
        mock_get_and_cache_secured_algolia_search_keys,
    ):
        """
        HTTPError raised by the enterprise-catalog Algolia key endpoint during context
        initialization is caught; its user_message / developer_message are forwarded
        as a structured error on the context.
        """
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data

        mock_response = mock.Mock()
        mock_response.json.return_value = {
            'user_message': 'Algolia key error',
            'developer_message': 'Invalid enterprise customer',
        }
        mock_get_and_cache_secured_algolia_search_keys.side_effect = HTTPError(response=mock_response)

        context = HandlerContext(self.request)

        self.assertEqual(len(context.errors), 1)
        self.assertEqual(context.errors[0]['user_message'], 'Algolia key error')
        self.assertEqual(context.errors[0]['developer_message'], 'Invalid enterprise customer')

    @mock.patch('enterprise_access.apps.bffs.context.HTTPError', new=HTTPError)
    @mock.patch('enterprise_access.apps.bffs.context.get_and_cache_secured_algolia_search_keys')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_handler_context_http_error_during_algolia_init_without_developer_message(
        self,
        mock_get_enterprise_customers_for_user,
        mock_get_and_cache_secured_algolia_search_keys,
    ):
        """
        When the upstream HTTP error payload omits developer_message, the context
        falls back to the default developer-facing message built from the exception.
        """
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data

        mock_response = mock.Mock()
        mock_response.json.return_value = {
            'user_message': 'Algolia key error',
        }
        mock_get_and_cache_secured_algolia_search_keys.side_effect = HTTPError(response=mock_response)

        context = HandlerContext(self.request)

        self.assertEqual(len(context.errors), 1)
        self.assertEqual(context.errors[0]['user_message'], 'Algolia key error')
        self.assertIn(
            'Could not initialize the secured algolia api keys. Error:',
            context.errors[0]['developer_message'],
        )

    @mock.patch('enterprise_access.apps.bffs.context.transform_secured_algolia_api_key_response')
    @mock.patch('enterprise_access.apps.bffs.context.get_and_cache_secured_algolia_search_keys')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_refresh_secured_algolia_api_keys_transform_exception_is_swallowed(
        self,
        mock_get_enterprise_customers_for_user,
        mock_get_and_cache_secured_algolia_search_keys,
        mock_transform_secured_algolia_api_key_response,
    ):
        """
        When transform_secured_algolia_api_key_response raises, the broad-except
        handler swallows the error.  context.data is still updated with None/empty
        algolia values and no error is appended to context.errors.
        """
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        mock_get_and_cache_secured_algolia_search_keys.return_value = {}
        mock_transform_secured_algolia_api_key_response.side_effect = Exception('transform failed')

        context = HandlerContext(self.request, initialize_secured_algolia_api_keys=False)
        context.refresh_secured_algolia_api_keys()

        self.assertIsNone(context.data['algolia']['secured_algolia_api_key'])
        self.assertIsNone(context.data['algolia']['valid_until'])
        self.assertEqual(context.data['catalog_uuids_to_catalog_query_uuids'], {})
        # The broad-except handler only logs; no error is surfaced to callers.
        self.assertEqual(context.errors, [])

    @mock.patch('enterprise_access.apps.bffs.context.transform_secured_algolia_api_key_response')
    @mock.patch('enterprise_access.apps.bffs.context.get_and_cache_secured_algolia_search_keys')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_refresh_secured_algolia_api_keys_with_nonempty_catalog_scope(
        self,
        mock_get_enterprise_customers_for_user,
        mock_get_and_cache_secured_algolia_search_keys,
        mock_transform_secured_algolia_api_key_response,
    ):
        """Non-empty catalog scopes are sorted and forwarded to the upstream fetch."""
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        mock_get_and_cache_secured_algolia_search_keys.return_value = self.mock_secured_algolia_api_key_response
        mock_transform_secured_algolia_api_key_response.return_value = (
            self.mock_algolia_object['secured_algolia_api_key'],
            self.mock_catalog_uuids_to_catalog_query_uuids,
            self.mock_algolia_object['valid_until'],
        )

        context = HandlerContext(self.request, initialize_secured_algolia_api_keys=False)

        context.refresh_secured_algolia_api_keys(catalog_uuids={'cat-b', 'cat-a'})

        mock_get_and_cache_secured_algolia_search_keys.assert_called_once_with(
            self.request,
            self.mock_enterprise_customer_uuid,
            catalog_uuids=['cat-a', 'cat-b'],
        )
        self.assertEqual(
            context.data['catalog_uuids_to_catalog_query_uuids'],
            self.mock_catalog_uuids_to_catalog_query_uuids,
        )
        self.assertEqual(context.data['algolia'], self.mock_algolia_object)
