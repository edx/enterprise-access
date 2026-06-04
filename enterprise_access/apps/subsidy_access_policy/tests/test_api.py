"""
Tests for the ``api.py`` module of the subsidy_access_policy app.
"""
from unittest import mock
from uuid import uuid4

import ddt
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.test import TestCase
from requests.exceptions import HTTPError
from rest_framework import status

from enterprise_access.apps.content_assignments.api import AllocationException
from enterprise_access.apps.content_assignments.tests.factories import LearnerContentAssignmentFactory
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.subsidy_access_policy.api import get_policy_for_approval, validate_and_allocate
from enterprise_access.apps.subsidy_access_policy.exceptions import (
    ContentPriceNullException,
    PriceValidationError,
    SubisidyAccessPolicyRequestApprovalError
)
from enterprise_access.apps.subsidy_access_policy.tests.factories import (
    PerLearnerSpendCapLearnerCreditAccessPolicyFactory
)
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequest

POLICY_PATH = 'enterprise_access.apps.subsidy_access_policy.models.SubsidyAccessPolicy'


class TestGetPolicyForApproval(TestCase):
    """Tests for get_policy_for_approval."""

    def setUp(self):
        super().setUp()
        self.policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
            active=True, retired=False,
        )

    def test_returns_existing_policy(self):
        result = get_policy_for_approval(self.policy.uuid)
        self.assertEqual(result.uuid, self.policy.uuid)

    def test_raises_for_nonexistent_policy(self):
        nonexistent_uuid = str(uuid4())
        with self.assertRaises(SubisidyAccessPolicyRequestApprovalError) as ctx:
            get_policy_for_approval(nonexistent_uuid)
        self.assertEqual(ctx.exception.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn(nonexistent_uuid, str(ctx.exception))


@ddt.ddt
class TestValidateAndAllocate(TestCase):
    """Tests for validate_and_allocate."""

    def setUp(self):
        super().setUp()
        self.user = UserFactory()
        self.policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
            active=True,
            retired=False,
            per_learner_spend_limit=0,
            spend_limit=4000,
        )

    @mock.patch.multiple(POLICY_PATH, approve=mock.DEFAULT, can_approve=mock.DEFAULT)
    def test_success(self, can_approve, approve):
        """Valid requests are approved and mapped to their assignments."""
        request = mock.MagicMock(spec=LearnerCreditRequest)
        assignment = LearnerContentAssignmentFactory()

        can_approve.return_value = {
            "valid_requests": [request],
            "failed_requests_by_reason": {},
        }
        approve.return_value = {request.uuid: assignment}

        approved_map, failed_by_reason = validate_and_allocate(self.policy, [request])

        self.assertIn(request.uuid, approved_map)
        self.assertEqual(approved_map[request.uuid]["assignment"], assignment)
        self.assertEqual(approved_map[request.uuid]["request"], request)
        self.assertEqual(failed_by_reason, {})

    @mock.patch.multiple(POLICY_PATH, approve=mock.DEFAULT, can_approve=mock.DEFAULT)
    def test_partial_failure(self, can_approve, approve):
        """Failed requests appear in failed_requests_by_reason alongside approved ones."""
        valid_request = mock.MagicMock(spec=LearnerCreditRequest)
        failed_request = mock.MagicMock(spec=LearnerCreditRequest)
        assignment = LearnerContentAssignmentFactory()
        reason = "content_not_in_catalog"

        can_approve.return_value = {
            "valid_requests": [valid_request],
            "failed_requests_by_reason": {reason: [failed_request]},
        }
        approve.return_value = {valid_request.uuid: assignment}

        approved_map, failed_by_reason = validate_and_allocate(
            self.policy, [valid_request, failed_request],
        )

        self.assertIn(valid_request.uuid, approved_map)
        self.assertIn(reason, failed_by_reason)
        self.assertIn(failed_request, failed_by_reason[reason])

    @mock.patch.multiple(POLICY_PATH, approve=mock.DEFAULT, can_approve=mock.DEFAULT)
    def test_all_requests_fail_validation(self, can_approve, approve):
        """
        All requests failing validation (without a global error_reason) returns an empty
        approved map and skips the allocation call entirely.
        """
        failed_request = mock.MagicMock(spec=LearnerCreditRequest)
        reason = "content_not_in_catalog"

        can_approve.return_value = {
            "valid_requests": [],
            "failed_requests_by_reason": {reason: [failed_request]},
        }

        approved_map, failed_by_reason = validate_and_allocate(self.policy, [failed_request])

        self.assertEqual(approved_map, {})
        self.assertEqual(failed_by_reason, {reason: [failed_request]})
        approve.assert_not_called()

    @mock.patch(f'{POLICY_PATH}.can_approve')
    def test_global_validation_error_raises(self, mock_can_approve):
        """A global error_reason from can_approve raises SubisidyAccessPolicyRequestApprovalError."""
        mock_can_approve.return_value = {
            "error_reason": "subsidy_expired",
            "valid_requests": [],
            "failed_requests_by_reason": {},
        }

        with self.assertRaises(SubisidyAccessPolicyRequestApprovalError):
            validate_and_allocate(self.policy, [])

    @mock.patch.multiple(POLICY_PATH, approve=mock.DEFAULT, can_approve=mock.DEFAULT)
    def test_consistency_error_on_missing_assignment(self, can_approve, approve):
        """Raises if approve() does not return an assignment for a validated request."""
        request = mock.MagicMock(spec=LearnerCreditRequest)

        can_approve.return_value = {
            "valid_requests": [request],
            "failed_requests_by_reason": {},
        }
        approve.return_value = {}  # "forgot" the assignment

        with self.assertRaises(SubisidyAccessPolicyRequestApprovalError) as ctx:
            validate_and_allocate(self.policy, [request])
        self.assertIn("Consistency Error", str(ctx.exception))

    @mock.patch(f'{POLICY_PATH}.can_approve')
    @ddt.data(
        AllocationException("Allocation failed"),
        PriceValidationError("Price validation failed"),
        ValidationError("Validation error"),
        DatabaseError("Database error"),
        HTTPError("HTTP error"),
        ConnectionError("Connection error"),
        ContentPriceNullException("Content price is null"),
    )
    def test_known_exceptions_wrapped(self, exception, mock_can_approve):
        """Known exception types are re-raised as SubisidyAccessPolicyRequestApprovalError."""
        mock_can_approve.side_effect = exception

        with self.assertRaises(SubisidyAccessPolicyRequestApprovalError) as ctx:
            validate_and_allocate(self.policy, [])
        self.assertEqual(ctx.exception.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(str(ctx.exception), str(exception))

    @mock.patch.multiple(POLICY_PATH, approve=mock.DEFAULT, can_approve=mock.DEFAULT)
    def test_approve_exception_wrapped(self, can_approve, approve):
        """Exceptions from policy.approve() are also wrapped."""
        request = mock.MagicMock(spec=LearnerCreditRequest)
        can_approve.return_value = {
            "valid_requests": [request],
            "failed_requests_by_reason": {},
        }
        approve.side_effect = AllocationException("Allocation failed during approval")

        with self.assertRaises(SubisidyAccessPolicyRequestApprovalError) as ctx:
            validate_and_allocate(self.policy, [])
        self.assertEqual(str(ctx.exception), "Allocation failed during approval")
