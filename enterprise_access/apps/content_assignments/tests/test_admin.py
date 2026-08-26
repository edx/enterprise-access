"""
Tests for the admin module of the content_assignments app.
"""
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

from .factories import LearnerContentAssignmentFactory


class TestLearnerContentAssignmentAdminActions(TestCase):
    """
    Tests for LearnerContentAssignmentAdmin custom actions.
    """

    def setUp(self):
        """Set up test fixtures."""
        self.admin_site = AdminSite()
        self.admin = LearnerContentAssignmentAdmin(LearnerContentAssignment, self.admin_site)
        self.factory = RequestFactory()

        # Create a mock user for the request
        self.user = UserFactory(id=999)

    def _build_request(self):
        """Build a request with a user and message storage attached, as admin actions require."""
        request = self.factory.post('/')
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)  # pylint: disable=protected-access
        return request

    def test_force_redeem_assignments_single_assignment(self):
        """
        Test that force_redeem_assignments creates ALLOCATED and REDEEMED audit actions.
        """
        assignment = LearnerContentAssignmentFactory(
            state=LearnerContentAssignmentStateChoices.ALLOCATED,
        )
        request = self._build_request()

        # Execute admin action
        queryset = LearnerContentAssignment.objects.filter(pk=assignment.pk)
        self.admin.force_redeem_assignments(request, queryset)

        # Refresh assignment to get updated state
        assignment.refresh_from_db()

        # Verify assignment state changed to ACCEPTED
        self.assertEqual(assignment.state, LearnerContentAssignmentStateChoices.ACCEPTED)

        # Verify audit actions were created
        actions = assignment.actions.all()
        self.assertEqual(actions.count(), 2)

        # Verify ALLOCATED action
        allocated_action = actions.filter(action_type=AssignmentActions.ALLOCATED).first()
        self.assertIsNotNone(allocated_action)
        self.assertEqual(allocated_action.actor_type, AssignmentActorTypes.ADMIN)
        self.assertEqual(allocated_action.source, AssignmentSources.DJANGO_ADMIN)
        self.assertEqual(allocated_action.actor_lms_user_id, self.user.id)

        # Verify REDEEMED action
        redeemed_action = actions.filter(action_type=AssignmentActions.REDEEMED).first()
        self.assertIsNotNone(redeemed_action)
        self.assertEqual(redeemed_action.actor_type, AssignmentActorTypes.ADMIN)
        self.assertEqual(redeemed_action.source, AssignmentSources.DJANGO_ADMIN)
        self.assertEqual(redeemed_action.actor_lms_user_id, self.user.id)

    def test_force_redeem_multiple_assignments(self):
        """
        Test that force_redeem_assignments works with multiple assignments.
        """
        assignments = [
            LearnerContentAssignmentFactory(state=LearnerContentAssignmentStateChoices.ALLOCATED),
            LearnerContentAssignmentFactory(state=LearnerContentAssignmentStateChoices.ALLOCATED),
            LearnerContentAssignmentFactory(state=LearnerContentAssignmentStateChoices.CANCELLED),
        ]
        request = self._build_request()

        # Execute admin action on all assignments
        queryset = LearnerContentAssignment.objects.filter(pk__in=[a.pk for a in assignments])
        self.admin.force_redeem_assignments(request, queryset)

        # Verify all assignments changed state to ACCEPTED
        for assignment in assignments:
            assignment.refresh_from_db()
            self.assertEqual(assignment.state, LearnerContentAssignmentStateChoices.ACCEPTED)

        # Verify each assignment has audit actions
        for assignment in assignments:
            actions = assignment.actions.all()
            # CANCELLED state should have 2 actions, others may have different counts based on prior state
            self.assertGreaterEqual(actions.count(), 2)

            # Verify both action types exist
            action_types = {a.action_type for a in actions}
            self.assertIn(AssignmentActions.ALLOCATED, action_types)
            self.assertIn(AssignmentActions.REDEEMED, action_types)

    def test_force_redeem_skips_allocation_for_already_accepted(self):
        """
        Test that force_redeem_assignments skips ALLOCATED action for ACCEPTED assignments.
        """
        assignment = LearnerContentAssignmentFactory(
            state=LearnerContentAssignmentStateChoices.ACCEPTED,
        )
        request = self._build_request()

        # Execute admin action
        queryset = LearnerContentAssignment.objects.filter(pk=assignment.pk)
        self.admin.force_redeem_assignments(request, queryset)

        # Refresh assignment
        assignment.refresh_from_db()

        # Verify assignment is still ACCEPTED
        self.assertEqual(assignment.state, LearnerContentAssignmentStateChoices.ACCEPTED)

        # Verify only REDEEMED action was created (no ALLOCATED)
        actions = assignment.actions.all()
        allocated_actions = actions.filter(action_type=AssignmentActions.ALLOCATED)
        redeemed_actions = actions.filter(action_type=AssignmentActions.REDEEMED)

        # No ALLOCATED action should have been created since the assignment was already ACCEPTED
        self.assertEqual(allocated_actions.count(), 0)
        self.assertGreaterEqual(redeemed_actions.count(), 1)

    def test_force_redeem_action_contains_actor_type_and_source(self):
        """
        Test that audit actions created by force_redeem have proper actor_type and source.
        """
        assignment = LearnerContentAssignmentFactory(
            state=LearnerContentAssignmentStateChoices.ERRORED,
        )
        request = self._build_request()

        # Execute admin action
        queryset = LearnerContentAssignment.objects.filter(pk=assignment.pk)
        self.admin.force_redeem_assignments(request, queryset)

        # Verify all audit actions have correct metadata
        assignment.refresh_from_db()
        for action in assignment.actions.all():
            if action.action_type in [AssignmentActions.ALLOCATED, AssignmentActions.REDEEMED]:
                self.assertEqual(action.actor_type, AssignmentActorTypes.ADMIN)
                self.assertEqual(action.source, AssignmentSources.DJANGO_ADMIN)
                self.assertEqual(action.actor_lms_user_id, self.user.id)
