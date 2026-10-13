"""
Python API for interacting with SubsidyAccessPolicy records.
"""
import logging
from typing import Iterable, TypedDict

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.utils import timezone
from requests.exceptions import HTTPError
from rest_framework import status

from enterprise_access.apps.content_assignments.api import AllocationException
from enterprise_access.apps.content_assignments.constants import AssignmentSources
from enterprise_access.apps.core.models import User
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequest
from enterprise_access.utils import format_traceback, localized_utcnow

from .exceptions import (
    ContentPriceNullException,
    PriceValidationError,
    SubisidyAccessPolicyRequestApprovalError,
    SubsidyAccessPolicyLockAttemptFailed,
    SubsidyAPIHTTPError
)
from .models import ForcedPolicyRedemption, SubsidyAccessPolicy

logger = logging.getLogger(__name__)

FORCED_REDEMPTION_CURRENT_TIME_KEY = 'geag_terms_accepted_at'
FORCED_REDEMPTION_DATA_SHARE_CONSENT_KEY = 'geag_data_share_consent'
FORCED_REDEMPTION_EMAIL_KEY = 'geag_email'
GEAG_DATETIME_FMT = '%Y-%m-%dT%H:%M:%SZ'


class GeagMetadata(TypedDict, total=False):
    """
    Exec Ed (GEAG) values a forced redemption copies into its transaction metadata. They're learner PII and
    aren't persisted on ``ForcedPolicyRedemption``. All values are strings, so this can be passed to a celery task.
    """
    geag_first_name: str
    geag_last_name: str
    geag_date_of_birth: str  # YYYY-MM-DD
    fallback_external_fulfillment_reference_id: str  # UUID of an existing GEAG order


def get_subsidy_access_policy(uuid):
    """
    Returns a `SubsidyAccessPolicy` record with the given uuid,
    or null if no such record exists.
    """
    try:
        return SubsidyAccessPolicy.objects.get(uuid=uuid)
    except SubsidyAccessPolicy.DoesNotExist:
        return None


def get_policy_for_approval(policy_uuid):
    """
    Fetch and validate that a policy exists for approval.

    Raises:
        SubisidyAccessPolicyRequestApprovalError: If the policy does not exist.
    """
    policy = get_subsidy_access_policy(policy_uuid)
    if not policy:
        error_msg = f"Policy with UUID {policy_uuid} does not exist."
        logger.error(error_msg)
        raise SubisidyAccessPolicyRequestApprovalError(
            message=error_msg,
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return policy


def validate_and_allocate(
    policy: SubsidyAccessPolicy,
    learner_credit_requests: Iterable[LearnerCreditRequest],
    actor_lms_user_id=None,
    source=AssignmentSources.API,
    correlation_id=None,
) -> tuple:
    """
    Validate requests against the policy and allocate assignments for valid ones.

    The caller MUST hold ``policy.lock()`` before calling this function
    to prevent concurrent budget races.

    Args:
        policy: The SubsidyAccessPolicy to approve against.
        learner_credit_requests: The requests to process.
        actor_lms_user_id, source, correlation_id: passed through to the resulting
            ``LearnerContentAssignmentAction`` audit rows.

    Returns:
        A tuple of (approved_requests_map, failed_requests_by_reason, pending_allocation_actions) where:
        - approved_requests_map maps request UUID -> {"request": ..., "assignment": ...}
        - failed_requests_by_reason maps reason string -> list of failed requests
        - pending_allocation_actions is a list of unsaved ``allocated``/``reallocated``
          ``LearnerContentAssignmentAction`` instances that the caller must persist
          (e.g. via ``bulk_create``)

    Raises:
        SubisidyAccessPolicyRequestApprovalError: On validation failure, allocation error,
            or internal consistency error.
    """
    try:
        validation_result = policy.can_approve(learner_credit_requests)

        error_reason = validation_result.get("error_reason", '')
        if error_reason:
            raise SubisidyAccessPolicyRequestApprovalError(
                message=error_reason,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        valid_requests = validation_result.get("valid_requests", [])
        failed_requests_by_reason = validation_result.get("failed_requests_by_reason", {})

        approved_requests_map = {}
        pending_allocation_actions = []
        if valid_requests:
            request_to_assignment_map, pending_allocation_actions = policy.approve(
                valid_requests,
                actor_lms_user_id=actor_lms_user_id,
                source=source,
                correlation_id=correlation_id,
            )
            for request in valid_requests:
                assignment = request_to_assignment_map.get(request.uuid)
                if not assignment:
                    raise SubisidyAccessPolicyRequestApprovalError(
                        f"Consistency Error: Missing assignment for approved request {request.uuid}"
                    )
                approved_requests_map[request.uuid] = {
                    "request": request,
                    "assignment": assignment,
                }

        return approved_requests_map, failed_requests_by_reason, pending_allocation_actions

    except (
        AllocationException, PriceValidationError, ValidationError, DatabaseError,
        HTTPError, ConnectionError, ContentPriceNullException,
    ) as exc:
        logger.exception(
            "A validation or database error occurred during bulk approval for policy %s: %s",
            policy.uuid, exc,
        )
        raise SubisidyAccessPolicyRequestApprovalError(
            message=str(exc),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc


def force_redeem_with_geag_metadata(
    forced_redemption: ForcedPolicyRedemption,
    geag_metadata: GeagMetadata,
    actor_lms_user_id: int | None,
    suppress_assignment_email: bool = False,
) -> None:
    """
    Calls ``forced_redemption.force_redeem()``. If ``geag_metadata`` has any non-empty values, they're added to
    the transaction metadata along with the other fields GEAG requires (terms acceptance, consent, email).

    Any failure is recorded on the record (``errored_at``/``traceback``) and re-raised.
    """
    try:
        # Drop empty values so callers like admin forms can pass '' for blank fields.
        extra_metadata: dict = {key: value for key, value in geag_metadata.items() if value}
        if extra_metadata:
            user_record = User.objects.get(lms_user_id=forced_redemption.lms_user_id)
            extra_metadata[FORCED_REDEMPTION_CURRENT_TIME_KEY] = timezone.now().strftime(GEAG_DATETIME_FMT)
            extra_metadata[FORCED_REDEMPTION_DATA_SHARE_CONSENT_KEY] = True
            extra_metadata[FORCED_REDEMPTION_EMAIL_KEY] = user_record.email
        forced_redemption.force_redeem(
            extra_metadata=extra_metadata or None,
            actor_lms_user_id=actor_lms_user_id,
            suppress_assignment_email=suppress_assignment_email,
        )
    except (SubsidyAccessPolicyLockAttemptFailed, SubsidyAPIHTTPError):
        raise  # force_redeem() already recorded these, with the subsidy API response payload.
    except Exception as exc:
        forced_redemption.errored_at = localized_utcnow()
        forced_redemption.traceback = format_traceback(exc)
        forced_redemption.save()
        raise
