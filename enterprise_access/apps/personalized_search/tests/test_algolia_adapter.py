"""Tests for the Algolia ``CatalogSearch`` adapter."""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from enterprise_access.apps.api_client.algolia_client import AlgoliaConfigurationError, AlgoliaSearchError
from enterprise_access.apps.personalized_search.adapters.algolia import AlgoliaCatalog, secured_key_from_payload
from enterprise_access.apps.personalized_search.core.errors import CatalogSearchError
from enterprise_access.apps.personalized_search.core.ports import CatalogSearch


class AlgoliaCatalogTests(TestCase):
    """The adapter passes the scope's key and unscoped flag through to the client."""

    def test_is_a_catalog_search(self):
        self.assertIsInstance(AlgoliaCatalog(), CatalogSearch)

    def test_search(self):
        client = type('Client', (), {})()
        calls = []
        client.multi_search_catalog_index = lambda queries, **kw: calls.append((queries, kw)) or [
            {'hits': []}]
        scope = SimpleNamespace(filter='f', secured_key='KEY', allow_unscoped=False)  # core.retrieval.CatalogScope
        self.assertEqual(AlgoliaCatalog(client).search([{'query': 'q'}], scope=scope, timeout_s=1.5), [{'hits': []}])
        expected_kwargs = {'secured_key': 'KEY', 'allow_unscoped': False, 'timeout_s': 1.5}
        self.assertEqual(calls, [([{'query': 'q'}], expected_kwargs)])

    @mock.patch('enterprise_access.apps.personalized_search.adapters.algolia.AlgoliaSearchClient')
    def test_a_client_config_searches_that_app_and_index(self, client_class):
        client_class.return_value.multi_search_catalog_index.return_value = [{'hits': []}]
        scope = SimpleNamespace(filter='f', secured_key=None, allow_unscoped=True,
                                client_config={'app_id': 'PROD', 'search_api_key': 'k', 'index_name': 'prod_index'})
        AlgoliaCatalog().search([{'query': 'q'}], scope=scope, timeout_s=1.5)
        client_class.assert_called_once_with(app_id='PROD', search_api_key='k')
        self.assertEqual(client_class.return_value.multi_search_catalog_index.call_args.kwargs['index_name'],
                         'prod_index')

    def test_client_errors_become_the_ports_error(self):
        scope = SimpleNamespace(filter='f', secured_key='KEY', allow_unscoped=False)
        for error in (AlgoliaSearchError('down'), AlgoliaConfigurationError('no key')):
            client = mock.Mock(**{'multi_search_catalog_index.side_effect': error})
            with self.assertRaises(CatalogSearchError):
                AlgoliaCatalog(client).search([{'query': 'q'}], scope=scope, timeout_s=1.5)

    def test_secured_key_from_the_vending_response(self):
        key = secured_key_from_payload({'algolia': {'secured_api_key': 'k', 'valid_until': '2030-01-01T00:00:00Z'}})
        self.assertEqual(key.api_key, 'k')
        self.assertFalse(key.is_expired())
