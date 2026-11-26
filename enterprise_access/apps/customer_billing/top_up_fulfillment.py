"""
Fulfillment steps for learner credit top-ups (``LearnerCreditTopUp``).

A paid top-up is fulfilled in two steps, each safe to run again:

1. Deposit (``paid -> deposited``): not built yet (impl-plan chunk 8).
2. Spend limit (``deposited -> fulfilled``): ``fulfill_spend_limit_step()``.

A step that fails leaves the top-up in the state it started in, and raises
``RetryableFulfillmentError`` when a later run may succeed, or ``NonRetryableFulfillmentError``
when a later run would fail the same way until someone fixes the cause. The caller (the
fulfillment task, chunk 10) decides what to do with each.
"""
import logging

import requests
from django.core.exceptions import ValidationError
from django.db import transaction

from enterprise_access.apps.customer_billing.constants import LearnerCreditTopUpState
from enterprise_access.apps.customer_billing.models import LearnerCreditTopUp
from enterprise_access.apps.subsidy_access_policy.models import SubsidyAccessPolicy

logger = logging.getLogger(__name__)


class LearnerCreditTopUpFulfillmentError(Exception):
    """
    A fulfillment step failed. The top-up is left in the state the step started in.
    """


class RetryableFulfillmentError(LearnerCreditTopUpFulfillmentError):
    """
    A later run of the step may succeed, for example once enterprise-subsidy answers again.
    """


class NonRetryableFulfillmentError(LearnerCreditTopUpFulfillmentError):
    """
    A later run of the step would fail the same way until someone fixes the cause.
    """


def fulfill_spend_limit_step(top_up: LearnerCreditTopUp) -> bool:
    """
    Make a deposited top-up's funds spendable: raise its budget's ``spend_limit`` by
    ``amount_cents``, once, and move the top-up to ``fulfilled``.

    The step runs only from ``deposited``, and checks the stored state rather than ``top_up``'s,
    so a second run, or a run racing this one, does nothing. ``top_up`` is reloaded from the
    database: unsaved changes to it are discarded.

    1. The subsidy record is read fresh, before any lock. ``save()`` validates the new spend limit
       against the subsidy's ``total_deposits``, and a record cached earlier in the request (by the
       eligibility check) or by an earlier task in the same worker predates the deposit.
    2. One ``transaction.atomic()`` block locks the policy, then the top-up (always in that order,
       so two steps for one budget can't deadlock), raises ``spend_limit`` and moves the top-up to
       ``fulfilled``. ``save()`` reads the record cached in step 1, so the block makes no network
       call. A policy with no ``spend_limit`` is left unchanged, and both audit fields stay null.
    3. If the raised spend limit fails validation, the block rolls back, and the subsidy is read
       again to tell a real failure from a race (see ``_validation_failure()``).

    Call it outside any transaction. Inside one, the block is a savepoint, and its row locks are
    held until the outer transaction ends, so the read in step 3 would run under them.

    Returns:
        True if this call fulfilled the top-up. False if the top-up was not ``deposited``: nothing
        was changed.

    Raises:
        RetryableFulfillmentError: the subsidy record couldn't be read (an HTTP error, a connection
            error or a timeout), or the raised spend limit failed validation but the subsidy's
            ``total_deposits`` grew while this run held no lock. Nothing was changed.
        NonRetryableFulfillmentError: the raised spend limit failed the policy's validation, and
            the subsidy's ``total_deposits`` hadn't changed. Nothing was changed.
    """
    top_up.refresh_from_db()
    if top_up.state != LearnerCreditTopUpState.DEPOSITED:
        logger.info(
            'Skipping the spend-limit step for LearnerCreditTopUp %s: it is %s, not %s.',
            top_up.uuid, top_up.state, LearnerCreditTopUpState.DEPOSITED,
        )
        return False

    total_deposits_read = _read_fresh_subsidy_record(top_up)

    try:
        with transaction.atomic():
            policy = SubsidyAccessPolicy.objects.select_for_update().get(uuid=top_up.subsidy_access_policy_id)
            top_up.refresh_from_db(from_queryset=LearnerCreditTopUp.objects.select_for_update())
            if top_up.state != LearnerCreditTopUpState.DEPOSITED:
                logger.info(
                    'Skipping the spend-limit step for LearnerCreditTopUp %s: another run moved it to %s.',
                    top_up.uuid, top_up.state,
                )
                return False

            spend_limit_before = policy.spend_limit
            spend_limit_after = None
            if spend_limit_before is not None:
                spend_limit_after = spend_limit_before + top_up.amount_cents
                policy.spend_limit = spend_limit_after
                # Recorded on the policy's history record, next to the spend_limit change.
                policy._change_reason = f'Learner credit top-up {top_up.uuid}'  # pylint: disable=protected-access
                policy.save()

            top_up.transition_to(
                LearnerCreditTopUpState.FULFILLED,
                spend_limit_before=spend_limit_before,
                spend_limit_after=spend_limit_after,
            )
    except ValidationError as exc:
        # Only policy.save() raises ValidationError in the block. The block has rolled back, releasing its locks.
        raise _validation_failure(top_up, total_deposits_read, exc) from exc

    logger.info(
        'Fulfilled LearnerCreditTopUp %s: policy %s spend_limit %s -> %s.',
        top_up.uuid, top_up.subsidy_access_policy_id, spend_limit_before, spend_limit_after,
    )
    return True


def _validation_failure(
    top_up: LearnerCreditTopUp,
    total_deposits_read: int,
    error: ValidationError,
) -> LearnerCreditTopUpFulfillmentError:
    """
    Return the error to raise when the raised spend limit failed the policy's validation. Call it after the block
    has rolled back, so no lock is held.

    The validation compares the spend limits of the subsidy's policies, read under the lock, with the
    ``total_deposits`` read before it (``total_deposits_read``). If another top-up of the same subsidy was deposited
    and fulfilled in between, the spend limits include its raise but the deposits read don't include its deposit, so
    the validation fails although a run with a fresh read would pass. The subsidy is read again to tell the two
    apart: if ``total_deposits`` grew, or the read fails, a later run may succeed.
    """
    try:
        total_deposits_now = _read_fresh_subsidy_record(top_up)
    except RetryableFulfillmentError:
        total_deposits_now = None

    message = (
        f'Raising the spend_limit of policy {top_up.subsidy_access_policy_id} by {top_up.amount_cents} for '
        f'LearnerCreditTopUp {top_up.uuid} failed validation: {error}'
    )
    if total_deposits_now is None or total_deposits_now > total_deposits_read:
        logger.warning(
            '%s Retryable: the subsidy total_deposits read before the lock was %s, and is now %s.',
            message, total_deposits_read, 'unknown' if total_deposits_now is None else total_deposits_now,
        )
        return RetryableFulfillmentError(message)

    logger.error('%s Not retryable: the subsidy total_deposits is still %s.', message, total_deposits_now)
    return NonRetryableFulfillmentError(message)


def _read_fresh_subsidy_record(top_up: LearnerCreditTopUp) -> int:
    """
    Replace the request-cached subsidy record of ``top_up``'s policy with one fetched now, and return the
    subsidy's ``total_deposits``.

    Clearing comes first because ``subsidy_record()`` returns a cached record even with
    ``raise_on_error=True``. The cache is keyed by the enterprise and the subsidy, so the locked copy of
    the policy that ``save()`` validates reads this same record.

    Raises:
        RetryableFulfillmentError: the read failed.
    """
    policy = top_up.subsidy_access_policy
    policy.clear_cached_subsidy_record()
    try:
        policy.subsidy_record(raise_on_error=True)
    except requests.exceptions.RequestException as exc:
        # SubsidyAPIHTTPError (any HTTP error status) is a RequestException, and so are connection errors and
        # timeouts, which subsidy_record() lets through unwrapped.
        logger.warning(
            'Spend-limit step for LearnerCreditTopUp %s could not read subsidy %s: %r',
            top_up.uuid, policy.subsidy_uuid, exc,
        )
        raise RetryableFulfillmentError(
            f'Could not read subsidy {policy.subsidy_uuid} for LearnerCreditTopUp {top_up.uuid}.'
        ) from exc
    return policy.total_deposits_for_subsidy
