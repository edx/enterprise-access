"""
Tests for the Learner Credit spent transactions CSV export endpoint.
"""
import io
import logging
from datetime import date
from unittest import mock
from urllib.parse import quote
from uuid import uuid4

import ddt
import requests
from django.core.cache import cache
from rest_framework import status
from rest_framework.reverse import reverse

from enterprise_access.apps.core.constants import (
    ALL_ACCESS_CONTEXT,
    SYSTEM_ENTERPRISE_ADMIN_ROLE,
    SYSTEM_ENTERPRISE_LEARNER_ROLE,
    SYSTEM_ENTERPRISE_OPERATOR_ROLE
)
from enterprise_access.apps.subsidy_access_policy.exceptions import SubsidyAPIHTTPError
from enterprise_access.apps.subsidy_access_policy.tests.factories import (
    PerLearnerSpendCapLearnerCreditAccessPolicyFactory
)
from test_utils import APITestWithMocks

EXPORT_FUNCTION_PATH = 'enterprise_access.apps.api.v1.views.subsidy_access_policy.get_subsidy_transactions_export'
VIEW_LOGGER_PATH = 'enterprise_access.apps.api.v1.views.subsidy_access_policy.logger'
SUBSIDY_CLIENT_GETTER_PATH = 'enterprise_access.apps.subsidy_access_policy.subsidy_api.get_versioned_subsidy_client'


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

    @mock.patch(VIEW_LOGGER_PATH)
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_exports_csv_and_forwards_filters(self, mock_export, mock_logger):
        """Relays the upstream file and headers (never Content-Length), uncached, even for Accept: text/csv."""
        csv_content = b'email,amount\nlearner@example.com,10\n'
        upstream = self._mock_upstream(mock_export, chunks=[csv_content], headers={
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': 'attachment; filename="upstream.csv"',
            'Content-Length': '10',
        })

        response = self.client.get(self.url, {
            'enterprise_customer_uuid': self.enterprise_uuid,
            'subsidy_uuid': self.subsidy_uuid,
            'subsidy_access_policy_uuid': self.policy.uuid,
            'search': 'learner@example.com',
            'start_date': '2026-01-01',
            'end_date': '2026-01-31',
        }, HTTP_ACCEPT='text/csv')

        assert response.status_code == status.HTTP_200_OK
        assert response['Content-Type'] == 'text/csv; charset=utf-8'
        assert response['Content-Disposition'] == 'attachment; filename="upstream.csv"'
        assert response['Cache-Control'] == 'no-store'
        assert not response.has_header('Content-Length')
        assert b''.join(response.streaming_content) == csv_content
        upstream.close.assert_called_once_with()
        mock_export.assert_called_once_with(
            subsidy_uuid=self.subsidy_uuid,
            enterprise_customer_uuid=self.enterprise_uuid,
            subsidy_access_policy_uuid=self.policy.uuid,
            search='learner@example.com',
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
        audit_message = mock_logger.info.call_args.args[0]
        for expected in (str(self.enterprise_uuid), str(self.subsidy_uuid), str(self.policy.uuid), 'user_id='):
            assert expected in audit_message

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_export_without_policy_covers_whole_subsidy(self, mock_export):
        """Without a policy the whole subsidy is exported, with a fallback filename."""
        self._mock_upstream(mock_export)

        response = self._get()

        assert response.status_code == status.HTTP_200_OK
        assert response['Content-Disposition'] == f'attachment; filename="spent_report_{self.subsidy_uuid}.csv"'
        assert mock_export.call_args.kwargs['subsidy_access_policy_uuid'] is None

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_head_request_does_not_run_an_export(self, mock_export):
        """DRF maps HEAD onto GET, which would run a whole export for a request that discards the body."""
        response = self.client.head(
            self.url,
            {'enterprise_customer_uuid': self.enterprise_uuid, 'subsidy_uuid': self.subsidy_uuid},
        )

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        mock_export.assert_not_called()

    @ddt.data(0, 1)
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_upstream_is_closed_when_download_is_abandoned(self, chunks_read, mock_export):
        """The upstream connection is released even if the client stops before the first chunk."""
        upstream = self._mock_upstream(mock_export, chunks=[b'header\n', b'row 1\n', b'row 2\n'])

        response = self._get()
        content = iter(response.streaming_content)
        for _ in range(chunks_read):
            next(content)
        response.close()

        # ``requests.Response.close()`` is idempotent, so only that it happened matters, not how often.
        assert upstream.close.called

    @ddt.data(
        'policy_of_other_subsidy',
        'policy_of_other_enterprise',
        'nonexistent_policy',
        'subsidy_of_other_enterprise',
        'unknown_subsidy',
    )
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_subsidy_or_policy_outside_the_enterprise_returns_not_found(self, case, mock_export):
        """The subsidy (and policy, if given) must belong to the requested enterprise."""
        other_subsidy_uuid = uuid4()
        if case == 'policy_of_other_subsidy':
            params = {'subsidy_access_policy_uuid': PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
                enterprise_customer_uuid=self.enterprise_uuid, subsidy_uuid=other_subsidy_uuid,
            ).uuid}
        elif case == 'policy_of_other_enterprise':
            params = {'subsidy_access_policy_uuid': PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
                enterprise_customer_uuid=uuid4(), subsidy_uuid=self.subsidy_uuid,
            ).uuid}
        elif case == 'nonexistent_policy':
            params = {'subsidy_access_policy_uuid': uuid4()}
        elif case == 'subsidy_of_other_enterprise':
            PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
                enterprise_customer_uuid=uuid4(), subsidy_uuid=other_subsidy_uuid,
            )
            params = {'subsidy_uuid': other_subsidy_uuid}
        else:
            params = {'subsidy_uuid': other_subsidy_uuid}

        response = self._get(**params)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_export.assert_not_called()

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

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_export_is_rate_limited(self, mock_export):
        """The 13th export within an hour is refused (12/hour)."""
        self._mock_upstream(mock_export)

        statuses = [self._get().status_code for _ in range(13)]

        assert statuses[:12] == [status.HTTP_200_OK] * 12
        assert statuses[12] == status.HTTP_429_TOO_MANY_REQUESTS
        assert mock_export.call_count == 12

    @mock.patch(VIEW_LOGGER_PATH)
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_learner_is_refused_and_logged(self, mock_export, mock_logger):
        """A refused attempt to export learner emails must leave a trace, not fail silently."""
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': self.enterprise_uuid,
        }])

        response = self._get()

        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_logger.warning.assert_called_once()
        assert 'refused' in mock_logger.warning.call_args.args[0]
        mock_export.assert_not_called()

    def test_unauthenticated_request_returns_unauthorized(self):
        self.client.cookies.clear()

        response = self._get()

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_admin_of_another_enterprise_is_forbidden(self, mock_export):
        """An admin can't export another enterprise's spend by passing its uuids."""
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

    @ddt.data(400, 403, 500, 503, 504, None)
    @mock.patch(EXPORT_FUNCTION_PATH)
    def test_upstream_failure_returns_bad_gateway_without_upstream_body(self, upstream_status_code, mock_export):
        """Any upstream failure (None: transport error) is a 502 without the upstream body."""
        if upstream_status_code is None:
            mock_export.side_effect = SubsidyAPIHTTPError('downstream failure')
            mock_export.side_effect.__cause__ = requests.Timeout()
        else:
            mock_export.side_effect = _subsidy_api_error(upstream_status_code)

        response = self._get()

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {'detail': 'Failed to export transactions from the Subsidy API.'}

    @ddt.data('error_status', 'connection_error')
    @mock.patch(SUBSIDY_CLIENT_GETTER_PATH)
    def test_upstream_failure_logs_never_contain_search(self, failure, mock_client_getter):
        """Upstream-failure logs, tracebacks included, never contain the search value."""
        search = 'learner@example.com'
        mock_client = mock_client_getter.return_value
        mock_client.TRANSACTIONS_LIST_ENDPOINT = 'http://subsidy/api/v2/subsidies/{subsidy_uuid}/admin/transactions/'
        upstream_url = requests.Request(
            'GET',
            mock_client.TRANSACTIONS_LIST_ENDPOINT.format(subsidy_uuid=self.subsidy_uuid) + 'export/',
            params={'search': search},
        ).prepare().url
        if failure == 'error_status':
            # A real Response, so the message comes from requests' own raise_for_status().
            upstream_response = requests.Response()
            upstream_response.status_code = 503
            upstream_response.url = upstream_url
            upstream_response.raw = io.BytesIO()
            mock_client.client.get.return_value = upstream_response
        else:
            mock_client.client.get.side_effect = requests.ConnectionError(
                f'Max retries exceeded with url: {upstream_url}'
            )

        with self.assertLogs('enterprise_access', level=logging.INFO) as logs:
            response = self._get(search=search)

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        logged = '\n'.join(logging.Formatter().format(record) for record in logs.records)
        assert 'export failed upstream' in logged
        assert search not in logged
        assert quote(search) not in logged
