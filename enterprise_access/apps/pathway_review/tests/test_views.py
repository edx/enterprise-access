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
from enterprise_access.apps.pathway_review.selectors import carried_acceptable
from enterprise_access.apps.pathway_review.tests.factories import (
    PathwayReviewItemFactory,
    PathwayReviewVoteFactory,
    ladder_payload
)
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
        self.item = PathwayReviewItemFactory(item_id='L0007', payload=ladder_payload())

    def test_records_a_vote(self):
        response = self.post('pathway_review:submit-vote', {
            'item': 'L0007', 'verdict': Verdict.NEEDS_WORK, 'drops': [3],
            'swaps': {'3': {'best': 'Alt+I1', 'also': ['Alt+I2']}}, 'reasons': ['wrong_level'],
            'notes': 'The third rung is introductory.', 'seconds': 92,
        })

        self.assertEqual(response.status_code, 200)
        vote = PathwayReviewVote.objects.get()
        self.assertEqual(vote.dropped_steps, [3])
        self.assertEqual(vote.replacements, {'3': {'best': 'Alt+I1', 'also': ['Alt+I2']}})
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


@ddt.ddt
class ReplacementTests(BenchTestCase):
    """
    One best replacement plus up to three also-fine ones, drawn only from what the rung offered.

    Steps 3 and 4 of ``ladder_payload`` are intermediate, so ``Alt+I*`` keys are theirs to offer
    and ``Alt+B*`` (introductory) keys are not.
    """

    def setUp(self):
        super().setUp()
        self.item = PathwayReviewItemFactory(item_id='L0100', payload=ladder_payload())

    def vote(self, swaps, drops=(3,)):
        return self.post('pathway_review:submit-vote', {
            'item': 'L0100', 'verdict': Verdict.NEEDS_WORK, 'drops': list(drops),
            'swaps': swaps, 'notes': 'The third rung is wrong.',
        })

    def stored(self):
        return PathwayReviewVote.objects.get().replacements

    def test_best_with_three_also_fine(self):
        swaps = {'3': {'best': 'Alt+I4', 'also': ['Alt+I1', 'Alt+I2', 'Alt+I3']}}
        self.assertEqual(self.vote(swaps).status_code, 200)
        self.assertEqual(self.stored(), swaps)

    @ddt.data({'best': 'Alt+I2'}, {'best': 'Alt+I2', 'also': []}, {'best': 'Alt+I2', 'also': None})
    def test_best_alone(self, pick):
        self.assertEqual(self.vote({'3': pick}).status_code, 200)
        self.assertEqual(self.stored(), {'3': {'best': 'Alt+I2', 'also': []}})

    def test_legacy_bare_key_is_stored_as_a_best_pick(self):
        """A tab opened before the picker changed still sends one key per step."""
        self.assertEqual(self.vote({'3': 'Alt+I2'}).status_code, 200)
        self.assertEqual(self.stored(), {'3': {'best': 'Alt+I2', 'also': []}})

    def test_nothing_works(self):
        """Both clients send the same sentinel, and it is stored as sent."""
        self.assertEqual(self.vote({'3': '__none__'}).status_code, 200)
        self.assertEqual(self.stored(), {'3': '__none__'})

    def test_legacy_empty_string_is_no_pick_and_is_not_stored(self):
        """The first client sent '' for a dropped rung left unanswered; it is not "nothing works"."""
        response = self.vote({'3': '', '4': {'best': 'Alt+I1', 'also': []}}, drops=[3, 4])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored(), {'4': {'best': 'Alt+I1', 'also': []}})

    def test_legacy_empty_string_alone_stores_nothing(self):
        self.assertEqual(self.vote({'3': ''}).status_code, 200)
        self.assertEqual(self.stored(), {})

    def test_nothing_works_needs_no_alternates(self):
        """A rung the search found nothing else for can still be marked as a catalog gap."""
        payload = ladder_payload()
        payload['alt']['Advanced'] = []
        self.item.payload = payload
        self.item.save()

        self.assertEqual(self.vote({'5': '__none__'}, drops=[5]).status_code, 200)
        self.assertEqual(self.stored(), {'5': '__none__'})

    def test_a_dropped_step_with_no_answer_is_absent(self):
        response = self.vote({'4': {'best': 'Alt+I3', 'also': []}}, drops=[3, 4])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored(), {'4': {'best': 'Alt+I3', 'also': []}})

    def test_steps_are_checked_against_their_own_level(self):
        response = self.vote(
            {'1': {'best': 'Alt+B5', 'also': ['Alt+B1']}, '5': {'best': 'Alt+A1', 'also': []}},
            drops=[1, 5],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(self.stored()), {'1', '5'})

    @ddt.data(
        ('a course from another level', {'3': {'best': 'Alt+B1', 'also': []}}, 'not one of'),
        ('an also-fine course from another level', {'3': {'best': 'Alt+I1', 'also': ['Alt+A1']}}, 'not one of'),
        ('a key the search never found', {'3': {'best': 'Nope+1', 'also': []}}, 'not one of'),
        ('the dropped course itself', {'3': {'best': 'Ladder+3', 'also': []}}, 'not one of'),
        ('a legacy key the search never found', {'3': 'Nope+1'}, 'not one of'),
        ('four also-fine', {'3': {'best': 'Alt+I1', 'also': ['Alt+I2', 'Alt+I3', 'Alt+I4', 'Alt+I5']}},
         'at most 3'),
        ('a repeated also-fine', {'3': {'best': 'Alt+I1', 'also': ['Alt+I2', 'Alt+I2']}}, 'more than once'),
        ('the best also marked also fine', {'3': {'best': 'Alt+I1', 'also': ['Alt+I1']}}, 'cannot also'),
        ('also-fine with no best', {'3': {'also': ['Alt+I1']}}, 'best replacement before'),
        ('also-fine with an empty best', {'3': {'best': '', 'also': ['Alt+I1']}}, 'best replacement before'),
        ('an empty pick', {'3': {}}, 'needs a best'),
        ('a best that is not a key', {'3': {'best': 7, 'also': []}}, 'one best course'),
        ('also-fine that is not a list', {'3': {'best': 'Alt+I1', 'also': 'Alt+I2'}}, 'one best course'),
        ('an also-fine that is not a key', {'3': {'best': 'Alt+I1', 'also': [7]}}, 'one best course'),
        ('a pick that is a list', {'3': ['Alt+I1']}, 'one best course'),
        ('a pick that is a number', {'3': 7}, 'one best course'),
        ('a pick that is null', {'3': None}, 'one best course'),
        ('a replacement for a kept step', {'4': {'best': 'Alt+I1', 'also': []}}, 'was kept'),
        ('a step the pathway does not have', {'9': '__none__'}, 'no step 9'),
        ('nothing-works for a kept step', {'4': '__none__'}, 'was kept'),
    )
    @ddt.unpack
    def test_rejects(self, _case, swaps, message):
        response = self.vote(swaps, drops=[3, 9] if '9' in swaps else [3])

        self.assertEqual(response.status_code, 400)
        self.assertIn(message, response.json()['error'])
        self.assertFalse(PathwayReviewVote.objects.exists())

    def test_one_bad_step_rejects_the_whole_vote(self):
        response = self.vote(
            {'3': {'best': 'Alt+I1', 'also': []}, '4': {'best': 'Alt+B1', 'also': []}}, drops=[3, 4],
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PathwayReviewVote.objects.exists())


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


@ddt.ddt
class SuggestionTests(BenchTestCase):
    """
    "Keep this course, and these would also work": up to three alternates for a KEPT rung,
    drawn only from what that rung offered, stored apart from replacements.
    """

    def setUp(self):
        super().setUp()
        self.item = PathwayReviewItemFactory(item_id='L0200', payload=ladder_payload())

    def vote(self, suggest, drops=(), swaps=None, verdict=Verdict.GOOD):
        return self.post('pathway_review:submit-vote', {
            'item': 'L0200', 'verdict': verdict, 'drops': list(drops), 'swaps': swaps or {},
            'suggest': suggest, 'notes': 'Fine, with options.',
        })

    def stored(self):
        return PathwayReviewVote.objects.get()

    def test_suggestions_for_kept_steps_are_stored_apart_from_replacements(self):
        response = self.vote({'1': ['Alt+B2', 'Alt+B3'], '3': ['Alt+I1']})

        self.assertEqual(response.status_code, 200)
        vote = self.stored()
        self.assertEqual(vote.suggestions, {'1': ['Alt+B2', 'Alt+B3'], '3': ['Alt+I1']})
        self.assertEqual((vote.replacements, vote.dropped_steps), ({}, []))

    def test_a_vote_can_carry_both_replacements_and_suggestions(self):
        response = self.vote({'1': ['Alt+B1']}, drops=[3], swaps={'3': {'best': 'Alt+I2', 'also': []}},
                             verdict=Verdict.NEEDS_WORK)

        self.assertEqual(response.status_code, 200)
        vote = self.stored()
        self.assertEqual(vote.suggestions, {'1': ['Alt+B1']})
        self.assertEqual(vote.replacements, {'3': {'best': 'Alt+I2', 'also': []}})

    def test_an_empty_list_is_no_suggestion(self):
        self.assertEqual(self.vote({'2': []}).status_code, 200)
        self.assertEqual(self.stored().suggestions, {})

    def test_a_client_that_sends_no_suggestions_still_works(self):
        response = self.post('pathway_review:submit-vote', {'item': 'L0200', 'verdict': Verdict.GOOD})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored().suggestions, {})

    def test_every_alternate_the_rung_offered_can_be_suggested(self):
        """Suggestions are uncapped: nothing among them is ranked, so there is no best to dilute."""
        every = [f'Alt+B{n}' for n in range(1, 6)]

        self.assertEqual(self.vote({'1': every}).status_code, 200)
        self.assertEqual(self.stored().suggestions, {'1': every})

    @ddt.data(
        ({'3': ['Alt+I1']}, [3], 'was dropped'),
        ({'1': ['Alt+I1']}, [], 'not one of the introductory courses'),
        ({'1': ['Alt+B1', 'Alt+B1']}, [], 'more than once'),
        ({'1': 'Alt+B1'}, [], 'a list of course keys'),
        ({'1': [7]}, [], 'a list of course keys'),
        ({'9': ['Alt+B1']}, [], 'no step 9'),
    )
    @ddt.unpack
    def test_invalid_suggestions_are_refused(self, suggest, drops, message):
        swaps = {str(step): '__none__' for step in drops}
        response = self.vote(suggest, drops=drops, swaps=swaps, verdict=Verdict.NEEDS_WORK)

        self.assertEqual(response.status_code, 400)
        self.assertIn(message, response.json()['error'])
        self.assertFalse(PathwayReviewVote.objects.exists())


class CarriedAcceptableTests(BenchTestCase):
    """
    What a reviewer calls acceptable on one shape is offered again on the next shape of the
    same career, so the same question is not asked four times.
    """

    def setUp(self):
        super().setUp()
        self.first = PathwayReviewItemFactory(item_id='F0001', family_key='business analyst',
                                              payload=ladder_payload())
        self.second = PathwayReviewItemFactory(item_id='F0002', family_key='business analyst',
                                               payload=ladder_payload())
        self.elsewhere = PathwayReviewItemFactory(item_id='X0001', family_key='data analyst',
                                                  payload=ladder_payload())

    def vote_on(self, item, *, suggest=None, drops=(), swaps=None, verdict=Verdict.GOOD, notes='Fine.'):
        return self.post('pathway_review:submit-vote', {
            'item': item.item_id, 'verdict': verdict, 'drops': list(drops), 'swaps': swaps or {},
            'suggest': suggest or {}, 'notes': notes,
        })

    def carried_for(self, item):
        return carried_acceptable(self.user, item)

    def test_suggestions_carry_to_another_shape_of_the_same_career(self):
        # Step 1 is introductory in ladder_payload; Alt+B1 and Alt+B2 are its alternates.
        self.vote_on(self.first, suggest={'1': ['Alt+B1', 'Alt+B2']})

        self.assertEqual(self.carried_for(self.second), {'Introductory': ['Alt+B1', 'Alt+B2']})

    def test_they_do_not_reach_another_career(self):
        self.vote_on(self.first, suggest={'1': ['Alt+B1']})

        self.assertEqual(self.carried_for(self.elsewhere), {})

    def test_the_also_fine_picks_beside_a_replacement_carry_too(self):
        self.vote_on(self.first, drops=[3], verdict=Verdict.NEEDS_WORK, notes='Wrong rung.',
                     swaps={'3': {'best': 'Alt+I1', 'also': ['Alt+I2']}})

        # The best pick says what belonged there instead, which is about this pathway; the
        # also-fine picks say what would serve, which is about the career.
        self.assertEqual(self.carried_for(self.second), {'Intermediate': ['Alt+I2']})

    def test_nothing_would_work_carries_nothing(self):
        self.vote_on(self.first, drops=[3], verdict=Verdict.NEEDS_WORK, notes='Thin rung.',
                     swaps={'3': '__none__'})

        self.assertEqual(self.carried_for(self.second), {})

    def test_the_latest_word_on_a_level_wins(self):
        self.vote_on(self.first, suggest={'1': ['Alt+B1', 'Alt+B2']})
        self.vote_on(self.second, suggest={'1': ['Alt+B1']})
        third = PathwayReviewItemFactory(item_id='F0003', family_key='business analyst',
                                         payload=ladder_payload())

        self.assertEqual(self.carried_for(third), {'Introductory': ['Alt+B1']})

    def test_a_vote_that_said_nothing_about_a_level_leaves_the_answer_standing(self):
        self.vote_on(self.first, suggest={'1': ['Alt+B1']})
        self.vote_on(self.second)
        third = PathwayReviewItemFactory(item_id='F0003', family_key='business analyst',
                                         payload=ladder_payload())

        self.assertEqual(self.carried_for(third), {'Introductory': ['Alt+B1']})

    def test_only_courses_this_item_offers_come_back(self):
        self.vote_on(self.first, suggest={'1': ['Alt+B1', 'Alt+B2']})
        thin = PathwayReviewItemFactory(item_id='F0004', family_key='business analyst',
                                        payload=dict(ladder_payload(),
                                                     alt={'Introductory': [{'key': 'Alt+B2', 'title': 'Kept'}],
                                                          'Intermediate': [], 'Advanced': []}))

        self.assertEqual(self.carried_for(thin), {'Introductory': ['Alt+B2']})

    def test_an_item_carries_nothing_from_itself(self):
        self.vote_on(self.first, suggest={'1': ['Alt+B1']})

        self.assertEqual(self.carried_for(self.first), {})

    def test_the_next_item_is_served_with_what_was_carried(self):
        self.vote_on(self.first, suggest={'1': ['Alt+B1']})

        served = self.client.get(reverse('pathway_review:next-item')).json()['item']

        self.assertEqual(served['carried'], {'Introductory': ['Alt+B1']})

    def test_one_reviewer_never_sees_another_reviewers_answers(self):
        self.vote_on(self.first, suggest={'1': ['Alt+B1']})

        self.assertEqual(carried_acceptable(UserFactory(), self.second), {})
