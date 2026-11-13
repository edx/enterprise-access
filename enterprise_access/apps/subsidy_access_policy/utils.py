"""
Utils for subsidy_access_policy
"""
import hashlib
import threading
import time

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from edx_enterprise_subsidy_client import get_enterprise_subsidy_api_client
from requests.adapters import HTTPAdapter
from simple_history.models import HistoricalRecords, registered_models
from urllib3.exceptions import ReadTimeoutError
from urllib3.util.retry import Retry

from enterprise_access.apps.subsidy_access_policy import constants

from .constants import ERROR_MSG_ACTIVE_UNKNOWN_SPEND, ERROR_MSG_ACTIVE_WITH_SPEND

LEDGERED_SUBSIDY_IDEMPOTENCY_KEY_PREFIX = 'ledger-for-subsidy'
TRANSACTION_METADATA_KEYS = {
    'lms_user_id',
    'content_key',
    'subsidy_access_policy_uuid',
    'historical_redemptions_uuids',
}


# Subsidy API clients are reused, but replaced well before an idle connection can be silently dropped in transit
# (the AWS NAT gateway does that after 350s), since a POST sent on a dropped connection can't safely be retried.
SUBSIDY_CLIENT_MAX_AGE_SECONDS = 60


class SubsidyClientRetry(Retry):
    """
    A ``Retry`` that never resends a request that timed out waiting for a response, nor a non-idempotent request
    (a POST) after an error that may have come once the request was sent.

    urllib3 counts a read timeout as a read error, so it would resend a GET the server may still be working on (a
    transactions export, say). It only checks ``allowed_methods`` for read errors, so it would also resend a POST
    after an ``SSLError`` raised while reading the response.
    """

    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        if isinstance(error, ReadTimeoutError) or (
            error is not None and not self._is_connection_error(error) and not self._is_method_retryable(method or '')
        ):
            raise error.with_traceback(_stacktrace)
        return super().increment(method, url, response, error, _pool, _stacktrace)


# At most 3 attempts. A failure to connect (including a dropped TLS handshake) is retried for any method,
# since the request was never sent. A connection that broke after a GET was sent is retried once, unless it timed
# out. HTTP responses, even a 503 with Retry-After, are never retried: they go back to the caller as they are.
SUBSIDY_CLIENT_RETRY = SubsidyClientRetry(
    total=2,
    read=1,
    backoff_factor=0.3,
    respect_retry_after_header=False,
)


class _ThreadLocalSubsidyClients(threading.local):
    """
    This thread's subsidy clients, as ``{version: (client, created_at)}``. A requests ``Session`` isn't thread-safe.
    """

    def __init__(self):
        super().__init__()
        self.by_version = {}


_subsidy_clients = _ThreadLocalSubsidyClients()


def get_versioned_subsidy_client(version=None):
    """
    Returns an instance of the enterprise subsidy client as the version specified by the
    Django setting `ENTERPRISE_SUBSIDY_API_CLIENT_VERSION`, if any.

    The client is reused within this thread for up to ``SUBSIDY_CLIENT_MAX_AGE_SECONDS``, so calls share open
    connections instead of each starting a new one, and it retries connection failures (see
    ``SUBSIDY_CLIENT_RETRY``).
    """
    kwargs = {}
    if not version:
        if getattr(settings, 'ENTERPRISE_SUBSIDY_API_CLIENT_VERSION', None):
            kwargs['version'] = int(settings.ENTERPRISE_SUBSIDY_API_CLIENT_VERSION)
    else:
        kwargs['version'] = int(version)

    now = time.monotonic()
    cached = _subsidy_clients.by_version.get(kwargs.get('version'))
    if cached:
        cached_client, created_at = cached
        if now - created_at < SUBSIDY_CLIENT_MAX_AGE_SECONDS:
            return cached_client
        cached_client.client.close()

    client = get_enterprise_subsidy_api_client(**kwargs)
    for prefix in ('http://', 'https://'):
        client.client.mount(prefix, HTTPAdapter(max_retries=SUBSIDY_CLIENT_RETRY))
    _subsidy_clients.by_version[kwargs.get('version')] = (client, now)
    return client


def create_idempotency_key_for_transaction(subsidy_uuid, **metadata):
    """
    Create a key that allows a transaction to be created idempotently.
    """
    idpk_data = {
        tx_key: value
        for tx_key, value in metadata.items()
        if tx_key in TRANSACTION_METADATA_KEYS
    }
    hashed_metadata = hashlib.md5(str(idpk_data).encode()).hexdigest()
    return f'{LEDGERED_SUBSIDY_IDEMPOTENCY_KEY_PREFIX}-{subsidy_uuid}-{hashed_metadata}'


def sort_subsidy_access_policies_for_redemption(queryset):
    """
    Sorts the query set during can-redeem by the following parameters
           - priority (of type)
           - expiration, sooner to expire first
           - balance, lower balance first

    A null expiration (never expires, or an unreachable subsidy's ``{}`` record) sorts last rather than being
    compared, which would raise TypeError.
    """
    if queryset.count() <= 1:
        return queryset

    def sort_key(policy):
        expiration = policy.subsidy_expiration_datetime
        return (policy.priority, expiration is None, expiration, policy.subsidy_balance())

    return sorted(queryset, key=sort_key)


class ProxyAwareHistoricalRecords(HistoricalRecords):
    """
    This specialized HistoricalRecords model field is to be used specifically for tracking history on instances of proxy
    models.  Set `history = ProxyAwareHistoricalRecords(inherit=True) on the parent (concrete) model, and proxy models
    that subclass the parent will have their history tracked in the history table of the parent.

    The only downside is that this seems to cause no-op migrations to be created for each proxy model (as opposed to no
    migration at all).  The migration defines a historical table for the proxy model with no fields, which sqlmigrate
    translates to an empty transaction: `BEGIN; COMMIT;`.

    Copied verbatim from https://github.com/jazzband/django-simple-history/issues/544#issuecomment-1538615799
    """
    def _find_base_history(self, opts):
        """
        Search for a history model in the parent models that we can re-use.
        """
        base_history = None
        for parent_class in opts.parents.keys():
            if hasattr(parent_class, 'history'):
                base_history = parent_class.history.model
        return base_history

    def create_history_model(self, model, inherited):
        """
        Override super.create_history_model() to force creation of a history model if this is a proxy model.
        """
        opts = model._meta
        if opts.proxy:
            base_history = self._find_base_history(opts)
            if base_history:
                return self.create_proxy_history_model(model, inherited, base_history)

        return super().create_history_model(model, inherited)

    def create_proxy_history_model(self, model, inherited, base_history):
        """
        Create a history model for this proxy model, inheriting from the parent history model so that historical records
        are tracked in the latter.
        """
        opts = model._meta
        attrs = {
            '__module__': self.module,
            '_history_excluded_fields': self.excluded_fields,
        }
        app_module = f'{opts.app_label}.models'
        if inherited:
            attrs['__module__'] = model.__module__
        elif model.__module__ != self.module:
            # registered under different app
            attrs['__module__'] = self.module
        elif app_module != self.module:
            # Abuse an internal API because the app registry is loading.
            app = apps.app_configs[opts.app_label]
            models_module = app.name
            attrs['__module__'] = models_module

        attrs.update(
            Meta=type(
                'Meta',
                (),
                {**self.get_meta_options(model), 'proxy': True}
            )
        )
        if self.table_name is not None:
            attrs['Meta'].db_table = self.table_name

        name = self.get_history_model_name(model)
        registered_models[opts.db_table] = model
        return type(str(name), (base_history,), attrs)


def cents_to_usd_string(cents):
    """
    Helper to convert cents as an int to dollars as a
    nicely formatted string.
    """
    if cents is None:
        return None
    return "${:,.2f}".format(float(cents) / constants.CENTS_PER_DOLLAR)


def validate_budget_deactivation_with_spend(policy_instance):
    """
    Raise a ValidationError if a policy would become inactive while having existing spend.
    The error is attached to the field(s) being changed: 'active', 'retired', or both.
    Uses original values from the database to detect changes.

    A Django setting ALLOW_BUDGET_DEACTIVATION_WITH_SPEND can be set to True
    to temporarily bypass this validation.
    """
    # Check if the Django setting allows bypassing this validation (for temporary use only)
    if getattr(settings, 'ALLOW_BUDGET_DEACTIVATION_WITH_SPEND', False):
        return

    # Only validate on updates (not creates) and only when deactivating
    if not policy_instance.pk or policy_instance.active:
        return

    # For updates, check if we're actually changing from active to inactive
    try:
        original = type(policy_instance).objects.get(pk=policy_instance.pk)
        if original.active == policy_instance.active:
            return  # No change to active status
    except ObjectDoesNotExist:
        return  # Can't determine original state, skip validation

    try:
        total_redeemed = policy_instance.total_redeemed
    except Exception as exc:
        raise ValidationError({'active': ERROR_MSG_ACTIVE_UNKNOWN_SPEND}) from exc

    if total_redeemed < 0:  # total_redeemed is negative
        raise ValidationError({'active': ERROR_MSG_ACTIVE_WITH_SPEND})
