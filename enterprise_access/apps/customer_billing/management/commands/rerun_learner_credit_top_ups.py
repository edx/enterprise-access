"""
Resume learner credit top-ups whose fulfillment stopped, on a schedule or by hand.
"""
import logging
from datetime import timedelta
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from enterprise_access.apps.customer_billing.constants import (
    LEARNER_CREDIT_TOP_UP_MAX_AUTOMATIC_RERUNS,
    LEARNER_CREDIT_TOP_UP_STALLED_MINUTES,
    LearnerCreditTopUpState
)
from enterprise_access.apps.customer_billing.models import LearnerCreditTopUp
from enterprise_access.apps.customer_billing.tasks import fulfill_learner_credit_top_up_task

logger = logging.getLogger(__name__)

IN_PROGRESS_STATES = (LearnerCreditTopUpState.PAID, LearnerCreditTopUpState.DEPOSITED)


class Command(BaseCommand):
    """
    Without ``--top-up-uuid`` (the CronJob, every 30 minutes):

    - resumes each ``errored_fulfillment`` top-up whose error is retryable and that has been resumed automatically
      fewer than ``LEARNER_CREDIT_TOP_UP_MAX_AUTOMATIC_RERUNS`` times: it moves back to ``deposited`` if a deposit
      was recorded (``deposit_uuid``), otherwise to ``paid``, its fulfillment task is queued, and the count goes up;
    - queues the fulfillment task again for each ``paid`` or ``deposited`` top-up unchanged for
      ``LEARNER_CREDIT_TOP_UP_STALLED_MINUTES``, for example after a lost Celery message. Its state isn't changed.

    It never resumes a top-up whose error isn't retryable, or one that reached the cap: ops fix the cause, then
    re-run it by hand with ``--top-up-uuid``, which ignores both checks and doesn't count.

    A resume changes the state only through ``transition_to()``, after re-checking the top-up under a row lock, and
    queues the task only after that commits. So two runs at once resume a top-up once. The stalled re-queue changes
    no state, so two runs at once can queue that task twice; the second does nothing. Like the task, the command
    ignores ``FEATURE_TOP_UP_LEARNER_CREDIT``.

    Usage:
        ./manage.py rerun_learner_credit_top_ups
        ./manage.py rerun_learner_credit_top_ups --dry-run
        ./manage.py rerun_learner_credit_top_ups --top-up-uuid <uuid> [--top-up-uuid <uuid> ...] [--dry-run]
    """

    help = (
        'Resume learner credit top-ups stopped in errored_fulfillment with a retryable error, and queue fulfillment '
        'again for top-ups stuck in paid or deposited. With --top-up-uuid, re-run those top-ups by hand.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--top-up-uuid',
            dest='top_up_uuids',
            action='append',
            type=UUID,
            default=[],
            help=(
                'Re-run this top-up by hand (repeatable): an errored_fulfillment one resumes even when its error '
                'is not retryable or it reached the automatic cap, and a paid or deposited one is queued again. '
                'Fix the cause of the error first, or it fails the same way.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be resumed and queued, without changing or queuing anything.',
        )

    # Set for each run by handle().
    dry_run = False
    resumed = ()
    queued = ()

    def handle(self, *args, **options):
        self.dry_run = options['dry_run']
        self.resumed, self.queued = [], []

        if options['top_up_uuids']:
            self._rerun_by_hand(options['top_up_uuids'])
        else:
            self._rerun_automatically()

        verbs = '[DRY RUN] Would resume {} and queue {}' if self.dry_run else 'Resumed {} and queued {}'
        summary = verbs.format(len(self.resumed), len(self.queued))
        resumed = [str(uuid) for uuid in self.resumed]
        queued = [str(uuid) for uuid in self.queued]
        self.stdout.write(self.style.SUCCESS(
            f'{summary} learner credit top-up(s). Resumed: {resumed}. Queued: {queued}.'
        ))

    def _rerun_automatically(self):
        """
        Resume retryable failures under the cap, then queue again the top-ups stuck in paid or deposited.
        """
        retryable_failures = LearnerCreditTopUp.objects.filter(
            state=LearnerCreditTopUpState.ERRORED_FULFILLMENT,
            last_error_retryable=True,
            automatic_rerun_count__lt=LEARNER_CREDIT_TOP_UP_MAX_AUTOMATIC_RERUNS,
        ).order_by('created').values_list('uuid', flat=True)
        for top_up_uuid in retryable_failures:
            self._resume(top_up_uuid, by_hand=False)

        # Selected after the resumes, which update `modified`, so a top-up just resumed isn't queued twice.
        stalled = LearnerCreditTopUp.objects.filter(
            state__in=IN_PROGRESS_STATES,
            modified__lte=timezone.now() - timedelta(minutes=LEARNER_CREDIT_TOP_UP_STALLED_MINUTES),
        ).order_by('created').values_list('uuid', flat=True)
        for top_up_uuid in stalled:
            self._queue(top_up_uuid, f'unchanged for {LEARNER_CREDIT_TOP_UP_STALLED_MINUTES} minutes')

    def _rerun_by_hand(self, top_up_uuids):
        """
        Resume or queue again each of ``top_up_uuids``, ignoring the cap and the retryable check.
        """
        not_found = []
        for top_up_uuid in top_up_uuids:
            top_up = LearnerCreditTopUp.objects.filter(uuid=top_up_uuid).first()
            if top_up is None:
                not_found.append(str(top_up_uuid))
            elif top_up.state in IN_PROGRESS_STATES:
                self._queue(top_up_uuid, f're-run by hand while {top_up.state}')
            else:
                self._resume(top_up_uuid, by_hand=True)
        if not_found:
            raise CommandError(f'No LearnerCreditTopUp found for {not_found}.')

    def _resume(self, top_up_uuid, by_hand):
        """
        Move an ``errored_fulfillment`` top-up back to the step that failed and queue its task once the move commits.

        The top-up is re-checked under a row lock, so one that another run resumed, or that moved on in the meantime,
        is skipped rather than resumed twice; ``transition_to()`` therefore never sees a disallowed change.
        """
        with transaction.atomic():
            top_up = LearnerCreditTopUp.objects.select_for_update().get(uuid=top_up_uuid)
            if not self._can_resume(top_up, by_hand):
                logger.info(
                    'Not resuming LearnerCreditTopUp %s: it is %s, retryable=%s, automatic re-runs=%s.',
                    top_up_uuid, top_up.state, top_up.last_error_retryable, top_up.automatic_rerun_count,
                )
                return

            resume_state = LearnerCreditTopUpState.DEPOSITED if top_up.deposit_uuid else LearnerCreditTopUpState.PAID
            how = 'by hand' if by_hand else f'automatically ({top_up.automatic_rerun_count + 1})'
            if self.dry_run:
                logger.info('[DRY RUN] Would resume LearnerCreditTopUp %s %s: %s -> %s, and queue its fulfillment.',
                            top_up_uuid, how, top_up.state, resume_state)
                self.resumed.append(top_up_uuid)
                return

            fields = {} if by_hand else {'automatic_rerun_count': top_up.automatic_rerun_count + 1}
            if top_up.transition_to(resume_state, **fields):
                logger.info('Resumed LearnerCreditTopUp %s %s: %s -> %s. Last error: %s',
                            top_up_uuid, how, LearnerCreditTopUpState.ERRORED_FULFILLMENT, resume_state,
                            top_up.last_error)
                self.resumed.append(top_up_uuid)
                transaction.on_commit(lambda: fulfill_learner_credit_top_up_task.delay(top_up_uuid=str(top_up_uuid)))

    @staticmethod
    def _can_resume(top_up, by_hand):
        """
        Whether ``top_up`` (read under its row lock) may be resumed.
        """
        if top_up.state != LearnerCreditTopUpState.ERRORED_FULFILLMENT:
            return False
        if by_hand:
            return True
        under_cap = top_up.automatic_rerun_count < LEARNER_CREDIT_TOP_UP_MAX_AUTOMATIC_RERUNS
        return top_up.last_error_retryable is True and under_cap

    def _queue(self, top_up_uuid, reason):
        """
        Queue the fulfillment task of a top-up in paid or deposited, without changing it.
        """
        if self.dry_run:
            logger.info('[DRY RUN] Would queue fulfillment of LearnerCreditTopUp %s: %s.', top_up_uuid, reason)
        else:
            logger.info('Queuing fulfillment of LearnerCreditTopUp %s: %s.', top_up_uuid, reason)
            fulfill_learner_credit_top_up_task.delay(top_up_uuid=str(top_up_uuid))
        self.queued.append(top_up_uuid)
