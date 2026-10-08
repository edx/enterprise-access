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

# HTTP status codes from the subsidy service that mean *one specific subsidy* is unusable
# (missing, soft-deleted, or otherwise inaccessible), as opposed to the subsidy service as
# a whole being unhealthy.  Note that enterprise-subsidy answers a request for a subsidy it
# cannot find with a 403 rather than a 404, because its RBAC context is derived from the
# subsidy record itself and cannot be resolved without it.
UNREACHABLE_SUBSIDY_STATUS_CODES = (403, 404)


def _indicates_unreachable_subsidy(exc):
    """
    Returns True if the given ``SubsidyAPIHTTPError`` indicates that a single subsidy is
    unusable, rather than a broader failure of the subsidy service.

    We deliberately do *not* treat other statuses (5xx, and anything else) this way: those
    are plausibly transient, and silently excluding policies because of a blip would tell
    learners that content is unavailable when it is not.
    """
    error_response = exc.error_response
    if error_response is None:
        return False
    return error_response.status_code in UNREACHABLE_SUBSIDY_STATUS_CODES


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

    If any individual subsidy is unreachable (see ``_indicates_unreachable_subsidy()``), that
    subsidy is skipped and its uuid is returned to the caller, so that the caller can exclude
    the affected policies from redemption entirely.  We deliberately exclude those policies
    rather than treating them as having no transactions: this mapping feeds per-learner spend
    caps and enrollment limits, so an empty transaction list would under-count spend and could
    allow policies on the same subsidy to over-redeem.

    Returns:
        tuple of (dict, set): the content_key -> {policy: [transactions]} mapping described
        above, and the set of subsidy uuids that could not be reached.
    """
    policies_by_subsidy_uuid = defaultdict(set)
    for policy in policies:
        policies_by_subsidy_uuid[policy.subsidy_uuid].add(policy)

    result = defaultdict(lambda: defaultdict(list))
    unreachable_subsidy_uuids = set()

    for subsidy_uuid, policies_with_subsidy in policies_by_subsidy_uuid.items():
        logger.info(f'Fetching learner transactions for subsidy {subsidy_uuid} via policies {policies_with_subsidy}')
        try:
            transactions_in_subsidy = get_and_cache_transactions_for_learner(
                subsidy_uuid, lms_user_id,
            )['transactions']
        except SubsidyAPIHTTPError as exc:
            if not _indicates_unreachable_subsidy(exc):
                raise
            unreachable_subsidy_uuids.add(subsidy_uuid)
            # Logged at ERROR because this is nearly always a misconfiguration that silently
            # blocks learners: a policy still pointing at a subsidy that no longer resolves.
            # It is also the only signal distinguishing that case from a genuine permissions
            # regression, so it needs to stay visible.
            logger.error(
                'Subsidy %s is unreachable (status %s); excluding its policies %s from redemption '
                'for lms_user_id %s. Check whether the subsidy record still exists.',
                subsidy_uuid,
                exc.error_response.status_code,
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
