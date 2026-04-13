"""
Tests for the backfill_learner_credit_request_action_statuses management command.
"""
from io import StringIO

from django.core.management import call_command
from pytest import mark

from enterprise_access.apps.subsidy_request.constants import (
    LearnerCreditAdditionalActionStates,
    LearnerCreditRequestUserMessages,
    SubsidyRequestStates
)
from enterprise_access.apps.subsidy_request.tests.factories import LearnerCreditRequestActionsFactory
from test_utils import APITestWithMocks


@mark.django_db
class TestBackfillLearnerCreditRequestActionStatuses(APITestWithMocks):
    """
    Tests for the backfill_learner_credit_request_action_statuses management command.
    """

    COMMAND_NAME = 'backfill_learner_credit_request_action_statuses'

    def _call_command(self, dry_run=False):
        """Helper to call the command and capture output."""
        out = StringIO()
        args = ['--dry-run'] if dry_run else []
        call_command(self.COMMAND_NAME, *args, stdout=out)
        return out.getvalue()

    def test_no_records(self):
        """Command runs cleanly when there are no records."""
        output = self._call_command()
        assert '0 record(s) were updated' in output

    def test_all_records_already_correct(self):
        """Command reports no updates when all statuses are correct."""
        status_mapping = dict(LearnerCreditRequestUserMessages.CHOICES)
        LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.APPROVED,
            status=status_mapping[SubsidyRequestStates.APPROVED],
        )
        LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.DECLINED,
            status=status_mapping[SubsidyRequestStates.DECLINED],
        )
        output = self._call_command()
        assert '0 record(s) were updated' in output

    def test_mismatched_statuses_are_updated(self):
        """Command updates records whose status doesn't match the expected value."""
        action = LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.APPROVED,
            status=SubsidyRequestStates.REQUESTED,  # wrong status
        )
        output = self._call_command()

        action.refresh_from_db()
        # The expected status for 'approved' recent_action is "Waiting For Learner"
        expected_status = dict(LearnerCreditRequestUserMessages.CHOICES)[SubsidyRequestStates.APPROVED]
        assert action.status == expected_status
        assert '1 record(s) updated' in output

    def test_multiple_action_types_updated(self):
        """Command updates records across multiple action types."""
        LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.APPROVED,
            status=SubsidyRequestStates.REQUESTED,  # wrong
        )
        LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.DECLINED,
            status=SubsidyRequestStates.REQUESTED,  # wrong
        )
        LearnerCreditRequestActionsFactory(
            recent_action=LearnerCreditAdditionalActionStates.REMINDED,
            status=SubsidyRequestStates.REQUESTED,  # wrong
        )
        output = self._call_command()
        assert '3 record(s) were updated' in output

    def test_dry_run_does_not_modify_records(self):
        """Dry run previews changes without modifying data."""
        action = LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.APPROVED,
            status=SubsidyRequestStates.REQUESTED,  # wrong
        )
        original_status = action.status

        output = self._call_command(dry_run=True)

        action.refresh_from_db()
        assert action.status == original_status
        assert 'DRY RUN' in output
        assert 'would be updated' in output

    def test_dry_run_reports_correct_counts(self):
        """Dry run reports accurate counts of records that would be updated."""
        for _ in range(3):
            LearnerCreditRequestActionsFactory(
                recent_action=SubsidyRequestStates.APPROVED,
                status=SubsidyRequestStates.REQUESTED,  # wrong
            )
        output = self._call_command(dry_run=True)
        assert '3 record(s) would be updated' in output

    def test_correct_records_not_affected(self):
        """Records with correct statuses are not modified during backfill."""
        status_mapping = dict(LearnerCreditRequestUserMessages.CHOICES)
        correct_status = status_mapping[SubsidyRequestStates.REQUESTED]
        correct_action = LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.REQUESTED,
            status=correct_status,
        )
        wrong_action = LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.APPROVED,
            status=SubsidyRequestStates.REQUESTED,  # wrong
        )

        self._call_command()

        correct_action.refresh_from_db()
        wrong_action.refresh_from_db()
        assert correct_action.status == correct_status
        assert wrong_action.status == status_mapping[SubsidyRequestStates.APPROVED]

    def test_unmapped_recent_actions_are_flagged(self):
        """Records with a recent_action not in the mapping are reported and left untouched."""
        unmapped = LearnerCreditRequestActionsFactory(
            recent_action=SubsidyRequestStates.ERROR,
            status=SubsidyRequestStates.ERROR,
        )
        output = self._call_command()

        unmapped.refresh_from_db()
        assert unmapped.status == SubsidyRequestStates.ERROR
        assert '1 record(s) have a recent_action with no status mapping' in output
