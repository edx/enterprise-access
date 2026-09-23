"""
Who may see the pathway review bench.

Two independent gates, both of which must pass:

* the ``enterprise_access.pathway_review_bench`` waffle flag, so the whole surface can be
  turned off without a deploy;
* the ``pathway_review.add_pathwayreviewvote`` model permission, granted to a reviewer group.

The model permission is used rather than an edx-rbac feature role because the bench is an
internal data-collection tool with no enterprise-customer scope -- the roles in
``core.constants`` all answer "which customer is this user an admin of", which is not the
question here. It is also not gated on ``is_staff``: curriculum reviewers need to rate
pathways without being handed the Django admin.
"""

from enterprise_access.toggles import pathway_review_bench_enabled

REVIEW_PERMISSION = 'pathway_review.add_pathwayreviewvote'


def can_review_pathways(user):
    """Return whether this user may open the bench and submit ratings."""
    if not pathway_review_bench_enabled():
        return False
    return bool(user and user.is_authenticated and user.has_perm(REVIEW_PERMISSION))
