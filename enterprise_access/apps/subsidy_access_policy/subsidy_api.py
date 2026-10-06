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
        raise SubsidyAPIHTTPError('HTTPError occurred in Subsidy API request.') from exc

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
        raise SubsidyAPIHTTPError('HTTPError occurred in Subsidy API request.') from exc

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
    Fetch a CSV export of Learner Credit spent transactions for a subsidy from enterprise-subsidy.

    Arguments are keyword-only, since several are optional values of the same type that are easy to swap.

    Arguments:
        subsidy_uuid (str|UUID): The subsidy whose spent transactions should be exported.
        enterprise_customer_uuid (str|UUID): The enterprise that owns the subsidy. Always forwarded so that
            enterprise-subsidy also scopes the export to this enterprise (defense in depth for cross-customer access).
        subsidy_access_policy_uuid (str|UUID, optional): Only export transactions redeemed via this policy (budget).
        search (str, optional): Free-text search filter, forwarded as-is to enterprise-subsidy.
        start_date (date, optional): Only include transactions created on/after this date.
        end_date (date, optional): Only include transactions created on/before this date (inclusive).

    Returns:
        requests.Response: the open, streamed CSV response from enterprise-subsidy, including its headers.
        The caller is responsible for closing it.

    Raises:
        SubsidyAPIHTTPError: if the Subsidy API request failed. The upstream response, if any, is already closed.
    """
    # The export is a v2 admin endpoint (it needs admin-level access to the subsidy), so always use the v2 client.
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

    # Production settings are loaded from YAML, which can only express a sequence as a list, and requests
    # raises a bare ValueError (not a RequestException) for a list timeout. Normalise it so overriding the
    # setting in config can't turn every export into a 500.
    timeout = settings.SUBSIDY_TRANSACTIONS_EXPORT_TIMEOUT
    if isinstance(timeout, (list, tuple)):
        timeout = tuple(timeout)

    try:
        # OAuthAPIClient only sets a timeout on its token fetch, so set one explicitly for this potentially slow call.
        response = client.client.get(export_url, params=query_params, stream=True, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        # Not logged here: the view logs every failure with the request's full context. Logging again would
        # duplicate the traceback, and the query params must not be logged at all because ``search`` is matched
        # against learner emails upstream, so admins type email addresses into it.
        raise SubsidyAPIHTTPError('HTTPError occurred in Subsidy API request.') from exc

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        # With stream=True the body hasn't been read, so the pooled connection isn't released until we close it.
        response.close()
        raise SubsidyAPIHTTPError('HTTPError occurred in Subsidy API request.') from exc
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
    """
    policies_by_subsidy_uuid = defaultdict(set)
    for policy in policies:
        policies_by_subsidy_uuid[policy.subsidy_uuid].add(policy)

    result = defaultdict(lambda: defaultdict(list))

    for subsidy_uuid, policies_with_subsidy in policies_by_subsidy_uuid.items():
        logger.info(f'Fetching learner transactions for subsidy {subsidy_uuid} via policies {policies_with_subsidy}')
        transactions_in_subsidy = get_and_cache_transactions_for_learner(subsidy_uuid, lms_user_id)['transactions']
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

    return result


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
