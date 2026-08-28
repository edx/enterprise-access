"""
Tests for the admin module of the content_assignments app.
"""
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

from .factories import LearnerContentAssignmentFactory


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

    def _build_request(self):
        """Build a request with a user and message storage attached, as admin actions require."""
        request = self.factory.post('/')
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)  # pylint: disable=protected-access
        return request

    def test_force_redeem_assignments_single_assignment(self):
        """
        Test that force_redeem_assignments creates ALLOCATED and REDEEMED audit actions, for a
        starting state (CANCELLED) that isn't in the ALLOCATED-row exclusion list.
        """
        assignment = LearnerContentAssignmentFactory(
            state=LearnerContentAssignmentStateChoices.CANCELLED,
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
        self.assertEqual(allocated_action.actor_lms_user_id, self.user.lms_user_id)

        # Verify REDEEMED action
        redeemed_action = actions.filter(action_type=AssignmentActions.REDEEMED).first()
        self.assertIsNotNone(redeemed_action)
        self.assertEqual(redeemed_action.actor_type, AssignmentActorTypes.ADMIN)
        self.assertEqual(redeemed_action.source, AssignmentSources.DJANGO_ADMIN)
        self.assertEqual(redeemed_action.actor_lms_user_id, self.user.lms_user_id)

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

        # ALLOCATED assignments: ALLOCATED is itself in the exclusion list, so only REDEEMED gets
        # recorded (1 action). CANCELLED assignments: not excluded, so both get recorded (2).
        allocated_state_assignments, cancelled_state_assignment = assignments[:2], assignments[2]

        for assignment in allocated_state_assignments:
            action_types = set(assignment.actions.values_list('action_type', flat=True))
            self.assertEqual(action_types, {AssignmentActions.REDEEMED})

        cancelled_action_types = set(cancelled_state_assignment.actions.values_list('action_type', flat=True))
        self.assertEqual(cancelled_action_types, {AssignmentActions.ALLOCATED, AssignmentActions.REDEEMED})

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

    @ddt.data(True, False)
    def test_force_redeem_action_contains_actor_type_and_source(self, actor_has_lms_user_id):
        """
        Test that audit actions created by force_redeem have proper actor_type and source. Also
        covers the missing-actor fallback (actor_has_lms_user_id=False): an internal Django
        superuser never linked to an LMS user must still succeed, simply recording a null
        actor_lms_user_id rather than raising.
        """
        if not actor_has_lms_user_id:
            self.user.lms_user_id = None
            self.user.save()

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
                self.assertEqual(action.actor_lms_user_id, self.user.lms_user_id)
