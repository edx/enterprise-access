"""Unit tests for top-level cache utilities."""

import hashlib
from unittest import mock

from django.test import SimpleTestCase, override_settings

from enterprise_access import __version__ as code_version
from enterprise_access.cache_utils import DEFAULT_NAMESPACE, request_cache, versioned_cache_key


class TestCacheUtils(SimpleTestCase):
    """Tests for enterprise_access.cache_utils."""

    def test_versioned_cache_key_without_stamp(self):
        cache_key = versioned_cache_key('one', 'two')
        expected_plain = ':'.join(['one', 'two', code_version])
        expected = hashlib.sha512(expected_plain.encode()).hexdigest()

        self.assertEqual(cache_key, expected)

    @override_settings(CACHE_KEY_VERSION_STAMP='build-123')
    def test_versioned_cache_key_includes_stamp(self):
        cache_key = versioned_cache_key('one', 'two')
        expected_plain = ':'.join(['one', 'two', code_version, 'build-123'])
        expected = hashlib.sha512(expected_plain.encode()).hexdigest()

        self.assertEqual(cache_key, expected)

    @mock.patch('enterprise_access.cache_utils.RequestCache')
    def test_request_cache_uses_default_namespace(self, mock_request_cache):
        request_cache()

        mock_request_cache.assert_called_once_with(namespace=DEFAULT_NAMESPACE)

    @mock.patch('enterprise_access.cache_utils.RequestCache')
    def test_request_cache_uses_custom_namespace(self, mock_request_cache):
        request_cache(namespace='custom-space')

        mock_request_cache.assert_called_once_with(namespace='custom-space')
