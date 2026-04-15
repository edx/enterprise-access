"""
Python API for interacting with SubsidyAccessPolicy records.
"""
import logging
from typing import Iterable

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from requests.exceptions import HTTPError
from rest_framework import status

from enterprise_access.apps.content_assignments.api import AllocationException
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequest

from .exceptions import (
    ContentPriceNullException,
    PriceValidationError,
    SubisidyAccessPolicyRequestApprovalError,
    SubsidyAccessPolicyLockAttemptFailed
)
from .models import SubsidyAccessPolicy

logger = logging.getLogger(__name__)


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
) -> tuple:
    """
    Validate requests against the policy and allocate assignments for valid ones.

    The caller MUST hold ``policy.lock()`` before calling this function
    to prevent concurrent budget races.

    Args:
        policy: The SubsidyAccessPolicy to approve against.
        learner_credit_requests: The requests to process.

    Returns:
        A tuple of (approved_requests_map, failed_requests_by_reason) where:
        - approved_requests_map maps request UUID -> {"request": ..., "assignment": ...}
        - failed_requests_by_reason maps reason string -> list of failed requests

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
        if valid_requests:
            request_to_assignment_map = policy.approve(valid_requests)
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

        return approved_requests_map, failed_requests_by_reason

    except SubisidyAccessPolicyRequestApprovalError:
        raise
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
