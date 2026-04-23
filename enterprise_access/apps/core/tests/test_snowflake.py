"""Tests for shared Snowflake helpers."""

import sys
from types import SimpleNamespace
from unittest import TestCase, mock

from django.core.exceptions import ImproperlyConfigured

from enterprise_access.apps.core import snowflake


class TestSnowflakeHelpers(TestCase):
    """Unit tests for shared Snowflake helper functions."""

    def test_get_snowflake_connection_raises_when_user_missing(self):
        """Connection helper should raise ImproperlyConfigured when SNOWFLAKE_SERVICE_USER is unset."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_USER='',
            SNOWFLAKE_SERVICE_USER_PASSWORD='svc-pass',
            SNOWFLAKE_ACCOUNT='edx.us-east-1',
            SNOWFLAKE_DATABASE='prod',
        )
        with mock.patch.object(snowflake, 'settings', mock_settings), mock.patch.dict(
            sys.modules, {'snowflake': mock.Mock()}
        ):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                snowflake.get_snowflake_connection()
        self.assertIn('SNOWFLAKE_SERVICE_USER', str(ctx.exception))

    def test_get_snowflake_connection_raises_when_password_missing(self):
        """Connection helper should raise ImproperlyConfigured when SNOWFLAKE_SERVICE_USER_PASSWORD is unset."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_USER='svc-user',
            SNOWFLAKE_SERVICE_USER_PASSWORD='',
            SNOWFLAKE_ACCOUNT='edx.us-east-1',
            SNOWFLAKE_DATABASE='prod',
        )
        with mock.patch.object(snowflake, 'settings', mock_settings), mock.patch.dict(
            sys.modules, {'snowflake': mock.Mock()}
        ):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                snowflake.get_snowflake_connection()
        self.assertIn('SNOWFLAKE_SERVICE_USER_PASSWORD', str(ctx.exception))

    def test_get_snowflake_connection_uses_default_account_and_database(self):
        """Connection helper should use default account and database from settings."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_USER='svc-user',
            SNOWFLAKE_SERVICE_USER_PASSWORD='svc-pass',
            SNOWFLAKE_ACCOUNT='edx.us-east-1',
            SNOWFLAKE_DATABASE='prod',
        )

        mock_connect = mock.Mock()
        mock_snowflake_module = mock.Mock()
        mock_snowflake_module.connector = mock.Mock(connect=mock_connect)

        with mock.patch.object(snowflake, 'settings', mock_settings), mock.patch.dict(
            sys.modules, {'snowflake': mock_snowflake_module}
        ):
            connection = snowflake.get_snowflake_connection()

        mock_connect.assert_called_once_with(
            user='svc-user',
            password='svc-pass',
            account='edx.us-east-1',
            database='prod',
        )
        self.assertEqual(connection, mock_connect.return_value)

    def test_get_snowflake_connection_uses_optional_settings(self):
        """Connection helper should include optional overrides when configured."""
        mock_settings = SimpleNamespace(
            SNOWFLAKE_SERVICE_USER='svc-user',
            SNOWFLAKE_SERVICE_USER_PASSWORD='svc-pass',
            SNOWFLAKE_ACCOUNT='custom-account',
            SNOWFLAKE_DATABASE='custom-database',
            SNOWFLAKE_WAREHOUSE='my_warehouse',
            SNOWFLAKE_ROLE='my_role',
        )

        mock_connect = mock.Mock()
        mock_snowflake_module = mock.Mock()
        mock_snowflake_module.connector = mock.Mock(connect=mock_connect)

        with mock.patch.object(snowflake, 'settings', mock_settings), mock.patch.dict(
            sys.modules, {'snowflake': mock_snowflake_module}
        ):
            connection = snowflake.get_snowflake_connection()

        mock_connect.assert_called_once_with(
            user='svc-user',
            password='svc-pass',
            account='custom-account',
            database='custom-database',
            warehouse='my_warehouse',
            role='my_role',
        )
        self.assertEqual(connection, mock_connect.return_value)

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

    @mock.patch('enterprise_access.apps.core.snowflake.get_snowflake_connection')
    def test_snowflake_cursor_closes_connection_when_cursor_creation_fails(self, mock_get_connection):
        """Connection must be closed even when cursor() itself raises, preventing a resource leak."""
        mock_connection = mock.Mock()
        mock_connection.cursor.side_effect = RuntimeError('cursor creation failed')
        mock_get_connection.return_value = mock_connection

        with self.assertRaisesRegex(RuntimeError, 'cursor creation failed'):
            with snowflake.snowflake_cursor():
                pass  # pragma: no cover

        mock_connection.close.assert_called_once_with()

    @mock.patch('enterprise_access.apps.core.snowflake.get_snowflake_connection')
    def test_snowflake_cursor_closes_connection_when_cursor_close_raises(self, mock_get_connection):
        """Connection must close even if cursor.close() itself raises."""
        mock_connection = mock.Mock()
        mock_cursor = mock.Mock()
        mock_cursor.close.side_effect = RuntimeError('cursor close failed')
        mock_connection.cursor.return_value = mock_cursor
        mock_get_connection.return_value = mock_connection

        # No exception bubbles up from the context manager itself
        with snowflake.snowflake_cursor():
            pass

        mock_cursor.close.assert_called_once_with()
        mock_connection.close.assert_called_once_with()
