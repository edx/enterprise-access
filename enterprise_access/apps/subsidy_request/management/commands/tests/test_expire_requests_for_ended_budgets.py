"""
Tests for the ``expire_requests_for_ended_budgets`` management command.
"""
from unittest import mock

import ddt
from django.core.management import CommandError, call_command
from django.test import TestCase
from requests.exceptions import HTTPError

from enterprise_access.apps.content_assignments.tests.factories import LearnerContentAssignmentFactory
from enterprise_access.apps.subsidy_access_policy.models import SubsidyAccessPolicy
from enterprise_access.apps.subsidy_access_policy.tests.factories import (
    PerLearnerSpendCapLearnerCreditAccessPolicyFactory
)
from enterprise_access.apps.subsidy_request.api import expire_unreviewed_learner_credit_request
from enterprise_access.apps.subsidy_request.constants import SubsidyRequestStates
from enterprise_access.apps.subsidy_request.models import LearnerCreditRequest
from enterprise_access.apps.subsidy_request.tests.factories import (
    LearnerCreditRequestConfigurationFactory,
    LearnerCreditRequestFactory
)

COMMAND = 'expire_requests_for_ended_budgets'
COMMAND_MODULE = f'enterprise_access.apps.subsidy_request.management.commands.{COMMAND}'
SWITCH_IS_ENABLED = f'{COMMAND_MODULE}.expire_requests_for_ended_budgets_enabled'
EXPIRE_REQUEST = f'{COMMAND_MODULE}.expire_unreviewed_learner_credit_request'
EXPIRED_SUBSIDY = {'is_active': False, 'expiration_datetime': '2020-01-01T00:00:00Z'}
LIVE_SUBSIDY = {'is_active': True, 'expiration_datetime': '2099-01-01T00:00:00Z'}
# Inactive because it hasn't started, which isn't the same as having ended.
FUTURE_SUBSIDY = {'is_active': False, 'expiration_datetime': '2099-01-01T00:00:00Z'}


@ddt.ddt
class TestExpireRequestsForEndedBudgets(TestCase):
    """
    Tests for the ``expire_requests_for_ended_budgets`` management command.
    """

    def setUp(self):
        super().setUp()
        subsidy_client_patcher = mock.patch.object(SubsidyAccessPolicy, 'subsidy_client')
        mock_subsidy_client = subsidy_client_patcher.start()
        self.addCleanup(subsidy_client_patcher.stop)
        self.subsidy_responses = {}
        mock_subsidy_client.retrieve_subsidy.side_effect = self._retrieve_subsidy

        switch_patcher = mock.patch(SWITCH_IS_ENABLED, return_value=True)
        self.mock_switch_is_enabled = switch_patcher.start()
        self.addCleanup(switch_patcher.stop)

    def _retrieve_subsidy(self, subsidy_uuid):
        subsidy_response = self.subsidy_responses[str(subsidy_uuid)]
        if isinstance(subsidy_response, Exception):
            raise subsidy_response
        return subsidy_response

    def _create_request(self, subsidy_response=None, state=SubsidyRequestStates.REQUESTED, **policy_kwargs):
        """
        Creates a request on its own Browse & Request budget, whose subsidy answers ``subsidy_response``
        (an expired subsidy unless given).
        """
        policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory(
            learner_credit_request_config=LearnerCreditRequestConfigurationFactory(active=True),
            **policy_kwargs,
        )
        self.subsidy_responses[str(policy.subsidy_uuid)] = subsidy_response or EXPIRED_SUBSIDY
        return LearnerCreditRequestFactory(
            learner_credit_request_config=policy.learner_credit_request_config,
            enterprise_customer_uuid=policy.enterprise_customer_uuid,
            state=state,
        )

    @ddt.data(
        {'subsidy_response': EXPIRED_SUBSIDY},
        {'subsidy_response': LIVE_SUBSIDY, 'retired': True},
        {'subsidy_response': LIVE_SUBSIDY, 'active': False},
        # A request whose approval errored is still awaiting review.
        {'state': SubsidyRequestStates.ERROR},
    )
    def test_expires_requests_awaiting_review_on_an_ended_budget(self, request_kwargs):
        request = self._create_request(**request_kwargs)

        call_command(COMMAND)

        request.refresh_from_db()
        self.assertEqual(request.state, SubsidyRequestStates.EXPIRED)
        action = request.actions.get()
        self.assertEqual(action.recent_action, SubsidyRequestStates.EXPIRED)
        self.assertEqual(action.status, SubsidyRequestStates.EXPIRED)
        # The expired request no longer stops the learner requesting the same course on another budget.
        LearnerCreditRequestFactory(
            user=request.user,
            enterprise_customer_uuid=request.enterprise_customer_uuid,
            course_id=request.course_id,
        )

    @ddt.data(
        {'subsidy_response': LIVE_SUBSIDY},
        {'subsidy_response': FUTURE_SUBSIDY},
        # A subsidy with no expiration date never ends on its own.
        {'subsidy_response': {'is_active': True, 'expiration_datetime': None}},
        # Requests that were reviewed are left alone, even on an ended budget.
        {'state': SubsidyRequestStates.APPROVED},
        {'state': SubsidyRequestStates.ACCEPTED},
        {'state': SubsidyRequestStates.DECLINED},
        {'state': SubsidyRequestStates.CANCELLED},
    )
    def test_leaves_other_requests_alone(self, request_kwargs):
        request = self._create_request(**request_kwargs)

        call_command(COMMAND)

        request.refresh_from_db()
        self.assertEqual(request.state, request_kwargs.get('state', SubsidyRequestStates.REQUESTED))
        self.assertFalse(request.actions.exists())

    def test_leaves_a_request_with_an_assignment_alone(self):
        """
        Test that a request tied to an assignment is left to expire with it, so the assignment isn't orphaned.
        """
        request = self._create_request()
        request.assignment = LearnerContentAssignmentFactory()
        request.save()

        call_command(COMMAND)

        request.refresh_from_db()
        self.assertEqual(request.state, SubsidyRequestStates.REQUESTED)

    @ddt.data(
        # Without the switch, a run across every budget only logs.
        {'switch_is_on': False, 'expires': False},
        {'switch_is_on': True, 'dry_run': True, 'expires': False},
        # A scoped run is a deliberate operator action, so it doesn't wait for the switch.
        {'switch_is_on': False, 'scope': 'enterprise_customer_uuid', 'expires': True},
        {'switch_is_on': False, 'scope': 'policy_uuid', 'expires': True},
        {'switch_is_on': False, 'scope': 'policy_uuid', 'dry_run': True, 'expires': False},
    )
    @ddt.unpack
    def test_switch_dry_run_and_scope(self, switch_is_on, expires, scope=None, dry_run=False):
        request = self._create_request()
        other_customer_request = self._create_request()
        options = {'dry_run': dry_run}
        if scope:
            policy = request.learner_credit_request_config.learner_credit_config
            scope_uuids = {'enterprise_customer_uuid': policy.enterprise_customer_uuid, 'policy_uuid': policy.uuid}
            options[scope] = str(scope_uuids[scope])

        self.mock_switch_is_enabled.return_value = switch_is_on
        call_command(COMMAND, **options)

        request.refresh_from_db()
        other_customer_request.refresh_from_db()
        self.assertEqual(request.state == SubsidyRequestStates.EXPIRED, expires)
        # In every case above, the other customer's request is either out of scope or only logged.
        self.assertEqual(other_customer_request.state, SubsidyRequestStates.REQUESTED)

    def test_subsidy_lookup_failure_skips_that_budget_and_fails_the_run(self):
        """
        Test that a failed subsidy lookup is never read as an ended budget, and is reported once the run is done.
        """
        unverified_request = self._create_request(subsidy_response=HTTPError('the subsidy service is down'))
        ended_budget_request = self._create_request()

        with self.assertRaises(CommandError):
            call_command(COMMAND)

        unverified_request.refresh_from_db()
        ended_budget_request.refresh_from_db()
        self.assertEqual(unverified_request.state, SubsidyRequestStates.REQUESTED)
        self.assertEqual(ended_budget_request.state, SubsidyRequestStates.EXPIRED)

    def test_request_that_fails_to_expire_fails_the_run_once_the_rest_are_processed(self):
        failing_request = self._create_request()
        other_request = self._create_request()

        def expire_all_but_one(request_uuid):
            if request_uuid == failing_request.uuid:
                raise ValueError('this request cannot be saved')
            return expire_unreviewed_learner_credit_request(request_uuid)

        with mock.patch(EXPIRE_REQUEST, side_effect=expire_all_but_one), self.assertRaises(CommandError):
            call_command(COMMAND)

        other_request.refresh_from_db()
        self.assertEqual(other_request.state, SubsidyRequestStates.EXPIRED)

    def test_request_reviewed_while_the_command_runs_is_not_counted_as_expired(self):
        self._create_request()

        with mock.patch(EXPIRE_REQUEST, return_value=False), self.assertLogs(COMMAND_MODULE, level='INFO') as logs:
            call_command(COMMAND)

        self.assertIn('Expired 0 requests', logs.output[-1])

    def test_request_reviewed_after_it_was_selected_is_not_expired(self):
        """
        Test that the stored state decides, not the copy the command loaded before an admin acted.
        """
        request = self._create_request()
        LearnerCreditRequest.objects.filter(pk=request.pk).update(state=SubsidyRequestStates.DECLINED)

        self.assertFalse(expire_unreviewed_learner_credit_request(request.uuid))

        request.refresh_from_db()
        self.assertEqual(request.state, SubsidyRequestStates.DECLINED)
        self.assertFalse(request.actions.exists())
