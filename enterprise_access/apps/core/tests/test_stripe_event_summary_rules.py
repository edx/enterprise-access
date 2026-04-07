"""
Tests for stripe event summary RBAC rules and permissions.
"""
import uuid

import ddt
import rules
from django.test import TestCase

from enterprise_access.apps.core import constants
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory


@ddt.ddt
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
        self.other_enterprise_uuid = uuid.uuid4()

        self.admin_user = UserFactory()
        self.unrelated_admin_user = UserFactory()
        self.no_role_user = UserFactory()

        admin_role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(
            name=constants.STRIPE_EVENT_SUMMARY_ADMIN_ROLE
        )

        EnterpriseAccessRoleAssignment.objects.create(
            user=self.admin_user,
            role=admin_role,
            enterprise_customer_uuid=self.enterprise_uuid,
        )

        # Admin for a different enterprise — should not have access to self.enterprise_uuid
        EnterpriseAccessRoleAssignment.objects.create(
            user=self.unrelated_admin_user,
            role=admin_role,
            enterprise_customer_uuid=self.other_enterprise_uuid,
        )

    @ddt.data(
        # (user_attr, use_assigned_enterprise, expected)
        # Admin on the correct enterprise → access granted
        ('admin_user', True, True),
        # Admin on a different enterprise → denied
        ('admin_user', False, False),
        # Admin only assigned to a different enterprise → denied
        ('unrelated_admin_user', True, False),
        # User with no role → denied
        ('no_role_user', True, False),
    )
    @ddt.unpack
    def test_read_permission(self, user_attr, use_assigned_enterprise, expected):
        """
        Guards against the regression where the stripe_event_summary predicate
        combination referenced requests_admin predicates instead of the correct
        stripe_event_summary_admin predicates.
        """
        user = getattr(self, user_attr)
        enterprise_uuid = self.enterprise_uuid if use_assigned_enterprise else uuid.uuid4()
        has_permission = user.has_perm(constants.STRIPE_EVENT_SUMMARY_READ_PERMISSION, enterprise_uuid)
        self.assertEqual(has_permission, expected)

    def test_permission_is_registered(self):
        self.assertTrue(rules.perm_exists(constants.STRIPE_EVENT_SUMMARY_READ_PERMISSION))
