"""
Tests for the admin module of the content_assignments app.
"""
from unittest import mock

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

        # Create a mock admin user for the request. lms_user_id is what force_redeem_assignments()
        # actually captures as the actor_lms_user_id (not the Django user's own pk).
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
        # Patched at the class level: assignment.assignment_configuration.policy fetches a fresh
        # instance from the DB on each access, so an instance-level patch on self.policy wouldn't
        # apply to the object the admin action actually calls into.
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
        """
        For every non-ACCEPTED starting state, force_redeem_assignments must call the real
        SubsidyAccessPolicy.redeem() (not a hand-rolled reimplementation), attributing the call to
        the requesting admin. Non-ALLOCATED starting states are first reset to ALLOCATED locally,
        recording an ALLOCATED audit row before redemption; ALLOCATED itself needs no reset.
        """
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
        """
        An already-ACCEPTED assignment must be skipped entirely (not reset to ALLOCATED and
        re-redeemed), since that would create a second ledger transaction for a single redemption.
        """
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
        """
        An assignment with no linked policy, or no lms_user_id yet, can't be redeemed -- it must be
        reported as a failure rather than raising and aborting the rest of the batch.
        """
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

        # The other, valid assignment in the same batch must still succeed.
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
        """
        If can_redeem() rejects the redemption, or redeem() itself raises (e.g. a real subsidy API
        error), the failure must be reported for that assignment without aborting the rest of the
        batch, and without leaving stray ALLOCATED-reset side effects for it.
        """
        assignment = LearnerContentAssignmentFactory(
            assignment_configuration=self.assignment_configuration,
            state=LearnerContentAssignmentStateChoices.CANCELLED,
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

        # Redemption failed either way -- the assignment was reset to ALLOCATED (that part is
        # local and did succeed), but no REDEEMED action exists since redemption itself failed.
        assignment.refresh_from_db()
        self.assertEqual(assignment.state, LearnerContentAssignmentStateChoices.ALLOCATED)
        self.assertFalse(assignment.actions.filter(action_type=AssignmentActions.REDEEMED).exists())

    def test_force_redeem_rolls_back_audit_rows_if_state_save_fails(self):
        """
        The ALLOCATED-reset audit row and its state-change save() must be atomic: if save() fails,
        the audit row recorded just before it must not persist either, or the audit trail would
        claim a redemption step that never actually happened. An unexpected DB error like this one
        is deliberately allowed to propagate (surfacing as a real error) rather than being
        swallowed into the per-assignment failure list, which is reserved for expected,
        recoverable redemption failures (can_redeem rejection, subsidy API errors).
        """
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
        """
        force_redeem_assignments must be gated by Django's admin action permission system (not
        exposed to any staff user who merely has view access to the changelist), which requires
        the @admin.action decorator to declare permissions=[...].
        """
        self.assertEqual(
            list(self.admin.force_redeem_assignments.allowed_permissions),
            ['change'],
        )

    def test_force_redeem_falls_back_when_admin_has_no_lms_user_id(self):
        """
        Missing-actor fallback: an internal Django superuser never linked to an LMS user must
        still succeed, simply recording a null actor_lms_user_id on the redeem() call rather than
        raising. (The has-an-lms_user_id case is already covered by every state in
        test_force_redeem_calls_real_redeem_with_admin_attribution.)
        """
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
