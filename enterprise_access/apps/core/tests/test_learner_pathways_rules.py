"""
Tests for learner pathways RBAC rules and permissions.
"""
import uuid

import rules
from django.test import TestCase

from enterprise_access.apps.core import constants
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory


class LearnerPathwaysPermissionTests(TestCase):
    """
    Tests for the LEARNER_PATHWAYS_LEARNING_INTENT_PERMISSION permission.

    This permission is not scoped to a specific enterprise customer context
    (the learning-intent request payload carries no enterprise_customer_uuid),
    so a DB role assignment for ANY enterprise customer should grant access.
    """

    def setUp(self):
        super().setUp()
        self.learner_role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(
            name=constants.LEARNER_PATHWAYS_LEARNER_ROLE,
        )

        self.learner_user = UserFactory()
        EnterpriseAccessRoleAssignment.objects.create(
            user=self.learner_user,
            role=self.learner_role,
            enterprise_customer_uuid=uuid.uuid4(),
        )

        self.no_role_user = UserFactory()

    def test_db_role_assignment_grants_permission_regardless_of_which_enterprise(self):
        # The assignment above references a random enterprise_customer_uuid.
        # Checking with no requested context (as the view's @permission_required
        # does, since there's no per-request enterprise_customer_uuid to extract)
        # grants access regardless of which enterprise the assignment names.
        assert self.learner_user.has_perm(constants.LEARNER_PATHWAYS_LEARNING_INTENT_PERMISSION, None)

    def test_user_without_assignment_is_denied(self):
        assert not self.no_role_user.has_perm(constants.LEARNER_PATHWAYS_LEARNING_INTENT_PERMISSION, None)

    def test_permission_is_registered(self):
        assert rules.perm_exists(constants.LEARNER_PATHWAYS_LEARNING_INTENT_PERMISSION)
