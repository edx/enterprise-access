"""Tests for shared Snowflake helpers."""

import sys
from types import SimpleNamespace
from unittest import TestCase, mock

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, Encoding, NoEncryption, PrivateFormat
from django.core.exceptions import ImproperlyConfigured

from enterprise_access.apps.core import snowflake


class TestSnowflakeHelpers(TestCase):
    """Unit tests for shared Snowflake helper functions."""

    def _make_mock_snowflake_module(self):
        mock_connect = mock.Mock()
        mock_module = mock.Mock()
        mock_module.connector = mock.Mock(connect=mock_connect)
        return mock_module, mock_connect

    def _mock_private_key_bytes(self, fake_der=b'fake-der-bytes'):
        """Return a context manager that patches _build_private_key_bytes."""
        return mock.patch.object(snowflake, '_build_private_key_bytes', return_value=fake_der)

    # ------------------------------------------------------------------
    # get_snowflake_connection — private key auth
    # ------------------------------------------------------------------

    def test_private_key_auth_with_passphrase(self):
        """Private key + passphrase should be forwarded to _build_private_key_bytes and connector."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_USER='svc-user',
            SNOWFLAKE_SERVICE_PRIVKEY='-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----',
            SNOWFLAKE_SERVICE_PASSPHRASE='s3cr3t',
        )
        mock_module, mock_connect = self._make_mock_snowflake_module()

        with mock.patch.object(snowflake, 'settings', mock_settings), \
                mock.patch.dict(sys.modules, {'snowflake': mock_module}), \
                self._mock_private_key_bytes(b'der-bytes') as mock_build:
            connection = snowflake.get_snowflake_connection()

        mock_build.assert_called_once_with(mock_settings.SNOWFLAKE_SERVICE_PRIVKEY, 's3cr3t')
        mock_connect.assert_called_once_with(
            user='svc-user',
            private_key=b'der-bytes',
            account='edx.us-east-1',
            database='prod',
        )
        self.assertEqual(connection, mock_connect.return_value)

    def test_private_key_auth_without_passphrase(self):
        """Private key without passphrase should call _build_private_key_bytes with None."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_USER='svc-user',
            SNOWFLAKE_SERVICE_PRIVKEY='-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----',
        )
        mock_module, mock_connect = self._make_mock_snowflake_module()

        with mock.patch.object(snowflake, 'settings', mock_settings), \
                mock.patch.dict(sys.modules, {'snowflake': mock_module}), \
                self._mock_private_key_bytes(b'der-bytes') as mock_build:
            connection = snowflake.get_snowflake_connection()

        mock_build.assert_called_once_with(mock_settings.SNOWFLAKE_SERVICE_PRIVKEY, None)
        mock_connect.assert_called_once_with(
            user='svc-user',
            private_key=b'der-bytes',
            account='edx.us-east-1',
            database='prod',
        )
        self.assertEqual(connection, mock_connect.return_value)

    def test_private_key_auth_with_optional_settings(self):
        """Private key auth should include optional warehouse/role/account/database overrides."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_USER='svc-user',
            SNOWFLAKE_SERVICE_PRIVKEY='-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----',
            SNOWFLAKE_SERVICE_PASSPHRASE='pass',
            SNOWFLAKE_ACCOUNT='custom-account',
            SNOWFLAKE_DATABASE='custom-database',
            SNOWFLAKE_WAREHOUSE='my_warehouse',
            SNOWFLAKE_ROLE='my_role',
        )
        mock_module, mock_connect = self._make_mock_snowflake_module()

        with mock.patch.object(snowflake, 'settings', mock_settings), \
                mock.patch.dict(sys.modules, {'snowflake': mock_module}), \
                self._mock_private_key_bytes(b'der-bytes'):
            snowflake.get_snowflake_connection()

        mock_connect.assert_called_once_with(
            user='svc-user',
            private_key=b'der-bytes',
            account='custom-account',
            database='custom-database',
            warehouse='my_warehouse',
            role='my_role',
        )

    # ------------------------------------------------------------------
    # get_snowflake_connection — ImproperlyConfigured guard-rails
    # ------------------------------------------------------------------

    def test_raises_when_user_missing(self):
        """ImproperlyConfigured raised when SNOWFLAKE_SERVICE_USER is absent."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_PRIVKEY='-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----',
        )
        with mock.patch.object(snowflake, 'settings', mock_settings):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                snowflake.get_snowflake_connection()
        self.assertIn('SNOWFLAKE_SERVICE_USER', str(ctx.exception))

    def test_raises_when_privkey_missing(self):
        """ImproperlyConfigured raised when SNOWFLAKE_SERVICE_PRIVKEY is absent."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_USER='svc-user',
        )
        with mock.patch.object(snowflake, 'settings', mock_settings):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                snowflake.get_snowflake_connection()
        self.assertIn('SNOWFLAKE_SERVICE_PRIVKEY', str(ctx.exception))

    def test_raises_when_both_user_and_privkey_missing(self):
        """ImproperlyConfigured raised when neither user nor privkey is set."""
        mock_settings = SimpleNamespace()
        with mock.patch.object(snowflake, 'settings', mock_settings):
            with self.assertRaises(ImproperlyConfigured):
                snowflake.get_snowflake_connection()

    # ------------------------------------------------------------------
    # _build_private_key_bytes unit tests
    # ------------------------------------------------------------------

    def _generate_pem_key(self, passphrase=None):
        """Generate a real RSA PEM key for testing (encrypted or unencrypted)."""
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        encryption = (
            BestAvailableEncryption(passphrase.encode('utf-8')) if passphrase else NoEncryption()
        )
        return private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        ).decode('utf-8')

    def test_build_private_key_bytes_without_passphrase(self):
        """_build_private_key_bytes returns DER bytes from an unencrypted PEM key."""
        pem = self._generate_pem_key()
        result = snowflake._build_private_key_bytes(pem, None)  # pylint: disable=protected-access
        self.assertIsInstance(result, bytes)
        self.assertEqual(result[0], 0x30)  # ASN.1 SEQUENCE tag

    def test_build_private_key_bytes_with_passphrase(self):
        """_build_private_key_bytes decrypts a passphrase-protected PEM key."""
        passphrase = 'test-passphrase'
        pem = self._generate_pem_key(passphrase=passphrase)
        result = snowflake._build_private_key_bytes(pem, passphrase)  # pylint: disable=protected-access
        self.assertIsInstance(result, bytes)
        self.assertEqual(result[0], 0x30)  # ASN.1 SEQUENCE tag

    # ------------------------------------------------------------------
    # snowflake_cursor / fetch_all_query_results
    # ------------------------------------------------------------------

    @mock.patch('enterprise_access.apps.core.snowflake.get_snowflake_connection')
    def test_fetch_all_query_results_executes_and_closes_resources(self, mock_get_connection):
        """Query helper should execute, fetch, and close cursor/connection."""
        mock_connection = mock.Mock()
        mock_cursor = mock.Mock()
        mock_cursor.fetchall.return_value = [('row-1',), ('row-2',)]
        mock_connection.cursor.return_value = mock_cursor
        mock_get_connection.return_value = mock_connection

        results = snowflake.fetch_all_query_results('SELECT 1')

        self.assertEqual(results, [('row-1',), ('row-2',)])
        mock_cursor.execute.assert_called_once_with('SELECT 1')
        mock_cursor.fetchall.assert_called_once_with()
        mock_cursor.close.assert_called_once_with()
        mock_connection.close.assert_called_once_with()

    @mock.patch('enterprise_access.apps.core.snowflake.get_snowflake_connection')
    def test_snowflake_cursor_closes_resources_on_exception(self, mock_get_connection):
        """Cursor helper should close cursor and connection even when an error occurs."""
        mock_connection = mock.Mock()
        mock_cursor = mock.Mock()
        mock_connection.cursor.return_value = mock_cursor
        mock_get_connection.return_value = mock_connection

        with self.assertRaisesRegex(RuntimeError, 'boom'):
            with snowflake.snowflake_cursor() as cursor:
                self.assertEqual(cursor, mock_cursor)
                raise RuntimeError('boom')

        mock_connection.cursor.assert_called_once_with()
        mock_cursor.close.assert_called_once_with()
        mock_connection.close.assert_called_once_with()
