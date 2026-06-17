"""
Tests for Discovery client.
"""

from unittest import mock
from urllib.parse import urlparse
from uuid import uuid4

import ddt
from django.conf import settings
from django.test import RequestFactory, TestCase
from faker import Faker
from requests import Response
from requests.exceptions import HTTPError

from enterprise_access.apps.api_client.enterprise_catalog_client import (
    EnterpriseCatalogApiClient,
    EnterpriseCatalogApiV1Client,
    EnterpriseCatalogUserV1ApiClient
)
from enterprise_access.apps.api_client.tests.test_constants import DATE_FORMAT_ISO_8601
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.utils import _days_from_now


class TestEnterpriseCatalogApiClient(TestCase):
    """
    Test Enterprise Catalog Api client.
    """

    @mock.patch('requests.Response.json')
    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_contains_content_items(self, mock_oauth_client, mock_json):
        mock_json.return_value = {
            "contains_content_items": True
        }
        request_response = Response()
        request_response.status_code = 200
        mock_oauth_client.return_value.get.return_value = request_response

        ent_uuid = '31d82348-b8f4-417a-85b0-1a7640623810'
        client = EnterpriseCatalogApiClient()
        contains_content_items = client.contains_content_items(ent_uuid, ['AB+CD101'])

        assert contains_content_items

        mock_oauth_client.return_value.get.assert_called_with(
            f'http://enterprise-catalog.example.com/api/v2/enterprise-catalogs/{ent_uuid}/contains_content_items/',
            params={'course_run_ids': ['AB+CD101']},
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_catalog_content_metadata(self, mock_oauth_client):
        content_keys = ['course+A', 'course+B']
        mock_response_json = {
            'next': None,
            'results': [
                {
                    'key': content_keys[0],
                    'other_metadata': 'foo',
                },
                {
                    'key': content_keys[1],
                    'other_metadata': 'bar',
                }
            ]
        }

        request_response = Response()
        request_response.status_code = 200
        mock_oauth_client.return_value.get.return_value.json.return_value = mock_response_json

        customer_uuid = uuid4()
        client = EnterpriseCatalogApiClient()
        fetched_metadata = client.catalog_content_metadata(customer_uuid, content_keys)

        self.assertEqual(fetched_metadata['results'], mock_response_json['results'])
        mock_oauth_client.return_value.get.assert_called_with(
            f'http://enterprise-catalog.example.com/api/v2/enterprise-catalogs/{customer_uuid}/get_content_metadata/',
            params={
                'content_keys': content_keys,
                'traverse_pagination': True,
            },
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_catalog_content_metadata_raises_http_error(self, mock_oauth_client):
        content_keys = ['course+A', 'course+B']
        request_response = Response()
        request_response.status_code = 400

        mock_oauth_client.return_value.get.return_value = request_response

        customer_uuid = uuid4()
        client = EnterpriseCatalogApiClient()

        with self.assertRaises(HTTPError):
            client.catalog_content_metadata(customer_uuid, content_keys)

        mock_oauth_client.return_value.get.assert_called_with(
            f'http://enterprise-catalog.example.com/api/v2/enterprise-catalogs/{customer_uuid}/get_content_metadata/',
            params={
                'content_keys': content_keys,
                'traverse_pagination': True,
            },
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_content_metadata_count(self, mock_oauth_client):
        mock_response_json = {
            'count': 2
        }
        request_response = Response()
        request_response.status_code = 200
        mock_oauth_client.return_value.get.return_value.json.return_value = mock_response_json

        catalog_uuid = uuid4()
        client = EnterpriseCatalogApiClient()
        fetched_metadata = client.get_content_metadata_count(catalog_uuid)

        self.assertEqual(fetched_metadata, mock_response_json['count'])
        mock_oauth_client.return_value.get.assert_called_with(
            f'http://enterprise-catalog.example.com/api/v2/enterprise-catalogs/{catalog_uuid}/get_content_metadata/',
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academies(self, mock_oauth_client):
        mock_response_json = {'count': 1, 'next': None, 'previous': None, 'results': [{'title': 'AI Academy'}]}
        mock_oauth_client.return_value.get.return_value.json.return_value = mock_response_json

        client = EnterpriseCatalogApiClient()
        fetched = client.get_academies()

        self.assertEqual(fetched, mock_response_json)
        mock_oauth_client.return_value.get.assert_called_with(
            'http://enterprise-catalog.example.com/api/v2/academies/',
            params=None,
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academies_with_uuid(self, mock_oauth_client):
        mock_response_json = {'count': 0, 'next': None, 'previous': None, 'results': []}
        mock_oauth_client.return_value.get.return_value.json.return_value = mock_response_json

        academy_uuid = uuid4()
        client = EnterpriseCatalogApiClient()
        fetched = client.get_academies(academy_uuid=str(academy_uuid))

        self.assertEqual(fetched, mock_response_json)
        mock_oauth_client.return_value.get.assert_called_with(
            'http://enterprise-catalog.example.com/api/v2/academies/',
            params={'academy_uuid': str(academy_uuid)},
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academy_fetches_single_record_from_v2(self, mock_oauth_client):
        """Ensure `get_academy` calls the v2 academies endpoint and returns the academy JSON."""
        academy_uuid = uuid4()
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'uuid': str(academy_uuid), 'title': 'Test Academy'}
        mock_resp.raise_for_status = mock.Mock()
        mock_oauth_client.return_value.get.return_value = mock_resp

        client = EnterpriseCatalogApiClient()
        result = client.get_academy(academy_uuid)

        self.assertEqual(result, {'uuid': str(academy_uuid), 'title': 'Test Academy'})
        mock_oauth_client.return_value.get.assert_called_with(
            f'http://enterprise-catalog.example.com/api/v2/academies/{academy_uuid}/'
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_catalogs(self, mock_oauth_client):
        mock_response_json = {'count': 1, 'next': None, 'previous': None, 'results': [{'uuid': str(uuid4())}]}
        mock_oauth_client.return_value.get.return_value.json.return_value = mock_response_json

        client = EnterpriseCatalogApiClient()
        fetched = client.get_catalogs()

        self.assertEqual(fetched, mock_response_json)
        mock_oauth_client.return_value.get.assert_called_with(
            'http://enterprise-catalog.example.com/api/v2/enterprise-catalogs/',
            params=None,
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_catalogs_with_enterprise_customer(self, mock_oauth_client):
        mock_response_json = {'count': 1, 'next': None, 'previous': None, 'results': [{'uuid': str(uuid4())}]}
        mock_oauth_client.return_value.get.return_value.json.return_value = mock_response_json

        customer_uuid = str(uuid4())
        client = EnterpriseCatalogApiClient()
        fetched = client.get_catalogs(enterprise_customer_uuid=customer_uuid)

        self.assertEqual(fetched, mock_response_json)
        mock_oauth_client.return_value.get.assert_called_with(
            'http://enterprise-catalog.example.com/api/v2/enterprise-catalogs/',
            params={'enterprise_customer': customer_uuid},
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academies_merges_paginated_results(self, mock_oauth_client):
        page_1 = {
            'count': 2,
            'next': 'http://enterprise-catalog.example.com/api/v2/academies/?page=2',
            'previous': None,
            'results': [{'title': 'AI Academy'}],
        }
        page_2 = {
            'count': 2,
            'next': None,
            'previous': 'http://enterprise-catalog.example.com/api/v2/academies/?page=1',
            'results': [{'title': 'Data Academy'}],
        }
        mock_oauth_client.return_value.get.side_effect = [
            mock.Mock(json=mock.Mock(return_value=page_1), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_2), raise_for_status=mock.Mock()),
        ]

        client = EnterpriseCatalogApiClient()
        fetched = client.get_academies()

        self.assertEqual(fetched['count'], 2)
        self.assertEqual(len(fetched['results']), 2)
        self.assertIsNone(fetched['next'])
        self.assertEqual(mock_oauth_client.return_value.get.call_count, 2)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_catalogs_merges_paginated_results(self, mock_oauth_client):
        page_1 = {
            'count': 2,
            'next': 'http://enterprise-catalog.example.com/api/v2/enterprise-catalogs/?page=2',
            'previous': None,
            'results': [{'uuid': str(uuid4())}],
        }
        page_2 = {
            'count': 2,
            'next': None,
            'previous': 'http://enterprise-catalog.example.com/api/v2/enterprise-catalogs/?page=1',
            'results': [{'uuid': str(uuid4())}],
        }
        mock_oauth_client.return_value.get.side_effect = [
            mock.Mock(json=mock.Mock(return_value=page_1), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_2), raise_for_status=mock.Mock()),
        ]

        client = EnterpriseCatalogApiClient()
        fetched = client.get_catalogs()

        self.assertEqual(fetched['count'], 2)
        self.assertEqual(len(fetched['results']), 2)
        self.assertIsNone(fetched['next'])
        self.assertEqual(mock_oauth_client.return_value.get.call_count, 2)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_associate_academy_with_catalog(self, mock_oauth_client):
        academy_uuid = uuid4()
        catalog_uuid = uuid4()
        mock_post = mock_oauth_client.return_value.post
        mock_post.return_value.json.return_value = {'detail': 'ok'}

        client = EnterpriseCatalogApiClient()
        result = client.associate_academy_with_catalog(academy_uuid, catalog_uuid)

        self.assertEqual(result, {'detail': 'ok'})
        mock_post.assert_called_once_with(
            f'http://enterprise-catalog.example.com/api/v2/academies/{academy_uuid}/associate-catalog/',
            json={'enterprise_catalog_uuid': str(catalog_uuid)},
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_associate_academy_with_catalog_empty_response_body(self, mock_oauth_client):
        academy_uuid = uuid4()
        catalog_uuid = uuid4()
        mock_post = mock_oauth_client.return_value.post
        mock_post.return_value.json.side_effect = ValueError()

        client = EnterpriseCatalogApiClient()
        result = client.associate_academy_with_catalog(academy_uuid, catalog_uuid)

        self.assertEqual(result, {})

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_get_academies_returns_non_dict_payload(self, mock_oauth_client):
        payload = ['not-a-dict']
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value=payload),
            raise_for_status=mock.Mock(),
        )

        client = EnterpriseCatalogApiClient()
        result = client.get_academies()

        self.assertEqual(result, payload)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_get_academies_with_empty_endpoint_returns_empty_payload(self, mock_oauth_client):
        client = EnterpriseCatalogApiClient()
        client.academies_endpoint = ''

        result = client.get_academies()

        self.assertEqual(result, {'count': 0, 'next': None, 'previous': None, 'results': []})
        mock_oauth_client.return_value.get.assert_not_called()

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_get_academies_ignores_non_list_results(self, mock_oauth_client):
        payload = {'count': 1, 'next': None, 'previous': None, 'results': {'title': 'not-a-list'}}
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value=payload),
            raise_for_status=mock.Mock(),
        )

        client = EnterpriseCatalogApiClient()
        result = client.get_academies()

        self.assertEqual(result['results'], [])

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_get_academies_with_is_active_param(self, mock_oauth_client):
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value={'count': 0, 'next': None, 'previous': None, 'results': []}),
            raise_for_status=mock.Mock(),
        )

        client = EnterpriseCatalogApiClient()
        result = client.get_academies(is_active=True)

        self.assertEqual(result['results'], [])
        mock_oauth_client.return_value.get.assert_called_with(
            'http://enterprise-catalog.example.com/api/v2/academies/',
            params={'is_active': True},
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_get_academies_merges_paginated_results_missing_count(self, mock_oauth_client):
        page_1 = {
            'count': None,
            'next': 'http://enterprise-catalog.example.com/api/v2/academies/?page=2',
            'previous': None,
            'results': [{'title': 'AI Academy'}],
        }
        page_2 = {
            'count': None,
            'next': None,
            'previous': 'http://enterprise-catalog.example.com/api/v2/academies/?page=1',
            'results': [{'title': 'Data Academy'}],
        }
        mock_oauth_client.return_value.get.side_effect = [
            mock.Mock(json=mock.Mock(return_value=page_1), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_2), raise_for_status=mock.Mock()),
        ]

        client = EnterpriseCatalogApiClient()
        fetched = client.get_academies()

        self.assertEqual(fetched['count'], 2)
        self.assertEqual(len(fetched['results']), 2)
        self.assertIsNone(fetched['next'])
        self.assertEqual(mock_oauth_client.return_value.get.call_count, 2)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_get_academies_merges_paginated_results_non_int_count(self, mock_oauth_client):
        page_1 = {
            'count': 'not-an-int',
            'next': 'http://enterprise-catalog.example.com/api/v2/academies/?page=2',
            'previous': None,
            'results': [{'title': 'AI Academy'}],
        }
        page_2 = {
            'count': 'also-not-int',
            'next': None,
            'previous': 'http://enterprise-catalog.example.com/api/v2/academies/?page=1',
            'results': [{'title': 'Data Academy'}],
        }
        mock_oauth_client.return_value.get.side_effect = [
            mock.Mock(json=mock.Mock(return_value=page_1), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_2), raise_for_status=mock.Mock()),
        ]

        client = EnterpriseCatalogApiClient()
        fetched = client.get_academies()

        # non-int counts should be handled gracefully and fallback to length
        self.assertEqual(fetched['count'], 2)
        self.assertEqual(len(fetched['results']), 2)
        self.assertIsNone(fetched['next'])
        self.assertEqual(mock_oauth_client.return_value.get.call_count, 2)

    def test_catalog_content_metadata_raises_for_empty_content_keys_with_traversal(self):
        client = EnterpriseCatalogApiClient()

        with self.assertRaisesRegex(Exception, 'Cannot request all metadata for a catalog'):
            client.catalog_content_metadata(uuid4(), content_keys=[], traverse_pagination=True)

    def test_content_metadata_not_implemented_for_v2_client(self):
        client = EnterpriseCatalogApiClient()

        with self.assertRaises(NotImplementedError):
            client.content_metadata('some-content-key')

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_catalogs_three_page_merge(self, mock_oauth_client):
        page_1 = {'results': [{'uuid': '1'}], 'next': 'http://p2', 'previous': None, 'count': None}
        page_2 = {'results': [{'uuid': '2'}], 'next': 'http://p3', 'previous': 'http://p1', 'count': None}
        page_3 = {'results': [{'uuid': '3'}], 'next': None, 'previous': 'http://p2', 'count': None}
        mock_oauth_client.return_value.get.side_effect = [
            mock.Mock(json=mock.Mock(return_value=page_1), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_2), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_3), raise_for_status=mock.Mock()),
        ]

        client = EnterpriseCatalogApiClient()
        fetched = client.get_catalogs()

        self.assertEqual(len(fetched['results']), 3)
        self.assertIsNone(fetched['next'])

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_catalog_content_metadata_traverse_false_allows_empty_keys(self, mock_oauth_client):
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value={}), raise_for_status=mock.Mock()
        )
        client = EnterpriseCatalogApiClient()
        # Should not raise when traverse_pagination is False and content_keys empty
        res = client.catalog_content_metadata('catalog', [], traverse_pagination=False)
        self.assertEqual(res, {})

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academies_wraps_unexpected_type(self, mock_oauth_client):
        # Upstream returns an int (unexpected); client should wrap as paginated dict
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value=123), raise_for_status=mock.Mock()
        )
        client = EnterpriseCatalogApiClient()
        res = client.get_academies()
        self.assertIsInstance(res, dict)
        self.assertEqual(res['count'], 1)
        self.assertEqual(res['results'], [123])

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient', autospec=True)
    def test_associate_academy_with_catalog_with_string_uuids(self, mock_oauth_client):
        # Ensure we post the expected JSON body and handle JSON response
        mock_post = mock_oauth_client.return_value.post
        mock_post.return_value.json.return_value = {'detail': 'ok'}

        client = EnterpriseCatalogApiClient()
        result = client.associate_academy_with_catalog('academy-1', 'catalog-1')

        self.assertEqual(result, {'detail': 'ok'})
        mock_post.assert_called_once_with(
            'http://enterprise-catalog.example.com/api/v2/academies/academy-1/associate-catalog/',
            json={'enterprise_catalog_uuid': 'catalog-1'},
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_contains_content_items_true_value(self, mock_oauth_client):
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value={'contains_content_items': True}), raise_for_status=mock.Mock()
        )
        client = EnterpriseCatalogApiClient()
        self.assertTrue(client.contains_content_items('catalog', ['x']))

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_content_metadata_count_raises_when_key_missing(self, mock_oauth_client):
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value={}), raise_for_status=mock.Mock()
        )
        client = EnterpriseCatalogApiClient()
        with self.assertRaises(KeyError):
            client.get_content_metadata_count('catalog')

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_catalogs_preserve_results_when_next_page_empty(self, mock_oauth_client):
        page_1 = {'results': [{'uuid': '1'}], 'next': 'http://p2', 'previous': None, 'count': 2}
        page_2 = {'results': [], 'next': None, 'previous': 'http://p1', 'count': 2}
        mock_oauth_client.return_value.get.side_effect = [
            mock.Mock(json=mock.Mock(return_value=page_1), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_2), raise_for_status=mock.Mock()),
        ]

        client = EnterpriseCatalogApiClient()
        fetched = client.get_catalogs()
        self.assertEqual(fetched['count'], 2)
        self.assertEqual(len(fetched['results']), 1)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academies_handles_malformed_next_page(self, mock_oauth_client):
        # Next page returns a dict with non-list results; extension should be safe
        page_1 = {'results': [{'id': 1}], 'next': 'http://p2', 'previous': None, 'count': None}
        page_2 = {'results': {'bad': 'type'}, 'next': None, 'previous': 'http://p1', 'count': None}
        mock_oauth_client.return_value.get.side_effect = [
            mock.Mock(json=mock.Mock(return_value=page_1), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_2), raise_for_status=mock.Mock()),
        ]

        client = EnterpriseCatalogApiClient()
        res = client.get_academies()
        # page_2 results are ignored (not list), so merged length remains 1
        self.assertEqual(len(res['results']), 1)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_catalog_content_metadata_returns_json_payload(self, mock_oauth_client):
        payload = {'next': None, 'results': [{'key': 'k'}], 'count': 1}
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value=payload), raise_for_status=mock.Mock()
        )
        client = EnterpriseCatalogApiClient()
        self.assertEqual(client.catalog_content_metadata('cat', ['k']), payload)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academies_preserve_raw_list(self, mock_oauth_client):
        payload = [{'a': 1}, {'a': 2}]
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value=payload),
            raise_for_status=mock.Mock(),
        )

        client = EnterpriseCatalogApiClient()
        result = client.get_academies()
        self.assertEqual(result, payload)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academies_preserves_explicit_count_across_pages(self, mock_oauth_client):
        # First page reports an explicit larger count; second page has fewer results
        page_1 = {
            'results': [{'id': 1}],
            'count': 4,
            'next': 'http://next',
            'previous': None,
        }
        page_2 = {
            'results': [{'id': 2}, {'id': 3}],
            'count': 2,
            'next': None,
            'previous': None,
        }
        mock_oauth_client.return_value.get.side_effect = [
            mock.Mock(json=mock.Mock(return_value=page_1), raise_for_status=mock.Mock()),
            mock.Mock(json=mock.Mock(return_value=page_2), raise_for_status=mock.Mock()),
        ]

        client = EnterpriseCatalogApiClient()
        res = client.get_academies()
        self.assertEqual(res['count'], 4)
        self.assertEqual(len(res['results']), 3)

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_contains_content_items_missing_key_returns_false(self, mock_oauth_client):
        mock_oauth_client.return_value.get.return_value = mock.Mock(
            json=mock.Mock(return_value={}),
            raise_for_status=mock.Mock(),
        )
        client = EnterpriseCatalogApiClient()
        self.assertFalse(client.contains_content_items('catalog', ['x']))


@ddt.ddt
class TestEnterpriseCatalogApiV1Client(TestCase):
    """
    Test EnterpriseCatalogApiV1Client.
    """

    @ddt.data(
        {'coerce_to_parent_course': False},
        {'coerce_to_parent_course': True},
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_content_metadata(self, mock_oauth_client, coerce_to_parent_course):
        content_key = 'course+A'
        mock_response_json = {
            'key': content_key,
            'other_metadata': 'foo',
        }

        request_response = Response()
        request_response.status_code = 200
        mock_oauth_client.return_value.get.return_value.json.return_value = mock_response_json

        client = EnterpriseCatalogApiV1Client()
        fetched_metadata = client.content_metadata(content_key, coerce_to_parent_course=coerce_to_parent_course)

        self.assertEqual(fetched_metadata, mock_response_json)
        expected_query_params_kwarg = {}
        if coerce_to_parent_course:
            expected_query_params_kwarg |= {'params': {'coerce_to_parent_course': True}}
        mock_oauth_client.return_value.get.assert_called_with(
            f'http://enterprise-catalog.example.com/api/v1/content-metadata/{content_key}',
            **expected_query_params_kwarg,
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_content_metadata_raises_http_error(self, mock_oauth_client):
        content_key = 'course+A'
        request_response = Response()
        request_response.status_code = 400

        mock_oauth_client.return_value.get.return_value = request_response

        client = EnterpriseCatalogApiV1Client()

        with self.assertRaises(HTTPError):
            client.content_metadata(content_key)

        mock_oauth_client.return_value.get.assert_called_with(
            f'http://enterprise-catalog.example.com/api/v1/content-metadata/{content_key}',
        )


@ddt.ddt
class TestEnterpriseCatalogUserV1ApiClient(TestCase):
    """
    Test EnterpriseCatalogUserV1ApiClient
    """

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.faker = Faker()
        self.request_id_key = settings.REQUEST_ID_RESPONSE_HEADER

        self.user = UserFactory()
        self.mock_enterprise_customer_uuid = self.faker.uuid4()
        self.mock_catalog_uuid = self.faker.uuid4()
        self.mock_catalog_query_uuid = self.faker.uuid4()

    @ddt.data(
        {'enterprise_customer_uuid': 'test_uuid'},
        {'enterprise_customer_uuid': None},
    )
    @ddt.unpack
    def test_secured_algolia_api_key_endpoint(self, enterprise_customer_uuid):
        expected_url = (
            f'http://enterprise-catalog.example.com/api/v1'
            f'/enterprise-customer/{enterprise_customer_uuid}/secured-algolia-api-key/'
        )
        request = self.factory.get(expected_url)
        request.headers = {
            "Authorization": 'test-auth',
            self.request_id_key: 'test-request-id'
        }
        request.user = self.user
        context = {
            "request": request
        }
        client = EnterpriseCatalogUserV1ApiClient(context['request'])
        if enterprise_customer_uuid is None:
            with self.assertRaises(ValueError):
                client.secured_algolia_api_key_endpoint(
                    enterprise_customer_uuid=enterprise_customer_uuid
                )
        else:
            secured_algolia_api_key_url = client.secured_algolia_api_key_endpoint(
                enterprise_customer_uuid=enterprise_customer_uuid
            )
            self.assertEqual(secured_algolia_api_key_url, expected_url)

    @mock.patch('requests.Session.send')
    @mock.patch('crum.get_current_request')
    def test_secured_algolia_api_key(self, mock_crum_get_current_request, mock_send):
        expected_url = (
            f'http://enterprise-catalog.example.com/api/v1'
            f'/enterprise-customer/{self.mock_enterprise_customer_uuid}/secured-algolia-api-key/'
        )
        request = self.factory.get(expected_url)
        request.headers = {
            "Authorization": 'test-auth',
            self.request_id_key: 'test-request-id'
        }
        request.user = self.user
        context = {
            "request": request
        }

        mock_crum_get_current_request.return_value = request

        expected_result = {
            "algolia": {
                "secured_api_key": "Th15I54Fak341gOlI4K3y",
                "valid_until": _days_from_now(1, DATE_FORMAT_ISO_8601),
            },
            'catalog_uuids_to_catalog_query_uuids': {
                self.mock_catalog_uuid: self.mock_catalog_query_uuid,
            }
        }
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = expected_result

        mock_send.return_value = mock_response

        client = EnterpriseCatalogUserV1ApiClient(context['request'])
        result = client.get_secured_algolia_api_key(enterprise_customer_uuid=self.mock_enterprise_customer_uuid)
        prepared_request = mock_send.call_args[0][0]

        # Assert base request URL/method is correct
        parsed_url = urlparse(prepared_request.url)
        self.assertEqual(f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}", expected_url)
        self.assertEqual(prepared_request.method, 'GET')

        # Assert headers are correctly set
        self.assertEqual(prepared_request.headers['Authorization'], 'test-auth')
        self.assertEqual(prepared_request.headers[self.request_id_key], 'test-request-id')

        # Assert the response is as expected
        self.assertEqual(result, expected_result)


class TestEnterpriseCatalogApiClientGetAcademy(TestCase):
    """Tests for EnterpriseCatalogApiClient.get_academy()."""

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academy_success(self, mock_oauth_client):
        academy_uuid = uuid4()
        expected = {'uuid': str(academy_uuid), 'title': 'AI Academy', 'description': 'Learn AI'}
        mock_oauth_client.return_value.get.return_value.json.return_value = expected

        client = EnterpriseCatalogApiClient()
        result = client.get_academy(academy_uuid)

        self.assertEqual(result, expected)
        mock_oauth_client.return_value.get.assert_called_with(
            f'http://enterprise-catalog.example.com/api/v2/academies/{academy_uuid}/',
        )

    @mock.patch('enterprise_access.apps.api_client.base_oauth.OAuthAPIClient')
    def test_get_academy_raises_on_error(self, mock_oauth_client):
        """get_academy() propagates HTTP errors."""
        mock_response = Response()
        mock_response.status_code = 404
        mock_oauth_client.return_value.get.return_value = mock_response

        client = EnterpriseCatalogApiClient()
        with self.assertRaises(Exception):
            client.get_academy(uuid4())
