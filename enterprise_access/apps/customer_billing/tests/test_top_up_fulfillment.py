"""
Tests for the learner credit top-up fulfillment steps.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

import ddt
import requests
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection
from django.db.models import QuerySet
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time

from enterprise_access.apps.customer_billing.constants import (
    LEARNER_CREDIT_TOP_UP_DEPOSIT_PROVIDER,
    LearnerCreditTopUpState
)
from enterprise_access.apps.customer_billing.models import LearnerCreditTopUp
from enterprise_access.apps.customer_billing.tests.factories import LearnerCreditTopUpFactory
from enterprise_access.apps.customer_billing.top_up_fulfillment import (
    NonRetryableFulfillmentError,
    RetryableFulfillmentError,
    fulfill_deposit_step,
    fulfill_spend_limit_step
)
from enterprise_access.apps.subsidy_access_policy.models import REQUEST_CACHE_NAMESPACE, SubsidyAccessPolicy
from enterprise_access.apps.subsidy_access_policy.tests.factories import (
    PerLearnerSpendCapLearnerCreditAccessPolicyFactory
)
from enterprise_access.cache_utils import request_cache

SUBSIDY_CLIENT_PATH = 'enterprise_access.apps.subsidy_access_policy.models.get_versioned_subsidy_client'


def http_error(status_code):
    """ An HTTPError like the subsidy client raises for an enterprise-subsidy answer with ``status_code``. """
    response = requests.Response()
    response.status_code = status_code
    response._content = b'{"detail": "enterprise-subsidy said no"}'  # pylint: disable=protected-access
    return requests.exceptions.HTTPError(f'{status_code} Error', response=response)


class SpendLimitStepTestMixin:
    """
    A budget with a 500,000-cent spend limit, a mocked enterprise-subsidy client whose subsidy already holds the
    top-up's 100,000-cent deposit, and an empty request cache.

    The mocked deposit call behaves like enterprise-subsidy's: a repeated idempotency key gets back the deposit it
    already made. ``self.deposits`` holds the deposits made, by key.
    """
    SPEND_LIMIT = 500_000
    AMOUNT = 100_000

    def setUp(self):
        super().setUp()
        self._clear_request_cache()
        self.addCleanup(self._clear_request_cache)

        patcher = mock.patch(SUBSIDY_CLIENT_PATH)
        self.mock_subsidy_client = patcher.start().return_value
        self.addCleanup(patcher.stop)

        self.policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory(spend_limit=self.SPEND_LIMIT)
        self.set_total_deposits(self.SPEND_LIMIT + self.AMOUNT)

        self.deposits = {}
        self.mock_subsidy_client.create_subsidy_deposit.side_effect = self._create_deposit

    def _create_deposit(self, **kwargs):
        """ Make a deposit, or return the one already made with the same idempotency key. """
        return self.deposits.setdefault(kwargs['idempotency_key'], {
            'uuid': str(uuid4()),
            'transaction': str(uuid4()),
            'desired_deposit_quantity': kwargs['desired_deposit_quantity'],
        })

    @staticmethod
    def _clear_request_cache():
        request_cache(namespace=REQUEST_CACHE_NAMESPACE).clear()

    def set_total_deposits(self, total_deposits):
        """ Make enterprise-subsidy answer with a subsidy holding ``total_deposits``. """
        self.mock_subsidy_client.retrieve_subsidy.return_value = {
            'uuid': str(self.policy.subsidy_uuid),
            'is_active': True,
            'total_deposits': total_deposits,
            'current_balance': total_deposits,
        }

    def create_top_up(self, state=LearnerCreditTopUpState.DEPOSITED, **kwargs):
        """ Create a top-up of the budget, already in ``state``. """
        timestamps = {}
        if state in (LearnerCreditTopUpState.DEPOSITED, LearnerCreditTopUpState.FULFILLED):
            timestamps['deposited_at'] = timezone.now() - timedelta(minutes=1)
        if state == LearnerCreditTopUpState.FULFILLED:
            timestamps['fulfilled_at'] = timezone.now()
        return LearnerCreditTopUpFactory(
            subsidy_access_policy=self.policy,
            amount_cents=kwargs.pop('amount_cents', self.AMOUNT),
            stripe_payment_intent_id=kwargs.pop('stripe_payment_intent_id', f'pi_{uuid4().hex}'),
            state=state,
            **{**timestamps, **kwargs},
        )

    def stored_spend_limit(self):
        """ The budget's spend_limit as stored in the database. """
        return SubsidyAccessPolicy.objects.get(uuid=self.policy.uuid).spend_limit


@ddt.ddt
class TestFulfillSpendLimitStep(SpendLimitStepTestMixin, TestCase):
    """
    Tests for ``fulfill_spend_limit_step()``.
    """

    def assert_nothing_changed(self, top_up, state=LearnerCreditTopUpState.DEPOSITED, history_count=None):
        """ Assert that the step left the top-up and its budget as they were. """
        top_up.refresh_from_db()
        self.assertEqual(top_up.state, state)
        self.assertIsNone(top_up.spend_limit_before)
        self.assertIsNone(top_up.spend_limit_after)
        if state != LearnerCreditTopUpState.FULFILLED:
            self.assertIsNone(top_up.fulfilled_at)
        if history_count is not None:
            self.assertEqual(top_up.history.count(), history_count)
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT)

    @freeze_time('2026-11-18 12:00:00')
    def test_raises_spend_limit_and_fulfills(self):
        """
        Scenario 1: the spend limit is raised by amount_cents, the before and after values are recorded,
        fulfilled_at is set and the top-up is fulfilled.
        """
        top_up = self.create_top_up()

        self.assertTrue(fulfill_spend_limit_step(top_up))

        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + self.AMOUNT)
        for fulfilled in (top_up, LearnerCreditTopUp.objects.get(uuid=top_up.uuid)):
            self.assertEqual(fulfilled.state, LearnerCreditTopUpState.FULFILLED)
            self.assertEqual(fulfilled.fulfilled_at, timezone.now())
            self.assertEqual(fulfilled.spend_limit_before, self.SPEND_LIMIT)
            self.assertEqual(fulfilled.spend_limit_after, self.SPEND_LIMIT + self.AMOUNT)
        # Latest by insertion, not by history_date: setUp created the policy at the real time, outside freeze_time.
        latest_policy_history = self.policy.history.order_by('-history_id').first()
        self.assertEqual(latest_policy_history.history_change_reason, f'Learner credit top-up {top_up.uuid}')

    def test_raise_and_state_change_share_one_transaction(self):
        """
        Scenario 1: the raise and the state change are written in one transaction. If moving the top-up to
        fulfilled fails after the raised spend limit is saved, the raise is rolled back with it.
        """
        top_up = self.create_top_up()
        policy_history_count = self.policy.history.count()
        spend_limit_when_transition_failed = []

        def fail_transition(*args, **kwargs):
            spend_limit_when_transition_failed.append(self.stored_spend_limit())
            raise DatabaseError('transition failed')

        with mock.patch.object(LearnerCreditTopUp, 'transition_to', side_effect=fail_transition):
            with self.assertRaises(DatabaseError):
                fulfill_spend_limit_step(top_up)

        # The raise was written before the transition failed, then rolled back with it.
        self.assertEqual(spend_limit_when_transition_failed, [self.SPEND_LIMIT + self.AMOUNT])
        self.assert_nothing_changed(top_up)
        self.assertEqual(self.policy.history.count(), policy_history_count)

    @ddt.data(
        # Scenario 2: the eligibility check cached the record earlier in the same request, before the deposit.
        {'cached_by_same_policy_instance': True},
        # Scenario 3: an earlier task in the same worker cached the record, through its own copy of the policy.
        {'cached_by_same_policy_instance': False},
    )
    @ddt.unpack
    def test_stale_cached_subsidy_record_is_read_again(self, cached_by_same_policy_instance):
        """
        A subsidy record cached before the deposit is replaced by a fresh one before the spend limit is raised.
        With the cached record, the raised spend limit would exceed total_deposits and fail validation.
        """
        top_up = self.create_top_up()
        self.set_total_deposits(self.SPEND_LIMIT)
        if cached_by_same_policy_instance:
            policy = top_up.subsidy_access_policy
        else:
            policy = SubsidyAccessPolicy.objects.get(uuid=self.policy.uuid)
        self.assertEqual(policy.subsidy_record()['total_deposits'], self.SPEND_LIMIT)
        self.set_total_deposits(self.SPEND_LIMIT + self.AMOUNT)

        self.assertTrue(fulfill_spend_limit_step(top_up))

        self.assertEqual(self.mock_subsidy_client.retrieve_subsidy.call_count, 2)
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + self.AMOUNT)

    def test_stale_cached_subsidy_record_fails_validation_if_not_cleared(self):
        """
        Control for the scenarios above: without clearing the cache, the record cached before the deposit is
        used and the raise fails validation.
        """
        top_up = self.create_top_up()
        self.set_total_deposits(self.SPEND_LIMIT)
        top_up.subsidy_access_policy.subsidy_record()
        self.set_total_deposits(self.SPEND_LIMIT + self.AMOUNT)

        with mock.patch.object(SubsidyAccessPolicy, 'clear_cached_subsidy_record'):
            with self.assertRaises(NonRetryableFulfillmentError):
                fulfill_spend_limit_step(top_up)

    @ddt.data(
        requests.exceptions.HTTPError,
        requests.exceptions.ConnectionError,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
    )
    def test_failed_subsidy_read_is_retryable(self, error_class):
        """
        Scenario 5: a failed fresh read raises a retryable error and changes nothing, even though a record
        is still cached from earlier in the request.
        """
        top_up = self.create_top_up()
        top_up.subsidy_access_policy.subsidy_record()
        history_count = top_up.history.count()
        self.mock_subsidy_client.retrieve_subsidy.side_effect = error_class

        with self.assertRaises(RetryableFulfillmentError) as context:
            fulfill_spend_limit_step(top_up)

        self.assertIsInstance(context.exception.__cause__, requests.exceptions.RequestException)
        self.assert_nothing_changed(top_up, history_count=history_count)

    @ddt.data(
        # Scenario 6: total_deposits is unchanged when read again, so a later run would fail the same way.
        {'second_read_error': None, 'expected_error': NonRetryableFulfillmentError},
        # The second read fails, so a race can't be ruled out.
        {'second_read_error': requests.exceptions.ConnectionError, 'expected_error': RetryableFulfillmentError},
    )
    @ddt.unpack
    def test_validation_failure(self, second_read_error, expected_error):
        """
        A raised spend limit that fails the policy's validation changes nothing, and the subsidy is read again
        after the block to decide whether a later run may succeed.
        """
        top_up = self.create_top_up()
        self.set_total_deposits(self.SPEND_LIMIT + self.AMOUNT - 1)
        record = self.mock_subsidy_client.retrieve_subsidy.return_value
        self.mock_subsidy_client.retrieve_subsidy.side_effect = [record, second_read_error or record]
        top_up_history_count = top_up.history.count()
        policy_history_count = self.policy.history.count()

        with self.assertRaises(expected_error) as context:
            fulfill_spend_limit_step(top_up)

        self.assertIsInstance(context.exception.__cause__, ValidationError)
        self.assertEqual(self.mock_subsidy_client.retrieve_subsidy.call_count, 2)
        self.assert_nothing_changed(top_up, history_count=top_up_history_count)
        self.assertEqual(self.policy.history.count(), policy_history_count)

    def test_validation_failure_after_another_top_up_is_retryable(self):
        """
        TR-5: another top-up of the same subsidy is deposited and fulfilled between this run's subsidy read and its
        lock. The spend limit read under the lock includes the other raise, but the total_deposits read before
        it doesn't include the other deposit, so validation fails. total_deposits has grown when read again, so
        the error is retryable, and the next run fulfills the top-up.
        """
        other_amount = 250_000
        top_up = self.create_top_up()
        record = self.mock_subsidy_client.retrieve_subsidy.return_value
        record_after_other_deposit = {**record, 'total_deposits': record['total_deposits'] + other_amount}

        def other_top_up_fulfilled_during_read(*args, **kwargs):
            # The other top-up's step raised the spend limit after its deposit; neither is in the record returned.
            SubsidyAccessPolicy.objects.filter(uuid=self.policy.uuid).update(
                spend_limit=self.SPEND_LIMIT + other_amount,
            )
            self.mock_subsidy_client.retrieve_subsidy.side_effect = None
            self.mock_subsidy_client.retrieve_subsidy.return_value = record_after_other_deposit
            return record

        self.mock_subsidy_client.retrieve_subsidy.side_effect = other_top_up_fulfilled_during_read

        with self.assertRaises(RetryableFulfillmentError) as context:
            fulfill_spend_limit_step(top_up)

        self.assertIsInstance(context.exception.__cause__, ValidationError)
        self.assertEqual(self.mock_subsidy_client.retrieve_subsidy.call_count, 2)
        top_up.refresh_from_db()
        self.assertEqual(top_up.state, LearnerCreditTopUpState.DEPOSITED)
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + other_amount)

        self.assertTrue(fulfill_spend_limit_step(top_up))

        self.assertEqual(
            (top_up.spend_limit_before, top_up.spend_limit_after),
            (self.SPEND_LIMIT + other_amount, self.SPEND_LIMIT + other_amount + self.AMOUNT),
        )
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + other_amount + self.AMOUNT)

    def test_budget_without_spend_limit(self):
        """
        Scenario 7: a budget with no spend limit is left without one, and the top-up is fulfilled with null
        spend_limit_before and spend_limit_after.
        """
        self.policy.spend_limit = None
        self.policy.save()
        top_up = self.create_top_up()
        policy_history_count = self.policy.history.count()

        self.assertTrue(fulfill_spend_limit_step(top_up))

        top_up.refresh_from_db()
        self.assertEqual(top_up.state, LearnerCreditTopUpState.FULFILLED)
        self.assertIsNotNone(top_up.fulfilled_at)
        self.assertIsNone(top_up.spend_limit_before)
        self.assertIsNone(top_up.spend_limit_after)
        self.assertIsNone(self.stored_spend_limit())
        self.assertEqual(self.policy.history.count(), policy_history_count)

    @ddt.data(*(state for state in LearnerCreditTopUpState if state != LearnerCreditTopUpState.DEPOSITED))
    def test_skips_top_up_that_is_not_deposited(self, state):
        """
        Scenario 8: a top-up in any state but deposited is skipped without reading the subsidy or changing anything.
        """
        top_up = self.create_top_up(state=state)
        history_count = top_up.history.count()

        self.assertFalse(fulfill_spend_limit_step(top_up))

        self.mock_subsidy_client.retrieve_subsidy.assert_not_called()
        self.assert_nothing_changed(top_up, state=state, history_count=history_count)

    def test_second_run_does_not_raise_again(self):
        """
        Scenario 9: running the step twice for one top-up raises the spend limit once.
        """
        top_up = self.create_top_up()

        self.assertTrue(fulfill_spend_limit_step(top_up))
        self.assertFalse(fulfill_spend_limit_step(top_up))

        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + self.AMOUNT)
        self.assertEqual(self.mock_subsidy_client.retrieve_subsidy.call_count, 1)

    def test_stale_copy_does_not_raise_again(self):
        """
        Scenario 9: a copy of the top-up loaded while it was still deposited doesn't raise the spend limit
        again after another copy fulfilled it.
        """
        top_up = self.create_top_up()
        first_copy = LearnerCreditTopUp.objects.get(uuid=top_up.uuid)
        second_copy = LearnerCreditTopUp.objects.get(uuid=top_up.uuid)

        self.assertTrue(fulfill_spend_limit_step(first_copy))
        self.assertFalse(fulfill_spend_limit_step(second_copy))

        self.assertEqual(second_copy.state, LearnerCreditTopUpState.FULFILLED)
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + self.AMOUNT)

    def test_run_overtaken_during_its_subsidy_read_does_nothing(self):
        """
        Scenario 9: a run that finds the top-up deposited, but is overtaken by another run while it reads the
        subsidy, finds the top-up fulfilled once it holds the locks and doesn't raise the spend limit again.
        """
        top_up = self.create_top_up()
        other_copy = LearnerCreditTopUp.objects.get(uuid=top_up.uuid)
        record = self.mock_subsidy_client.retrieve_subsidy.return_value

        def overtaken_by_another_run(*args, **kwargs):
            self.mock_subsidy_client.retrieve_subsidy.side_effect = None
            self.assertTrue(fulfill_spend_limit_step(other_copy))
            return record

        self.mock_subsidy_client.retrieve_subsidy.side_effect = overtaken_by_another_run

        self.assertFalse(fulfill_spend_limit_step(top_up))

        self.assertEqual(top_up.state, LearnerCreditTopUpState.FULFILLED)
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + self.AMOUNT)

    def test_two_top_ups_on_one_budget(self):
        """
        Scenario 3 and TR-5: two top-ups on one budget each raise its spend limit, from the value the other one
        left. The second run, in the same process, reads the subsidy again rather than using the record the
        first run cached before the second deposit, which would fail validation.
        """
        first = self.create_top_up(amount_cents=100_000)
        second = self.create_top_up(amount_cents=250_000)
        self.set_total_deposits(self.SPEND_LIMIT + 100_000)

        self.assertTrue(fulfill_spend_limit_step(first))
        # The second top-up's deposit lands after the first run cached the subsidy record.
        self.set_total_deposits(self.SPEND_LIMIT + 350_000)
        self.assertTrue(fulfill_spend_limit_step(second))

        self.assertEqual(self.mock_subsidy_client.retrieve_subsidy.call_count, 2)
        self.assertEqual((first.spend_limit_before, first.spend_limit_after), (500_000, 600_000))
        self.assertEqual((second.spend_limit_before, second.spend_limit_after), (600_000, 850_000))
        self.assertEqual(self.stored_spend_limit(), 850_000)

    def test_locks_policy_before_top_up(self):
        """
        TR-5: the policy row is locked before the top-up row, so steps for one budget can't deadlock.
        SQLite ignores select_for_update(), so this checks the order the locks are asked for.
        """
        top_up = self.create_top_up()
        locked_models = []
        select_for_update = QuerySet.select_for_update

        def record_lock(queryset, *args, **kwargs):
            locked_models.append(queryset.model)
            return select_for_update(queryset, *args, **kwargs)

        with mock.patch.object(QuerySet, 'select_for_update', autospec=True, side_effect=record_lock):
            self.assertTrue(fulfill_spend_limit_step(top_up))

        self.assertGreater(len(locked_models), 1)
        self.assertTrue(issubclass(locked_models[0], SubsidyAccessPolicy))
        self.assertTrue(all(model is LearnerCreditTopUp for model in locked_models[1:]))


@ddt.ddt
class TestFulfillDepositStep(SpendLimitStepTestMixin, TestCase):
    """
    Tests for ``fulfill_deposit_step()``.
    """

    @freeze_time('2026-11-27 12:00:00')
    def test_deposits_and_moves_to_deposited(self):
        """
        The full amount is deposited once, keyed by the top-up, with the PaymentIntent as its reference, and the
        returned deposit is recorded on the move to deposited.
        """
        top_up = self.create_top_up(
            state=LearnerCreditTopUpState.PAID, discount_rate=Decimal('0.1000'), discount_cents=10_000,
        )

        self.assertTrue(fulfill_deposit_step(top_up))

        self.mock_subsidy_client.create_subsidy_deposit.assert_called_once_with(
            subsidy_uuid=self.policy.subsidy_uuid,
            desired_deposit_quantity=self.AMOUNT,
            sales_contract_reference_id=top_up.stripe_payment_intent_id,
            sales_contract_reference_provider=LEARNER_CREDIT_TOP_UP_DEPOSIT_PROVIDER,
            metadata={
                'source': 'learner_credit_top_up',
                'learner_credit_top_up_uuid': str(top_up.uuid),
                'top_up_policy_uuid': str(self.policy.uuid),
                'stripe_payment_intent_id': top_up.stripe_payment_intent_id,
                'discount_rate': '0.1000',
                'charge_amount_cents': self.AMOUNT - 10_000,
            },
            idempotency_key=f'lc-top-up-{top_up.uuid}',
        )
        deposit = self.deposits[f'lc-top-up-{top_up.uuid}']
        top_up.refresh_from_db()
        self.assertEqual(top_up.state, LearnerCreditTopUpState.DEPOSITED)
        self.assertEqual(top_up.deposit_uuid, UUID(deposit['uuid']))
        self.assertEqual(top_up.ledger_transaction_uuid, UUID(deposit['transaction']))
        self.assertEqual(top_up.deposited_at, timezone.now())
        # The spend limit is the next step's job.
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT)

    @ddt.data(
        LearnerCreditTopUpState.CREATED,
        LearnerCreditTopUpState.DEPOSITED,
        LearnerCreditTopUpState.FULFILLED,
        LearnerCreditTopUpState.ERRORED_FULFILLMENT,
    )
    def test_sends_nothing_unless_paid(self, state):
        """
        A top-up that isn't paid, including one already deposited, sends no deposit and is left as it was.
        """
        top_up = self.create_top_up(state=state)

        self.assertFalse(fulfill_deposit_step(top_up))

        self.mock_subsidy_client.create_subsidy_deposit.assert_not_called()
        top_up.refresh_from_db()
        self.assertEqual(top_up.state, state)

    @ddt.data(
        (http_error(400), NonRetryableFulfillmentError),
        (http_error(403), NonRetryableFulfillmentError),
        (http_error(404), NonRetryableFulfillmentError),
        (http_error(409), NonRetryableFulfillmentError),
        (http_error(422), NonRetryableFulfillmentError),
        (http_error(429), RetryableFulfillmentError),
        (http_error(500), RetryableFulfillmentError),
        (http_error(503), RetryableFulfillmentError),
        (requests.exceptions.ConnectionError('connection reset'), RetryableFulfillmentError),
        (requests.exceptions.Timeout('read timed out'), RetryableFulfillmentError),
    )
    @ddt.unpack
    def test_failed_deposit(self, error, expected_error):
        """
        A failed deposit leaves the top-up paid with no deposit recorded. 400, 403, 404, 409 and 422 would fail the
        same way again; anything else may pass later.
        """
        top_up = self.create_top_up(state=LearnerCreditTopUpState.PAID)
        self.mock_subsidy_client.create_subsidy_deposit.side_effect = error

        with self.assertRaises(expected_error) as context:
            fulfill_deposit_step(top_up)

        self.assertIs(context.exception.__cause__, error)
        top_up.refresh_from_db()
        self.assertEqual(top_up.state, LearnerCreditTopUpState.PAID)
        self.assertIsNone(top_up.deposit_uuid)
        self.assertIsNone(top_up.deposited_at)

    def test_another_run_fulfilled_it_meanwhile(self):
        """
        A duplicate run whose deposit call is answered (with the same deposit) after another run has deposited and
        fulfilled the top-up records nothing and doesn't raise.
        """
        top_up = self.create_top_up(state=LearnerCreditTopUpState.PAID)

        def other_run_fulfills_it(**kwargs):
            LearnerCreditTopUp.objects.filter(uuid=top_up.uuid).update(state=LearnerCreditTopUpState.FULFILLED)
            return self._create_deposit(**kwargs)
        self.mock_subsidy_client.create_subsidy_deposit.side_effect = other_run_fulfills_it

        self.assertFalse(fulfill_deposit_step(top_up))

        top_up.refresh_from_db()
        self.assertEqual(top_up.state, LearnerCreditTopUpState.FULFILLED)
        self.assertIsNone(top_up.deposit_uuid)


class TestFulfillSpendLimitStepNetworkCalls(SpendLimitStepTestMixin, TransactionTestCase):
    """
    Scenario 4: enterprise-subsidy is called only before the transaction opens.

    A TransactionTestCase, because a TestCase runs every test inside a transaction, so
    ``connection.in_atomic_block`` would always be True.
    """

    def test_subsidy_is_called_only_before_the_transaction(self):
        """
        The subsidy record is fetched outside the atomic block, and the policy's validation inside it reads
        the cached copy.
        """
        top_up = self.create_top_up()
        # A record cached before the deposit, as the eligibility check in `pay` leaves it.
        self.set_total_deposits(self.SPEND_LIMIT)
        top_up.subsidy_access_policy.subsidy_record()
        self.set_total_deposits(self.SPEND_LIMIT + self.AMOUNT)
        self.mock_subsidy_client.reset_mock()

        fetched_in_atomic_block = []
        record = self.mock_subsidy_client.retrieve_subsidy.return_value

        def retrieve_subsidy(*args, **kwargs):
            fetched_in_atomic_block.append(connection.in_atomic_block)
            return record

        reads = []
        subsidy_record = SubsidyAccessPolicy.subsidy_record

        def read_subsidy_record(policy, *args, **kwargs):
            reads.append((connection.in_atomic_block, kwargs.get('raise_on_error', False)))
            return subsidy_record(policy, *args, **kwargs)

        self.mock_subsidy_client.retrieve_subsidy.side_effect = retrieve_subsidy
        self.assertFalse(connection.in_atomic_block)
        with mock.patch.object(
            SubsidyAccessPolicy, 'subsidy_record', autospec=True, side_effect=read_subsidy_record,
        ):
            self.assertTrue(fulfill_spend_limit_step(top_up))

        # One fetch, before the transaction, and no other enterprise-subsidy call.
        self.assertEqual(fetched_in_atomic_block, [False])
        self.assertEqual([name for name, _, _ in self.mock_subsidy_client.method_calls], ['retrieve_subsidy'])
        # Scenario 2: the fresh read comes first, before the transaction, with raise_on_error=True. Then the
        # validation in save() reads the record inside the transaction, from the cache.
        self.assertEqual(reads[0], (False, True))
        self.assertIn(True, [in_atomic_block for in_atomic_block, _ in reads[1:]])
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT + self.AMOUNT)

    def test_subsidy_is_read_again_after_the_transaction_on_a_validation_failure(self):
        """
        After a validation failure, the subsidy is read again once the block has rolled back, so no lock is held
        during the call.
        """
        top_up = self.create_top_up()
        self.set_total_deposits(self.SPEND_LIMIT + self.AMOUNT - 1)
        fetched_in_atomic_block = []
        record = self.mock_subsidy_client.retrieve_subsidy.return_value

        def retrieve_subsidy(*args, **kwargs):
            fetched_in_atomic_block.append(connection.in_atomic_block)
            return record

        self.mock_subsidy_client.retrieve_subsidy.side_effect = retrieve_subsidy

        with self.assertRaises(NonRetryableFulfillmentError):
            fulfill_spend_limit_step(top_up)

        self.assertEqual(fetched_in_atomic_block, [False, False])
        self.assertEqual(self.stored_spend_limit(), self.SPEND_LIMIT)
