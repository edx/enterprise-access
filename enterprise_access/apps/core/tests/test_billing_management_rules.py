"""
Tests for billing management RBAC rules and permissions.
"""
import uuid

import rules
from django.contrib.auth import get_user_model
from django.test import TestCase

from enterprise_access.apps.core import constants
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory

User = get_user_model()


class BillingManagementPermissionTests(TestCase):
    """
    Tests for the BILLING_MANAGEMENT_ACCESS_PERMISSION permission.
    Ensures operators and admins have access, while other roles do not.
    """

    def setUp(self):
        """Set up test users and enterprise context."""
        super().setUp()
        self.enterprise_uuid = uuid.uuid4()
        self.operator_user = UserFactory()
        self.admin_user = UserFactory()
        self.learner_user = UserFactory()
        self.unrelated_admin_user = UserFactory()
        self.no_role_user = UserFactory()

        # Create or get the role objects
        self.operator_role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(
            name=constants.CUSTOMER_BILLING_OPERATOR_ROLE
        )
        self.admin_role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(
            name=constants.CUSTOMER_BILLING_ADMIN_ROLE
        )

        # Create explicit role assignments via database
        EnterpriseAccessRoleAssignment.objects.create(
            user=self.operator_user,
            role=self.operator_role,
            enterprise_customer_uuid=self.enterprise_uuid,
        )

        EnterpriseAccessRoleAssignment.objects.create(
            user=self.admin_user,
            role=self.admin_role,
            enterprise_customer_uuid=self.enterprise_uuid,
        )

        # Admin for a different enterprise
        other_enterprise_uuid = uuid.uuid4()
        EnterpriseAccessRoleAssignment.objects.create(
            user=self.unrelated_admin_user,
            role=self.admin_role,
            enterprise_customer_uuid=other_enterprise_uuid,
        )

    def test_operator_has_billing_management_access(self):
        """
        Test that users with CUSTOMER_BILLING_OPERATOR_ROLE have access.
        """
        has_permission = self.operator_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )
        self.assertTrue(has_permission)

    def test_admin_has_billing_management_access(self):
        """
        Test that users with CUSTOMER_BILLING_ADMIN_ROLE have access.
        """
        has_permission = self.admin_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )
        self.assertTrue(has_permission)

    def test_operator_access_limited_to_assigned_enterprise(self):
        """
        Test that operators can only access their assigned enterprise.
        """
        other_enterprise_uuid = uuid.uuid4()
        has_permission = self.operator_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            other_enterprise_uuid
        )
        self.assertFalse(has_permission)

    def test_admin_access_limited_to_assigned_enterprise(self):
        """
        Test that admins can only access their assigned enterprise.
        """
        other_enterprise_uuid = uuid.uuid4()
        has_permission = self.admin_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            other_enterprise_uuid
        )
        self.assertFalse(has_permission)

    def test_unrelated_admin_no_access_to_different_enterprise(self):
        """
        Test that admin for enterprise A cannot access enterprise B.
        """
        has_permission = self.unrelated_admin_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )
        self.assertFalse(has_permission)

    def test_learner_no_billing_management_access(self):
        """
        Test that learners do not have billing management access.
        """
        has_permission = self.learner_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )
        self.assertFalse(has_permission)

    def test_no_role_user_no_billing_management_access(self):
        """
        Test that users without any role do not have access.
        """
        has_permission = self.no_role_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )
        self.assertFalse(has_permission)

    def test_billing_management_access_with_none_context(self):
        """
        Test that permission check with None context returns False.
        """
        has_permission = self.operator_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            None
        )
        self.assertFalse(has_permission)

    def test_billing_management_permission_is_registered(self):
        """
        Test that the BILLING_MANAGEMENT_ACCESS_PERMISSION is properly registered.
        """
        # Verify the permission exists in the rules registry
        self.assertTrue(
            rules.perm_exists(constants.BILLING_MANAGEMENT_ACCESS_PERMISSION)
        )

    def test_operator_and_admin_predicates_combine_correctly(self):
        """
        Test that operator and admin predicates are correctly combined with OR logic.
        """
        # Both operator and admin should have access
        operator_has_access = self.operator_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )
        admin_has_access = self.admin_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )

        self.assertTrue(operator_has_access)
        self.assertTrue(admin_has_access)

    def test_multiple_role_assignments(self):
        """
        Test that a user with multiple role assignments has access if any role grants it.
        """
        # Assign both operator and admin roles to the same user
        multi_role_user = UserFactory()

        EnterpriseAccessRoleAssignment.objects.create(
            user=multi_role_user,
            role=self.operator_role,
            enterprise_customer_uuid=self.enterprise_uuid,
        )

        EnterpriseAccessRoleAssignment.objects.create(
            user=multi_role_user,
            role=self.admin_role,
            enterprise_customer_uuid=self.enterprise_uuid,
        )

        has_permission = multi_role_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )
        self.assertTrue(has_permission)

    def test_explicit_role_assignment_overrides_lack_of_implicit_access(self):
        """
        Test that explicit database role assignments work when there's no JWT context.
        """
        # This test verifies explicit access works independently of implicit JWT access
        has_permission = self.admin_user.has_perm(
            constants.BILLING_MANAGEMENT_ACCESS_PERMISSION,
            self.enterprise_uuid
        )
        self.assertTrue(has_permission)
