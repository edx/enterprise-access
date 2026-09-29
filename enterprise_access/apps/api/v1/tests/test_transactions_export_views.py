"""
Tests for the Learner Credit spent transactions CSV export endpoint.
"""
from unittest import mock
from uuid import uuid4

import ddt
import requests
from rest_framework import status
from rest_framework.reverse import reverse

from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_ADMIN_ROLE, SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.subsidy_access_policy.exceptions import SubsidyAPIHTTPError
from enterprise_access.apps.subsidy_access_policy.subsidy_api import get_subsidy_transactions_export
from enterprise_access.apps.subsidy_access_policy.tests.factories import (
    PerLearnerSpendCapLearnerCreditAccessPolicyFactory
)
from test_utils import APITestWithMocks


@ddt.ddt
class TestTransactionsExportView(APITestWithMocks):
    """Tests for the transactions export gateway endpoint."""

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = uuid4()
        self.subsidy_uuid = uuid4()
        self.policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
            enterprise_customer_uuid=self.enterprise_uuid,
            subsidy_uuid=self.subsidy_uuid,
        )
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
            'subsidy_access_policy_uuid': self.policy.uuid,
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
        mock_export.return_value.close.assert_called_once_with()
        mock_export.assert_called_once_with(
            subsidy_uuid=self.subsidy_uuid,
            enterprise_customer_uuid=self.enterprise_uuid,
            subsidy_access_policy_uuid=self.policy.uuid,
            search='learner@example.com',
            start_date='2026-01-01',
            end_date='2026-01-31',
        )

    @mock.patch('enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export')
    def test_export_without_policy_covers_whole_subsidy(self, mock_export):
        """Omitting subsidy_access_policy_uuid exports spend across every budget funded by the subsidy."""
        mock_export.return_value.headers = {}
        mock_export.return_value.iter_content.return_value = [b'header\n']

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
        })

        assert response.status_code == status.HTTP_200_OK
        assert mock_export.call_args.kwargs['subsidy_access_policy_uuid'] is None

    @ddt.data('other_subsidy_same_enterprise', 'same_subsidy_other_enterprise', 'nonexistent')
    @mock.patch('enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export')
    def test_policy_not_belonging_to_enterprise_and_subsidy_returns_not_found(self, policy_case, mock_export):
        """A requested budget must belong to both the requested enterprise and subsidy."""
        policy_uuid = {
            'other_subsidy_same_enterprise': lambda: PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
                enterprise_customer_uuid=self.enterprise_uuid,
                subsidy_uuid=uuid4(),
            ).uuid,
            'same_subsidy_other_enterprise': lambda: PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
                enterprise_customer_uuid=uuid4(),
                subsidy_uuid=self.subsidy_uuid,
            ).uuid,
            'nonexistent': uuid4,
        }[policy_case]()

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
            'subsidy_access_policy_uuid': policy_uuid,
        })

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_export.assert_not_called()

    @mock.patch('enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export')
    def test_aborted_download_closes_upstream_response(self, mock_export):
        """If the client stops reading partway through, the upstream Subsidy API connection is still released."""
        mock_export.return_value.headers = {}
        mock_export.return_value.iter_content.return_value = iter([b'header\n', b'row 1\n', b'row 2\n'])

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
        })
        assert next(iter(response.streaming_content)) == b'header\n'
        mock_export.return_value.close.assert_not_called()

        # Django closes the response (and with it the streaming generator) when the client disconnects.
        response.close()

        mock_export.return_value.close.assert_called_once_with()

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
    def test_subsidy_of_another_enterprise_returns_not_found(self, mock_export):
        """
        An admin of this enterprise must not be able to export a subsidy owned by a different enterprise,
        even though the Subsidy API is called with this service's all-access credentials.
        """
        other_enterprise_subsidy_uuid = uuid4()
        PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
            enterprise_customer_uuid=uuid4(),
            subsidy_uuid=other_enterprise_subsidy_uuid,
        )

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': other_enterprise_subsidy_uuid,
        })

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_export.assert_not_called()

    @mock.patch('enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export')
    def test_unknown_subsidy_returns_not_found(self, mock_export):
        """A subsidy with no policy under the requested enterprise returns a 404 without calling the Subsidy API."""
        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': uuid4(),
        })

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_export.assert_not_called()

    @ddt.data(
        {'active': True, 'retired': False},
        {'active': False, 'retired': False},
        {'active': True, 'retired': True},
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export')
    def test_inactive_or_retired_policy_still_allows_export(self, mock_export, active, retired):
        """Admins can still download historical spend for budgets whose policy is inactive or retired."""
        subsidy_uuid = uuid4()
        PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
            enterprise_customer_uuid=self.enterprise_uuid,
            subsidy_uuid=subsidy_uuid,
            active=active,
            retired=retired,
        )
        mock_export.return_value.headers = {}
        mock_export.return_value.iter_content.return_value = [b'header\n']

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': subsidy_uuid,
        })

        assert response.status_code == status.HTTP_200_OK
        mock_export.assert_called_once()

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

    @mock.patch('enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export')
    def test_downstream_non_json_error_returns_bad_gateway(self, mock_export):
        """A non-JSON error body from enterprise-subsidy (e.g. an HTML error page from a proxy) shouldn't 500."""
        downstream_error = requests.HTTPError()
        downstream_error.response = mock.Mock(status_code=504)
        downstream_error.response.json.side_effect = ValueError('not valid JSON')
        downstream_error.response.text = '<html><body>504 Gateway Time-out</body></html>'
        wrapped_error = SubsidyAPIHTTPError('downstream failure')
        wrapped_error.__cause__ = downstream_error
        mock_export.side_effect = wrapped_error

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
        })

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.data['detail'] == downstream_error.response.text
        assert str(response.data['subsidy_status_code']) == '504'


class TestTransactionsExportClient(APITestWithMocks):
    """Tests for the enterprise-subsidy export wrapper."""

    @mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.get_versioned_subsidy_client')
    def test_export_wrapper_builds_request(self, mock_client_getter):
        subsidy_uuid = uuid4()
        policy_uuid = uuid4()
        response = mock_client_getter.return_value.client.get.return_value

        result = get_subsidy_transactions_export(
            subsidy_uuid,
            enterprise_customer_uuid='enterprise-uuid',
            subsidy_access_policy_uuid=policy_uuid,
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
                'subsidy_access_policy_uuid': str(policy_uuid),
                'search': 'learner',
                'start_date': '2026-01-01',
                'end_date': '2026-01-31',
            },
            stream=True,
        )
        response.raise_for_status.assert_called_once_with()

    @mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.get_versioned_subsidy_client')
    def test_export_wrapper_converts_transport_error(self, mock_client_getter):
        mock_client_getter.return_value.client.get.side_effect = requests.Timeout()

        with self.assertRaises(SubsidyAPIHTTPError):
            get_subsidy_transactions_export(uuid4(), enterprise_customer_uuid=uuid4())
