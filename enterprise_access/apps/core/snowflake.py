"""Shared Snowflake helpers for enterprise-access reporting commands."""

from contextlib import contextmanager, suppress
from importlib import import_module

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SNOWFLAKE_ACCOUNT = 'edx.us-east-1'
SNOWFLAKE_DATABASE = 'prod'


def get_snowflake_connection():
    """Create a Snowflake connection using enterprise-access settings."""
    if not getattr(settings, 'SNOWFLAKE_SERVICE_USER', ''):
        raise ImproperlyConfigured('SNOWFLAKE_SERVICE_USER is required but not set.')
    if not getattr(settings, 'SNOWFLAKE_SERVICE_USER_PASSWORD', ''):
        raise ImproperlyConfigured('SNOWFLAKE_SERVICE_USER_PASSWORD is required but not set.')

    try:
        snowflake_module = import_module('snowflake')
        snowflake_connector = snowflake_module.connector
    except (ModuleNotFoundError, AttributeError) as exc:
        raise ImproperlyConfigured(
            'snowflake-connector-python is required but not installed correctly.'
        ) from exc

    connection_kwargs = {
        'user': settings.SNOWFLAKE_SERVICE_USER,
        'password': settings.SNOWFLAKE_SERVICE_USER_PASSWORD,
        'account': getattr(settings, 'SNOWFLAKE_ACCOUNT', SNOWFLAKE_ACCOUNT),
        'database': getattr(settings, 'SNOWFLAKE_DATABASE', SNOWFLAKE_DATABASE),
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
