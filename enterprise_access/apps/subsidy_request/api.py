"""
Primary Python API for interacting with Subsidy Request
records and business logic.
"""

import logging
from typing import Iterable

from django.db import transaction

from enterprise_access.apps.content_assignments import api as assignments_api
from enterprise_access.apps.subsidy_access_policy.api import get_policy_for_approval, validate_and_allocate
from enterprise_access.apps.subsidy_access_policy.exceptions import (
    SubisidyAccessPolicyRequestApprovalError,
    SubsidyAccessPolicyLockAttemptFailed
)
from enterprise_access.apps.subsidy_request.constants import (
    APPROVABLE_STATES,
    CANCELABLE_STATES,
    DECLINABLE_STATES,
    REMINDABLE_STATES,
    LearnerCreditAdditionalActionStates,
    LearnerCreditRequestActionErrorReasons,
    SubsidyRequestStates
)
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequest, LearnerCreditRequestActions
from enterprise_access.apps.subsidy_request.tasks import (
    send_learner_credit_bnr_cancel_notification_task,
    send_learner_credit_bnr_decline_notification_task,
    send_learner_credit_bnr_request_approve_task,
    send_reminder_email_for_pending_learner_credit_request
)
from enterprise_access.apps.subsidy_request.utils import (
    get_action_choice,
    get_error_reason_choice,
    get_user_message_choice
)
from enterprise_access.utils import format_traceback, localized_utcnow

logger = logging.getLogger(__name__)


def approve_learner_credit_requests(
    learner_credit_requests: Iterable[LearnerCreditRequest],
    policy_uuid: str,
    reviewer: object,
) -> dict:
    """
    Bulk approve Learner Credit Requests against a specific policy.

    Acquires the policy lock and performs validation, assignment allocation,
    request state update, and audit trail creation in a single transaction.
    On approval failures, makes a best-effort attempt to record failure
    actions for the affected requests.
    """
    requests_to_process = [
        req for req in learner_credit_requests
        if req.state in APPROVABLE_STATES
    ]
    if not requests_to_process:
        # 'failed' = pre-filtered by state (never reached the policy).
        # 'failed_approval' stays empty because nothing entered the approval flow.
        return {
            "approved": [],
            "failed": list(learner_credit_requests),
            "failed_approval": [],
            "error_message": None,
        }

    try:
        policy = get_policy_for_approval(policy_uuid)
        with policy.lock():
            return _approve_under_lock(policy, requests_to_process, reviewer)
    except SubsidyAccessPolicyLockAttemptFailed as exc:
        error_message = f"Failed to acquire lock for policy {policy_uuid}. Try again later."
        logger.warning("Bulk approval failed for policy %s: lock not acquired.", policy_uuid)
        _record_failure_actions(requests_to_process, exc)
        return {
            "approved": [],
            "failed": [],
            "failed_approval": requests_to_process,
            "error_message": error_message,
        }
    except SubisidyAccessPolicyRequestApprovalError as exc:
        logger.warning(
            "Bulk approval failed for policy %s with a global error. Reason: %s", policy_uuid, exc.message,
        )
        _record_failure_actions(requests_to_process, exc)
        return {
            "approved": [],
            "failed": [],
            "failed_approval": requests_to_process,
            "error_message": exc.message,
        }
    except Exception as exc:
        # Sanitized for the client response; full exception + traceback stay in logs
        # (via logger.exception) and in the LearnerCreditRequestActions audit row.
        error_message = "Unexpected error during approval."
        logger.exception("Unexpected error approving requests for policy %s", policy_uuid)
        _record_failure_actions(requests_to_process, exc)
        return {
            "approved": [],
            "failed": [],
            "failed_approval": requests_to_process,
            "error_message": error_message,
        }


def _approve_under_lock(policy, requests_to_process, reviewer):
    """
    Perform the full approval flow while the policy lock is held.

    All DB mutations (assignment allocation, request state update, and audit trail)
    occur in a single transaction. If any step fails, everything rolls back.
    """
    approved_requests = []
    failed_requests = []
    actions_to_create = []

    with transaction.atomic():
        # This call runs inside the outer transaction opened here; any nested
        # savepoint behavior depends on validate_and_allocate() or code it calls.
        approved_requests_map, failed_requests_by_reason = validate_and_allocate(
            policy, requests_to_process,
        )

        approved_requests = _prepare_requests_for_update(approved_requests_map, reviewer)
        failed_requests, failed_actions = _prepare_failed_requests_and_actions(failed_requests_by_reason)
        actions_to_create.extend(failed_actions)

        if approved_requests:
            approved_requests = _update_and_refresh_requests(
                approved_requests, ['state', 'assignment', 'reviewer', 'reviewed_at'],
            )

        actions_to_create.extend(_build_success_action(request) for request in approved_requests)

        LearnerCreditRequestActions.bulk_create(actions_to_create)

        # on_commit registered inside atomic() fires once the outer transaction commits —
        # so notifications never run if we roll back.
        for request in approved_requests:
            transaction.on_commit(
                lambda assignment_uuid=request.assignment.uuid:
                    send_learner_credit_bnr_request_approve_task.delay(assignment_uuid)
            )

    return {
        "approved": approved_requests,
        "failed": [],
        "failed_approval": failed_requests,
        "error_message": None,
    }


def _build_success_action(request):
    return LearnerCreditRequestActions(
        learner_credit_request=request,
        recent_action=get_action_choice(SubsidyRequestStates.APPROVED),
        status=get_user_message_choice(SubsidyRequestStates.APPROVED),
    )


def _build_failure_action(request, traceback_str):
    return LearnerCreditRequestActions(
        learner_credit_request=request,
        recent_action=get_action_choice(SubsidyRequestStates.APPROVED),
        status=get_user_message_choice(SubsidyRequestStates.REQUESTED),
        error_reason=LearnerCreditRequestActionErrorReasons.FAILED_APPROVAL,
        traceback=traceback_str,
    )


def _record_failure_actions(requests, exception):
    """
    Record failure audit trail for all requests.

    Runs in its own transaction since the main transaction may have rolled back.
    Swallows exceptions to avoid masking the original error.
    """
    traceback_str = format_traceback(exception)
    actions = [_build_failure_action(request, traceback_str) for request in requests]
    try:
        with transaction.atomic():
            LearnerCreditRequestActions.bulk_create(actions)
    except Exception:
        logger.exception(
            "Failed to record failure audit trail for approval attempt. "
            "Original exception: %r. Request uuids: %s",
            exception, [str(r.uuid) for r in requests],
        )


def _update_and_refresh_requests(requests_to_update, fields_to_update):
    """
    Helper to bulk update LearnerCreditRequest records and refresh their state from the DB,
    mirroring the pattern in the content_assignments API.
    """
    if not requests_to_update:
        return []

    LearnerCreditRequest.bulk_update(requests_to_update, fields_to_update)

    return list(
        LearnerCreditRequest.objects.select_related(
            'assignment'
        ).prefetch_related(
            'actions'
        ).filter(
            uuid__in=[record.uuid for record in requests_to_update]
        )
    )


def _prepare_requests_for_update(approved_requests_map, reviewer):
    """
    Prepares successful LearnerCreditRequest objects for bulk_update.
    Does NOT prepare actions.
    """
    requests_to_update = []
    if approved_requests_map:
        requests_to_update = [item["request"] for item in approved_requests_map.values()]
        for request in requests_to_update:
            request.state = SubsidyRequestStates.APPROVED
            request.reviewer = reviewer
            request.reviewed_at = localized_utcnow()
            request.assignment = approved_requests_map[request.uuid]["assignment"]
    return requests_to_update


def _prepare_failed_requests_and_actions(failed_requests_by_reason):
    """
    Prepares failure action objects for bulk create and returns the list of failed requests.
    """
    all_failed_requests = []
    actions_to_create = []
    for reason, requests in failed_requests_by_reason.items():
        for request in requests:
            failure_reason_str = getattr(request, 'failure_reason', reason)
            actions_to_create.append(_build_failure_action(
                request, traceback_str=f"Validation failed with reason: {failure_reason_str}",
            ))
        all_failed_requests.extend(requests)
    return all_failed_requests, actions_to_create


def remind_learner_credit_requests(
    learner_credit_requests: Iterable[LearnerCreditRequest],
) -> dict:
    """
    Send reminder emails for learner credit requests.

    Filters requests to only those that are:
    - In APPROVED state
    - Have an associated assignment

    For each remindable request, queues a Celery task to send the reminder email
    and creates a REMINDED action record.

    Args:
        learner_credit_requests: Iterable of LearnerCreditRequest objects to potentially remind.

    Returns:
        dict: Contains:
            - 'remindable': List of LearnerCreditRequest objects that were reminded
            - 'non_remindable': List of LearnerCreditRequest objects that could not be reminded
    """
    remindable = []
    non_remindable = []

    for request in learner_credit_requests:
        if request.state in REMINDABLE_STATES and request.assignment:
            remindable.append(request)
        else:
            non_remindable.append(request)

    if not remindable:
        return {'remindable': [], 'non_remindable': non_remindable}

    # Bulk create audit action records
    actions_to_create = [
        LearnerCreditRequestActions(
            learner_credit_request=request,
            recent_action=get_action_choice(LearnerCreditAdditionalActionStates.REMINDED),
            status=get_user_message_choice(LearnerCreditAdditionalActionStates.REMINDED),
        ) for request in remindable
    ]
    with transaction.atomic():
        LearnerCreditRequestActions.bulk_create(actions_to_create)

    # Queue email tasks — remind doesn't change request state, so on_commit is not needed.
    for request in remindable:
        send_reminder_email_for_pending_learner_credit_request.delay(request.assignment.uuid)

    return {
        'remindable': remindable,
        'non_remindable': non_remindable,
    }


def cancel_learner_credit_requests(
    learner_credit_requests: Iterable[LearnerCreditRequest],
    reviewer,
) -> dict:
    """
    Cancel learner credit requests using bulk operations.

    Filters requests to only those that are:
    - In APPROVED state
    - Have an associated assignment

    For each cancelable request, cancels the assignment, updates the request state,
    creates a CANCELLED action record, and queues a Celery task to send the cancellation email.
    Uses bulk operations and transactions for better performance and data integrity.

    Args:
        learner_credit_requests: Iterable of LearnerCreditRequest objects to potentially cancel.
        reviewer: The user performing the cancellation.

    Returns:
        dict: Contains:
            - 'cancelable': List of LearnerCreditRequest objects that were cancelled
            - 'non_cancelable': List of LearnerCreditRequest objects that could not be cancelled
    """
    cancelable = []
    non_cancelable = []

    for request in learner_credit_requests:
        if request.state in CANCELABLE_STATES and request.assignment:
            cancelable.append(request)
        else:
            non_cancelable.append(request)

    if not cancelable:
        return {
            'cancelable': cancelable,
            'non_cancelable': non_cancelable,
        }

    # Collect assignments to cancel
    assignments_to_cancel = [req.assignment for req in cancelable]

    # Bulk update cancelable requests and prepare actions
    reviewed_at = localized_utcnow()
    for request in cancelable:
        request.state = SubsidyRequestStates.CANCELLED
        request.reviewer = reviewer
        request.reviewed_at = reviewed_at

    actions_to_create = []

    # Use transaction to ensure atomic updates - cancel assignments, update requests, and create actions together
    # This prevents partial failures where assignments are cancelled but request state isn't updated
    with transaction.atomic():
        # Cancel assignments within the transaction
        cancel_response = assignments_api.cancel_assignments(assignments_to_cancel, False)

        if cancel_response.get('non_cancelable'):
            # If any assignments failed to cancel, mark those requests as non_cancelable
            non_cancelable_assignment_ids = {a.uuid for a in cancel_response['non_cancelable']}
            actually_cancelable = []
            for request in cancelable:
                if request.assignment.uuid in non_cancelable_assignment_ids:
                    non_cancelable.append(request)
                    # Reset state since cancellation failed
                    request.state = SubsidyRequestStates.APPROVED
                    request.reviewer = None
                    request.reviewed_at = None
                    # Add error action for non-cancelable (assignment cancellation failure)
                    actions_to_create.append(
                        LearnerCreditRequestActions(
                            learner_credit_request=request,
                            recent_action=get_action_choice(SubsidyRequestStates.CANCELLED),
                            status=get_user_message_choice(SubsidyRequestStates.APPROVED),
                            error_reason=get_error_reason_choice(
                                LearnerCreditRequestActionErrorReasons.FAILED_CANCELLATION
                            ),
                            traceback=f"Failed to cancel assignment {request.assignment.uuid}",
                        )
                    )
                else:
                    actually_cancelable.append(request)
            cancelable = actually_cancelable

        if not cancelable:
            # Create error actions if any exist
            if actions_to_create:
                LearnerCreditRequestActions.bulk_create(actions_to_create)
            return {
                'cancelable': cancelable,
                'non_cancelable': non_cancelable,
            }

        # Use the model's bulk_update method to preserve audit/history consistency
        LearnerCreditRequest.bulk_update(
            cancelable,
            ['state', 'reviewer', 'reviewed_at']
        )

        # Refresh from DB to get updated state
        cancelable = list(LearnerCreditRequest.objects.filter(
            uuid__in=[req.uuid for req in cancelable]
        ).select_related('assignment'))

        # Create success actions for cancelled requests
        success_actions = [
            LearnerCreditRequestActions(
                learner_credit_request=request,
                recent_action=get_action_choice(SubsidyRequestStates.CANCELLED),
                status=get_user_message_choice(SubsidyRequestStates.CANCELLED),
            ) for request in cancelable
        ]
        actions_to_create.extend(success_actions)

        # Bulk create all actions (errors + successes)
        if actions_to_create:
            LearnerCreditRequestActions.bulk_create(actions_to_create)

    # Enqueue notification tasks after commit to avoid running against partially-committed state
    for request in cancelable:
        transaction.on_commit(
            lambda assignment_uuid=request.assignment.uuid: send_learner_credit_bnr_cancel_notification_task.delay(
                str(assignment_uuid)
            )
        )

    return {
        'cancelable': cancelable,
        'non_cancelable': non_cancelable,
    }


def decline_learner_credit_requests(
    learner_credit_requests: Iterable[LearnerCreditRequest],
    reviewer: object,
    reason: str = None,
) -> dict:
    """
    Bulk decline Learner Credit Requests.

    Filters to only declinable requests, bulk updates their state, creates audit
    action records, and queues decline notification emails.

    Args:
        learner_credit_requests: Iterable of LearnerCreditRequest objects to decline.
        reviewer: The user performing the decline.
        reason: Optional decline reason string.

    Returns:
        dict with 'declined' and 'non_declinable' lists of LearnerCreditRequest objects.
    """
    declinable_requests = []
    non_declinable_requests = []

    for request in learner_credit_requests:
        if request.state in DECLINABLE_STATES:
            declinable_requests.append(request)
        else:
            non_declinable_requests.append(request)

    logger.info('Skipping %d non-declinable requests.', len(non_declinable_requests))
    logger.info('Declining %d requests.', len(declinable_requests))

    if not declinable_requests:
        return {'declined': [], 'non_declinable': non_declinable_requests}

    # Bulk update state
    with transaction.atomic():
        LearnerCreditRequest.bulk_decline_requests(declinable_requests, reviewer, reason=reason)

    declined_requests = list(
        LearnerCreditRequest.objects.prefetch_related('actions').filter(
            uuid__in=[r.uuid for r in declinable_requests],
        )
    )

    # Bulk create audit action records
    actions_to_create = [
        LearnerCreditRequestActions(
            learner_credit_request=request,
            recent_action=get_action_choice(SubsidyRequestStates.DECLINED),
            status=get_user_message_choice(SubsidyRequestStates.DECLINED),
        ) for request in declined_requests
    ]
    if actions_to_create:
        with transaction.atomic():
            LearnerCreditRequestActions.bulk_create(actions_to_create)

    # Queue notification emails after commit
    for request in declined_requests:
        transaction.on_commit(
            lambda request_uuid=request.uuid: send_learner_credit_bnr_decline_notification_task.delay(
                str(request_uuid)
            )
        )

    return {'declined': declined_requests, 'non_declinable': non_declinable_requests}
