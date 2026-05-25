"""Unit tests for API client exception helpers."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from enterprise_access.apps.api_client.exceptions import APIClientException, safe_error_response_content


class TestApiClientExceptions(SimpleTestCase):
    """Tests for API client exception utilities."""

    def test_safe_error_response_content_returns_decoded_content(self):
        exception_object = SimpleNamespace(response=SimpleNamespace(content=b'failure payload'))

        self.assertEqual(safe_error_response_content(exception_object), 'failure payload')

    def test_safe_error_response_content_returns_none_when_content_empty(self):
        exception_object = SimpleNamespace(response=SimpleNamespace(content=b''))

        self.assertIsNone(safe_error_response_content(exception_object))

    def test_safe_error_response_content_logs_warning_on_decode_error(self):
        class BrokenContent:
            def decode(self):
                raise RuntimeError('decode failed')

        exception_object = SimpleNamespace(response=SimpleNamespace(content=BrokenContent()))

        with mock.patch('enterprise_access.apps.api_client.exceptions.logger') as mock_logger:
            content = safe_error_response_content(exception_object)

        self.assertIsNone(content)
        mock_logger.warning.assert_called_once()

    def test_api_client_exception_includes_response_content(self):
        wrapped = SimpleNamespace(response=SimpleNamespace(content=b'upstream error'))

        exception = APIClientException('Request failed', wrapped)

        self.assertIn('Request failed', str(exception))
        self.assertIn('response content: upstream error', str(exception))

    def test_api_client_exception_includes_none_when_response_unavailable(self):
        wrapped = SimpleNamespace(response=None)

        exception = APIClientException('Request failed', wrapped)

        self.assertIn('response content: None', str(exception))
