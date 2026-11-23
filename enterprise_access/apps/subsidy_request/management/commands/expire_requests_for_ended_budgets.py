"""
Management command to expire learner credit requests that are still awaiting review on a budget that has ended.
"""
import logging

import requests
from django.core.management.base import BaseCommand, CommandError
from pytz import UTC

from enterprise_access.apps.content_assignments.content_metadata_api import parse_datetime_string
from enterprise_access.apps.subsidy_access_policy.models import SubsidyAccessPolicy
from enterprise_access.apps.subsidy_request.api import expire_unreviewed_learner_credit_request
from enterprise_access.apps.subsidy_request.constants import APPROVABLE_STATES
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequest
from enterprise_access.toggles import expire_requests_for_ended_budgets_enabled
from enterprise_access.utils import localized_utcnow

logger = logging.getLogger(__name__)

LOG_PREFIX = '[EXPIRE_REQUESTS_FOR_ENDED_BUDGETS]'


class Command(BaseCommand):
    """
    Expires Browse & Request learner credit requests that nobody can approve any more.

    A request awaiting review has no assignment, so nothing closes it when its budget is retired or its subsidy
    expires. It then keeps blocking the learner: the learner portal shows the course as awaiting approval, and the
    learner can't request it again on another budget. Approved requests are not handled here; they expire with
    their assignment (``automatically_expire_assignments``).

    Expiring a request can't be undone in bulk, so only a budget that can't come back counts as ended. Requests on
    a budget that is merely deactivated (``active=False``), or whose Browse & Request configuration was switched
    off while the budget is still live, are left alone: both can be switched back on.
    """
    help = 'Expire learner credit requests that are still awaiting review on a budget that has ended.'

    def add_arguments(self, parser):
        scoped_run_note = 'A scoped run expires requests even while the nightly switch is off.'
        parser.add_argument(
            '--dry-run', action='store_true', help='Only log the budgets and request counts that would be expired.',
        )
        parser.add_argument('--enterprise-customer-uuid', help=f'Only process this customer. {scoped_run_note}')
        parser.add_argument('--policy-uuid', help=f'Only process this budget. {scoped_run_note}')

    @staticmethod
    def budget_has_ended(policy):
        """
        Returns True if the policy can never approve a request again: it is retired, or its subsidy's expiration
        date has passed. A subsidy that hasn't started yet has not ended.

        Raises:
            requests.exceptions.RequestException: if the subsidy can't be fetched (an HTTP error, a dropped
                connection or a timeout), so that a failed lookup isn't read as "ended".
        """
        if policy.retired:
            return True
        expiration_datetime = policy.subsidy_record(raise_on_error=True).get('expiration_datetime')
        if not expiration_datetime:
            return False
        return parse_datetime_string(expiration_datetime).replace(tzinfo=UTC) < localized_utcnow()

    def handle(self, *args, **options):
        enterprise_customer_uuid = options['enterprise_customer_uuid']
        policy_uuid = options['policy_uuid']
        is_scoped = bool(enterprise_customer_uuid or policy_uuid)
        dry_run = options['dry_run'] or not (is_scoped or expire_requests_for_ended_budgets_enabled())

        policies = SubsidyAccessPolicy.objects.filter(learner_credit_request_config__isnull=False)
        if enterprise_customer_uuid:
            policies = policies.filter(enterprise_customer_uuid=enterprise_customer_uuid)
        if policy_uuid:
            policies = policies.filter(uuid=policy_uuid)

        expired_count = 0
        failures = []
        for policy in policies:
            # Freeze the candidates up front: expiring a request removes it from this filter.
            request_uuids = list(
                LearnerCreditRequest.objects.filter(
                    learner_credit_request_config=policy.learner_credit_request_config,
                    state__in=APPROVABLE_STATES,
                    assignment__isnull=True,
                ).values_list('uuid', flat=True)
            )
            if not request_uuids:
                continue

            try:
                if not self.budget_has_ended(policy):
                    continue
            except requests.exceptions.RequestException:
                # Any failure to reach the subsidy service, not only an HTTP error: one budget's lookup failing
                # must not stop the run for every other customer.
                logger.exception('%s Could not fetch the subsidy for policy %s, skipping it.', LOG_PREFIX, policy.uuid)
                failures.append(f'policy {policy.uuid}')
                continue

            logger.info(
                '%s Policy %s of enterprise %s has ended with %d requests awaiting review. dry_run [%s]',
                LOG_PREFIX, policy.uuid, policy.enterprise_customer_uuid, len(request_uuids), dry_run,
            )
            if dry_run:
                continue

            for request_uuid in request_uuids:
                try:
                    if expire_unreviewed_learner_credit_request(request_uuid):
                        expired_count += 1
                except Exception:  # pylint: disable=broad-except
                    logger.exception('%s Failed to expire request %s', LOG_PREFIX, request_uuid)
                    failures.append(f'request {request_uuid}')

        logger.info('%s Expired %d requests. dry_run [%s]', LOG_PREFIX, expired_count, dry_run)
        if failures:
            raise CommandError(f'Failed to process {len(failures)} budgets or requests: {failures}')
