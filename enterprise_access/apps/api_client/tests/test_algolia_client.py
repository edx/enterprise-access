"""
Tests for the search-only Algolia client.
"""
from datetime import datetime, timedelta, timezone
from unittest import mock

import ddt
from algoliasearch.exceptions import AlgoliaException, AlgoliaUnreachableHostException, RequestException
from algoliasearch.search_client import SearchClient
from django.test import TestCase, override_settings

from enterprise_access.apps.api_client.algolia_client import (
    AlgoliaConfigurationError,
    AlgoliaSearchClient,
    AlgoliaSearchError,
    SecuredAlgoliaKey,
    looks_like_secured_api_key
)

CATALOG_INDEX = 'enterprise_catalog_incremental'
JOBS_INDEX = 'stage_taxonomy'
APP_ID = 'TESTAPPID'
PLAIN_SEARCH_KEY = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'

ALGOLIA_SETTINGS = {
    'ALGOLIA_APP_ID': APP_ID,
    'ALGOLIA_SEARCH_API_KEY': PLAIN_SEARCH_KEY,
    'ALGOLIA_CATALOG_INDEX_NAME': CATALOG_INDEX,
    'ALGOLIA_JOBS_INDEX_NAME': JOBS_INDEX,
    'ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH': False,
}

SEARCH_CLIENT_PATH = 'enterprise_access.apps.api_client.algolia_client.SearchClient'


def _live_secured_key(api_key='secured-key-value'):
    return SecuredAlgoliaKey(
        api_key=api_key,
        valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@override_settings(**ALGOLIA_SETTINGS)
@ddt.ddt
class TestAlgoliaSearchClient(TestCase):
    """
    Tests for ``AlgoliaSearchClient``.

    Every test patches ``SearchClient`` — this suite must never issue a real Algolia
    request. Assertions are written against the credential actually handed to
    ``SearchClient.create`` and the index actually handed to ``init_index``, because
    "which key reached which index" is the property this client exists to guarantee.
    """

    def setUp(self):
        super().setUp()
        self.mock_index = mock.MagicMock()
        self.mock_index.search.return_value = {'hits': [], 'nbHits': 0}

    def _patched_search_client(self):
        """Patch ``SearchClient`` and return (patcher_mock, index_mock)."""
        patcher = mock.patch(SEARCH_CLIENT_PATH)
        mock_search_client = patcher.start()
        self.addCleanup(patcher.stop)
        mock_search_client.create.return_value.init_index.return_value = self.mock_index
        return mock_search_client

    def _assert_searched_with(self, mock_search_client, expected_key, expected_index):
        mock_search_client.create.assert_called_once_with(APP_ID, expected_key)
        mock_search_client.create.return_value.init_index.assert_called_once_with(expected_index)

    # -- Configuration ----------------------------------------------------------------

    @override_settings(ALGOLIA_APP_ID='')
    def test_missing_app_id_raises(self):
        with self.assertRaisesRegex(AlgoliaConfigurationError, 'ALGOLIA_APP_ID'):
            AlgoliaSearchClient()

    @override_settings(ALGOLIA_JOBS_INDEX_NAME='')
    def test_missing_index_name_raises_before_request(self):
        mock_search_client = self._patched_search_client()

        with self.assertRaisesRegex(AlgoliaConfigurationError, 'without an index name'):
            AlgoliaSearchClient().search_jobs_index('data analyst')

        mock_search_client.create.assert_not_called()

    @override_settings(ALGOLIA_SEARCH_API_KEY='')
    def test_missing_search_key_raises_before_request(self):
        mock_search_client = self._patched_search_client()

        with self.assertRaisesRegex(AlgoliaConfigurationError, 'without an API key'):
            AlgoliaSearchClient().search_jobs_index('data analyst')

        mock_search_client.create.assert_not_called()

    # -- Catalog index: enterprise scoping --------------------------------------------

    def test_catalog_search_uses_the_secured_key(self):
        """Scenario: Catalog search is scoped to the enterprise."""
        mock_search_client = self._patched_search_client()
        secured_key = _live_secured_key()

        result = AlgoliaSearchClient().search_catalog_index(
            'python',
            secured_key=secured_key,
            hitsPerPage=20,
        )

        self.assertEqual(result, {'hits': [], 'nbHits': 0})
        self._assert_searched_with(mock_search_client, secured_key.api_key, CATALOG_INDEX)
        self.mock_index.search.assert_called_once_with('python', {'hitsPerPage': 20})
        # The plain, unscoped search key must not have been used.
        self.assertNotIn(
            mock.call(APP_ID, PLAIN_SEARCH_KEY),
            mock_search_client.create.mock_calls,
        )

    def test_catalog_search_without_secured_key_raises(self):
        mock_search_client = self._patched_search_client()

        with self.assertRaisesRegex(AlgoliaConfigurationError, 'secured Algolia API key is required'):
            AlgoliaSearchClient().search_catalog_index('python')

        mock_search_client.create.assert_not_called()

    @ddt.data(
        # An expiry in the past.
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        # No expiry at all: unknown validity is not evidence of validity.
        None,
    )
    def test_expired_secured_key_raises_and_never_falls_back(self, valid_until):
        """Scenario: Expired secured keys are refreshed / no unscoped query is issued."""
        mock_search_client = self._patched_search_client()
        expired = SecuredAlgoliaKey(api_key='stale', valid_until=valid_until)

        with self.assertRaisesRegex(AlgoliaConfigurationError, 'expired'):
            AlgoliaSearchClient().search_catalog_index('python', secured_key=expired)

        mock_search_client.create.assert_not_called()

    # -- Catalog index: the explicit unscoped diagnostic path -------------------------

    def test_unscoped_catalog_search_is_refused_by_default(self):
        mock_search_client = self._patched_search_client()

        with self.assertRaisesRegex(AlgoliaConfigurationError, 'ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH'):
            AlgoliaSearchClient().search_catalog_index('python', allow_unscoped=True)

        mock_search_client.create.assert_not_called()

    @override_settings(ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH=True)
    def test_unscoped_catalog_search_uses_plain_key_when_enabled(self):
        mock_search_client = self._patched_search_client()

        AlgoliaSearchClient().search_catalog_index('python', allow_unscoped=True, hitsPerPage=20)

        self._assert_searched_with(mock_search_client, PLAIN_SEARCH_KEY, CATALOG_INDEX)

    @override_settings(ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH=True)
    def test_enabling_unscoped_search_does_not_weaken_the_scoped_path(self):
        """Even with the escape hatch on, a caller that does not ask for it still needs a key."""
        mock_search_client = self._patched_search_client()

        with self.assertRaises(AlgoliaConfigurationError):
            AlgoliaSearchClient().search_catalog_index('python')

        mock_search_client.create.assert_not_called()

    # -- Jobs index -------------------------------------------------------------------

    def test_jobs_search_uses_the_plain_key(self):
        """Scenario: Jobs index uses the plain search key."""
        mock_search_client = self._patched_search_client()
        self.mock_index.search.return_value = {
            'hits': [{
                'name': 'Data Analyst',
                'external_id': 'ET123',
                'skills': [{'name': 'SQL'}],
                'industry_names': ['Finance and Insurance'],
            }],
        }

        result = AlgoliaSearchClient().search_jobs_index(
            'data analyst',
            facetFilters=[['skills.name:SQL']],
            hitsPerPage=10,
        )

        self._assert_searched_with(mock_search_client, PLAIN_SEARCH_KEY, JOBS_INDEX)
        self.mock_index.search.assert_called_once_with(
            'data analyst',
            {'facetFilters': [['skills.name:SQL']], 'hitsPerPage': 10},
        )
        hit = result['hits'][0]
        self.assertEqual(hit['name'], 'Data Analyst')
        self.assertEqual(hit['skills'], [{'name': 'SQL'}])
        self.assertEqual(hit['industry_names'], ['Finance and Insurance'])

    def test_jobs_search_refuses_a_secured_key(self):
        """Scenario: A secured key is never sent to the jobs index."""
        secured_key = SearchClient.generate_secured_api_key(
            PLAIN_SEARCH_KEY,
            {'restrictIndices': [CATALOG_INDEX], 'validUntil': 4102444800},
        )
        mock_search_client = self._patched_search_client()

        with override_settings(ALGOLIA_SEARCH_API_KEY=secured_key):
            with self.assertRaisesRegex(AlgoliaConfigurationError, 'secured key'):
                AlgoliaSearchClient().search_jobs_index('data analyst')

        mock_search_client.create.assert_not_called()

    @override_settings(ALGOLIA_JOBS_INDEX_NAME=CATALOG_INDEX)
    def test_jobs_search_refuses_the_catalog_index(self):
        """A misconfigured jobs index name must not become an unscoped catalog search."""
        mock_search_client = self._patched_search_client()

        with self.assertRaisesRegex(AlgoliaConfigurationError, 'requires a secured key'):
            AlgoliaSearchClient().search_jobs_index('python')

        mock_search_client.create.assert_not_called()

    # -- Transport failures -----------------------------------------------------------

    @ddt.data(
        AlgoliaException('boom'),
        AlgoliaUnreachableHostException('unreachable'),
        RequestException('500 server error', 500),
    )
    def test_transport_failures_are_typed(self, raised_exception):
        """Scenario: Transport failures are typed."""
        self._patched_search_client()
        self.mock_index.search.side_effect = raised_exception

        with self.assertRaises(AlgoliaSearchError):
            AlgoliaSearchClient().search_jobs_index('data analyst')

    def test_client_is_closed_even_when_the_search_fails(self):
        mock_search_client = self._patched_search_client()
        self.mock_index.search.side_effect = AlgoliaException('boom')

        with self.assertRaises(AlgoliaSearchError):
            AlgoliaSearchClient().search_jobs_index('data analyst')

        mock_search_client.create.return_value.close.assert_called_once()


@ddt.ddt
class TestSecuredAlgoliaKey(TestCase):
    """
    Tests for ``SecuredAlgoliaKey`` and secured-key detection.
    """

    @ddt.data('secured_api_key', 'secured_algolia_api_key')
    def test_from_response_payload_accepts_both_field_names(self, field_name):
        """enterprise-catalog returns ``secured_api_key``; the BFF renames it."""
        key = SecuredAlgoliaKey.from_response_payload({
            'algolia': {field_name: 'abc123', 'valid_until': '2099-01-01T00:00:00Z'},
        })

        self.assertEqual(key.api_key, 'abc123')
        self.assertEqual(key.valid_until, datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertFalse(key.is_expired())

    @ddt.data(
        {},
        {'algolia': {}},
        {'algolia': {'secured_api_key': ''}},
        None,
    )
    def test_from_response_payload_without_a_key_raises(self, payload):
        with self.assertRaisesRegex(AlgoliaConfigurationError, 'no secured Algolia API key'):
            SecuredAlgoliaKey.from_response_payload(payload)

    def test_naive_valid_until_is_treated_as_utc(self):
        key = SecuredAlgoliaKey.from_response_payload({
            'algolia': {'secured_api_key': 'abc', 'valid_until': '2099-01-01T00:00:00'},
        })

        self.assertEqual(key.valid_until.tzinfo, timezone.utc)

    def test_is_expired_at_the_boundary(self):
        boundary = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        key = SecuredAlgoliaKey(api_key='abc', valid_until=boundary)

        self.assertTrue(key.is_expired(now=boundary))
        self.assertFalse(key.is_expired(now=boundary - timedelta(seconds=1)))

    def test_looks_like_secured_api_key_detects_a_real_secured_key(self):
        secured = SearchClient.generate_secured_api_key(
            PLAIN_SEARCH_KEY,
            {'restrictIndices': [CATALOG_INDEX], 'validUntil': 4102444800},
        )

        self.assertTrue(looks_like_secured_api_key(secured))

    @ddt.data('', None, PLAIN_SEARCH_KEY, '68c192f7c3aeca5d488c9e1a8ee15966', 'not-base64!!')
    def test_looks_like_secured_api_key_is_false_for_plain_keys(self, api_key):
        self.assertFalse(looks_like_secured_api_key(api_key))
