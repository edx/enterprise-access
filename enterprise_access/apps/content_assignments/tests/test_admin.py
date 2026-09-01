"""
Tests for the admin module of the content_assignments app.
"""
from unittest import mock
from uuid import uuid4

import ddt
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from enterprise_access.apps.content_assignments.admin import LearnerContentAssignmentAdmin
from enterprise_access.apps.content_assignments.constants import (
    AssignmentActions,
    AssignmentActorTypes,
    AssignmentSources,
    LearnerContentAssignmentStateChoices
)
from enterprise_access.apps.content_assignments.models import LearnerContentAssignment
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.subsidy_access_policy.exceptions import SubsidyAPIHTTPError
from enterprise_access.apps.subsidy_access_policy.models import AssignedLearnerCreditAccessPolicy
from enterprise_access.apps.subsidy_access_policy.tests.factories import AssignedLearnerCreditAccessPolicyFactory

from .factories import AssignmentConfigurationFactory, LearnerContentAssignmentFactory


@ddt.ddt
class TestLearnerContentAssignmentAdminActions(TestCase):
    """
    Tests for LearnerContentAssignmentAdmin custom actions.
    """

    def setUp(self):
        """Set up test fixtures."""
        self.admin_site = AdminSite()
        self.admin = LearnerContentAssignmentAdmin(LearnerContentAssignment, self.admin_site)
        self.factory = RequestFactory()

        # force_redeem_assignments() captures lms_user_id as the actor, not the Django user's pk.
        self.user = UserFactory(lms_user_id=999)

        self.assignment_configuration = AssignmentConfigurationFactory()
        self.policy = AssignedLearnerCreditAccessPolicyFactory(
            assignment_configuration=self.assignment_configuration,
        )

    def _build_request(self):
        """Build a request with a user and message storage attached, as admin actions require."""
        request = self.factory.post('/')
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)  # pylint: disable=protected-access
        return request

    def _mock_can_redeem(self, can_redeem=True, reason=None):
        # Patched at the class level since assignment.assignment_configuration.policy is a fresh instance each access.
        return mock.patch.object(
            AssignedLearnerCreditAccessPolicy, 'can_redeem', return_value=(can_redeem, reason, []),
        )

    def _mock_redeem(self, side_effect=None):
        return mock.patch.object(AssignedLearnerCreditAccessPolicy, 'redeem', side_effect=side_effect)

    @ddt.data(
        LearnerContentAssignmentStateChoices.ALLOCATED,
        LearnerContentAssignmentStateChoices.CANCELLED,
        LearnerContentAssignmentStateChoices.ERRORED,
        LearnerContentAssignmentStateChoices.REVERSED,
        LearnerContentAssignmentStateChoices.EXPIRED,
    )
    def test_force_redeem_calls_real_redeem_with_admin_attribution(self, starting_state):
        """For every non-ACCEPTED starting state, force_redeem_assignments must call the real redeem()."""
        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=self.assignment_configuration,
            state=starting_state,
        )
        request = self._build_request()
        queryset = LearnerContentAssignment.objects.filter(pk=assignment.pk)

        with self._mock_can_redeem() as mock_can_redeem, self._mock_redeem() as mock_redeem:
            self.admin.force_redeem_assignments(request, queryset)

        mock_can_redeem.assert_called_once_with(
            assignment.lms_user_id, assignment.content_key, skip_enrollment_deadline_check=True,
        )
        mock_redeem.assert_called_once_with(
            assignment.lms_user_id, assignment.content_key, [],
            actor_lms_user_id=self.user.lms_user_id,
            actor_type=AssignmentActorTypes.ADMIN,
            source=AssignmentSources.DJANGO_ADMIN,
        )

        allocated_action = assignment.actions.filter(action_type=AssignmentActions.ALLOCATED).first()
        if starting_state == LearnerContentAssignmentStateChoices.ALLOCATED:
            self.assertIsNone(allocated_action)
        else:
            self.assertIsNotNone(allocated_action)
            self.assertEqual(allocated_action.actor_type, AssignmentActorTypes.ADMIN)
            self.assertEqual(allocated_action.source, AssignmentSources.DJANGO_ADMIN)
            self.assertEqual(allocated_action.actor_lms_user_id, self.user.lms_user_id)

    def test_force_redeem_skips_already_accepted_to_avoid_double_spend(self):
        """An already-ACCEPTED assignment must be skipped entirely, not reset and re-redeemed."""
        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=self.assignment_configuration,
            state=LearnerContentAssignmentStateChoices.ACCEPTED,
        )
        request = self._build_request()
        queryset = LearnerContentAssignment.objects.filter(pk=assignment.pk)

        with self._mock_can_redeem() as mock_can_redeem, self._mock_redeem() as mock_redeem:
            self.admin.force_redeem_assignments(request, queryset)

        mock_can_redeem.assert_not_called()
        mock_redeem.assert_not_called()
        assignment.refresh_from_db()
        self.assertEqual(assignment.state, LearnerContentAssignmentStateChoices.ACCEPTED)
        self.assertEqual(assignment.actions.count(), 0)

    @ddt.data(
        {'missing_policy': True},
        {'missing_lms_user_id': True},
    )
    def test_force_redeem_reports_failure_without_crashing_batch(self, missing_kwargs):
        """A missing policy or lms_user_id is reported as a failure without aborting the batch."""
        assignment_configuration = self.assignment_configuration
        if missing_kwargs.get('missing_policy'):
            assignment_configuration = AssignmentConfigurationFactory()  # no linked policy
        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=assignment_configuration,
            state=LearnerContentAssignmentStateChoices.CANCELLED,
            lms_user_id=None if missing_kwargs.get('missing_lms_user_id') else 555,
        )
        other_assignment = LearnerContentAssignmentFactory(
            assignment_configuration=self.assignment_configuration,
            state=LearnerContentAssignmentStateChoices.CANCELLED,
        )
        request = self._build_request()
        queryset = LearnerContentAssignment.objects.filter(pk__in=[assignment.pk, other_assignment.pk])

        with self._mock_can_redeem() as mock_can_redeem, self._mock_redeem() as mock_redeem:
            self.admin.force_redeem_assignments(request, queryset)

        mock_can_redeem.assert_called_once_with(
            other_assignment.lms_user_id, other_assignment.content_key, skip_enrollment_deadline_check=True,
        )
        mock_redeem.assert_called_once()
        assignment.refresh_from_db()
        self.assertEqual(assignment.state, LearnerContentAssignmentStateChoices.CANCELLED)
        self.assertEqual(assignment.actions.count(), 0)

    @ddt.data(
        {'can_redeem': False},
        {'redeem_raises': True},
    )
    def test_force_redeem_reports_subsidy_failure_without_crashing_batch(self, case):
        """A can_redeem/redeem() failure is reported without aborting the batch or leaving a stale transaction_uuid."""
        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=self.assignment_configuration,
            state=LearnerContentAssignmentStateChoices.REVERSED,
            transaction_uuid=uuid4(),
        )
        request = self._build_request()
        queryset = LearnerContentAssignment.objects.filter(pk=assignment.pk)

        can_redeem = case.get('can_redeem', True)
        redeem_side_effect = SubsidyAPIHTTPError() if case.get('redeem_raises') else None

        with self._mock_can_redeem(can_redeem=can_redeem, reason='nope') as mock_can_redeem, \
                self._mock_redeem(side_effect=redeem_side_effect) as mock_redeem:
            self.admin.force_redeem_assignments(request, queryset)

        mock_can_redeem.assert_called_once()
        if not can_redeem:
            mock_redeem.assert_not_called()
        else:
            mock_redeem.assert_called_once()

        assignment.refresh_from_db()
        self.assertEqual(assignment.state, LearnerContentAssignmentStateChoices.ALLOCATED)
        self.assertFalse(assignment.actions.filter(action_type=AssignmentActions.REDEEMED).exists())
        self.assertIsNone(assignment.transaction_uuid)

    def test_force_redeem_rolls_back_audit_rows_if_state_save_fails(self):
        """The ALLOCATED-reset audit row and its save() must be atomic; the raw error still propagates."""
        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=self.assignment_configuration,
            state=LearnerContentAssignmentStateChoices.CANCELLED,
        )
        request = self._build_request()
        queryset = LearnerContentAssignment.objects.filter(pk=assignment.pk)

        with mock.patch.object(LearnerContentAssignment, 'save', side_effect=Exception('DB boom')):
            with self._mock_can_redeem() as mock_can_redeem, self._mock_redeem() as mock_redeem:
                with self.assertRaisesMessage(Exception, 'DB boom'):
                    self.admin.force_redeem_assignments(request, queryset)

        mock_can_redeem.assert_not_called()
        mock_redeem.assert_not_called()
        assignment.refresh_from_db()
        self.assertEqual(assignment.state, LearnerContentAssignmentStateChoices.CANCELLED)
        self.assertEqual(assignment.actions.count(), 0)

    def test_force_redeem_requires_change_permission(self):
        """force_redeem_assignments must declare permissions=['change'], not be open to view-only staff."""
        self.assertEqual(
            list(self.admin.force_redeem_assignments.allowed_permissions),
            ['change'],
        )

    def test_force_redeem_falls_back_when_admin_has_no_lms_user_id(self):
        """An admin user with no lms_user_id must still succeed, recording a null actor_lms_user_id."""
        self.user.lms_user_id = None
        self.user.save()

        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=self.assignment_configuration,
            state=LearnerContentAssignmentStateChoices.ERRORED,
        )
        request = self._build_request()
        queryset = LearnerContentAssignment.objects.filter(pk=assignment.pk)

        with self._mock_can_redeem(), self._mock_redeem() as mock_redeem:
            self.admin.force_redeem_assignments(request, queryset)

        mock_redeem.assert_called_once_with(
            assignment.lms_user_id, assignment.content_key, [],
            actor_lms_user_id=None,
            actor_type=AssignmentActorTypes.ADMIN,
            source=AssignmentSources.DJANGO_ADMIN,
        )
