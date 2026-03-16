"""
Tests for the subsidy_request.api module.
"""
from unittest import mock
from uuid import uuid4

import ddt
from django.test import TestCase

from enterprise_access.apps.content_assignments.tests.factories import (
    AssignmentConfigurationFactory,
    LearnerContentAssignmentFactory
)
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.subsidy_access_policy.exceptions import SubisidyAccessPolicyRequestApprovalError
from enterprise_access.apps.subsidy_request import api as subsidy_request_api
from enterprise_access.apps.subsidy_request.constants import (
    APPROVABLE_STATES,
    DECLINABLE_STATES,
    REMINDABLE_STATES,
    LearnerCreditAdditionalActionStates,
    LearnerCreditRequestActionErrorReasons,
    SubsidyRequestStates
)
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequest, LearnerCreditRequestActions
from enterprise_access.apps.subsidy_request.tests.factories import (
    LearnerCreditRequestConfigurationFactory,
    LearnerCreditRequestFactory
)
from enterprise_access.apps.subsidy_request.utils import get_action_choice, get_user_message_choice


@ddt.ddt
class TestDeclineLearnerCreditRequests(TestCase):
    """
    Tests for decline_learner_credit_requests API function.
    """

    def setUp(self):
        super().setUp()
        self.reviewer = UserFactory()
        self.config = LearnerCreditRequestConfigurationFactory(active=True)
        self.enterprise_customer_uuid = uuid4()

    def _create_request(self, state=SubsidyRequestStates.REQUESTED):
        return LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=state,
        )

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_decline_notification_task')
    def test_decline_success(self, mock_decline_task):
        """All REQUESTED requests are declined, actions created, and notifications queued."""
        request_1 = self._create_request()
        request_2 = self._create_request()

        result = subsidy_request_api.decline_learner_credit_requests(
            [request_1, request_2],
            reviewer=self.reviewer,
            reason='Budget exhausted',
        )

        self.assertEqual(len(result['declined']), 2)
        self.assertEqual(len(result['non_declinable']), 0)

        for req in result['declined']:
            req.refresh_from_db()
            self.assertEqual(req.state, SubsidyRequestStates.DECLINED)
            self.assertEqual(req.reviewer, self.reviewer)
            self.assertEqual(req.decline_reason, 'Budget exhausted')
            # Verify action record was created
            self.assertTrue(
                req.actions.filter(
                    recent_action=get_action_choice(SubsidyRequestStates.DECLINED),
                ).exists()
            )

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_decline_notification_task')
    def test_decline_filters_non_declinable(self, mock_decline_task):
        """Requests not in DECLINABLE_STATES are returned as non_declinable."""
        declinable = self._create_request(state=SubsidyRequestStates.REQUESTED)
        non_declinable = self._create_request(state=SubsidyRequestStates.APPROVED)

        result = subsidy_request_api.decline_learner_credit_requests(
            [declinable, non_declinable],
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['declined']), 1)
        self.assertEqual(len(result['non_declinable']), 1)
        self.assertEqual(result['declined'][0].uuid, declinable.uuid)
        self.assertEqual(result['non_declinable'][0].uuid, non_declinable.uuid)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_decline_notification_task')
    def test_decline_all_non_declinable(self, mock_decline_task):
        """If no requests are declinable, returns empty declined list."""
        approved = self._create_request(state=SubsidyRequestStates.APPROVED)
        declined_already = self._create_request(state=SubsidyRequestStates.DECLINED)

        result = subsidy_request_api.decline_learner_credit_requests(
            [approved, declined_already],
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['declined']), 0)
        self.assertEqual(len(result['non_declinable']), 2)
        mock_decline_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_decline_notification_task')
    def test_decline_no_reason(self, mock_decline_task):
        """Decline works without a reason."""
        request = self._create_request()

        result = subsidy_request_api.decline_learner_credit_requests(
            [request],
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['declined']), 1)
        declined = result['declined'][0]
        declined.refresh_from_db()
        self.assertIsNone(declined.decline_reason)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_decline_notification_task')
    def test_decline_empty_list(self, mock_decline_task):
        """Passing empty iterable returns empty results."""
        result = subsidy_request_api.decline_learner_credit_requests(
            [],
            reviewer=self.reviewer,
        )

        self.assertEqual(result['declined'], [])
        self.assertEqual(result['non_declinable'], [])
        mock_decline_task.delay.assert_not_called()


@ddt.ddt
class TestRemindLearnerCreditRequests(TestCase):
    """
    Tests for remind_learner_credit_requests API function.
    """

    def setUp(self):
        super().setUp()
        self.config = LearnerCreditRequestConfigurationFactory(active=True)
        self.enterprise_customer_uuid = uuid4()

    def _create_approved_request_with_assignment(self):
        from enterprise_access.apps.content_assignments.tests.factories import AssignmentConfigurationFactory
        assignment_config = AssignmentConfigurationFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
        )
        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=assignment_config,
            content_quantity=-500,
            state='allocated',
        )
        return LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=SubsidyRequestStates.APPROVED,
            assignment=assignment,
        )

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_reminder_email_for_pending_learner_credit_request')
    def test_remind_success(self, mock_reminder_task):
        """Remindable requests get action records and email tasks queued."""
        request = self._create_approved_request_with_assignment()

        result = subsidy_request_api.remind_learner_credit_requests([request])

        self.assertEqual(len(result['remindable']), 1)
        self.assertEqual(len(result['non_remindable']), 0)
        mock_reminder_task.delay.assert_called_once_with(request.assignment.uuid)

        # Verify action record
        request.refresh_from_db()
        self.assertTrue(
            LearnerCreditRequestActions.objects.filter(
                learner_credit_request=request,
                recent_action=get_action_choice(LearnerCreditAdditionalActionStates.REMINDED),
            ).exists()
        )

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_reminder_email_for_pending_learner_credit_request')
    def test_remind_no_assignment(self, mock_reminder_task):
        """Approved requests without assignments are non-remindable."""
        request = LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=SubsidyRequestStates.APPROVED,
            assignment=None,
        )

        result = subsidy_request_api.remind_learner_credit_requests([request])

        self.assertEqual(len(result['remindable']), 0)
        self.assertEqual(len(result['non_remindable']), 1)
        mock_reminder_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_reminder_email_for_pending_learner_credit_request')
    def test_remind_wrong_state(self, mock_reminder_task):
        """Requests not in REMINDABLE_STATES are non-remindable."""
        request = LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=SubsidyRequestStates.REQUESTED,
            assignment=None,
        )

        result = subsidy_request_api.remind_learner_credit_requests([request])

        self.assertEqual(len(result['remindable']), 0)
        self.assertEqual(len(result['non_remindable']), 1)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_reminder_email_for_pending_learner_credit_request')
    def test_remind_empty_list(self, mock_reminder_task):
        """Empty input returns empty results."""
        result = subsidy_request_api.remind_learner_credit_requests([])

        self.assertEqual(result['remindable'], [])
        self.assertEqual(result['non_remindable'], [])
        mock_reminder_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_reminder_email_for_pending_learner_credit_request')
    def test_remind_all_non_remindable_early_return(self, mock_reminder_task):
        """If all requests are non-remindable, returns early without creating actions."""
        request = LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=SubsidyRequestStates.REQUESTED,
        )

        result = subsidy_request_api.remind_learner_credit_requests([request])

        self.assertEqual(len(result['remindable']), 0)
        self.assertEqual(len(result['non_remindable']), 1)
        mock_reminder_task.delay.assert_not_called()
        # No action records should be created
        self.assertFalse(
            LearnerCreditRequestActions.objects.filter(
                learner_credit_request=request,
            ).exists()
        )


class TestApproveLearnerCreditRequests(TestCase):
    """
    Tests for approve_learner_credit_requests API function.
    """

    def setUp(self):
        super().setUp()
        self.reviewer = UserFactory()
        self.config = LearnerCreditRequestConfigurationFactory(active=True)
        self.enterprise_customer_uuid = uuid4()
        self.policy_uuid = uuid4()

    def _create_request(self, state=SubsidyRequestStates.REQUESTED):
        return LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=state,
        )

    def _make_assignment(self):
        assignment_config = AssignmentConfigurationFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
        )
        return LearnerContentAssignmentFactory(
            assignment_configuration=assignment_config,
            content_quantity=-500,
            state='allocated',
        )

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.approve_learner_credit_requests_via_policy')
    def test_approve_success(self, mock_approve_via_policy, mock_approve_task):
        """Successful approval updates state, creates actions, and queues notifications."""
        request_1 = self._create_request()
        request_2 = self._create_request()
        assignment_1 = self._make_assignment()
        assignment_2 = self._make_assignment()

        mock_approve_via_policy.return_value = {
            "approved_requests": {
                request_1.uuid: {"request": request_1, "assignment": assignment_1},
                request_2.uuid: {"request": request_2, "assignment": assignment_2},
            },
            "failed_requests_by_reason": {},
        }

        with self.captureOnCommitCallbacks(execute=True):
            result = subsidy_request_api.approve_learner_credit_requests(
                [request_1, request_2],
                policy_uuid=str(self.policy_uuid),
                reviewer=self.reviewer,
            )

        self.assertEqual(len(result['approved']), 2)
        self.assertEqual(len(result['failed_approval']), 0)
        self.assertIsNone(result['error_message'])

        # Verify state was updated
        for req in result['approved']:
            req.refresh_from_db()
            self.assertEqual(req.state, SubsidyRequestStates.APPROVED)
            self.assertEqual(req.reviewer, self.reviewer)
            self.assertIsNotNone(req.reviewed_at)
            # Verify action record was created
            self.assertTrue(
                req.actions.filter(
                    recent_action=get_action_choice(SubsidyRequestStates.APPROVED),
                ).exists()
            )

        # Verify notification tasks were queued
        self.assertEqual(mock_approve_task.delay.call_count, 2)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.approve_learner_credit_requests_via_policy')
    def test_approve_partial_failure(self, mock_approve_via_policy, mock_approve_task):
        """Partial failure returns approved and failed lists with failure action records."""
        request_1 = self._create_request()
        request_2 = self._create_request()
        assignment_1 = self._make_assignment()

        mock_approve_via_policy.return_value = {
            "approved_requests": {
                request_1.uuid: {"request": request_1, "assignment": assignment_1},
            },
            "failed_requests_by_reason": {
                "content_not_in_catalog": [request_2],
            },
        }

        with self.captureOnCommitCallbacks(execute=True):
            result = subsidy_request_api.approve_learner_credit_requests(
                [request_1, request_2],
                policy_uuid=str(self.policy_uuid),
                reviewer=self.reviewer,
            )

        self.assertEqual(len(result['approved']), 1)
        self.assertEqual(len(result['failed_approval']), 1)
        self.assertIsNone(result['error_message'])

        # Failed request should have error action
        failed_actions = LearnerCreditRequestActions.objects.filter(
            learner_credit_request=request_2,
            error_reason=LearnerCreditRequestActionErrorReasons.FAILED_APPROVAL,
        )
        self.assertTrue(failed_actions.exists())

        # Notification only for approved
        self.assertEqual(mock_approve_task.delay.call_count, 1)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.approve_learner_credit_requests_via_policy')
    def test_approve_global_failure(self, mock_approve_via_policy, mock_approve_task):
        """Global failure from SubisidyAccessPolicyRequestApprovalError marks all as failed."""
        request_1 = self._create_request()
        request_2 = self._create_request()

        mock_approve_via_policy.side_effect = SubisidyAccessPolicyRequestApprovalError(
            message="Policy expired",
            status_code=422,
        )

        result = subsidy_request_api.approve_learner_credit_requests(
            [request_1, request_2],
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['approved']), 0)
        self.assertEqual(len(result['failed_approval']), 2)
        self.assertEqual(result['error_message'], "Policy expired")

        # Verify error action records created for all requests
        for req in [request_1, request_2]:
            self.assertTrue(
                LearnerCreditRequestActions.objects.filter(
                    learner_credit_request=req,
                    error_reason=LearnerCreditRequestActionErrorReasons.FAILED_APPROVAL,
                ).exists()
            )

        # No notifications sent
        mock_approve_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.approve_learner_credit_requests_via_policy')
    def test_approve_filters_non_approvable_states(self, mock_approve_via_policy, mock_approve_task):
        """Requests not in APPROVABLE_STATES are returned as failed without calling policy."""
        declined_request = self._create_request(state=SubsidyRequestStates.DECLINED)

        result = subsidy_request_api.approve_learner_credit_requests(
            [declined_request],
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['approved']), 0)
        self.assertEqual(len(result['failed']), 1)
        mock_approve_via_policy.assert_not_called()
        mock_approve_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.approve_learner_credit_requests_via_policy')
    def test_approve_error_state_is_approvable(self, mock_approve_via_policy, mock_approve_task):
        """Requests in ERROR state can be re-approved."""
        error_request = self._create_request(state=SubsidyRequestStates.ERROR)
        assignment = self._make_assignment()

        mock_approve_via_policy.return_value = {
            "approved_requests": {
                error_request.uuid: {"request": error_request, "assignment": assignment},
            },
            "failed_requests_by_reason": {},
        }

        result = subsidy_request_api.approve_learner_credit_requests(
            [error_request],
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['approved']), 1)
        mock_approve_via_policy.assert_called_once()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.approve_learner_credit_requests_via_policy')
    def test_approve_empty_list(self, mock_approve_via_policy, mock_approve_task):
        """Empty input returns empty results without calling policy."""
        result = subsidy_request_api.approve_learner_credit_requests(
            [],
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(result['approved'], [])
        self.assertEqual(len(result['failed']), 0)
        mock_approve_via_policy.assert_not_called()
        mock_approve_task.delay.assert_not_called()


class TestCancelLearnerCreditRequests(TestCase):
    """
    Tests for cancel_learner_credit_requests API function.
    """

    def setUp(self):
        super().setUp()
        self.reviewer = UserFactory()
        self.config = LearnerCreditRequestConfigurationFactory(active=True)
        self.enterprise_customer_uuid = uuid4()

    def _create_approved_request_with_assignment(self):
        assignment_config = AssignmentConfigurationFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
        )
        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=assignment_config,
            content_quantity=-500,
            state='allocated',
        )
        return LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=SubsidyRequestStates.APPROVED,
            assignment=assignment,
        )

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_cancel_notification_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.assignments_api.cancel_assignments')
    def test_cancel_success(self, mock_cancel_assignments, mock_cancel_task):
        """Approved requests with assignments are cancelled successfully."""
        request = self._create_approved_request_with_assignment()

        mock_cancel_assignments.return_value = {
            'cancelable': [request.assignment],
            'non_cancelable': [],
        }

        with self.captureOnCommitCallbacks(execute=True):
            result = subsidy_request_api.cancel_learner_credit_requests(
                [request],
                reviewer=self.reviewer,
            )

        self.assertEqual(len(result['cancelable']), 1)
        self.assertEqual(len(result['non_cancelable']), 0)

        cancelled = result['cancelable'][0]
        cancelled.refresh_from_db()
        self.assertEqual(cancelled.state, SubsidyRequestStates.CANCELLED)
        self.assertEqual(cancelled.reviewer, self.reviewer)

        # Verify action record
        self.assertTrue(
            LearnerCreditRequestActions.objects.filter(
                learner_credit_request=cancelled,
                recent_action=get_action_choice(SubsidyRequestStates.CANCELLED),
            ).exists()
        )

        # Verify notification task was queued
        mock_cancel_task.delay.assert_called_once()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_cancel_notification_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.assignments_api.cancel_assignments')
    def test_cancel_non_cancelable_state(self, mock_cancel_assignments, mock_cancel_task):
        """Requests not in APPROVED state are returned as non_cancelable."""
        request = LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=SubsidyRequestStates.REQUESTED,
        )

        result = subsidy_request_api.cancel_learner_credit_requests(
            [request],
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['cancelable']), 0)
        self.assertEqual(len(result['non_cancelable']), 1)
        mock_cancel_assignments.assert_not_called()
        mock_cancel_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_cancel_notification_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.assignments_api.cancel_assignments')
    def test_cancel_no_assignment(self, mock_cancel_assignments, mock_cancel_task):
        """Approved requests without assignments are returned as non_cancelable."""
        request = LearnerCreditRequestFactory(
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            learner_credit_request_config=self.config,
            state=SubsidyRequestStates.APPROVED,
            assignment=None,
        )

        result = subsidy_request_api.cancel_learner_credit_requests(
            [request],
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['cancelable']), 0)
        self.assertEqual(len(result['non_cancelable']), 1)
        mock_cancel_assignments.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_cancel_notification_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.assignments_api.cancel_assignments')
    def test_cancel_assignment_failure(self, mock_cancel_assignments, mock_cancel_task):
        """When assignment cancellation fails, request is marked as non_cancelable with error action."""
        request = self._create_approved_request_with_assignment()

        mock_cancel_assignments.return_value = {
            'cancelable': [],
            'non_cancelable': [request.assignment],
        }

        result = subsidy_request_api.cancel_learner_credit_requests(
            [request],
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['cancelable']), 0)
        self.assertEqual(len(result['non_cancelable']), 1)

        # Verify error action record was created
        self.assertTrue(
            LearnerCreditRequestActions.objects.filter(
                learner_credit_request=request,
                error_reason=LearnerCreditRequestActionErrorReasons.FAILED_CANCELLATION,
            ).exists()
        )

        # Request state should NOT have changed
        request.refresh_from_db()
        self.assertEqual(request.state, SubsidyRequestStates.APPROVED)
        mock_cancel_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_cancel_notification_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.assignments_api.cancel_assignments')
    def test_cancel_empty_list(self, mock_cancel_assignments, mock_cancel_task):
        """Empty input returns empty results."""
        result = subsidy_request_api.cancel_learner_credit_requests(
            [],
            reviewer=self.reviewer,
        )

        self.assertEqual(result['cancelable'], [])
        self.assertEqual(result['non_cancelable'], [])
        mock_cancel_assignments.assert_not_called()
        mock_cancel_task.delay.assert_not_called()
