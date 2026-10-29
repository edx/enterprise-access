"""
Unit tests for the test implementations
of the abstract workflow models.
"""
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from ..exceptions import UnitOfWorkException
from ..models import AbstractWorkflow
from .models import (
    TestSquaredWorkflowStep,
    TestStepInput,
    TestStepOutput,
    TestTwoStepWorkflow,
    TestWorkflow,
    TestWorkflowStep
)


def skip_step(step_class):
    """
    Patch ``step_class`` to opt out of executing.

    ``create=True`` because ``should_execute`` is an optional hook: the whole point is
    that a step class normally does not define it.
    """
    return mock.patch.object(
        step_class,
        'should_execute',
        create=True,
        new=lambda accumulated_output, workflow: False,
    )


class TestWorkflowModels(TestCase):
    """
    Unit tests for the test implementations
    of the abstract workflow models.
    """
    INPUT_DATA = {
        TestStepInput.KEY: {
            'argument_1': 2,
            'argument_2': 3,
        },
    }

    def test_simple_workflow(self):
        """
        Tests that we can execute a simple workflow consisting of one step that adds two numbers.
        """
        workflow = TestWorkflow.objects.create(
            input_data=self.INPUT_DATA,
        )
        output_record = workflow.execute()
        self.assertEqual(output_record.test_step_output.result, 5)

    def test_workflow_error(self):
        """
        Tests that exception handling and propagation within a workflow works
        as expected.
        """
        test_exception = Exception('this step failed')
        with mock.patch.object(TestWorkflowStep, 'process_input', side_effect=test_exception):
            workflow = TestWorkflow.objects.create(
                input_data=self.INPUT_DATA,
            )
            with self.assertRaises(UnitOfWorkException):
                output_record = workflow.execute()
                self.assertIsNone(output_record.test_step_output.result)

            step = TestWorkflowStep.objects.filter(workflow_record_uuid=workflow.uuid).first()
            self.assertIsNotNone(step.failed_at)
            self.assertEqual(step.exception_message, str(test_exception))

            self.assertIsNotNone(workflow.failed_at)
            self.assertEqual(workflow.exception_message, str(test_exception))

    def test_two_step_workflow(self):
        """
        Tests that we can execute a two-step workflow that adds two numbers and then squares them.
        """
        workflow = TestTwoStepWorkflow.objects.create(
            input_data=self.INPUT_DATA,
        )
        output_record = workflow.execute()
        self.assertEqual(output_record.test_square_output.result, 25)


class TestStepOptOut(TestCase):
    """
    Tests for ``AbstractWorkflow``'s optional ``should_execute`` hook, which lets a step
    class decline to run on a given execution.

    The default-on half of this is covered implicitly by every other test in this module:
    no step in ``.models`` defines ``should_execute``, and they all still execute.
    """
    INPUT_DATA = {
        TestStepInput.KEY: {
            'argument_1': 2,
            'argument_2': 3,
        },
    }

    def test_step_should_execute_defaults_to_true(self):
        """A step class with no ``should_execute`` always runs."""
        self.assertTrue(
            AbstractWorkflow.step_should_execute(TestWorkflowStep, None, None)
        )

    def test_step_should_execute_consults_the_step(self):
        """When the hook is defined, its answer is the answer."""
        with skip_step(TestWorkflowStep):
            self.assertFalse(
                AbstractWorkflow.step_should_execute(TestWorkflowStep, None, None)
            )

    def test_opted_out_step_creates_no_record(self):
        """
        A skipped step leaves no step record, so it is distinguishable from a step that
        ran and produced nothing.
        """
        workflow = TestTwoStepWorkflow.objects.create(input_data=self.INPUT_DATA)

        with skip_step(TestSquaredWorkflowStep):
            workflow.process_input()

        self.assertFalse(
            TestSquaredWorkflowStep.objects.filter(workflow_record_uuid=workflow.uuid).exists()
        )

    def test_steps_that_do_not_opt_out_still_run(self):
        """
        Skipping one step neither prevents the others from running nor disturbs the
        output they accumulate; the skipped step's key is simply left unset.
        """
        workflow = TestTwoStepWorkflow.objects.create(input_data=self.INPUT_DATA)

        with skip_step(TestSquaredWorkflowStep):
            accumulated_output = workflow.process_input()

        self.assertTrue(
            TestWorkflowStep.objects.filter(workflow_record_uuid=workflow.uuid).exists()
        )
        self.assertEqual(accumulated_output.test_step_output.result, 5)
        self.assertIsNone(accumulated_output.test_square_output)

    def test_serializing_a_skipped_step_needs_optional_typed_output(self):
        """
        The boundary between this hook and ``AbstractConditionalWorkflow``.

        ``AbstractWorkflow`` builds its dynamic output class with the step's output type
        declared non-optional (default ``None``, but not ``Optional``), so cattrs emits
        unstructure code that dereferences every field. Skipping a step leaves its field
        ``None``, so ``execute()`` raises while serialising its own output -- *after* the
        loop has correctly skipped the step.

        So the hook alone is not sufficient for a workflow that actually skips steps: it
        also needs ``Optional``-typed output fields, which is what
        ``pathways.models.AbstractConditionalWorkflow`` exists to supply. This test pins
        that division of labour, so removing the subclass would fail loudly here.
        """
        workflow = TestTwoStepWorkflow.objects.create(input_data=self.INPUT_DATA)

        with skip_step(TestSquaredWorkflowStep):
            with self.assertRaises(UnitOfWorkException):
                workflow.execute()

        # The step was skipped as asked; only the serialization of the result failed.
        self.assertFalse(
            TestSquaredWorkflowStep.objects.filter(workflow_record_uuid=workflow.uuid).exists()
        )


class TestCallIssuedAt(TestCase):
    """
    Tests for the ``call_issued_at`` idempotency marker on ``AbstractUnitOfWork``.

    ``process_input()`` is where a unit of work issues its external -- and possibly
    billed -- call, and neither ``succeeded_at`` nor ``failed_at`` is written until it
    returns. ``call_issued_at`` is stamped and committed *before* that call, so an attempt
    interrupted mid-flight stays distinguishable from one that never started.
    """
    INPUT_DATA = {
        TestStepInput.KEY: {
            'argument_1': 2,
            'argument_2': 3,
        },
    }

    def test_call_issued_at_is_committed_before_process_input_runs(self):
        """
        The property the marker exists for: by the time ``process_input()`` is running,
        the marker is already durable in the database -- not merely set on the in-memory
        instance, which a crash would take with it.
        """
        observed = {}

        # autospec=True means this stands in for the real process_input, so it has to
        # accept that signature even though it reads none of it.
        def record_marker_state_at_call_time(self, accumulated_output=None, **kwargs):
            # pylint: disable=unused-argument
            # Read the row back from the database rather than trusting ``self``.
            persisted = TestWorkflowStep.objects.get(uuid=self.uuid)
            observed['call_issued_at'] = persisted.call_issued_at
            observed['succeeded_at'] = persisted.succeeded_at
            observed['failed_at'] = persisted.failed_at
            return TestStepOutput(result=99)

        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)
        with mock.patch.object(
            TestWorkflowStep, 'process_input', autospec=True,
            side_effect=record_marker_state_at_call_time,
        ):
            workflow.execute()

        self.assertIsNotNone(observed['call_issued_at'])
        # And at that moment the outcome genuinely was still unrecorded -- which is the
        # window a crash would freeze the record in.
        self.assertIsNone(observed['succeeded_at'])
        self.assertIsNone(observed['failed_at'])

    def test_call_issued_at_is_set_on_a_successful_step(self):
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)

        workflow.execute()

        step = TestWorkflowStep.objects.get(workflow_record_uuid=workflow.uuid)
        self.assertIsNotNone(step.call_issued_at)
        self.assertLessEqual(step.call_issued_at, step.succeeded_at)

    def test_call_issued_at_survives_a_failing_step(self):
        """
        A step that raises still leaves the marker behind, since it was committed before
        the call that failed.
        """
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)
        with mock.patch.object(
            TestWorkflowStep, 'process_input', side_effect=Exception('boom'),
        ):
            with self.assertRaises(UnitOfWorkException):
                workflow.execute()

        step = TestWorkflowStep.objects.get(workflow_record_uuid=workflow.uuid)
        self.assertIsNotNone(step.call_issued_at)
        self.assertIsNotNone(step.failed_at)

    def test_the_marker_save_does_not_flush_other_pending_changes(self):
        """
        The marker is saved with ``update_fields``, so an unrelated mutation already
        pending on the instance is not written early as a side effect of stamping it.
        """
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)
        step = TestWorkflowStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            input_data=self.INPUT_DATA[TestStepInput.KEY],
        )
        step.exception_message = 'pending, must not be flushed by the marker save'
        observed = {}

        def inspect_persisted_row(self, accumulated_output=None, **kwargs):  # pylint: disable=unused-argument
            persisted = TestWorkflowStep.objects.get(uuid=self.uuid)
            observed['exception_message'] = persisted.exception_message
            observed['call_issued_at'] = persisted.call_issued_at
            return TestStepOutput(result=1)

        with mock.patch.object(
            TestWorkflowStep, 'process_input', autospec=True,
            side_effect=inspect_persisted_row,
        ):
            step.execute()

        self.assertIsNotNone(observed['call_issued_at'])
        self.assertIsNone(observed['exception_message'])


class TestUnresolvedCallWarning(TestCase):
    """
    Tests the warning ``AbstractWorkflow.process_input()`` emits when it reuses a step
    record whose call was issued but whose outcome was never recorded.

    This is observability only -- the step still re-executes exactly as it did before the
    warning existed -- so these tests assert on logs, not on control flow.
    """
    INPUT_DATA = {
        TestStepInput.KEY: {
            'argument_1': 2,
            'argument_2': 3,
        },
    }
    LOGGER = 'enterprise_access.apps.workflow.models'
    WARNING_FRAGMENT = 'has an unresolved call issued at'

    def _run_and_collect_warnings(self, workflow):
        """Run the workflow's step loop, returning only the warning-level log lines."""
        with self.assertLogs(self.LOGGER, level='INFO') as logs:
            workflow.process_input()
        return [line for line in logs.output if line.startswith('WARNING')]

    def _unresolved_step_record(self, workflow):
        """A step record left in the issued-but-unresolved state a crash would produce."""
        return TestWorkflowStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            input_data=self.INPUT_DATA[TestStepInput.KEY],
            call_issued_at=timezone.now(),
        )

    def test_warns_for_a_reused_record_with_an_unresolved_call(self):
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)
        step = self._unresolved_step_record(workflow)

        warnings = self._run_and_collect_warnings(workflow)

        self.assertTrue(any(self.WARNING_FRAGMENT in line for line in warnings))
        self.assertTrue(any(str(step.uuid) in line for line in warnings))

    def test_the_warned_step_still_re_executes(self):
        """The warning surfaces the risk; it deliberately does not change behaviour."""
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)
        step = self._unresolved_step_record(workflow)

        workflow.process_input()

        step.refresh_from_db()
        self.assertIsNotNone(step.succeeded_at)
        self.assertEqual(step.output_object.result, 5)

    def test_does_not_warn_for_a_freshly_created_record(self):
        """The ordinary first run of a workflow must stay quiet."""
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)

        warnings = self._run_and_collect_warnings(workflow)

        self.assertEqual(warnings, [])

    def test_a_subclass_can_override_handle_unresolved_call_to_refuse_reexecution(self):
        """
        The hook is a real extension point, not just a differently-named log call -- a
        subclass whose steps issue costly or billed calls can replace the default
        log-and-proceed behaviour with something that actually stops the re-run.
        """
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)
        self._unresolved_step_record(workflow)

        class RefuseUnresolvedCalls(Exception):
            pass

        def _refuse(self, step_record, workflow_step_class):
            raise RefuseUnresolvedCalls(f'{workflow_step_class.__name__} has an unresolved call')

        with mock.patch.object(TestWorkflow, 'handle_unresolved_call', new=_refuse):
            with self.assertRaises(RefuseUnresolvedCalls):
                workflow.process_input()

    def test_does_not_warn_for_a_record_that_already_succeeded(self):
        """A resolved outcome means the call is accounted for, issued marker or not."""
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)
        workflow.execute()
        self.assertIsNotNone(
            TestWorkflowStep.objects.get(workflow_record_uuid=workflow.uuid).call_issued_at
        )
        # Clear the workflow's own marker so its step loop runs a second time.
        workflow.succeeded_at = None
        workflow.save()

        warnings = self._run_and_collect_warnings(workflow)

        self.assertEqual(warnings, [])

    def test_does_not_warn_for_a_record_that_already_failed(self):
        """A recorded failure is also a resolved outcome."""
        workflow = TestWorkflow.objects.create(input_data=self.INPUT_DATA)
        with mock.patch.object(
            TestWorkflowStep, 'process_input', side_effect=Exception('boom'),
        ):
            with self.assertRaises(UnitOfWorkException):
                workflow.execute()
        workflow.succeeded_at = None
        workflow.failed_at = None
        workflow.save()

        warnings = self._run_and_collect_warnings(workflow)

        self.assertEqual(warnings, [])
