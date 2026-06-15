"""Shared Snowflake helpers for enterprise-access reporting commands."""

from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SNOWFLAKE_ACCOUNT = 'edx.us-east-1'
SNOWFLAKE_DATABASE = 'prod'


def _build_private_key_bytes(private_key_pem, passphrase):
    """
    Decode a PEM private key into the DER bytes the Snowflake connector expects.

    ``private_key_pem`` is the raw PEM string (with or without a passphrase).
    ``passphrase`` is an optional string used to decrypt the key; pass None for
    unencrypted keys.
    """
    from cryptography.hazmat.backends import default_backend  # pylint: disable=import-outside-toplevel
    from cryptography.hazmat.primitives.serialization import (  # pylint: disable=import-outside-toplevel
        Encoding,
        NoEncryption,
        PrivateFormat,
        load_pem_private_key
    )

    passphrase_bytes = passphrase.encode('utf-8') if passphrase else None
    p_key = load_pem_private_key(
        private_key_pem.encode('utf-8'),
        password=passphrase_bytes,
        backend=default_backend(),
    )
    return p_key.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )


def get_snowflake_connection():
    """Create a Snowflake connection using private key pair authentication.

    Requires ``SNOWFLAKE_SERVICE_USER`` and ``SNOWFLAKE_SERVICE_PRIVKEY`` to be
    set in Django settings (mapped from edx-internal secrets).
    ``SNOWFLAKE_SERVICE_PASSPHRASE`` is optional and only needed when the private
    key is passphrase-protected.
    """
    user = getattr(settings, 'SNOWFLAKE_SERVICE_USER', None)
    private_key_pem = getattr(settings, 'SNOWFLAKE_SERVICE_PRIVKEY', None)
    passphrase = getattr(settings, 'SNOWFLAKE_SERVICE_PASSPHRASE', None)

    if not user or not private_key_pem:
        raise ImproperlyConfigured(
            'Snowflake credentials are not configured: SNOWFLAKE_SERVICE_USER and '
            'SNOWFLAKE_SERVICE_PRIVKEY must be set in Django settings '
            '(check edx-internal config and environment variables).'
        )

    from snowflake import connector as snowflake_connector  # pylint: disable=import-outside-toplevel

    connection_kwargs = {
        'user': user,
        'private_key': _build_private_key_bytes(private_key_pem, passphrase),
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
