"""
Tests for the conditional workflow base.
"""
import ddt
from django.test import TestCase

from enterprise_access.apps.pathways.models import AbstractConditionalWorkflow
from enterprise_access.apps.pathways.tests.models import (
    AddStep,
    ConditionalDoubleStep,
    ConditionalTestWorkflow,
    SquareStep,
    UnconditionalTestWorkflow
)
from enterprise_access.apps.provisioning.models import ProvisionNewCustomerWorkflow
from enterprise_access.apps.workflow.models import AbstractWorkflow


def build_conditional_workflow(argument_1, argument_2):
    """A ``ConditionalTestWorkflow`` whose input feeds ``AddStep``."""
    workflow = ConditionalTestWorkflow()
    workflow.input_data = {
        'add_input': {'argument_1': argument_1, 'argument_2': argument_2},
    }
    workflow.save()
    return workflow


@ddt.ddt
class TestAbstractConditionalWorkflow(TestCase):
    """
    Tests for ``AbstractConditionalWorkflow``.
    """

    def test_a_step_opts_out(self):
        """Scenario: A step opts out."""
        # 2 + 2 = 4, which is even, so the doubling step declines to run.
        workflow = build_conditional_workflow(2, 2)

        workflow.execute()

        # No step record at all for the skipped step -- distinguishable from a step that
        # ran and produced nothing.
        self.assertFalse(
            ConditionalDoubleStep.objects.filter(workflow_record_uuid=workflow.uuid).exists()
        )
        # The step after it still ran, and received the accumulated output of the step
        # that did run: 4 ** 2, not (4 * 2) ** 2.
        square_record = SquareStep.objects.get(workflow_record_uuid=workflow.uuid)
        self.assertEqual(square_record.output_object.result, 16)
        self.assertIsNotNone(square_record.succeeded_at)

    def test_a_step_opts_in(self):
        """The same workflow, same code path, condition satisfied."""
        # 1 + 2 = 3, which is odd, so the doubling step runs: (3 * 2) ** 2 == 36.
        workflow = build_conditional_workflow(1, 2)

        workflow.execute()

        double_record = ConditionalDoubleStep.objects.get(workflow_record_uuid=workflow.uuid)
        self.assertEqual(double_record.output_object.result, 6)
        square_record = SquareStep.objects.get(workflow_record_uuid=workflow.uuid)
        self.assertEqual(square_record.output_object.result, 36)

    @ddt.data(
        (2, 2, 16),   # even -> skipped
        (1, 2, 36),   # odd -> executed
        (3, 4, 196),  # odd -> executed, (7*2)**2
        (4, 4, 64),   # even -> skipped, 8**2
    )
    @ddt.unpack
    def test_conditional_result_is_data_dependent(self, argument_1, argument_2, expected):
        workflow = build_conditional_workflow(argument_1, argument_2)

        workflow.execute()

        self.assertEqual(workflow.output_object.square_output.result, expected)

    def test_skipped_step_leaves_its_output_key_unset(self):
        """A skipped step must not fabricate output for downstream steps to read."""
        workflow = build_conditional_workflow(2, 2)

        workflow.execute()

        self.assertIsNone(workflow.output_object.double_output)
        self.assertIsNotNone(workflow.output_object.add_output)

    def test_skipped_step_output_round_trips_as_null(self):
        """
        Regression: the parent's dynamic output class declares each field with the step's
        output type (default ``None``, but not ``Optional``), so cattrs emits
        unstructure code that dereferences every field. That is safe only because
        ``AbstractWorkflow`` steps always run. With a skipped step the field stays
        ``None`` and ``to_dict()`` raised ``AttributeError`` mid-run -- which is why
        ``AbstractConditionalWorkflow`` re-declares these fields as ``Optional``.
        """
        workflow = build_conditional_workflow(2, 2)

        workflow.execute()

        # The persisted output is JSON with an explicit null for the skipped step.
        self.assertIsNone(workflow.output_data['double_output'])
        self.assertEqual(workflow.output_data['add_output']['result'], 4)
        # And it structures back into an object without raising.
        self.assertIsNone(workflow.output_object.double_output)

    def test_preceding_step_uuid_skips_over_the_skipped_step(self):
        """
        The step chain must link to the step that actually ran before it, so a trace of
        a run with a skipped step is still a connected chain.
        """
        workflow = build_conditional_workflow(2, 2)

        workflow.execute()

        add_record = AddStep.objects.get(workflow_record_uuid=workflow.uuid)
        square_record = SquareStep.objects.get(workflow_record_uuid=workflow.uuid)
        self.assertEqual(square_record.preceding_step_uuid, add_record.uuid)

    def test_default_behaviour_is_unchanged(self):
        """Scenario: Default behaviour is unchanged."""
        workflow = UnconditionalTestWorkflow()
        workflow.input_data = {'add_input': {'argument_1': 3, 'argument_2': 4}}
        workflow.save()

        workflow.execute()

        # Every step ran, in order, exactly as AbstractWorkflow would have run them.
        self.assertTrue(AddStep.objects.filter(workflow_record_uuid=workflow.uuid).exists())
        self.assertEqual(workflow.output_object.square_output.result, 49)

    def test_step_should_execute_defaults_to_true(self):
        """A step class with no ``should_execute`` always runs."""
        self.assertTrue(
            AbstractConditionalWorkflow.step_should_execute(AddStep, None, None)
        )

    def test_step_should_execute_consults_the_step(self):
        self.assertFalse(
            AbstractConditionalWorkflow.step_should_execute(ConditionalDoubleStep, None, None)
        )

    def test_already_succeeded_workflow_is_not_re_executed(self):
        """Idempotency is inherited, not lost, by the override."""
        workflow = build_conditional_workflow(1, 2)
        workflow.execute()
        original_output = workflow.output_data

        self.assertIsNone(workflow.process_input())
        self.assertEqual(workflow.output_data, original_output)

    def test_succeeded_steps_are_skipped_on_re_execution(self):
        """
        Re-running a workflow reuses succeeded step records rather than duplicating them,
        which is what makes the runbook's "just re-run it" advice safe.
        """
        workflow = build_conditional_workflow(1, 2)
        workflow.execute()
        first_add_uuid = AddStep.objects.get(workflow_record_uuid=workflow.uuid).uuid

        # Clear the workflow's own success marker so process_input() runs the loop again.
        workflow.succeeded_at = None
        workflow.save()
        workflow.execute()

        self.assertEqual(
            AddStep.objects.filter(workflow_record_uuid=workflow.uuid).count(), 1
        )
        self.assertEqual(
            AddStep.objects.get(workflow_record_uuid=workflow.uuid).uuid, first_add_uuid
        )


class TestProvisioningIsUnaffected(TestCase):
    """
    Scenario: Provisioning is untouched.

    ``AbstractConditionalWorkflow`` subclasses ``AbstractWorkflow`` rather than modifying
    it, so provisioning cannot be affected. This asserts the structural guarantee; the
    behavioural guarantee is the provisioning suite itself, which runs unmodified.
    """

    def test_the_conditional_base_does_not_modify_abstract_workflow(self):
        # AbstractWorkflow retains its own process_input, distinct from the override.
        self.assertNotEqual(
            AbstractWorkflow.process_input,
            AbstractConditionalWorkflow.process_input,
        )
        # And it has gained no conditional-execution surface.
        self.assertFalse(hasattr(AbstractWorkflow, 'step_should_execute'))

    def test_provisioning_workflow_still_uses_the_unconditional_base(self):
        self.assertTrue(issubclass(ProvisionNewCustomerWorkflow, AbstractWorkflow))
        self.assertFalse(issubclass(ProvisionNewCustomerWorkflow, AbstractConditionalWorkflow))
