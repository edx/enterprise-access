"""
Tests for the subsidy_api module.
"""
import uuid
from datetime import date
from unittest import mock

import ddt
import requests
from django.test import TestCase, override_settings

from ..exceptions import SubsidyAPIHTTPError
from ..subsidy_api import (
    get_and_cache_transactions_for_learner,
    get_redemptions_by_content_and_policy_for_learner,
    get_subsidy_transactions_export
)
from .factories import PerLearnerSpendCapLearnerCreditAccessPolicyFactory


class TransactionsForLearnerTests(TestCase):
    """
    Tests the ``get_and_cache_transactions_for_learner`` function.
    """
    @mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.get_versioned_subsidy_client')
    def test_request_caching_works(self, mock_client_getter):
        """
        Test that we utilize the request cache.
        """
        response_payload = {
            'next': None,
            'previous': None,
            'count': 1,
            'results': [{'thing': 3}],
        }
        mock_client = mock_client_getter.return_value
        mock_client.list_subsidy_transactions.return_value = response_payload

        subsidy_uuid = uuid.uuid4()
        lms_user_id = 42

        result = get_and_cache_transactions_for_learner(subsidy_uuid, lms_user_id)

        expected_result = {
            'transactions': [{'thing': 3}],
            'aggregates': {},
        }
        self.assertEqual(result, expected_result)

        # call it again, should be using the cache this time
        next_result = get_and_cache_transactions_for_learner(subsidy_uuid, lms_user_id)

        self.assertEqual(next_result, expected_result)

        # we should only have used the client in the first call
        mock_client.list_subsidy_transactions.assert_called_once_with(
            subsidy_uuid=subsidy_uuid,
            lms_user_id=lms_user_id,
            include_aggregates=False,
        )
        # no pagination happened here
        self.assertFalse(mock_client.client.get.called)

    @mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.get_versioned_subsidy_client')
    def test_multiple_pages_are_traversed(self, mock_client_getter):
        """
        Test that we read multiple pages of data, if present
        in the client's response for ``list_subsidy_transactions()``.
        """
        first_response_payload = {
            'next': 'http://the.next.page',
            'previous': None,
            'count': 12,
            'results': [{'thing': 1}, {'thing': 2}],
        }
        second_response_payload = {
            'next': None,
            'previous': None,
            'count': 3,
            'results': [{'thing': 3}],
        }
        mock_client = mock_client_getter.return_value
        mock_client.list_subsidy_transactions.return_value = first_response_payload
        mock_second_response = mock.Mock()
        mock_second_response.json.return_value = second_response_payload
        mock_client.client.get.return_value = mock_second_response

        subsidy_uuid = uuid.uuid4()
        lms_user_id = 42

        result = get_and_cache_transactions_for_learner(subsidy_uuid, lms_user_id)

        expected_result = {
            'transactions': [{'thing': 1}, {'thing': 2}, {'thing': 3}],
            'aggregates': {},
        }
        self.assertEqual(result, expected_result)
        mock_client.list_subsidy_transactions.assert_called_once_with(
            subsidy_uuid=subsidy_uuid,
            lms_user_id=lms_user_id,
            include_aggregates=False,
        )
        mock_client.client.get.assert_called_once_with(first_response_payload['next'])

    @mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.get_and_cache_transactions_for_learner')
    def test_redemptions_by_content_and_policy(self, mock_transaction_cache):
        cake_subsidy_uuid = uuid.uuid4()
        pie_subsidy_uuid = uuid.uuid4()

        cherry_policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory(subsidy_uuid=pie_subsidy_uuid)
        apple_policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory(subsidy_uuid=pie_subsidy_uuid)

        german_chocolate_policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory(subsidy_uuid=cake_subsidy_uuid)

        # The transaction uuids and content keys don't really matter much here, they don't even need
        # to be proper uuids, just unique amongst this list of test data.
        mock_pie_transactions = [
            {
                'uuid': 'alpha',
                'content_key': 'content-1',
                'subsidy_access_policy_uuid': str(cherry_policy.uuid),
            },
            {
                'uuid': 'beta',
                'content_key': 'content-2',
                'subsidy_access_policy_uuid': str(apple_policy.uuid),
            },
        ]
        mock_cake_transactions = [
            {
                'uuid': 'delta',
                'content_key': 'content-3',
                'subsidy_access_policy_uuid': str(german_chocolate_policy.uuid),
            },
            # Add some unmatched policy uuid in here,
            # which we'll later verify is omitted from the mapping.
            {
                'uuid': 'epsilon',
                'content_key': 'content-4',
                'subsidy_access_policy_uuid': str(uuid.uuid4()),
            },
        ]

        mock_transaction_cache.side_effect = [
            {'transactions': mock_pie_transactions, 'aggregates': {}},
            {'transactions': mock_cake_transactions, 'aggregates': {}},
        ]

        result = get_redemptions_by_content_and_policy_for_learner(
            [cherry_policy, apple_policy, german_chocolate_policy],
            123,
        )

        self.assertEqual(
            {
                'content-1': {cherry_policy: [mock_pie_transactions[0]]},
                'content-2': {apple_policy: [mock_pie_transactions[1]]},
                'content-3': {german_chocolate_policy: [mock_cake_transactions[0]]},
            },
            result,
        )


@ddt.ddt
@override_settings(SUBSIDY_TRANSACTIONS_EXPORT_TIMEOUT=(1, 2))
@mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.get_versioned_subsidy_client')
class TransactionsExportTests(TestCase):
    """
    Tests the ``get_subsidy_transactions_export`` function.
    """
    LIST_ENDPOINT = 'http://subsidy/api/v2/subsidies/{subsidy_uuid}/admin/transactions/'

    def _mock_client(self, mock_client_getter):
        mock_client = mock_client_getter.return_value
        mock_client.TRANSACTIONS_LIST_ENDPOINT = self.LIST_ENDPOINT
        return mock_client

    def test_builds_streamed_request_with_timeout(self, mock_client_getter):
        subsidy_uuid = uuid.uuid4()
        policy_uuid = uuid.uuid4()
        mock_client = self._mock_client(mock_client_getter)
        response = mock_client.client.get.return_value

        result = get_subsidy_transactions_export(
            subsidy_uuid=subsidy_uuid,
            enterprise_customer_uuid='enterprise-uuid',
            subsidy_access_policy_uuid=policy_uuid,
            search='learner',
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )

        assert result is response
        mock_client_getter.assert_called_once_with(version=2)
        mock_client.client.get.assert_called_once_with(
            f'http://subsidy/api/v2/subsidies/{subsidy_uuid}/admin/transactions/export/',
            params={
                'enterprise_customer_uuid': 'enterprise-uuid',
                'subsidy_access_policy_uuid': str(policy_uuid),
                'search': 'learner',
                'start_date': '2026-01-01',
                'end_date': '2026-01-31',
            },
            stream=True,
            timeout=(1, 2),
        )
        response.raise_for_status.assert_called_once_with()
        response.close.assert_not_called()

    def test_optional_filters_are_omitted(self, mock_client_getter):
        mock_client = self._mock_client(mock_client_getter)
        subsidy_uuid = uuid.uuid4()

        get_subsidy_transactions_export(subsidy_uuid=subsidy_uuid, enterprise_customer_uuid='enterprise-uuid')

        assert mock_client.client.get.call_args.kwargs['params'] == {
            'enterprise_customer_uuid': 'enterprise-uuid',
        }

    @ddt.data(
        (120, 120),      # the production shape: a plain int from the setting
        ([1, 2], (1, 2)),  # YAML config can only express a pair as a list
        ((1, 2), (1, 2)),
    )
    @ddt.unpack
    def test_timeout_is_normalised_for_requests(self, configured, expected, mock_client_getter):
        """
        ``requests`` rejects a list timeout with a bare ValueError rather than a RequestException, so a value
        overridden in YAML config would otherwise escape the handler and turn every export into a 500.
        """
        mock_client = self._mock_client(mock_client_getter)

        with override_settings(SUBSIDY_TRANSACTIONS_EXPORT_TIMEOUT=configured):
            get_subsidy_transactions_export(subsidy_uuid=uuid.uuid4(), enterprise_customer_uuid=uuid.uuid4())

        assert mock_client.client.get.call_args.kwargs['timeout'] == expected

    @mock.patch('enterprise_access.apps.subsidy_access_policy.subsidy_api.logger')
    def test_search_value_never_reaches_the_logs(self, mock_logger, mock_client_getter):
        """
        Upstream matches ``search`` against learner emails, so admins type addresses into it. It must not be
        logged, even when the request fails.
        """
        self._mock_client(mock_client_getter).client.get.side_effect = requests.Timeout()

        with self.assertRaises(SubsidyAPIHTTPError):
            get_subsidy_transactions_export(
                subsidy_uuid=uuid.uuid4(),
                enterprise_customer_uuid=uuid.uuid4(),
                search='learner@example.com',
            )

        logged = ' '.join(str(arg) for call in mock_logger.mock_calls for arg in call.args)
        assert 'learner@example.com' not in logged

    def test_transport_error_is_wrapped(self, mock_client_getter):
        self._mock_client(mock_client_getter).client.get.side_effect = requests.Timeout()

        with self.assertRaises(SubsidyAPIHTTPError) as context:
            get_subsidy_transactions_export(subsidy_uuid=uuid.uuid4(), enterprise_customer_uuid=uuid.uuid4())

        assert isinstance(context.exception.__cause__, requests.Timeout)

    def test_error_status_closes_streamed_response_and_is_wrapped(self, mock_client_getter):
        """With stream=True, an unread error response must be closed to release its pooled connection."""
        response = self._mock_client(mock_client_getter).client.get.return_value
        response.status_code = 503
        http_error = requests.HTTPError(response=response)
        response.raise_for_status.side_effect = http_error

        with self.assertRaises(SubsidyAPIHTTPError) as context:
            get_subsidy_transactions_export(subsidy_uuid=uuid.uuid4(), enterprise_customer_uuid=uuid.uuid4())

        assert context.exception.__cause__ is http_error
        response.close.assert_called_once_with()
