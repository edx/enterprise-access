"""Shared Snowflake helpers for enterprise-access reporting commands."""

from contextlib import contextmanager, suppress

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def get_snowflake_connection():
    """Create a Snowflake connection using enterprise-access settings."""
    if not getattr(settings, 'SNOWFLAKE_SERVICE_USER', ''):
        raise ImproperlyConfigured(
            'SNOWFLAKE_SERVICE_USER is required but not set. '
            'Set the SNOWFLAKE_SERVICE_USER environment variable.'
        )
    if not getattr(settings, 'SNOWFLAKE_SERVICE_USER_PASSWORD', ''):
        raise ImproperlyConfigured(
            'SNOWFLAKE_SERVICE_USER_PASSWORD is required but not set. '
            'Set the SNOWFLAKE_SERVICE_USER_PASSWORD environment variable.'
        )

    from snowflake import connector as snowflake_connector  # pylint: disable=import-outside-toplevel

    connection_kwargs = {
        'user': settings.SNOWFLAKE_SERVICE_USER,
        'password': settings.SNOWFLAKE_SERVICE_USER_PASSWORD,
        'account': settings.SNOWFLAKE_ACCOUNT,
        'database': settings.SNOWFLAKE_DATABASE,
    }
    warehouse = getattr(settings, 'SNOWFLAKE_WAREHOUSE', None)
    if warehouse:
        connection_kwargs['warehouse'] = warehouse
    role = getattr(settings, 'SNOWFLAKE_ROLE', None)
    if role:
        connection_kwargs['role'] = role

    return snowflake_connector.connect(**connection_kwargs)


@contextmanager
def snowflake_cursor():
    """Yield a Snowflake cursor and ensure all resources are closed."""
    connection = get_snowflake_connection()
    try:
        cursor = connection.cursor()
        try:
            yield cursor
        finally:
            with suppress(Exception):
                cursor.close()
    finally:
        with suppress(Exception):
            connection.close()


def fetch_all_query_results(query):
    """
    Execute a query and return all rows.
    """
    with snowflake_cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchall()
