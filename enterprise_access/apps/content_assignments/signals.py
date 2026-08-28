"""
Signal handlers for content_assignments app.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from openedx_events.enterprise.signals import LEDGER_TRANSACTION_REVERSED
from simple_history.utils import bulk_create_with_history

from enterprise_access.apps.content_assignments.constants import (
    AssignmentActions,
    AssignmentActorTypes,
    AssignmentSources,
    LearnerContentAssignmentStateChoices
)
from enterprise_access.apps.content_assignments.models import (
    BULK_OPERATION_BATCH_SIZE,
    LearnerContentAssignment,
    LearnerContentAssignmentAction
)
from enterprise_access.apps.core.models import User
from enterprise_access.apps.subsidy_request.models import (
    LearnerCreditRequestActionErrorReasons,
    LearnerCreditRequestActions,
    SubsidyRequestStates
)
from enterprise_access.apps.subsidy_request.utils import (
    get_action_choice,
    get_error_reason_choice,
    get_user_message_choice
)
from enterprise_access.utils import format_traceback

logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def update_assignment_lms_user_id_from_user_email(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Post save hook to update assignment lms_user_id from core user records.
    """
    user = kwargs['instance']
    if user.lms_user_id:
        assignments_to_update = list(
            LearnerContentAssignment.objects.filter(
                learner_email__iexact=user.email,
                lms_user_id=None,
            ).select_related('assignment_configuration')
        )

        # Update multiple assignments in a history-safe way.
        for assignment in assignments_to_update:
            assignment.lms_user_id = user.lms_user_id
        num_assignments_updated = LearnerContentAssignment.bulk_update(assignments_to_update, ['lms_user_id'])

        # Record audit actions for user-linking (system-driven via signal)
        completed_at = timezone.now()
        actions_to_create = [
            LearnerContentAssignmentAction(
                assignment=assignment,
                action_type=AssignmentActions.LEARNER_LINKED,
                actor_type=AssignmentActorTypes.SYSTEM,
                source=AssignmentSources.SIGNAL,
                learner_lms_user_id=assignment.lms_user_id,
                learner_email=assignment.learner_email,
                learner_external_key=None,
                enterprise_customer_uuid=(
                    assignment.assignment_configuration.enterprise_customer_uuid
                    if assignment.assignment_configuration else None
                ),
                completed_at=completed_at,
            )
            for assignment in assignments_to_update
        ]
        bulk_create_with_history(
            actions_to_create,
            LearnerContentAssignmentAction,
            batch_size=BULK_OPERATION_BATCH_SIZE,
        )

        # Intentionally not logging PII (email).
        if len(assignments_to_update) > 0:
            logger.info(
                f'Set lms_user_id={user.lms_user_id} on {num_assignments_updated} assignments for User.id={user.id}'
            )


@receiver(LEDGER_TRANSACTION_REVERSED)
def update_assignment_status_for_reversed_transaction(**kwargs):
    """
    OEP-49 event handler to update assignment status for reversed transaction.
    """
    ledger_transaction = kwargs.get('ledger_transaction')
    transaction_uuid = ledger_transaction.uuid

    try:
        assignment_to_update = LearnerContentAssignment.objects.get(transaction_uuid=transaction_uuid)
    except LearnerContentAssignment.DoesNotExist:
        logger.info(f'No LearnerContentAssignment exists with transaction uuid: {transaction_uuid}')
        return
    if assignment_to_update.state not in LearnerContentAssignmentStateChoices.REVERSIBLE_STATES:
        logger.warning(
            f'Cannot reverse LearnerContentAssignment {assignment_to_update.uuid} '
            f'because its state is {assignment_to_update.state}'
        )
        return

    learner_credit_request = getattr(assignment_to_update, "credit_request", None)
    action_instance = None

    if learner_credit_request:
        action_instance = LearnerCreditRequestActions.create_action(
            learner_credit_request=learner_credit_request,
            recent_action=get_action_choice(SubsidyRequestStates.REVERSED),
            status=get_user_message_choice(SubsidyRequestStates.REVERSED),
        )

    try:
        assignment_to_update.state = LearnerContentAssignmentStateChoices.REVERSED
        assignment_to_update.reversed_at = timezone.now()
        assignment_to_update.save()
        # Record audit action for reversal (system-driven via signal)
        assignment_to_update.add_audit_action(
            action_type=AssignmentActions.REVERSED,
            actor_type=AssignmentActorTypes.SYSTEM,
            source=AssignmentSources.SIGNAL,
        )
        logger.info(
            f"LearnerContentAssignment {assignment_to_update.uuid} reversed."
        )

        if learner_credit_request:
            learner_credit_request.state = SubsidyRequestStates.REVERSED
            learner_credit_request.save(update_fields=["state"])
            logger.info(
                f"LearnerCreditRequest {learner_credit_request.uuid} reversed due to assignment reversal."
            )

    except (ValidationError, IntegrityError, DatabaseError) as exc:
        error_msg = f"Failed to reverse LearnerContentAssignment {assignment_to_update.uuid}"
        if learner_credit_request:
            error_msg += f" and its associated LearnerCreditRequest {learner_credit_request.uuid}"
        error_msg += f". The entire transaction was rolled back. Error: {exc}"
        logger.error(error_msg)

        if action_instance:
            action_instance.status = get_user_message_choice(SubsidyRequestStates.ACCEPTED)
            action_instance.error_reason = get_error_reason_choice(
                LearnerCreditRequestActionErrorReasons.FAILED_REVERSAL
            )
            action_instance.traceback = format_traceback(exc)
            action_instance.save()
