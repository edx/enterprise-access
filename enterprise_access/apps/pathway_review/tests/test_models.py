"""
Tests for the pathway review models.
"""
import ddt
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_review.models import ReviewPool, Verdict
from enterprise_access.apps.pathway_review.tests.factories import PathwayReviewItemFactory, PathwayReviewVoteFactory


@ddt.ddt
class PathwayReviewVoteTests(TestCase):
    """ A verdict that is not positive has to explain itself, or it cannot be acted on. """

    def setUp(self):
        super().setUp()
        self.reviewer = UserFactory()
        self.item = PathwayReviewItemFactory()

    @ddt.data(Verdict.NEEDS_WORK, Verdict.BAD)
    def test_negative_verdict_requires_notes(self, verdict):
        with self.assertRaises(ValidationError) as ctx:
            PathwayReviewVoteFactory(
                item=self.item, reviewer=self.reviewer, verdict=verdict, notes='   ',
            )
        self.assertIn('notes', ctx.exception.message_dict)

    @ddt.data(Verdict.NEEDS_WORK, Verdict.BAD)
    def test_negative_verdict_saves_with_notes(self, verdict):
        vote = PathwayReviewVoteFactory(
            item=self.item, reviewer=self.reviewer, verdict=verdict,
            notes='The advanced rung is a genomics course.',
        )
        self.assertEqual(vote.verdict, verdict)

    @ddt.data(Verdict.GOOD, Verdict.SKIP)
    def test_positive_and_skipped_verdicts_need_no_notes(self, verdict):
        vote = PathwayReviewVoteFactory(
            item=self.item, reviewer=self.reviewer, verdict=verdict, notes='',
        )
        self.assertEqual(vote.notes, '')

    def test_one_vote_per_reviewer_per_item(self):
        PathwayReviewVoteFactory(item=self.item, reviewer=self.reviewer)
        with self.assertRaises(IntegrityError):
            PathwayReviewVoteFactory(item=self.item, reviewer=self.reviewer)

    def test_two_reviewers_may_rate_the_same_item(self):
        PathwayReviewVoteFactory(item=self.item, reviewer=self.reviewer)
        PathwayReviewVoteFactory(item=self.item, reviewer=UserFactory())
        self.assertEqual(self.item.votes.count(), 2)


@ddt.ddt
class PathwayReviewItemTests(TestCase):
    """ Only the seeded-control pool is a control. """

    @ddt.data(
        (ReviewPool.CONTROL, True),
        (ReviewPool.REACH, False),
        (ReviewPool.TAIL, False),
    )
    @ddt.unpack
    def test_is_control(self, pool, expected):
        item = PathwayReviewItemFactory(pool=pool)
        self.assertEqual(item.is_control, expected)
