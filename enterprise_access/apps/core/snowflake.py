"""Shared Snowflake helpers for enterprise-access reporting commands."""

from contextlib import contextmanager

from django.conf import settings


SNOWFLAKE_ACCOUNT = 'edx.us-east-1'
SNOWFLAKE_DATABASE = 'prod'


def get_snowflake_connection():
    """Create a Snowflake connection using enterprise-access settings."""
    from snowflake import connector as snowflake_connector  # pylint: disable=import-outside-toplevel

    return snowflake_connector.connect(
        user=settings.SNOWFLAKE_SERVICE_USER,
        password=settings.SNOWFLAKE_SERVICE_USER_PASSWORD,
        account=SNOWFLAKE_ACCOUNT,
        database=SNOWFLAKE_DATABASE,
    )


@contextmanager
def snowflake_cursor():
    """Yield a Snowflake cursor and ensure all resources are closed."""
    connection = get_snowflake_connection()
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()
        connection.close()


def fetch_all_query_results(query):
    """
    Execute a query and return all rows.
    """
    with snowflake_cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchall()