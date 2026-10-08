"""
Tests for the pathway review models.
"""
import ddt
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_review.models import ReviewPool, Verdict, normalize_replacements
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

    def test_suggestions_default_to_none(self):
        """Votes cast before suggestions existed, and votes with none, read as an empty dict."""
        vote = PathwayReviewVoteFactory(item=self.item, reviewer=self.reviewer, verdict=Verdict.GOOD)
        vote.refresh_from_db()
        self.assertEqual(vote.suggestions, {})

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


@ddt.ddt
class NormalizeReplacementsTests(TestCase):
    """ Legacy and current votes read back in one shape. """

    @ddt.data(
        ('legacy bare key', {'3': 'Alt+I1'}, {'3': {'best': 'Alt+I1', 'also': []}}),
        ('nothing works, in legacy and current votes alike', {'3': '__none__'}, {'3': '__none__'}),
        ('legacy empty string is no pick, not nothing-works', {'3': ''}, {}),
        ('current shape', {'3': {'best': 'Alt+I1', 'also': ['Alt+I2', 'Alt+I3']}},
         {'3': {'best': 'Alt+I1', 'also': ['Alt+I2', 'Alt+I3']}}),
        ('current shape with no also-fine list', {'3': {'best': 'Alt+I1'}}, {'3': {'best': 'Alt+I1', 'also': []}}),
        ('mixed within one vote',
         {'1': '', '2': 'Alt+B1', '4': {'best': 'Alt+I1', 'also': ['Alt+I2']}, '5': '__none__'},
         {'2': {'best': 'Alt+B1', 'also': []}, '4': {'best': 'Alt+I1', 'also': ['Alt+I2']}, '5': '__none__'}),
        ('unreadable entries are left out', {'1': None, '2': 7, '3': {'also': ['Alt+I1']}, '4': {'best': ''}}, {}),
        ('an also-fine list that is not a list', {'3': {'best': 'Alt+I1', 'also': 'Alt+I2'}},
         {'3': {'best': 'Alt+I1', 'also': []}}),
        ('not a mapping at all', ['Alt+I1'], {}),
        ('never set', None, {}),
    )
    @ddt.unpack
    def test_normalizes(self, _case, stored, expected):
        self.assertEqual(normalize_replacements(stored), expected)

    def test_step_keys_come_back_as_strings(self):
        self.assertEqual(normalize_replacements({3: 'Alt+I1'}), {'3': {'best': 'Alt+I1', 'also': []}})

    def test_does_not_alias_the_stored_value(self):
        stored = {'3': {'best': 'Alt+I1', 'also': ['Alt+I2']}}
        normalize_replacements(stored)['3']['also'].append('Alt+I3')
        self.assertEqual(stored['3']['also'], ['Alt+I2'])

    def test_vote_reads_through_the_helper(self):
        vote = PathwayReviewVoteFactory(
            dropped_steps=[1, 2, 3, 4],
            replacements={'1': '', '2': 'Alt+B1', '3': {'best': 'Alt+I1', 'also': ['Alt+I2']}, '4': '__none__'},
        )
        self.assertEqual(vote.replacement_picks, {
            '2': {'best': 'Alt+B1', 'also': []},
            '3': {'best': 'Alt+I1', 'also': ['Alt+I2']},
            '4': '__none__',
        })

    def test_legacy_vote_shapes_as_stored_by_the_first_client(self):
        """
        The first client's two sentinels, as they sit in older votes, read without a data edit.

        ``''`` was what it sent for a dropped rung left unanswered, so it reads as no pick;
        ``'__none__'`` was "nothing here would work", and still is.
        """
        unanswered = PathwayReviewVoteFactory(dropped_steps=[2], replacements={'2': ''})
        nothing = PathwayReviewVoteFactory(dropped_steps=[1, 2], replacements={'1': 'Alt+B1', '2': '__none__'})

        self.assertEqual(unanswered.replacement_picks, {})
        self.assertEqual(nothing.replacement_picks, {'1': {'best': 'Alt+B1', 'also': []}, '2': '__none__'})
