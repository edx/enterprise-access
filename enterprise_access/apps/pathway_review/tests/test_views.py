"""
Tests for the pathway review bench views.
"""
import json
from unittest import mock

import ddt
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from edx_toggles.toggles.testutils import override_waffle_flag

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_review.models import PathwayReviewVote, ReviewPool, Verdict
from enterprise_access.apps.pathway_review.tests.factories import PathwayReviewItemFactory, PathwayReviewVoteFactory
from enterprise_access.toggles import PATHWAY_REVIEW_BENCH


def reviewer():
    """A user who holds the reviewer permission."""
    user = UserFactory()
    user.user_permissions.add(Permission.objects.get(codename='add_pathwayreviewvote'))
    return get_user_model().objects.get(pk=user.pk)


class BenchTestCase(TestCase):
    """Shared setup: a signed-in reviewer with the flag on."""

    def setUp(self):
        super().setUp()
        self.user = reviewer()
        self.client.force_login(self.user)
        self.enterContext(override_waffle_flag(PATHWAY_REVIEW_BENCH, active=True))

    def post(self, name, payload):
        return self.client.post(
            reverse(name), data=json.dumps(payload), content_type='application/json',
        )


@ddt.ddt
class AccessTests(BenchTestCase):
    """ The bench is invisible, not merely forbidden, to anyone who may not use it. """

    @ddt.data('pathway_review:bench', 'pathway_review:next-item', 'pathway_review:leaderboard')
    def test_visible_to_a_permitted_reviewer(self, route):
        self.assertEqual(self.client.get(reverse(route)).status_code, 200)

    @ddt.data('pathway_review:bench', 'pathway_review:next-item', 'pathway_review:leaderboard')
    def test_hidden_when_the_flag_is_off(self, route):
        with override_waffle_flag(PATHWAY_REVIEW_BENCH, active=False):
            self.assertEqual(self.client.get(reverse(route)).status_code, 404)

    @ddt.data('pathway_review:bench', 'pathway_review:next-item', 'pathway_review:leaderboard')
    def test_hidden_without_the_permission(self, route):
        self.client.force_login(UserFactory())
        self.assertEqual(self.client.get(reverse(route)).status_code, 404)

    def test_anonymous_page_request_goes_to_login(self):
        """A reviewer following a link while logged out should reach SSO, not a dead end."""
        self.client.logout()
        response = self.client.get(reverse('pathway_review:bench'))
        self.assertEqual(response.status_code, 302)

    @ddt.data('pathway_review:next-item', 'pathway_review:leaderboard')
    def test_anonymous_api_requests_are_hidden(self, route):
        self.client.logout()
        self.assertEqual(self.client.get(reverse(route)).status_code, 404)


class NextItemTests(BenchTestCase):
    """ Which pathway a reviewer is handed, and what the response is allowed to contain. """

    def test_serves_an_item_and_progress(self):
        PathwayReviewItemFactory(item_id='L0001')
        body = self.client.get(reverse('pathway_review:next-item')).json()

        self.assertEqual(body['item']['id'], 'L0001')
        self.assertEqual(body['progress']['reviewed'], 0)

    def test_never_serves_the_blinding_fields(self):
        """A reviewer who can spot a seeded control is no longer blind."""
        PathwayReviewItemFactory(
            item_id='L0002', pool=ReviewPool.CONTROL,
            control_key={'planted_steps': [2, 5], 'planted_keys': ['X+1']},
        )
        raw = self.client.get(reverse('pathway_review:next-item')).content.decode()

        for leaked in ('control', 'planted', 'pool', 'weight', 'stratum'):
            self.assertNotIn(leaked, raw)

    def test_skips_items_this_reviewer_already_rated(self):
        done = PathwayReviewItemFactory(item_id='L0003')
        PathwayReviewVoteFactory(item=done, reviewer=self.user)
        PathwayReviewItemFactory(item_id='L0004')

        body = self.client.get(reverse('pathway_review:next-item')).json()
        self.assertEqual(body['item']['id'], 'L0004')

    def test_least_reviewed_item_comes_first(self):
        """Coverage evens out regardless of how many reviewers turn up."""
        rated = PathwayReviewItemFactory(item_id='L0005', careers_covered=500)
        PathwayReviewVoteFactory(item=rated, reviewer=UserFactory())
        PathwayReviewItemFactory(item_id='L0006', careers_covered=1)

        body = self.client.get(reverse('pathway_review:next-item')).json()
        self.assertEqual(body['item']['id'], 'L0006')

    def test_empty_queue_reports_no_item(self):
        body = self.client.get(reverse('pathway_review:next-item')).json()
        self.assertIsNone(body['item'])


@ddt.ddt
class SubmitVoteTests(BenchTestCase):
    """ Recording a judgement, and refusing the ones that cannot be acted on. """

    def setUp(self):
        super().setUp()
        self.item = PathwayReviewItemFactory(item_id='L0007')

    def test_records_a_vote(self):
        response = self.post('pathway_review:submit-vote', {
            'item': 'L0007', 'verdict': Verdict.NEEDS_WORK, 'drops': [3],
            'swaps': {'3': 'RITx+PM9001x'}, 'reasons': ['wrong_level'],
            'notes': 'The third rung is introductory.', 'seconds': 92,
        })

        self.assertEqual(response.status_code, 200)
        vote = PathwayReviewVote.objects.get()
        self.assertEqual(vote.replacements, {'3': 'RITx+PM9001x'})
        self.assertEqual(response.json()['progress']['reviewed'], 1)

    @ddt.data(Verdict.NEEDS_WORK, Verdict.BAD)
    def test_rejects_a_negative_verdict_with_no_notes(self, verdict):
        response = self.post('pathway_review:submit-vote', {
            'item': 'L0007', 'verdict': verdict, 'notes': '   ',
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PathwayReviewVote.objects.exists())

    def test_skip_needs_no_notes(self):
        response = self.post('pathway_review:submit-vote', {
            'item': 'L0007', 'verdict': Verdict.SKIP,
        })
        self.assertEqual(response.status_code, 200)

    def test_rejects_an_unknown_verdict(self):
        response = self.post('pathway_review:submit-vote', {
            'item': 'L0007', 'verdict': 'excellent',
        })
        self.assertEqual(response.status_code, 400)

    def test_rejects_a_second_vote_on_the_same_item(self):
        self.post('pathway_review:submit-vote', {'item': 'L0007', 'verdict': Verdict.GOOD})
        response = self.post('pathway_review:submit-vote', {'item': 'L0007', 'verdict': Verdict.BAD,
                                                            'notes': 'changed my mind'})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(PathwayReviewVote.objects.count(), 1)

    def test_unknown_item_is_not_found(self):
        response = self.post('pathway_review:submit-vote', {'item': 'L9999', 'verdict': Verdict.GOOD})
        self.assertEqual(response.status_code, 404)


class GoalAndLeaderboardTests(BenchTestCase):
    """ Goals are per reviewer; the board reports families but never verdicts. """

    def test_setting_a_goal(self):
        response = self.post('pathway_review:set-goal', {'goal': 35})

        self.assertEqual(response.json()['goal'], 35)
        body = self.client.get(reverse('pathway_review:next-item')).json()
        self.assertEqual(body['progress']['goal'], 35)
        self.assertTrue(body['progress']['goal_set'])

    def test_goal_is_clamped(self):
        self.assertEqual(self.post('pathway_review:set-goal', {'goal': 0}).json()['goal'], 1)

    def test_rejects_a_goal_that_is_not_a_number(self):
        self.assertEqual(self.post('pathway_review:set-goal', {'goal': 'lots'}).status_code, 400)

    def test_leaderboard_reports_families_not_verdicts(self):
        item = PathwayReviewItemFactory(item_id='L0008', pathway='Project Manager')
        PathwayReviewVoteFactory(
            item=item, reviewer=self.user, verdict=Verdict.BAD, notes='not this job',
        )
        response = self.client.get(reverse('pathway_review:leaderboard'))
        body = response.json()

        row = body['rows'][0]
        self.assertEqual(row['total'], 1)
        self.assertEqual(row['families'], ['Project Manager'])
        self.assertEqual(body['you'], self.user.id)
        raw = response.content.decode()
        self.assertNotIn('verdict', raw)
        self.assertNotIn('not this job', raw)


class MalformedInputTests(BenchTestCase):
    """ A client sending nonsense should get a 4xx, never a 500. """

    def setUp(self):
        super().setUp()
        self.item = PathwayReviewItemFactory(item_id='L0200')

    def test_non_numeric_seconds_is_ignored_rather_than_fatal(self):
        response = self.post('pathway_review:submit-vote', {
            'item': 'L0200', 'verdict': Verdict.GOOD, 'seconds': 'ages',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PathwayReviewVote.objects.get().seconds, 0)

    def test_absurd_seconds_is_clamped(self):
        self.post('pathway_review:submit-vote', {
            'item': 'L0200', 'verdict': Verdict.GOOD, 'seconds': 10 ** 9,
        })
        self.assertLessEqual(PathwayReviewVote.objects.get().seconds, 60 * 60 * 6)

    def test_wrongly_shaped_drops_and_swaps_are_dropped(self):
        response = self.post('pathway_review:submit-vote', {
            'item': 'L0200', 'verdict': Verdict.GOOD,
            'drops': 'three', 'swaps': ['not', 'a', 'map'], 'reasons': 7,
        })
        self.assertEqual(response.status_code, 200)
        vote = PathwayReviewVote.objects.get()
        self.assertEqual(vote.dropped_steps, [])
        self.assertEqual(vote.replacements, {})
        self.assertEqual(vote.reasons, [])

    def test_malformed_json_body(self):
        response = self.client.post(
            reverse('pathway_review:submit-vote'), data='{nope', content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)


class AdminTests(TestCase):
    """ The queue changelist annotates a vote count; make sure it actually renders. """

    def test_item_changelist_renders(self):
        admin_user = UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(admin_user)
        item = PathwayReviewItemFactory(item_id='L0300')
        PathwayReviewVoteFactory(item=item, reviewer=UserFactory())

        response = self.client.get('/admin/pathway_review/pathwayreviewitem/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('L0300', response.content.decode())


class ConcurrentVoteTests(BenchTestCase):
    """ The unique constraint is the authority, and hitting it must not break the request. """

    def test_losing_the_race_returns_409_not_500(self):
        """Simulates a second tab slipping past the exists() check."""
        item = PathwayReviewItemFactory(item_id='L0400')
        with mock.patch(
            'enterprise_access.apps.pathway_review.views.PathwayReviewVote.objects.filter'
        ) as mocked:
            mocked.return_value.exists.return_value = False
            PathwayReviewVoteFactory(item=item, reviewer=self.user)
            response = self.post('pathway_review:submit-vote', {
                'item': 'L0400', 'verdict': Verdict.GOOD,
            })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(PathwayReviewVote.objects.filter(item=item).count(), 1)
