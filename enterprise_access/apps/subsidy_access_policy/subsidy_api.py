"""
Python API for fetching and interacting
with transaction and subsidy/ledger data
from the enterprise-subsidy service.
"""
import logging
from collections import defaultdict

import requests
from django.conf import settings
from edx_django_utils.cache import TieredCache

from enterprise_access.cache_utils import request_cache, versioned_cache_key

from .exceptions import SubsidyAPIHTTPError
from .utils import get_versioned_subsidy_client

logger = logging.getLogger(__name__)

REQUEST_CACHE_NAMESPACE = 'subsidy_access_policy'

CACHE_MISS = object()

# Subsidy API status codes that can mean one particular subsidy is unreachable, rather than the Subsidy API being
# unhealthy. A soft-deleted or unknown subsidy answers 403, because enterprise-subsidy resolves the permission
# context through a manager that hides soft-deleted rows; 404 covers a subsidy that disappears after that check.
# A 403 is also what we'd get if our own access to enterprise-subsidy broke, so these are only isolated while at
# least one subsidy is still reachable. Any other failure (notably a 5xx outage) affects every subsidy and is
# re-raised.
UNREACHABLE_SUBSIDY_STATUS_CODES = frozenset({403, 404})
SUBSIDY_API_HTTP_ERROR_MESSAGE = 'HTTPError occurred in Subsidy API request.'


class TransactionPolicyMismatchError(Exception):
    """
    Should be raised in a context where, for a given policy,
    if this policy's uuid doesn't match the recorded one in a transaction
    that we searched for by the policy's subsidy_uuid value.
    """


def subsidy_learner_aggregate_data_cache_key(subsidy_uuid, policy_uuid=None):
    return versioned_cache_key('get_subsidy_learners_aggregate_data', subsidy_uuid, policy_uuid)


def get_and_cache_subsidy_learners_aggregate_data(subsidy_uuid, policy_uuid=None):
    """
    Get aggregated learner data for a given subsidy. This can be optionally further filtered
    """
    cache_key = subsidy_learner_aggregate_data_cache_key(subsidy_uuid, policy_uuid)
    cached_response = TieredCache.get_cached_response(cache_key)
    if cached_response.is_found:
        logger.info(
            f'subsidy_learners_aggregate_data cache hit for subsidy {subsidy_uuid} and policy {policy_uuid}'
        )
        return cached_response.value

    client = get_versioned_subsidy_client(version=1)
    try:
        response_payload = client.get_subsidy_aggregates_by_learner_data(
            subsidy_uuid,
            policy_uuid,
        )
    except requests.exceptions.HTTPError as exc:
        raise SubsidyAPIHTTPError(SUBSIDY_API_HTTP_ERROR_MESSAGE) from exc

    results = {}
    for aggregated_data in response_payload:
        results[aggregated_data.get('lms_user_id')] = aggregated_data.get('total')
    TieredCache.set_all_tiers(cache_key, results, settings.SUBSIDY_AGGREGATES_CACHE_TIMEOUT)
    return results


def learner_transaction_cache_key(subsidy_uuid, lms_user_id):
    return versioned_cache_key('get_transactions_for_learner', subsidy_uuid, lms_user_id)


def get_and_cache_transactions_for_learner(subsidy_uuid, lms_user_id):
    """
    Get all transactions for a learner in a given subsidy.  This can
    include transactions from multiple access policies.
    """
    cache_key = learner_transaction_cache_key(subsidy_uuid, lms_user_id)
    cached_response = request_cache(namespace=REQUEST_CACHE_NAMESPACE).get_cached_response(cache_key)
    if cached_response.is_found:
        return cached_response.value

    client = get_versioned_subsidy_client()
    try:
        response_payload = client.list_subsidy_transactions(
            subsidy_uuid=subsidy_uuid,
            lms_user_id=lms_user_id,
            include_aggregates=False,
        )
    except requests.exceptions.HTTPError as exc:
        raise SubsidyAPIHTTPError(SUBSIDY_API_HTTP_ERROR_MESSAGE) from exc

    result = {
        'transactions': response_payload['results'],
        # TODO: this is some tech. debt  we're going to live with
        # for the moment in pursuit of https://2u-internal.atlassian.net/browse/ENT-7222
        'aggregates': {},
    }
    next_page = response_payload.get('next')
    while next_page:
        next_response = client.client.get(next_page)
        next_payload = next_response.json()
        result['transactions'].extend(next_payload['results'])
        next_page = next_payload.get('next')

    logger.info(
        'Fetched transactions for subsidy %s and lms_user_id %s. Number transactions = %s',
        subsidy_uuid,
        lms_user_id,
        len(result['transactions']),
    )
    request_cache(namespace=REQUEST_CACHE_NAMESPACE).set(cache_key, result)
    return result


def get_subsidy_transactions_export(
    *,
    subsidy_uuid,
    enterprise_customer_uuid,
    subsidy_access_policy_uuid=None,
    search=None,
    start_date=None,
    end_date=None,
):
    """
    Opens a streamed CSV export of a subsidy's spend from enterprise-subsidy. ``enterprise_customer_uuid`` is always
    forwarded so enterprise-subsidy also checks ownership.

    Returns the open ``requests.Response``; the caller must close it.
    Raises ``SubsidyAPIHTTPError`` on failure, with any upstream response already closed.
    """
    # The export is a v2 admin endpoint.
    client = get_versioned_subsidy_client(version=2)
    export_url = client.TRANSACTIONS_LIST_ENDPOINT.format(subsidy_uuid=subsidy_uuid) + 'export/'
    query_params = {
        'enterprise_customer_uuid': str(enterprise_customer_uuid),
    }
    if subsidy_access_policy_uuid:
        query_params['subsidy_access_policy_uuid'] = str(subsidy_access_policy_uuid)
    if search:
        query_params['search'] = search
    if start_date:
        query_params['start_date'] = start_date.isoformat()
    if end_date:
        query_params['end_date'] = end_date.isoformat()

    # YAML config can only give a list, which requests rejects with a ValueError (not a RequestException).
    timeout = settings.SUBSIDY_TRANSACTIONS_EXPORT_TIMEOUT
    if isinstance(timeout, (list, tuple)):
        timeout = tuple(timeout)

    response = None
    try:
        # OAuthAPIClient only times out its token fetch, so set one here.
        response = client.client.get(export_url, params=query_params, stream=True, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        # With stream=True the connection is only released once the response is closed.
        if response is not None:
            response.close()
        # Not logged: the view logs failures, and the params include ``search`` (possibly an email).
        raise SubsidyAPIHTTPError(SUBSIDY_API_HTTP_ERROR_MESSAGE) from exc
    return response


def get_redemptions_by_content_and_policy_for_learner(policies, lms_user_id):
    """
    Returns a mapping of content keys to a mapping of policy uuids to lists of transactions
    for the given learner, filtered to only those transactions associated with a **subsidy**
    to which any of the given **policies** are associated.

    The nice thing about ``get_and_cache_transactions_for_learner()`` is that it allows us
    to make one call per subsidy for a customer’s set of policies, to get all transactions for the learner
    and store them in a request cache for later computation (rather than making one call to the subsidy service
    per [lms_user_id, content_key, policy uuid] combination).

    This will usually result in just the one call against a given subsidy,
    based on how we want to configure our customers, but we have to deal with the
    possibility that there are multiple subsidies in play.

    This particular function takes those resulting transactions and
    maps them by content_key to maps of policy_uuid -> [transactions]
    Within the list of transactions for a given subsidy, if we come across a transaction
    with a policy uuid that’s *not* currently associated with the subsidy we requested transactions for,
    we don’t want it the mapping, because we’ll later compute aggregates for the policies’
    spend caps and learner limits based on that mapping.

    Returns:
        2-tuple of (dict, set):
            * the mapping of content_key -> {policy -> [transactions]} described above, and
            * the set of subsidy uuids that were individually unreachable (see
              ``UNREACHABLE_SUBSIDY_STATUS_CODES``). Those subsidies' policies are absent from the mapping,
              and callers should exclude them from redeemability evaluation: without the learner's
              transactions we can't compute spend against them, and evaluating them would call the same
              unreachable subsidy again.

    Raises:
        SubsidyAPIHTTPError: if fetching a subsidy's transactions failed for any other reason, e.g. a 5xx
            from the Subsidy API, which affects every subsidy rather than just one; or if every subsidy was
            unreachable.
    """
    policies_by_subsidy_uuid = defaultdict(set)
    for policy in policies:
        policies_by_subsidy_uuid[policy.subsidy_uuid].add(policy)

    result = defaultdict(lambda: defaultdict(list))
    unreachable_subsidy_uuids = set()
    last_unreachable_error = None

    for subsidy_uuid, policies_with_subsidy in policies_by_subsidy_uuid.items():
        logger.info(f'Fetching learner transactions for subsidy {subsidy_uuid} via policies {policies_with_subsidy}')
        try:
            transactions_in_subsidy = get_and_cache_transactions_for_learner(subsidy_uuid, lms_user_id)['transactions']
        except SubsidyAPIHTTPError as exc:
            status_code = getattr(exc.error_response, 'status_code', None)
            if status_code not in UNREACHABLE_SUBSIDY_STATUS_CODES:
                raise
            # A subsidy can become unreachable independently of the policies that still reference it, for example
            # when it is soft-deleted. Isolating the failure here keeps the customer's remaining, healthy subsidies
            # evaluable instead of failing the whole request.
            unreachable_subsidy_uuids.add(subsidy_uuid)
            last_unreachable_error = exc
            logger.warning(
                'Could not fetch learner transactions for subsidy %s (subsidy_status_code=%s). Excluding its '
                'policies %s from redeemability evaluation for lms_user_id %s.',
                subsidy_uuid,
                status_code,
                sorted(str(policy.uuid) for policy in policies_with_subsidy),
                lms_user_id,
            )
            continue
        for redemption in transactions_in_subsidy:
            transaction_uuid = redemption['uuid']
            content_key = redemption['content_key']
            subsidy_access_policy_uuid = redemption['subsidy_access_policy_uuid']

            matching_policies = [
                policy for policy in policies_with_subsidy
                if str(policy.uuid) == subsidy_access_policy_uuid
            ]
            # We can assume there's at most one matching policy because the ``policies`` arg passed into this function
            # should not contain duplicates.
            matching_policy = matching_policies[0] if matching_policies else None
            if matching_policy:
                result[content_key][matching_policy].append(redemption)
            else:
                logger.warning(
                    f"Transaction {transaction_uuid} has unmatched policy uuid for subsidy {subsidy_uuid}: "
                    f"Found policy uuid {subsidy_access_policy_uuid} that is no longer tied to this subsidy."
                )

    if last_unreachable_error and unreachable_subsidy_uuids == set(policies_by_subsidy_uuid):
        # Every subsidy refused us, which more likely means our own access to enterprise-subsidy is broken than
        # that every subsidy was deleted. Fail loudly rather than tell the learner nothing is available.
        raise last_unreachable_error

    return result, unreachable_subsidy_uuids


def get_tiered_cache_subsidy_record(subsidy_uuid, *cache_key_args):
    """
    Gets the subsidy record (a dictionary) with the given ``subsidy_uuid``
    from the TieredCache (meaning memcache) if present.
    If not present, returns a ``CACHE_MISS`` object.
    """
    cache_key = versioned_cache_key('get_subsidy_record', subsidy_uuid, *cache_key_args)
    cached_response = TieredCache.get_cached_response(cache_key)
    if cached_response.is_found:
        logger.info(f"cache hit for subsidy record {subsidy_uuid} record")
        return cached_response.value

    logger.info(f"cache miss for subsidy record {subsidy_uuid}")
    return CACHE_MISS


def set_tiered_cache_subsidy_record(subsidy_record, *cache_key_args):
    """
    Sets the given subsidy_record in the TieredCache by uuid and any additional
    provided args.
    """
    subsidy_uuid = subsidy_record['uuid']
    cache_key = versioned_cache_key('get_subsidy_record', subsidy_uuid, *cache_key_args)
    logger.info(f"cache set for subsidy record {subsidy_uuid}")
    TieredCache.set_all_tiers(cache_key, subsidy_record, settings.SUBSIDY_RECORD_CACHE_TIMEOUT)
