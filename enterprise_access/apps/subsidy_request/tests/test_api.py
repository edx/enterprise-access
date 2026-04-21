"""
Tests for the subsidy_request.api module.
"""
import contextlib
from unittest import mock
from uuid import uuid4

import ddt
from django.db import DatabaseError
from django.test import TestCase

from enterprise_access.apps.content_assignments.tests.factories import (
    AssignmentConfigurationFactory,
    LearnerContentAssignmentFactory
)
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.subsidy_access_policy.exceptions import (
    SubisidyAccessPolicyRequestApprovalError,
    SubsidyAccessPolicyLockAttemptFailed
)
from enterprise_access.apps.subsidy_request import api as subsidy_request_api
from enterprise_access.apps.subsidy_request.constants import (
    LearnerCreditAdditionalActionStates,
    LearnerCreditRequestActionErrorReasons,
    SubsidyRequestStates
)
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequestActions
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
    def test_decline_success(self, _mock_decline_task):
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
    def test_decline_filters_non_declinable(self, _mock_decline_task):
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
    def test_decline_no_reason(self, _mock_decline_task):
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
        """Create an approved request with a linked assignment for testing."""
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
    def test_remind_wrong_state(self, _mock_reminder_task):
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


@ddt.ddt
class TestApproveLearnerCreditRequests(TestCase):
    """Tests for approve_learner_credit_requests API function."""

    def setUp(self):
        super().setUp()
        self.reviewer = UserFactory()
        self.config = LearnerCreditRequestConfigurationFactory(active=True)
        self.enterprise_customer_uuid = uuid4()
        self.policy_uuid = uuid4()

        # Mock policy with a no-op lock context manager
        self.mock_policy = mock.MagicMock()
        self.mock_policy.lock.return_value = contextlib.nullcontext()

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

    def _build_approval_result(self, requests, approved_count):
        """
        Build mock return value for validate_and_allocate.

        First ``approved_count`` requests are approved (with assignments),
        the rest are failed with reason 'content_not_in_catalog'.
        """
        approved = requests[:approved_count]
        failed = requests[approved_count:]
        approved_map = {
            req.uuid: {"request": req, "assignment": self._make_assignment()}
            for req in approved
        }
        failed_by_reason = {"content_not_in_catalog": failed} if failed else {}
        return approved_map, failed_by_reason

    EXPECTED_RESULT_KEYS = {'approved', 'failed', 'failed_approval', 'error_message'}

    def _assert_has_expected_keys(self, result):
        self.assertEqual(set(result.keys()), self.EXPECTED_RESULT_KEYS)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.validate_and_allocate')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_return_shape_is_consistent_across_paths(
        self, mock_get_policy, mock_validate, _mock_approve_task,
    ):
        """Every return path produces the same 4-key dict so callers don't branch on shape."""
        mock_get_policy.return_value = self.mock_policy

        # 1. non-approvable-only early return
        self._assert_has_expected_keys(subsidy_request_api.approve_learner_credit_requests(
            [self._create_request(state=SubsidyRequestStates.DECLINED)],
            policy_uuid=str(self.policy_uuid), reviewer=self.reviewer,
        ))

        # 2. happy path
        request = self._create_request()
        mock_validate.return_value = self._build_approval_result([request], approved_count=1)
        self._assert_has_expected_keys(subsidy_request_api.approve_learner_credit_requests(
            [request], policy_uuid=str(self.policy_uuid), reviewer=self.reviewer,
        ))

        # 3. lock failure
        self.mock_policy.lock.side_effect = SubsidyAccessPolicyLockAttemptFailed("busy")
        self._assert_has_expected_keys(subsidy_request_api.approve_learner_credit_requests(
            [self._create_request()], policy_uuid=str(self.policy_uuid), reviewer=self.reviewer,
        ))
        self.mock_policy.lock.side_effect = None
        self.mock_policy.lock.return_value = contextlib.nullcontext()

        # 4. policy-level failure
        mock_validate.side_effect = SubisidyAccessPolicyRequestApprovalError(message="nope", status_code=422)
        self._assert_has_expected_keys(subsidy_request_api.approve_learner_credit_requests(
            [self._create_request()], policy_uuid=str(self.policy_uuid), reviewer=self.reviewer,
        ))

        # 5. unexpected exception
        mock_validate.side_effect = RuntimeError("boom")
        self._assert_has_expected_keys(subsidy_request_api.approve_learner_credit_requests(
            [self._create_request()], policy_uuid=str(self.policy_uuid), reviewer=self.reviewer,
        ))

    @ddt.data(
        SubsidyRequestStates.DECLINED,
        SubsidyRequestStates.APPROVED,
        SubsidyRequestStates.CANCELLED,
    )
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_non_approvable_requests_are_filtered(self, state, mock_get_policy):
        """Requests not in APPROVABLE_STATES are returned as 'failed' without touching the policy."""
        request = self._create_request(state=state)

        result = subsidy_request_api.approve_learner_credit_requests(
            [request],
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['approved']), 0)
        self.assertEqual(len(result['failed']), 1)
        mock_get_policy.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_empty_list_returns_early(self, mock_get_policy):
        """Empty input returns empty results without calling policy."""
        result = subsidy_request_api.approve_learner_credit_requests(
            [],
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(result['approved'], [])
        self.assertEqual(len(result['failed']), 0)
        mock_get_policy.assert_not_called()

    @ddt.data(
        # (approved_count, failed_count)
        (2, 0),  # all approved
        (1, 1),  # partial failure
        (0, 2),  # all failed validation
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.validate_and_allocate')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_approval_outcomes(
        self, approved_count, failed_count,
        mock_get_policy, mock_validate, mock_approve_task,
    ):
        """
        Approved requests get state=APPROVED, success actions, and notifications.
        Failed-validation requests get error actions and no state change.
        """
        mock_get_policy.return_value = self.mock_policy
        requests = [self._create_request() for _ in range(approved_count + failed_count)]
        mock_validate.return_value = self._build_approval_result(requests, approved_count)

        with self.captureOnCommitCallbacks(execute=True):
            result = subsidy_request_api.approve_learner_credit_requests(
                requests,
                policy_uuid=str(self.policy_uuid),
                reviewer=self.reviewer,
            )

        self.assertEqual(len(result['approved']), approved_count)
        self.assertEqual(len(result['failed_approval']), failed_count)
        self.assertIsNone(result['error_message'])

        # Approved requests: state updated, success action created
        for req in result['approved']:
            req.refresh_from_db()
            self.assertEqual(req.state, SubsidyRequestStates.APPROVED)
            self.assertEqual(req.reviewer, self.reviewer)
            self.assertIsNotNone(req.reviewed_at)
            self.assertTrue(req.actions.filter(
                recent_action=get_action_choice(SubsidyRequestStates.APPROVED),
                status=get_user_message_choice(SubsidyRequestStates.APPROVED),
            ).exists())

        # Failed requests: error action, state unchanged
        for req in result['failed_approval']:
            req.refresh_from_db()
            self.assertEqual(req.state, SubsidyRequestStates.REQUESTED)
            self.assertTrue(LearnerCreditRequestActions.objects.filter(
                learner_credit_request=req,
                error_reason=LearnerCreditRequestActionErrorReasons.FAILED_APPROVAL,
            ).exists())

        # Notifications only for approved
        self.assertEqual(mock_approve_task.delay.call_count, approved_count)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.validate_and_allocate')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_error_state_is_approvable(self, mock_get_policy, mock_validate, _mock_approve_task):
        """Requests in ERROR state can be re-approved (ERROR is in APPROVABLE_STATES)."""
        mock_get_policy.return_value = self.mock_policy
        request = self._create_request(state=SubsidyRequestStates.ERROR)
        mock_validate.return_value = self._build_approval_result([request], approved_count=1)

        result = subsidy_request_api.approve_learner_credit_requests(
            [request],
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['approved']), 1)
        mock_validate.assert_called_once()

    @ddt.data(
        (
            'enterprise_access.apps.subsidy_request.api.validate_and_allocate',
            SubisidyAccessPolicyRequestApprovalError(message="Policy expired", status_code=422),
        ),
        (
            'enterprise_access.apps.subsidy_request.api.validate_and_allocate',
            RuntimeError("Something unexpected"),
        ),
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_failure_always_creates_audit_trail(
        self, mock_target, exception,
        mock_get_policy, mock_approve_task,
    ):
        """Any exception produces error action records for all requests and zero notifications."""
        mock_get_policy.return_value = self.mock_policy
        requests = [self._create_request() for _ in range(2)]

        with mock.patch(mock_target, side_effect=exception):
            result = subsidy_request_api.approve_learner_credit_requests(
                requests,
                policy_uuid=str(self.policy_uuid),
                reviewer=self.reviewer,
            )

        self.assertEqual(len(result['approved']), 0)
        self.assertEqual(len(result['failed_approval']), 2)
        self.assertIsNotNone(result['error_message'])

        for req in requests:
            self.assertTrue(LearnerCreditRequestActions.objects.filter(
                learner_credit_request=req,
                error_reason=LearnerCreditRequestActionErrorReasons.FAILED_APPROVAL,
            ).exists())

        mock_approve_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_lock_failure_creates_audit_trail(self, mock_get_policy, mock_approve_task):
        """Lock acquisition failure produces error actions for all requests."""
        mock_policy = mock.MagicMock()
        mock_policy.lock.side_effect = SubsidyAccessPolicyLockAttemptFailed("Lock busy")
        mock_get_policy.return_value = mock_policy

        requests = [self._create_request() for _ in range(2)]

        result = subsidy_request_api.approve_learner_credit_requests(
            requests,
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['approved']), 0)
        self.assertEqual(len(result['failed_approval']), 2)
        self.assertIn("lock", result['error_message'].lower())

        for req in requests:
            self.assertTrue(LearnerCreditRequestActions.objects.filter(
                learner_credit_request=req,
                error_reason=LearnerCreditRequestActionErrorReasons.FAILED_APPROVAL,
            ).exists())

        mock_approve_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.validate_and_allocate')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_db_error_during_audit_trail_rolls_back_state(
        self, mock_get_policy, mock_validate, mock_approve_task,
    ):
        """
        When the in-transaction bulk_create fails, the state update is rolled back,
        and _record_failure_actions still writes a FAILED_APPROVAL audit row on retry.
        """
        mock_get_policy.return_value = self.mock_policy
        request = self._create_request()
        mock_validate.return_value = self._build_approval_result([request], approved_count=1)

        # First call (inside _approve_under_lock) fails; retry in _record_failure_actions succeeds.
        with mock.patch.object(
            LearnerCreditRequestActions, 'bulk_create',
            side_effect=[DatabaseError("DB error"), None],
        ):
            result = subsidy_request_api.approve_learner_credit_requests(
                [request],
                policy_uuid=str(self.policy_uuid),
                reviewer=self.reviewer,
            )

        self.assertEqual(len(result['approved']), 0)
        self.assertIsNotNone(result['error_message'])

        request.refresh_from_db()
        self.assertEqual(request.state, SubsidyRequestStates.REQUESTED)
        mock_approve_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.validate_and_allocate')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_double_failure_swallowed_in_record_failure_actions(
        self, mock_get_policy, mock_validate, _mock_approve_task,
    ):
        """
        If both the main bulk_create and the _record_failure_actions retry fail,
        the outer function still returns a structured error_message and logs the secondary failure.
        """
        mock_get_policy.return_value = self.mock_policy
        request = self._create_request()
        mock_validate.return_value = self._build_approval_result([request], approved_count=1)

        with mock.patch.object(
            LearnerCreditRequestActions, 'bulk_create', side_effect=DatabaseError("DB down"),
        ), self.assertLogs(
            'enterprise_access.apps.subsidy_request.api', level='ERROR',
        ) as log_ctx:
            result = subsidy_request_api.approve_learner_credit_requests(
                [request],
                policy_uuid=str(self.policy_uuid),
                reviewer=self.reviewer,
            )

        self.assertEqual(len(result['approved']), 0)
        self.assertIsNotNone(result['error_message'])
        self.assertTrue(
            any('Failed to record failure audit trail' in msg for msg in log_ctx.output),
            "expected _record_failure_actions to log its own failure",
        )

        self.assertFalse(
            LearnerCreditRequestActions.objects.filter(learner_credit_request=request).exists(),
            "no audit actions should exist when both bulk_create calls fail",
        )
        request.refresh_from_db()
        self.assertEqual(request.state, SubsidyRequestStates.REQUESTED)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.validate_and_allocate')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_unexpected_runtime_error_creates_audit_trail(
        self, mock_get_policy, mock_validate, mock_approve_task,
    ):
        """
        An unhandled exception yields:
          - a FAILED_APPROVAL audit row per request, carrying the full exception detail
          - a fixed client-facing error_message (exception detail stays internal)
        """
        mock_get_policy.return_value = self.mock_policy
        requests = [self._create_request() for _ in range(2)]
        exception_detail = "connection refused on upstream-7f3a"
        mock_validate.side_effect = RuntimeError(exception_detail)

        result = subsidy_request_api.approve_learner_credit_requests(
            requests,
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['approved']), 0)
        self.assertEqual(len(result['failed_approval']), 2)
        self.assertEqual(result['error_message'], "Unexpected error during approval.")

        for req in requests:
            action = LearnerCreditRequestActions.objects.filter(
                learner_credit_request=req,
                error_reason=LearnerCreditRequestActionErrorReasons.FAILED_APPROVAL,
            ).first()
            self.assertIsNotNone(action)
            self.assertIn(exception_detail, action.traceback)
        mock_approve_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_policy_vanishes_mid_flight(self, mock_get_policy, mock_approve_task):
        """
        If the policy is deleted between the view's pre-filter and get_policy_for_approval,
        the service must return a structured error — not raise.
        """
        mock_get_policy.side_effect = SubisidyAccessPolicyRequestApprovalError(
            message=f"Policy with UUID {self.policy_uuid} does not exist.",
            status_code=404,
        )
        request = self._create_request()

        result = subsidy_request_api.approve_learner_credit_requests(
            [request],
            policy_uuid=str(self.policy_uuid),
            reviewer=self.reviewer,
        )

        self.assertEqual(len(result['approved']), 0)
        self.assertEqual(len(result['failed_approval']), 1)
        self.assertIn("does not exist", result['error_message'])
        self.assertTrue(LearnerCreditRequestActions.objects.filter(
            learner_credit_request=request,
            error_reason=LearnerCreditRequestActionErrorReasons.FAILED_APPROVAL,
        ).exists())
        mock_approve_task.delay.assert_not_called()

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.validate_and_allocate')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_notifications_deferred_to_post_commit(self, mock_get_policy, mock_validate, mock_approve_task):
        """Notifications are registered via on_commit and carry the approved assignment uuid."""
        mock_get_policy.return_value = self.mock_policy
        request = self._create_request()
        approved_map, _ = self._build_approval_result([request], approved_count=1)
        expected_assignment_uuid = approved_map[request.uuid]['assignment'].uuid
        mock_validate.return_value = (approved_map, {})

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            subsidy_request_api.approve_learner_credit_requests(
                [request],
                policy_uuid=str(self.policy_uuid),
                reviewer=self.reviewer,
            )

        mock_approve_task.delay.assert_not_called()
        self.assertEqual(len(callbacks), 1)

        callbacks[0]()
        mock_approve_task.delay.assert_called_once_with(expected_assignment_uuid)

    @mock.patch('enterprise_access.apps.subsidy_request.api.send_learner_credit_bnr_request_approve_task')
    @mock.patch('enterprise_access.apps.subsidy_request.api.validate_and_allocate')
    @mock.patch('enterprise_access.apps.subsidy_request.api.get_policy_for_approval')
    def test_no_notifications_registered_on_rollback(
        self, mock_get_policy, mock_validate, mock_approve_task,
    ):
        """When the atomic block rolls back, no on_commit callbacks are registered."""
        mock_get_policy.return_value = self.mock_policy
        request = self._create_request()
        mock_validate.return_value = self._build_approval_result([request], approved_count=1)

        with self.captureOnCommitCallbacks(execute=False) as callbacks, mock.patch.object(
            LearnerCreditRequestActions, 'bulk_create',
            side_effect=[DatabaseError("DB error"), None],
        ):
            result = subsidy_request_api.approve_learner_credit_requests(
                [request],
                policy_uuid=str(self.policy_uuid),
                reviewer=self.reviewer,
            )

        self.assertIsNotNone(result['error_message'])
        self.assertEqual(callbacks, [])
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
        """Create an approved request with a linked assignment for testing."""
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
    def test_cancel_no_assignment(self, mock_cancel_assignments, _mock_cancel_task):
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
