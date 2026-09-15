"""
Tests for the pathway review access gate.
"""
import ddt
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from edx_toggles.toggles.testutils import override_waffle_flag

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_review.permissions import can_review_pathways
from enterprise_access.toggles import PATHWAY_REVIEW_BENCH


def grant_review_permission(user):
    """Give a user the permission a reviewer group would carry."""
    user.user_permissions.add(Permission.objects.get(codename='add_pathwayreviewvote'))
    return get_user_model().objects.get(pk=user.pk)


@ddt.ddt
class CanReviewPathwaysTests(TestCase):
    """ Both the flag and the permission are required, and neither alone is enough. """

    @ddt.data(
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    )
    @ddt.unpack
    def test_flag_and_permission_are_both_required(self, flag_on, has_perm, expected):
        user = UserFactory()
        if has_perm:
            user = grant_review_permission(user)
        with override_waffle_flag(PATHWAY_REVIEW_BENCH, active=flag_on):
            self.assertEqual(can_review_pathways(user), expected)

    def test_anonymous_users_are_refused(self):
        class Anonymous:
            """Stand-in for django.contrib.auth.models.AnonymousUser."""
            is_authenticated = False

            def has_perm(self, _perm):
                return False

        with override_waffle_flag(PATHWAY_REVIEW_BENCH, active=True):
            self.assertFalse(can_review_pathways(Anonymous()))
            self.assertFalse(can_review_pathways(None))
