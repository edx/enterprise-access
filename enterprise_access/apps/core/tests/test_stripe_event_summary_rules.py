"""
Tests for stripe event summary RBAC rules and permissions.
"""
import uuid

import rules
from django.test import TestCase

from enterprise_access.apps.core import constants
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory


class StripeEventSummaryPermissionTests(TestCase):
    """
    Tests for the STRIPE_EVENT_SUMMARY_READ_PERMISSION permission.

    The previous predicate combination for has_stripe_event_summary_admin_access
    accidentally referenced has_implicit_access_to_requests_admin instead of
    has_implicit_access_to_stripe_event_summary_admin, causing 403s for users
    who only held STRIPE_EVENT_SUMMARY_ADMIN_ROLE (without REQUESTS_ADMIN_ROLE).
    These tests use explicit DB role assignments to catch that class of regression.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = uuid.uuid4()
        self.admin_user = UserFactory()
        self.unrelated_admin_user = UserFactory()
        self.no_role_user = UserFactory()

        self.admin_role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(
            name=constants.STRIPE_EVENT_SUMMARY_ADMIN_ROLE
        )

        EnterpriseAccessRoleAssignment.objects.create(
            user=self.admin_user,
            role=self.admin_role,
            enterprise_customer_uuid=self.enterprise_uuid,
        )

        # Admin for a different enterprise — should not have access to self.enterprise_uuid
        other_enterprise_uuid = uuid.uuid4()
        EnterpriseAccessRoleAssignment.objects.create(
            user=self.unrelated_admin_user,
            role=self.admin_role,
            enterprise_customer_uuid=other_enterprise_uuid,
        )

    def test_admin_has_read_access(self):
        """
        A user explicitly assigned STRIPE_EVENT_SUMMARY_ADMIN_ROLE has read access.

        This test specifically guards against the regression where the predicate
        combination referenced requests_admin predicates instead of
        stripe_event_summary_admin, causing has_perm to return False even for
        a correctly-assigned admin.
        """
        has_permission = self.admin_user.has_perm(
            constants.STRIPE_EVENT_SUMMARY_READ_PERMISSION,
            self.enterprise_uuid,
        )
        self.assertTrue(has_permission)

    def test_admin_access_limited_to_assigned_enterprise(self):
        """
        An admin can only read summaries for their own enterprise, not others.
        """
        other_enterprise_uuid = uuid.uuid4()
        has_permission = self.admin_user.has_perm(
            constants.STRIPE_EVENT_SUMMARY_READ_PERMISSION,
            other_enterprise_uuid,
        )
        self.assertFalse(has_permission)

    def test_unrelated_admin_no_access_to_different_enterprise(self):
        """
        An admin for enterprise A cannot read summaries for enterprise B.
        """
        has_permission = self.unrelated_admin_user.has_perm(
            constants.STRIPE_EVENT_SUMMARY_READ_PERMISSION,
            self.enterprise_uuid,
        )
        self.assertFalse(has_permission)

    def test_no_role_user_no_read_access(self):
        """
        A user with no role assignment has no read access.
        """
        has_permission = self.no_role_user.has_perm(
            constants.STRIPE_EVENT_SUMMARY_READ_PERMISSION,
            self.enterprise_uuid,
        )
        self.assertFalse(has_permission)

    def test_permission_is_registered(self):
        """
        STRIPE_EVENT_SUMMARY_READ_PERMISSION is registered in the rules registry.
        """
        self.assertTrue(rules.perm_exists(constants.STRIPE_EVENT_SUMMARY_READ_PERMISSION))
