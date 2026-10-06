"""
Tests for the Learner Credit spent transactions CSV export endpoint.
"""
from unittest import mock
from uuid import uuid4

import ddt
import requests
from django.core.cache import cache
from django.test import TestCase
from rest_framework import status
from rest_framework.reverse import reverse

from enterprise_access.apps.core import constants
from enterprise_access.apps.core.constants import (
    ALL_ACCESS_CONTEXT,
    SYSTEM_ENTERPRISE_ADMIN_ROLE,
    SYSTEM_ENTERPRISE_LEARNER_ROLE,
    SYSTEM_ENTERPRISE_OPERATOR_ROLE
)
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.subsidy_access_policy.exceptions import SubsidyAPIHTTPError
from enterprise_access.apps.subsidy_access_policy.tests.factories import (
    PerLearnerSpendCapLearnerCreditAccessPolicyFactory
)
from test_utils import APITestWithMocks

EXPORT_FUNCTION_PATH = 'enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export'
VIEW_LOGGER_PATH = 'enterprise_access.apps.api.v1.views.subsidy_access_policy.logger'


def _subsidy_api_error(status_code, body='<html><body>Internal permission subsidy.can_read</body></html>'):
    """
    Build the SubsidyAPIHTTPError that ``get_subsidy_transactions_export`` raises for a non-2xx upstream response.
    """
    downstream_error = requests.HTTPError()
    downstream_error.response = mock.Mock(status_code=status_code, text=body)
    wrapped_error = SubsidyAPIHTTPError('downstream failure')
    wrapped_error.__cause__ = downstream_error
    return wrapped_error


@ddt.ddt
class TestTransactionsExportView(APITestWithMocks):
    """Tests for the transactions export gateway endpoint."""

    def setUp(self):
        super().setUp()
        # The export is rate limited and DRF counts requests in the default cache, which otherwise persists
        # between test methods.
        cache.clear()
        self.addCleanup(cache.clear)
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

    def _mock_upstream(self, mock_export, chunks=(b'header\n',), headers=None):
        mock_export.return_value.headers = headers if headers is not None else {}
        mock_export.return_value.iter_content.return_value = iter(chunks)
        return mock_export.return_value

    def _get(self, **params):
        """GET the export endpoint for this enterprise and subsidy; ``None``-valued params are dropped."""
        query = {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
            **params,
        }
        return self.client.get(self.url, {key: value for key, value in query.items() if value is not None})

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_exports_csv_and_forwards_filters(self, mock_export):
        csv_content = b'email,amount\nlearner@example.com,10\n'
        upstream = self._mock_upstream(mock_export, chunks=[csv_content])

        response = self._get(
            subsidy_access_policy_uuid=self.policy.uuid,
            search='learner@example.com',
            start_date='2026-01-01',
            end_date='2026-01-31',
        )

        assert response.status_code == status.HTTP_200_OK
        assert response['Content-Type'] == 'text/csv; charset=utf-8'
        assert response['Content-Disposition'] == (
            f'attachment; filename="spent_report_{self.subsidy_uuid}.csv"'
        )
        assert not response.has_header('Content-Length')
        assert b''.join(response.streaming_content) == csv_content
        upstream.close.assert_called_once_with()
        mock_export.assert_called_once_with(
            subsidy_uuid=self.subsidy_uuid,
            enterprise_customer_uuid=self.enterprise_uuid,
            subsidy_access_policy_uuid=self.policy.uuid,
            search='learner@example.com',
            start_date=mock.ANY,
            end_date=mock.ANY,
        )
        assert mock_export.call_args.kwargs['start_date'].isoformat() == '2026-01-01'
        assert mock_export.call_args.kwargs['end_date'].isoformat() == '2026-01-31'

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_upstream_headers_are_passed_through(self, mock_export):
        """The upstream filename and charset are relayed to the client."""
        self._mock_upstream(mock_export, chunks=[b'0123456789'], headers={
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': 'attachment; filename="upstream.csv"',
        })

        response = self._get()

        assert response['Content-Type'] == 'text/csv; charset=utf-8'
        assert response['Content-Disposition'] == 'attachment; filename="upstream.csv"'

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_content_length_is_never_forwarded(self, mock_export):
        """
        The upstream streams its response and never sends Content-Length, and ``iter_content()`` would
        decompress a compressed body anyway, so relaying a length could only ever be wrong.
        """
        self._mock_upstream(mock_export, headers={'Content-Length': '10'})

        response = self._get()

        assert response.status_code == status.HTTP_200_OK
        assert not response.has_header('Content-Length')

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_download_is_not_cached(self, mock_export):
        """The report contains learner emails, so it must not sit in a shared or browser cache."""
        self._mock_upstream(mock_export)

        response = self._get()

        assert response['Cache-Control'] == 'no-store'

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_accept_text_csv_is_not_rejected(self, mock_export):
        """A download client asking for text/csv must not be refused by content negotiation."""
        self._mock_upstream(mock_export)

        response = self.client.get(
            self.url,
            {'enterprise_customer_uuid': self.enterprise_uuid, 'subsidy_uuid': self.subsidy_uuid},
            HTTP_ACCEPT='text/csv',
        )

        assert response.status_code == status.HTTP_200_OK

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_head_request_does_not_run_an_export(self, mock_export):
        """DRF maps HEAD onto GET, which would run a whole export for a request that discards the body."""
        response = self.client.head(
            self.url,
            {'enterprise_customer_uuid': self.enterprise_uuid, 'subsidy_uuid': self.subsidy_uuid},
        )

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        mock_export.assert_not_called()

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_upstream_is_closed_even_if_the_response_is_never_read(self, mock_export):
        """
        A generator's ``finally`` only runs once iteration has started, so the upstream response would leak if
        the response were discarded before its first chunk.
        """
        upstream = self._mock_upstream(mock_export, chunks=[b'header\n', b'row\n'])

        response = self._get()
        response.close()

        upstream.close.assert_called_once_with()

    @mock.patch(VIEW_LOGGER_PATH)
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_export_is_audit_logged(self, mock_export, mock_logger):
        self._mock_upstream(mock_export)

        self._get(subsidy_access_policy_uuid=self.policy.uuid)

        mock_logger.info.assert_called_once()
        audit_message = mock_logger.info.call_args.args[0]
        for expected in (str(self.enterprise_uuid), str(self.subsidy_uuid), str(self.policy.uuid), 'user_id='):
            assert expected in audit_message

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_export_without_policy_covers_whole_subsidy(self, mock_export):
        """Omitting subsidy_access_policy_uuid exports spend across every budget funded by the subsidy."""
        self._mock_upstream(mock_export)

        response = self._get()

        assert response.status_code == status.HTTP_200_OK
        assert mock_export.call_args.kwargs['subsidy_access_policy_uuid'] is None

    @ddt.data('other_subsidy_same_enterprise', 'same_subsidy_other_enterprise', 'nonexistent')
    @mock.patch(EXPORT_FUNCTION_PATH)
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

        response = self._get(subsidy_access_policy_uuid=policy_uuid)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_export.assert_not_called()

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_aborted_download_closes_upstream_response(self, mock_export):
        """If the client stops reading partway through, the upstream Subsidy API connection is still released."""
        upstream = self._mock_upstream(mock_export, chunks=[b'header\n', b'row 1\n', b'row 2\n'])

        response = self._get()
        assert next(iter(response.streaming_content)) == b'header\n'

        # Django closes the response (and with it the upstream stream) when the client disconnects.
        response.close()

        # ``requests.Response.close()`` is idempotent, so only that it happened matters, not how often.
        assert upstream.close.called

    @mock.patch(VIEW_LOGGER_PATH)
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_mid_stream_failure_is_logged_and_aborts_download(self, mock_export, mock_logger):
        """A failure after the headers are sent must not silently end the download as if the report were complete."""

        def failing_chunks():
            yield b'header\n'
            raise requests.exceptions.ChunkedEncodingError('connection broken')

        upstream = self._mock_upstream(mock_export, chunks=failing_chunks())

        response = self._get()
        content = iter(response.streaming_content)
        assert next(content) == b'header\n'
        with self.assertRaises(requests.exceptions.ChunkedEncodingError):
            next(content)

        mock_logger.exception.assert_called_once()
        assert str(self.subsidy_uuid) in mock_logger.exception.call_args.args[0]
        upstream.close.assert_called_once_with()

    @ddt.data(
        {'enterprise_customer_uuid': None},
        {'subsidy_uuid': None},
        {'enterprise_customer_uuid': 'not-a-uuid'},
        {'subsidy_uuid': 'abc'},
        {'subsidy_access_policy_uuid': 'abc'},
        {'start_date': 'not-a-date'},
        {'start_date': 'x' * 5000},
        {'search': 'x' * 321},
        {'end_date': '2024-02-30'},
        {'end_date': '2024-01-10T15:30:00'},
        {'start_date': '2024-02-01', 'end_date': '2024-01-31'},
    )
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_invalid_parameters_return_bad_request(self, params, mock_export):
        """Bad input is the client's error (400), and is never forwarded to the Subsidy API."""
        response = self._get(**params)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        mock_export.assert_not_called()

    def test_unauthenticated_request_returns_unauthorized(self):
        self.client.cookies.clear()

        response = self._get()

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_learner_is_forbidden(self, mock_export):
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }])

        response = self._get()

        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_export.assert_not_called()

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_admin_of_another_enterprise_is_forbidden(self, mock_export):
        """
        Tenant boundary: an admin of enterprise A can't export enterprise B's spend by passing B's uuid, even though
        B's subsidy and policy really exist and belong together.
        """
        other_enterprise_uuid = uuid4()
        other_subsidy_uuid = uuid4()
        PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
            enterprise_customer_uuid=other_enterprise_uuid,
            subsidy_uuid=other_subsidy_uuid,
        )

        response = self._get(enterprise_customer_uuid=other_enterprise_uuid, subsidy_uuid=other_subsidy_uuid)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_export.assert_not_called()

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_operator_can_export(self, mock_export):
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_OPERATOR_ROLE,
            'context': ALL_ACCESS_CONTEXT,
        }])
        self._mock_upstream(mock_export)

        response = self._get()

        assert response.status_code == status.HTTP_200_OK
        mock_export.assert_called_once()

    @mock.patch(EXPORT_FUNCTION_PATH)
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

        response = self._get(subsidy_uuid=other_enterprise_subsidy_uuid)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_export.assert_not_called()

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_unknown_subsidy_returns_not_found(self, mock_export):
        """A subsidy with no policy under the requested enterprise returns a 404 without calling the Subsidy API."""
        response = self._get(subsidy_uuid=uuid4())

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_export.assert_not_called()

    @ddt.data(
        {'active': True, 'retired': False},
        {'active': False, 'retired': False},
        {'active': True, 'retired': True},
    )
    @ddt.unpack
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_inactive_or_retired_policy_still_allows_export(self, mock_export, active, retired):
        """Admins can still download historical spend for budgets whose policy is inactive or retired."""
        subsidy_uuid = uuid4()
        PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
            enterprise_customer_uuid=self.enterprise_uuid,
            subsidy_uuid=subsidy_uuid,
            active=active,
            retired=retired,
        )
        self._mock_upstream(mock_export)

        response = self._get(subsidy_uuid=subsidy_uuid)

        assert response.status_code == status.HTTP_200_OK
        mock_export.assert_called_once()

    @ddt.data(400, 403, 500, 503, 504)
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_upstream_error_returns_bad_gateway_without_upstream_body(self, upstream_status_code, mock_export):
        """
        Params are validated before the upstream call, so any upstream failure is a gateway error. The upstream body
        (which may be an HTML error page or name internal permissions) is never passed through to the client.
        """
        mock_export.side_effect = _subsidy_api_error(upstream_status_code)

        response = self._get()

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {'detail': 'Failed to export transactions from the Subsidy API.'}

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_upstream_transport_error_returns_bad_gateway(self, mock_export):
        wrapped_error = SubsidyAPIHTTPError('downstream failure')
        wrapped_error.__cause__ = requests.Timeout()
        mock_export.side_effect = wrapped_error

        response = self._get()

        assert response.status_code == status.HTTP_502_BAD_GATEWAY


@ddt.ddt
class TestTransactionsExportPermission(TestCase):
    """
    Tests who is granted SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION via explicit (database) role assignments.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = uuid4()
        self.user = UserFactory()

    def _assign_role(self, role_name, enterprise_uuid):
        role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(name=role_name)
        EnterpriseAccessRoleAssignment.objects.create(
            user=self.user,
            role=role,
            enterprise_customer_uuid=enterprise_uuid,
        )

    @ddt.data(
        (constants.CONTENT_ASSIGNMENTS_ADMIN_ROLE, True),
        (constants.CONTENT_ASSIGNMENTS_OPERATOR_ROLE, True),
        (constants.SUBSIDY_ACCESS_POLICY_OPERATOR_ROLE, True),
        # The Browse & Request admin role alone must not grant access to learner spend PII.
        (constants.REQUESTS_ADMIN_ROLE, False),
        (constants.SUBSIDY_ACCESS_POLICY_LEARNER_ROLE, False),
        (constants.CONTENT_ASSIGNMENTS_LEARNER_ROLE, False),
    )
    @ddt.unpack
    def test_explicit_role_grants(self, role_name, expected_access):
        self._assign_role(role_name, self.enterprise_uuid)

        assert self.user.has_perm(
            constants.SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION,
            str(self.enterprise_uuid),
        ) is expected_access

    def test_role_for_another_enterprise_does_not_grant_access(self):
        self._assign_role(constants.CONTENT_ASSIGNMENTS_ADMIN_ROLE, uuid4())

        assert not self.user.has_perm(
            constants.SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION,
            str(self.enterprise_uuid),
        )
