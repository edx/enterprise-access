"""
Tests for subsidy_access_policy utils.
"""
import io
import ssl
import threading
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import ddt
import requests
from django.test import TestCase
from edx_rest_api_client.client import OAuthAPIClient
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ConnectTimeoutError, ProtocolError, ReadTimeoutError
from urllib3.exceptions import SSLError as Urllib3SSLError
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

from enterprise_access.apps.subsidy_access_policy.models import SubsidyAccessPolicy
from enterprise_access.apps.subsidy_access_policy.tests.factories import (
    AssignedLearnerCreditAccessPolicyFactory,
    PerLearnerEnrollmentCapLearnerCreditAccessPolicyFactory,
    PerLearnerSpendCapLearnerCreditAccessPolicyFactory
)
from enterprise_access.apps.subsidy_access_policy.utils import (
    SUBSIDY_CLIENT_MAX_AGE_SECONDS,
    SUBSIDY_CLIENT_RETRY,
    create_idempotency_key_for_transaction,
    get_versioned_subsidy_client,
    sort_subsidy_access_policies_for_redemption
)


@ddt.ddt
class SubsidyAccessPolicyUtilsTests(TestCase):
    """
    SubsidyAccessPolicy utils tests.
    """

    def setUp(self):
        super().setUp()

        now = datetime.utcnow()
        yesterday = now - timedelta(days=1)
        tomorrow = now + timedelta(days=1)
        day_after_tomorrow = now + timedelta(days=2)

        self.mock_subsidy_one = {
            'id': 1,
            'active_datetime': yesterday,
            'expiration_datetime': tomorrow,
            'is_active': True,
            'current_balance': 100,
        }
        self.mock_subsidy_two = {
            'id': 2,
            'active_datetime': yesterday,
            'expiration_datetime': tomorrow,
            'is_active': True,
            'current_balance': 50,
        }
        self.mock_subsidy_three = {
            'id': 3,
            'active_datetime': yesterday,
            'expiration_datetime': day_after_tomorrow,
            'is_active': True,
            'current_balance': 50,
        }
        self.mock_subsidy_four = {
            'id': 4,
            'active_datetime': yesterday,
            'expiration_datetime': tomorrow,
            'is_active': True,
            'current_balance': 100,
        }

        self.policy_one = PerLearnerEnrollmentCapLearnerCreditAccessPolicyFactory.create()
        self.policy_two = PerLearnerEnrollmentCapLearnerCreditAccessPolicyFactory.create()
        self.policy_three = PerLearnerEnrollmentCapLearnerCreditAccessPolicyFactory.create()
        self.policy_four = PerLearnerSpendCapLearnerCreditAccessPolicyFactory.create()
        self.policy_five = AssignedLearnerCreditAccessPolicyFactory.create()

        # Map policies to subsidy mocks
        self.policy_subsidy_map = {
            self.policy_one.pk: self.mock_subsidy_one,
            self.policy_two.pk: self.mock_subsidy_two,
            self.policy_three.pk: self.mock_subsidy_three,
            self.policy_four.pk: self.mock_subsidy_four,
            self.policy_five.pk: self.mock_subsidy_one,
        }

        policy_subsidy_map = self.policy_subsidy_map

        def mocked_subsidy_record(policy_instance):
            return policy_subsidy_map[policy_instance.pk]

        self.subsidy_record_patcher = patch(
            'enterprise_access.apps.subsidy_access_policy.models.SubsidyAccessPolicy.subsidy_record',
            new=mocked_subsidy_record
        )
        self.subsidy_record_patcher.start()
        self.addCleanup(self.subsidy_record_patcher.stop)

    def test_setup(self):
        """
        Ensure each policy has the correctly mocked subsidy object.
        """
        assert self.policy_one.subsidy_record() == self.mock_subsidy_one
        assert self.policy_two.subsidy_record() == self.mock_subsidy_two
        assert self.policy_three.subsidy_record() == self.mock_subsidy_three
        assert self.policy_four.subsidy_record() == self.mock_subsidy_four
        assert self.policy_five.subsidy_record() == self.mock_subsidy_one

    def test_create_idempotency_key_for_transaction_fixed_output(self):
        """
        Test happy path for creating an idempotency key.

        Also catch any code change which might alter the output given a fixed input.  At the time of writing, we're
        relying only on the idempotency key as a long-term uniqueness constraint on transactions, so this test should
        help draw attention to any logic changes which would result in allowing transactions to be created when they
        should not.
        """
        expected_idempotency_key = \
            "ledger-for-subsidy-f492b063-42bb-40dd-8464-6b5668329f1d-423a5bd5f0c19e7d750b03e9f21a04c7"
        inputs = {
            "subsidy_uuid": "f492b063-42bb-40dd-8464-6b5668329f1d",
            "lms_user_id": 12345,
            "content_key": "course-v1:edX+test+content",
            "subsidy_access_policy_uuid": "4ca49903-b66a-4fe9-a139-79d829deb944",
            "historical_redemptions_uuids": [
                "d459818c-458d-485b-a758-a0a060f8a7c4",
                "69beac67-52e2-47e0-994b-b0e1fac85845",
            ],
        }
        actual_idempotency_key_1 = create_idempotency_key_for_transaction(**inputs)
        actual_idempotency_key_2 = create_idempotency_key_for_transaction(foo="bar", **inputs)
        assert actual_idempotency_key_1 == expected_idempotency_key
        assert actual_idempotency_key_2 == expected_idempotency_key

    def test_create_idempotency_key_for_transaction_collisions(self):
        """
        Verify that the idempotency key is either unique OR collides whenever it should.

        This test is not concerned with key formatting or prefixes.
        """
        baseline_inputs = {
            "subsidy_uuid": str(uuid.uuid4()),
            "lms_user_id": 12345,
            "content_key": "course-v1:edX+test+content",
            "subsidy_access_policy_uuid": str(uuid.uuid4()),
            "historical_redemptions_uuids": [],
            "foo": "foo-value",
        }
        # Applying any of these modifications SHOULD change the idempotency key.
        modified_inputs_should_change_output = {
            "subsidy_uuid": str(uuid.uuid4()),
            "lms_user_id": 23456,
            "content_key": "course-v1:edX+test+content_different",
            "subsidy_access_policy_uuid": str(uuid.uuid4()),
            "historical_redemptions_uuids": [str(uuid.uuid4())],
        }
        # Applying any of these modifications SHOULD NOT change the idempotency key.
        modified_inputs_should_not_change_output = {
            "foo": "foo-value-different",
            "bar": "bar-value",
        }

        baseline_key = create_idempotency_key_for_transaction(**baseline_inputs)

        different_keys = set([baseline_key])
        for modify_key, new_value in modified_inputs_should_change_output.items():
            modified_inputs = baseline_inputs.copy()
            modified_inputs[modify_key] = new_value
            different_keys.add(create_idempotency_key_for_transaction(**modified_inputs))

        same_keys = set([baseline_key])
        for modify_key, new_value in modified_inputs_should_not_change_output.items():
            modified_inputs = baseline_inputs.copy()
            modified_inputs[modify_key] = new_value
            same_keys.add(create_idempotency_key_for_transaction(**modified_inputs))

        assert len(different_keys) == len(modified_inputs_should_change_output) + 1
        assert len(same_keys) == 1

    def test_sort_subsidy_access_policies_for_redemption_priority(self):
        """
        Test resolve given two policies with different balances, different expiration
        the sooner expiration policy should be returned.
        """
        queryset = SubsidyAccessPolicy.objects.filter(pk__in=[
            self.policy_one.pk,
            self.policy_five.pk,
            self.policy_four.pk,
        ])
        sorted_policies = sort_subsidy_access_policies_for_redemption(queryset=queryset)
        assert sorted_policies[0] == self.policy_five

    def test_sort_subsidy_access_policies_for_redemption_subsidy_balance(self):
        """
        Test resolve given two policies with same balances, same expiration
        but different type-priority.
        """
        queryset = SubsidyAccessPolicy.objects.filter(pk__in=[
            self.policy_one.pk,
            self.policy_two.pk
        ])
        sorted_policies = sort_subsidy_access_policies_for_redemption(queryset=queryset)
        assert sorted_policies[0] == self.policy_two

    @ddt.data(
        # A dated expiration sorts before "never expires"...
        ({'policy_three': None}, ['policy_three', 'policy_one'], ['policy_one', 'policy_three']),
        # ...including the ISO string the Subsidy API actually returns.
        (
            {'policy_one': '2030-01-01T00:00:00Z', 'policy_three': None},
            ['policy_three', 'policy_one'],
            ['policy_one', 'policy_three'],
        ),
        # All null: falls through to the balance (policy_two has 50, policy_one 100).
        ({'policy_one': None, 'policy_two': None}, ['policy_one', 'policy_two'], ['policy_two', 'policy_one']),
    )
    @ddt.unpack
    def test_sort_subsidy_access_policies_for_redemption_null_expiration(self, expirations, policies, expected):
        """
        A null expiration (never expires, or an unreachable subsidy's {} record) sorts last instead of raising.
        """
        for name, expiration in expirations.items():
            pk = getattr(self, name).pk
            self.policy_subsidy_map[pk] = {**self.policy_subsidy_map[pk], 'expiration_datetime': expiration}
        queryset = SubsidyAccessPolicy.objects.filter(pk__in=[getattr(self, name).pk for name in policies])

        sorted_policies = sort_subsidy_access_policies_for_redemption(queryset=queryset)

        assert list(sorted_policies) == [getattr(self, name) for name in expected]

    def test_sort_subsidy_access_policies_for_redemption_expiration(self):
        """
        Test resolve given two policies with different balances, different expiration
        the sooner expiration policy should be returned.
        """
        queryset = SubsidyAccessPolicy.objects.filter(pk__in=[
            self.policy_three.pk,
            self.policy_one.pk
        ])
        sorted_policies = sort_subsidy_access_policies_for_redemption(queryset=queryset)
        assert sorted_policies[0] == self.policy_one


# What ``HTTPConnectionPool._make_request`` raises or returns for each outcome of one attempt.
def _dropped_tls_handshake():
    return Urllib3SSLError(ssl.SSLEOFError(8, 'EOF occurred in violation of protocol'))


def _connect_timeout():
    return ConnectTimeoutError('Connection to enterprise-subsidy timed out.')


def _connection_reset():
    return ProtocolError('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))


def _read_timeout():
    return ReadTimeoutError(None, '/api/', 'Read timed out. (read timeout=120)')


def _response(status=200, headers=None):
    return HTTPResponse(
        body=io.BytesIO(b'{}'),
        status=status,
        headers={'Content-Type': 'application/json', **(headers or {})},
        preload_content=False,
    )


def _unavailable_with_retry_after():
    return _response(status=503, headers={'Retry-After': '1'})


@ddt.ddt
class GetVersionedSubsidyClientTests(TestCase):
    """
    Tests for ``get_versioned_subsidy_client()``: client reuse and retries.
    """

    def setUp(self):
        super().setUp()
        # Skip the OAuth token fetch, so requests go straight to the (mocked) connection pool.
        auth_patcher = patch.object(OAuthAPIClient, '_ensure_authentication')
        auth_patcher.start()
        self.addCleanup(auth_patcher.stop)

        # Don't wait out the backoff between retries.
        sleep_patcher = patch.object(Retry, 'sleep')
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    @ddt.data(1, 2)
    def test_reuses_client_within_a_thread(self, version):
        client = get_versioned_subsidy_client(version=version)

        assert get_versioned_subsidy_client(version=version) is client

    def test_separate_client_per_version(self):
        assert get_versioned_subsidy_client(version=1) is not get_versioned_subsidy_client(version=2)

    def test_separate_client_per_thread(self):
        client = get_versioned_subsidy_client()
        other_thread_clients = []
        thread = threading.Thread(target=lambda: other_thread_clients.append(get_versioned_subsidy_client()))
        thread.start()
        thread.join()

        assert other_thread_clients[0] is not client

    @patch('enterprise_access.apps.subsidy_access_policy.utils.time.monotonic')
    def test_replaces_client_after_max_age(self, mock_monotonic):
        mock_monotonic.return_value = 1000
        client = get_versioned_subsidy_client()
        mock_monotonic.return_value = 1000 + SUBSIDY_CLIENT_MAX_AGE_SECONDS - 1
        assert get_versioned_subsidy_client() is client

        mock_monotonic.return_value = 1000 + SUBSIDY_CLIENT_MAX_AGE_SECONDS
        with patch.object(client.client, 'close') as mock_close:
            replacement = get_versioned_subsidy_client()

        assert replacement is not client
        mock_close.assert_called_once_with()

    @ddt.data('http://enterprise-subsidy.example.com/', 'https://enterprise-subsidy.edx.org/')
    def test_retries_mounted_for_http_and_https(self, url):
        client = get_versioned_subsidy_client()

        assert client.client.get_adapter(url).max_retries is SUBSIDY_CLIENT_RETRY

    @ddt.data(
        # The production failure: the TLS handshake was dropped, so a GET is retried...
        ('GET', [_dropped_tls_handshake, _response], 200, 2),
        # ...at most twice, then the error is raised.
        ('GET', [_dropped_tls_handshake] * 3, requests.exceptions.SSLError, 3),
        # An SSL error can also come after a request was sent, so a POST is never resent after one.
        ('POST', [_dropped_tls_handshake, _response], requests.exceptions.SSLError, 1),
        # A connection that couldn't be opened never carried the request, so even a POST is retried.
        ('POST', [_connect_timeout, _response], 200, 2),
        # A connection that broke after the request was sent: a GET is retried once, a POST never.
        ('GET', [_connection_reset, _response], 200, 2),
        ('GET', [_connection_reset, _connection_reset, _response], requests.exceptions.ConnectionError, 2),
        ('POST', [_connection_reset, _response], requests.exceptions.ConnectionError, 1),
        # A timed-out request may still be running on the server (e.g. a transactions export), so it's never resent.
        ('GET', [_read_timeout, _response], requests.exceptions.ReadTimeout, 1),
        # HTTP error responses go back to the caller untouched, even one with a Retry-After header.
        ('GET', [_unavailable_with_retry_after], 503, 1),
    )
    @ddt.unpack
    def test_retries(self, method, attempt_outcomes, expected_result, expected_attempts):
        client = get_versioned_subsidy_client()

        with patch.object(
            HTTPConnectionPool, '_make_request', side_effect=[outcome() for outcome in attempt_outcomes],
        ) as mock_make_request:
            if isinstance(expected_result, int):
                response = client.client.request(method, client.API_BASE_URL)
                assert response.status_code == expected_result
            else:
                with self.assertRaises(expected_result):
                    client.client.request(method, client.API_BASE_URL)

        assert mock_make_request.call_count == expected_attempts
