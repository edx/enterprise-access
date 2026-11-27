"""
Tests for the rerun_learner_credit_top_ups management command.
"""
from datetime import timedelta
from io import StringIO
from unittest import mock
from uuid import uuid4

import ddt
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from freezegun import freeze_time

from enterprise_access.apps.customer_billing.constants import LearnerCreditTopUpState
from enterprise_access.apps.customer_billing.management.commands.rerun_learner_credit_top_ups import Command
from enterprise_access.apps.customer_billing.models import LearnerCreditTopUp
from enterprise_access.apps.customer_billing.tests.test_top_up_fulfillment import SpendLimitStepTestMixin

COMMAND = 'rerun_learner_credit_top_ups'
TASK_PATH = (
    'enterprise_access.apps.customer_billing.management.commands.rerun_learner_credit_top_ups.'
    'fulfill_learner_credit_top_up_task'
)


class RerunTestMixin(SpendLimitStepTestMixin):
    """
    Helpers to create top-ups where the command looks for them, and to run it.
    """

    def create_errored_top_up(self, retryable=True, automatic_reruns=0, deposited=False, **kwargs):
        """ A top-up in errored_fulfillment, with a deposit recorded when ``deposited``. """
        return self.create_top_up(
            state=LearnerCreditTopUpState.ERRORED_FULFILLMENT,
            last_error='RetryableFulfillmentError: 503' if retryable else 'NonRetryableFulfillmentError: 422',
            last_error_retryable=retryable,
            automatic_rerun_count=automatic_reruns,
            deposit_uuid=uuid4() if deposited else None,
            **kwargs,
        )

    def set_unchanged_for(self, top_up, minutes):
        """ Make ``top_up`` look unchanged for ``minutes``. """
        LearnerCreditTopUp.objects.filter(uuid=top_up.uuid).update(modified=timezone.now() - timedelta(minutes=minutes))

    def run_command(self, *args):
        """ Run the command, running the callbacks it registers to run on commit, and return its output. """
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command(COMMAND, *args, stdout=out)
        return out.getvalue()


@ddt.ddt
@override_settings(FEATURE_TOP_UP_LEARNER_CREDIT=False)
class RerunLearnerCreditTopUpsTests(RerunTestMixin, TestCase):
    """
    Tests for rerun_learner_credit_top_ups, with the fulfillment task mocked. The feature flag is off throughout:
    the command ignores it.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch(TASK_PATH)
        self.mock_task = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_queued(self, *top_ups):
        """ Assert that the fulfillment task was queued for exactly ``top_ups``, in that order. """
        self.assertEqual(
            self.mock_task.delay.call_args_list,
            [mock.call(top_up_uuid=str(top_up.uuid)) for top_up in top_ups],
        )

    def assert_unchanged(self, top_up):
        """ Assert that the command wrote nothing to ``top_up``. """
        history_count = top_up.history.count()
        stored = LearnerCreditTopUp.objects.get(uuid=top_up.uuid)
        self.assertEqual(
            (stored.state, stored.automatic_rerun_count, stored.modified),
            (top_up.state, top_up.automatic_rerun_count, top_up.modified),
        )
        self.assertEqual(stored.history.count(), history_count)

    @ddt.data(
        (True, LearnerCreditTopUpState.DEPOSITED),
        (False, LearnerCreditTopUpState.PAID),
    )
    @ddt.unpack
    def test_resumes_a_retryable_failure_at_the_step_that_failed(self, deposited, resume_state):
        """
        A retryable failure with 2 automatic re-runs resumes at deposited if a deposit was recorded, otherwise at
        paid; the task is queued once and the count becomes 3.
        """
        top_up = self.create_errored_top_up(automatic_reruns=2, deposited=deposited)

        output = self.run_command()

        top_up.refresh_from_db()
        self.assertEqual(top_up.state, resume_state)
        self.assertEqual(top_up.automatic_rerun_count, 3)
        self.assert_queued(top_up)
        self.assertIn('Resumed 1 and queued 0', output)

    @ddt.data(
        {'retryable': True, 'automatic_reruns': 3},
        {'retryable': False},
        {'retryable': None},
    )
    def test_leaves_some_failures_for_ops(self, top_up_kwargs):
        """
        A failure that reached the cap of 3 automatic re-runs, or that isn't retryable (or was never classified),
        is left as it is, and nothing is queued.
        """
        top_up = self.create_errored_top_up(**top_up_kwargs)

        self.run_command()

        self.assert_unchanged(top_up)
        self.mock_task.delay.assert_not_called()

    @ddt.data(
        (LearnerCreditTopUpState.PAID, 45, True),
        (LearnerCreditTopUpState.DEPOSITED, 45, True),
        (LearnerCreditTopUpState.PAID, 44, False),
        (LearnerCreditTopUpState.DEPOSITED, 44, False),
    )
    @ddt.unpack
    @freeze_time('2026-11-27 12:00:00')
    def test_queues_a_top_up_whose_task_was_lost(self, state, minutes, queued):
        """
        A paid or deposited top-up unchanged for 45 minutes has its task queued again; its state isn't changed.
        """
        top_up = self.create_top_up(state=state)
        self.set_unchanged_for(top_up, minutes)
        top_up.refresh_from_db()

        self.run_command()

        self.assert_unchanged(top_up)
        if queued:
            self.assert_queued(top_up)
        else:
            self.mock_task.delay.assert_not_called()

    def test_resumed_top_up_is_not_also_queued_as_stalled(self):
        """
        A retryable failure that hasn't changed for an hour is resumed and queued once, not queued again as stalled.
        """
        top_up = self.create_errored_top_up()
        self.set_unchanged_for(top_up, 60)

        self.run_command()

        self.assert_queued(top_up)

    @ddt.data(
        LearnerCreditTopUpState.CREATED,
        LearnerCreditTopUpState.REQUIRES_ACTION,
        LearnerCreditTopUpState.EXPIRED,
        LearnerCreditTopUpState.FULFILLED,
    )
    def test_ignores_other_states(self, state):
        """
        Top-ups not yet paid, or already fulfilled, are never touched, however old.
        """
        top_up = self.create_top_up(state=state)
        self.set_unchanged_for(top_up, 600)
        top_up.refresh_from_db()

        self.run_command()

        self.assert_unchanged(top_up)
        self.mock_task.delay.assert_not_called()

    @ddt.data(
        None,
        LearnerCreditTopUpState.DEPOSITED,
        LearnerCreditTopUpState.FULFILLED,
    )
    def test_two_runs_at_once_resume_a_top_up_once(self, moved_on_to):
        """
        Two runs that both selected a retryable failure resume it once: the second re-checks it under the lock and
        skips it, whether the first run's resume is all that happened or its task has since moved the top-up on.
        The task is queued once and the count is 1.
        """
        top_up = self.create_errored_top_up()
        resume = Command._resume  # pylint: disable=protected-access
        other_run = []

        def other_run_resumes_it_first(command, top_up_uuid, by_hand):
            if not other_run:
                other_run.append(command)
                call_command(COMMAND, stdout=StringIO())
                if moved_on_to:
                    LearnerCreditTopUp.objects.filter(uuid=top_up_uuid).update(state=moved_on_to)
            return resume(command, top_up_uuid, by_hand)

        with mock.patch.object(Command, '_resume', autospec=True, side_effect=other_run_resumes_it_first):
            output = self.run_command()

        top_up.refresh_from_db()
        self.assertEqual(top_up.state, moved_on_to or LearnerCreditTopUpState.PAID)
        self.assertEqual(top_up.automatic_rerun_count, 1)
        self.assert_queued(top_up)
        self.assertIn('Resumed 0 and queued 0', output)

    @ddt.data(
        (True, LearnerCreditTopUpState.DEPOSITED),
        (False, LearnerCreditTopUpState.PAID),
    )
    @ddt.unpack
    def test_ops_rerun_past_the_cap(self, deposited, resume_state):
        """
        Ops re-run a top-up that isn't retryable and has 3 automatic re-runs: it resumes and its task is queued.
        A run by hand doesn't count as automatic.
        """
        top_up = self.create_errored_top_up(retryable=False, automatic_reruns=3, deposited=deposited)

        self.run_command('--top-up-uuid', str(top_up.uuid))

        top_up.refresh_from_db()
        self.assertEqual(top_up.state, resume_state)
        self.assertEqual(top_up.automatic_rerun_count, 3)
        self.assert_queued(top_up)

    def test_ops_rerun_several_top_ups(self):
        """
        --top-up-uuid is repeatable: an errored top-up is resumed, a paid one is queued again, a fulfilled one is
        left as it is, and the selection of the automatic run isn't used.
        """
        errored = self.create_errored_top_up(retryable=False)
        paid = self.create_top_up(state=LearnerCreditTopUpState.PAID)
        fulfilled = self.create_top_up(state=LearnerCreditTopUpState.FULFILLED)
        other_retryable_failure = self.create_errored_top_up()

        output = self.run_command(
            '--top-up-uuid', str(errored.uuid), '--top-up-uuid', str(paid.uuid), '--top-up-uuid', str(fulfilled.uuid),
        )

        self.assertEqual(LearnerCreditTopUp.objects.get(uuid=errored.uuid).state, LearnerCreditTopUpState.PAID)
        self.assert_unchanged(paid)
        self.assert_unchanged(fulfilled)
        self.assert_unchanged(other_retryable_failure)
        self.assertCountEqual(
            self.mock_task.delay.call_args_list,
            [mock.call(top_up_uuid=str(errored.uuid)), mock.call(top_up_uuid=str(paid.uuid))],
        )
        self.assertIn('Resumed 1 and queued 1', output)

    def test_ops_rerun_of_an_unknown_top_up(self):
        """
        An unknown UUID is reported as an error after the others are re-run.
        """
        top_up = self.create_errored_top_up()
        unknown = uuid4()

        with self.assertRaisesRegex(CommandError, str(unknown)):
            self.run_command('--top-up-uuid', str(unknown), '--top-up-uuid', str(top_up.uuid))

        self.assert_queued(top_up)

    def test_dry_run_by_hand_changes_nothing(self):
        """
        A dry run of a re-run by hand changes nothing and queues nothing, and reports what it would do.
        """
        top_up = self.create_errored_top_up(retryable=False, automatic_reruns=3)

        with self.assertLogs('enterprise_access.apps.customer_billing.management.commands', level='INFO') as logs:
            output = self.run_command('--top-up-uuid', str(top_up.uuid), '--dry-run')

        self.assert_unchanged(top_up)
        self.mock_task.delay.assert_not_called()
        self.assertIn('[DRY RUN] Would resume 1 and queue 0', output)
        self.assertIn(
            f'[DRY RUN] Would resume LearnerCreditTopUp {top_up.uuid} by hand: errored_fulfillment -> paid',
            '\n'.join(logs.output),
        )

    def test_dry_run_changes_nothing(self):
        """
        A dry run of the scheduled job changes nothing and queues nothing, and reports what it would do.
        """
        retryable_failure = self.create_errored_top_up(deposited=True)
        stalled = self.create_top_up(state=LearnerCreditTopUpState.PAID)
        self.set_unchanged_for(stalled, 45)
        stalled.refresh_from_db()

        output = self.run_command('--dry-run')

        self.assert_unchanged(retryable_failure)
        self.assert_unchanged(stalled)
        self.mock_task.delay.assert_not_called()
        self.assertIn(
            f"[DRY RUN] Would resume 1 and queue 1 learner credit top-up(s). Resumed: ['{retryable_failure.uuid}']. "
            f"Queued: ['{stalled.uuid}'].",
            output,
        )


class RerunNeverDepositsOrRaisesTwiceTests(RerunTestMixin, TestCase):
    """
    The command with the real fulfillment task (run eagerly) and a mocked enterprise-subsidy that, like the real one,
    answers a repeated idempotency key with the deposit it already made.
    """

    def test_rerun_of_a_deposited_failure_raises_the_spend_limit_once(self):
        """
        A failure with a deposit recorded and its spend limit not yet raised resumes at the spend-limit step: no
        second deposit is requested, and the spend limit is raised once, however many times it is re-run.
        """
        top_up = self.create_errored_top_up(deposited=True)

        self.run_command()
        self.run_command('--top-up-uuid', str(top_up.uuid))
        self.run_command()

        top_up.refresh_from_db()
        self.assertEqual(top_up.state, LearnerCreditTopUpState.FULFILLED)
        self.mock_subsidy_client.create_subsidy_deposit.assert_not_called()
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + self.AMOUNT)
        self.assertEqual(top_up.automatic_rerun_count, 1)

    def test_rerun_after_a_lost_deposit_answer_deposits_once(self):
        """
        enterprise-subsidy made the deposit, but its answer was lost, so no deposit was recorded: the re-run resumes
        at paid and sends the same key again, so enterprise-subsidy holds one deposit for lc-top-up-<uuid>, and the
        spend limit is raised once.
        """
        top_up = self.create_errored_top_up()
        self._create_deposit(idempotency_key=f'lc-top-up-{top_up.uuid}', desired_deposit_quantity=self.AMOUNT)

        self.run_command()
        self.run_command()

        top_up.refresh_from_db()
        self.assertEqual(top_up.state, LearnerCreditTopUpState.FULFILLED)
        self.assertEqual(list(self.deposits), [f'lc-top-up-{top_up.uuid}'])
        self.assertEqual(str(top_up.deposit_uuid), self.deposits[f'lc-top-up-{top_up.uuid}']['uuid'])
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + self.AMOUNT)
