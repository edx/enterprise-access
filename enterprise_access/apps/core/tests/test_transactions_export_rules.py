"""
Tests for the learner credit transactions export RBAC rule.
"""
from uuid import uuid4

import ddt
from django.test import TestCase

from enterprise_access.apps.core import constants
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory


@ddt.ddt
class TestTransactionsExportPermission(TestCase):
    """
    Tests who is granted SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION via explicit (database) role assignments.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_uuid = uuid4()
        self.user = UserFactory()

    def _assign_role(self, role_name, enterprise_uuid):
        role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(name=role_name)
        EnterpriseAccessRoleAssignment.objects.create(
            user=self.user,
            role=role,
            enterprise_customer_uuid=enterprise_uuid,
        )

    @ddt.data(
        (constants.CONTENT_ASSIGNMENTS_ADMIN_ROLE, True),
        (constants.CONTENT_ASSIGNMENTS_OPERATOR_ROLE, True),
        (constants.SUBSIDY_ACCESS_POLICY_OPERATOR_ROLE, True),
        # The Browse & Request admin role alone must not grant access to learner spend PII.
        (constants.REQUESTS_ADMIN_ROLE, False),
        (constants.SUBSIDY_ACCESS_POLICY_LEARNER_ROLE, False),
        (constants.CONTENT_ASSIGNMENTS_LEARNER_ROLE, False),
    )
    @ddt.unpack
    def test_explicit_role_grants(self, role_name, expected_access):
        self._assign_role(role_name, self.enterprise_uuid)

        assert self.user.has_perm(
            constants.SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION,
            str(self.enterprise_uuid),
        ) is expected_access

    def test_role_for_another_enterprise_does_not_grant_access(self):
        self._assign_role(constants.CONTENT_ASSIGNMENTS_ADMIN_ROLE, uuid4())

        assert not self.user.has_perm(
            constants.SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION,
            str(self.enterprise_uuid),
        )
