"""
Text for the BFF context
"""
import ddt
from django.test import RequestFactory, TestCase
from faker import Faker
from rest_framework.exceptions import ValidationError

from enterprise_access.apps.bffs.context import HandlerContext
from enterprise_access.apps.core.tests.factories import UserFactory


@ddt.ddt
class TestHandlerContext(TestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.mock_user = UserFactory()
        self.faker = Faker()

        self.mock_enterprise_customer_uuid = self.faker.uuid4()
        self.request = self.factory.get('sample/api/call')
        self.request.user = self.mock_user
        self.request.query_params = {
            'enterprise_customer_uuid': self.mock_enterprise_customer_uuid
        }
        self.request.data = {
            'enterprise_customer_uuid': self.mock_enterprise_customer_uuid
        }
        self.context = HandlerContext(self.request)

    def test_handler_context_init(self):
        context = HandlerContext(self.request)

        self.assertEqual(context.request, self.request)
        self.assertEqual(context.user, self.mock_user)
        self.assertEqual(context.data, {})
        self.assertEqual(context.errors, [])
        self.assertEqual(context.warnings, [])
        self.assertEqual(context.enterprise_customer_uuid, self.mock_enterprise_customer_uuid)
        self.assertEqual(context.lms_user_id, self.mock_user.lms_user_id)

    @ddt.data(
        {
            'query_params': True,
            'data': True,
        },
        {
            'query_params': False,
            'data': True,
        },
        {
            'query_params': True,
            'data': False,
        },
        {
            'query_params': False,
            'data': False,
        }
    )
    @ddt.unpack
    def test_handler_context_enterprise_customer_uuid(self, query_params, data):
        if not query_params:
            del self.request.query_params['enterprise_customer_uuid']
        if not data:
            del self.request.data['enterprise_customer_uuid']
        request = self.request
        if not (query_params or data):
            with self.assertRaises(ValueError):
                HandlerContext(request)
        else:
            context = HandlerContext(request)
            self.assertEqual(context.enterprise_customer_uuid, self.mock_enterprise_customer_uuid)
            self.assertEqual(context.lms_user_id, self.mock_user.lms_user_id)

    def test_handler_context_add_error_serializer(self):
        context = self.context
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

    def test_handler_context_add_error_serializer_is_valid(self):
        context = self.context
        malformed_output = {
            "developer_message": "No enterprise uuid associated to the user mock-id",
        }
        with self.assertRaises(ValidationError):
            context.add_error(**malformed_output)

    def test_handler_context_add_warning_serializer(self):
        context = self.context
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

    def test_handler_context_add_warning_serializer_is_valid(self):
        context = self.context
        malformed_output = {
            "user_message": "The data received might be out-dated",
        }
        with self.assertRaises(ValidationError):
            context.add_error(**malformed_output)
