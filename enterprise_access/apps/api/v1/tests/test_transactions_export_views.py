"""
Tests for the Learner Credit spent transactions CSV export endpoint.
"""
from unittest import mock
from uuid import uuid4

import requests
from rest_framework import status
from rest_framework.reverse import reverse

from enterprise_access.apps.core.constants import (
    SYSTEM_ENTERPRISE_ADMIN_ROLE,
    SYSTEM_ENTERPRISE_LEARNER_ROLE,
)
from enterprise_access.apps.subsidy_access_policy.exceptions import SubsidyAPIHTTPError
from enterprise_access.apps.subsidy_access_policy.subsidy_api import get_subsidy_transactions_export
from test_utils import APITestWithMocks


class TestTransactionsExportView(APITestWithMocks):
    """Tests for the transactions export gateway endpoint."""

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = uuid4()
        self.subsidy_uuid = uuid4()
        self.url = reverse('api:v1:transactions-export')
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_ADMIN_ROLE,
            'context': self.enterprise_uuid,
        }])

    @mock.patch('enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export')
    def test_exports_csv_and_forwards_filters(self, mock_export):
        csv_content = b'email,amount\nlearner@example.com,10\n'
        mock_export.return_value.headers = {}
        mock_export.return_value.iter_content.return_value = [csv_content]

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
            'search': 'learner@example.com',
            'start_date': '2026-01-01',
            'end_date': '2026-01-31',
        })

        assert response.status_code == status.HTTP_200_OK
        assert response['Content-Type'] == 'text/csv'
        assert response['Content-Disposition'] == (
            f'attachment; filename="spent_report_{self.subsidy_uuid}.csv"'
        )
        assert b''.join(response.streaming_content) == csv_content
        mock_export.assert_called_once_with(
            subsidy_uuid=self.subsidy_uuid,
            enterprise_customer_uuid=self.enterprise_uuid,
            search='learner@example.com',
            start_date='2026-01-01',
            end_date='2026-01-31',
        )

    def test_missing_required_parameters_returns_bad_request(self):
        response = self.client.get(self.url, {'subsidy_uuid': self.subsidy_uuid})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'enterprise_customer_uuid' in response.data

    def test_unauthenticated_request_returns_unauthorized(self):
        self.client.cookies.clear()

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
        })

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_user_without_enterprise_admin_access_is_forbidden(self):
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }])

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
        })

        assert response.status_code == status.HTTP_403_FORBIDDEN

    @mock.patch('enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export')
    def test_downstream_error_returns_bad_gateway(self, mock_export):
        downstream_error = requests.HTTPError()
        downstream_error.response = mock.Mock(status_code=503)
        downstream_error.response.json.return_value = {'detail': 'unavailable'}
        wrapped_error = SubsidyAPIHTTPError('downstream failure')
        wrapped_error.__cause__ = downstream_error
        mock_export.side_effect = wrapped_error

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
        })

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert str(response.data['subsidy_status_code']) == '503'


class TestTransactionsExportClient(APITestWithMocks):
    """Tests for the enterprise-subsidy export wrapper."""

    @mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.get_versioned_subsidy_client')
    def test_export_wrapper_builds_request(self, mock_client_getter):
        subsidy_uuid = uuid4()
        response = mock_client_getter.return_value.client.get.return_value

        result = get_subsidy_transactions_export(
            subsidy_uuid,
            enterprise_customer_uuid='enterprise-uuid',
            search='learner',
            start_date='2026-01-01',
            end_date='2026-01-31',
        )

        assert result is response
        mock_client_getter.return_value.client.get.assert_called_once_with(
            mock_client_getter.return_value.TRANSACTIONS_ENDPOINT + 'export/',
            params={
                'subsidy_uuid': str(subsidy_uuid),
                'enterprise_customer_uuid': 'enterprise-uuid',
                'search': 'learner',
                'start_date': '2026-01-01',
                'end_date': '2026-01-31',
            },
        )
        response.raise_for_status.assert_called_once_with()

    @mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.get_versioned_subsidy_client')
    def test_export_wrapper_converts_transport_error(self, mock_client_getter):
        mock_client_getter.return_value.client.get.side_effect = requests.Timeout()

        with self.assertRaises(SubsidyAPIHTTPError):
            get_subsidy_transactions_export(uuid4())
