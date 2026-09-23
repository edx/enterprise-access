"""
Tests for the seeded-control scorecard, which is what keeps the leaderboard honest.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_review.models import ReviewPool, Verdict
from enterprise_access.apps.pathway_review.selectors import control_performance
from enterprise_access.apps.pathway_review.tests.factories import PathwayReviewItemFactory, PathwayReviewVoteFactory


class ControlPerformanceTests(TestCase):
    """ A reviewer who waves a planted item through has not been reading. """

    def setUp(self):
        super().setUp()
        self.control = PathwayReviewItemFactory(
            item_id='L0100', pool=ReviewPool.CONTROL,
            control_key={'planted_steps': [2, 5]},
        )

    def test_a_negative_verdict_catches_the_control(self):
        user = UserFactory()
        PathwayReviewVoteFactory(
            item=self.control, reviewer=user, verdict=Verdict.BAD, notes='two are unrelated',
        )
        self.assertEqual(control_performance()[user.id], {'seen': 1, 'caught': 1})

    def test_dropping_a_planted_rung_catches_it_even_when_the_verdict_is_good(self):
        user = UserFactory()
        PathwayReviewVoteFactory(
            item=self.control, reviewer=user, verdict=Verdict.GOOD, dropped_steps=[5],
        )
        self.assertEqual(control_performance()[user.id], {'seen': 1, 'caught': 1})

    def test_approving_a_control_outright_is_not_caught(self):
        user = UserFactory()
        PathwayReviewVoteFactory(item=self.control, reviewer=user, verdict=Verdict.GOOD)
        self.assertEqual(control_performance()[user.id], {'seen': 1, 'caught': 0})

    def test_real_items_are_not_scored(self):
        user = UserFactory()
        real = PathwayReviewItemFactory(item_id='L0101', pool=ReviewPool.REACH)
        PathwayReviewVoteFactory(item=real, reviewer=user, verdict=Verdict.GOOD)
        self.assertEqual(control_performance(), {})


class ReportControlsCommandTests(TestCase):
    """ The scorecard has to be reachable, or the controls are just decoration. """

    def run_command(self):
        out = StringIO()
        call_command('report_pathway_review_controls', stdout=out)
        return out.getvalue()

    def test_reports_nothing_when_no_controls_are_loaded(self):
        self.assertIn('No seeded controls', self.run_command())

    def test_reports_when_controls_are_unrated(self):
        PathwayReviewItemFactory(item_id='L0102', pool=ReviewPool.CONTROL)
        self.assertIn('none rated yet', self.run_command())

    def test_names_the_reviewer_who_passed_a_control(self):
        control = PathwayReviewItemFactory(
            item_id='L0103', pool=ReviewPool.CONTROL, control_key={'planted_steps': [1]},
        )
        careless = UserFactory(username='waves-them-through')
        careful = UserFactory(username='actually-reading')
        PathwayReviewVoteFactory(item=control, reviewer=careless, verdict=Verdict.GOOD)
        PathwayReviewVoteFactory(
            item=control, reviewer=careful, verdict=Verdict.BAD, notes='step 1 is unrelated',
        )

        output = self.run_command()
        self.assertIn('waves-them-through', output)
        self.assertIn('passed controls', output)
        self.assertIn('actually-reading', output)
