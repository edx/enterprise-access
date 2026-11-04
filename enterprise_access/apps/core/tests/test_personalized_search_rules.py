"""
Tests for the Personalized Search learner permission: enterprise-scoped, unlike the learner pathways
permissions, so a role assignment for one enterprise doesn't open another's sessions.
"""
import uuid

import rules
from django.test import TestCase

from enterprise_access.apps.core import constants
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory


class PersonalizedSearchPermissionTests(TestCase):
    """``personalized_search.has_learner_access`` for the requested enterprise only."""

    def setUp(self):
        super().setUp()
        role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(name=constants.LEARNER_PATHWAYS_LEARNER_ROLE)
        self.enterprise = str(uuid.uuid4())
        self.learner = UserFactory()
        EnterpriseAccessRoleAssignment.objects.create(user=self.learner, role=role,
                                                      enterprise_customer_uuid=self.enterprise)

    def test_registered(self):
        assert rules.perm_exists(constants.PERSONALIZED_SEARCH_LEARNER_PERMISSION)

    def test_db_assignment_grants_its_own_enterprise_only(self):
        assert self.learner.has_perm(constants.PERSONALIZED_SEARCH_LEARNER_PERMISSION, self.enterprise)
        assert not self.learner.has_perm(constants.PERSONALIZED_SEARCH_LEARNER_PERMISSION, str(uuid.uuid4()))

    def test_user_without_assignment_is_denied(self):
        assert not UserFactory().has_perm(constants.PERSONALIZED_SEARCH_LEARNER_PERMISSION, self.enterprise)
