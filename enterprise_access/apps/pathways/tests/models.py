"""
Concrete implementations of the pathways abstract models, for unit-testing.

Registered as its own app in ``settings/test.py``, mirroring
``enterprise_access.apps.workflow.tests``, so these tables exist only under test.
"""
from attrs import define

from enterprise_access.apps.pathways.models import AbstractConditionalWorkflow
from enterprise_access.apps.workflow.models import AbstractWorkflowStep
from enterprise_access.apps.workflow.serialization import BaseInputOutput


@define
class AddInput(BaseInputOutput):
    KEY = 'add_input'

    argument_1: int = 0
    argument_2: int = 0


@define
class AddOutput(BaseInputOutput):
    KEY = 'add_output'

    result: int = None


@define
class DoubleInput(BaseInputOutput):
    KEY = 'double_input'

    unused: int = 0


@define
class DoubleOutput(BaseInputOutput):
    KEY = 'double_output'

    result: int = None


@define
class SquareInput(BaseInputOutput):
    KEY = 'square_input'

    unused: int = 0


@define
class SquareOutput(BaseInputOutput):
    KEY = 'square_output'

    result: int = None


class AddStep(AbstractWorkflowStep):
    """Adds its two arguments. Always executes."""

    input_class = AddInput
    output_class = AddOutput

    def process_input(self, accumulated_output=None, **kwargs):
        return self.output_class(
            result=self.input_object.argument_1 + self.input_object.argument_2
        )


class ConditionalDoubleStep(AbstractWorkflowStep):
    """
    Doubles the running result, but only when it is odd.

    A deliberately data-dependent condition: the point of the conditional base is that a
    step decides from the *accumulated output*, not from static configuration.
    """

    input_class = DoubleInput
    output_class = DoubleOutput

    @classmethod
    def should_execute(cls, accumulated_output, workflow):  # pylint: disable=unused-argument
        """Run only when the running sum is odd."""
        add_output = getattr(accumulated_output, AddOutput.KEY, None)
        if add_output is None or add_output.result is None:
            return False
        return add_output.result % 2 == 1

    def process_input(self, accumulated_output=None, **kwargs):
        return self.output_class(result=accumulated_output.add_output.result * 2)


class SquareStep(AbstractWorkflowStep):
    """
    Squares the running result, preferring the doubled value when the doubling ran.

    Exists to prove the step *after* a skipped one still executes and still sees the
    accumulated output of the steps that did run.
    """

    input_class = SquareInput
    output_class = SquareOutput

    def process_input(self, accumulated_output=None, **kwargs):
        double_output = getattr(accumulated_output, DoubleOutput.KEY, None)
        if double_output is not None and double_output.result is not None:
            operand = double_output.result
        else:
            operand = accumulated_output.add_output.result
        return self.output_class(result=operand ** 2)


class ConditionalTestWorkflow(AbstractConditionalWorkflow):
    """
    (x + y), doubled only if odd, then squared.

    So (1 + 2) -> 3 -> doubled to 6 -> 36, but (2 + 2) -> 4 -> not doubled -> 16.
    """

    steps = [
        AddStep,
        ConditionalDoubleStep,
        SquareStep,
    ]


class UnconditionalTestWorkflow(AbstractConditionalWorkflow):
    """
    The same shape, but with no step defining ``should_execute``.

    Used to prove the conditional base is behaviour-preserving by default.
    """

    steps = [
        AddStep,
        SquareStep,
    ]
