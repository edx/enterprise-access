""" Abstract models and classes to support concrete workflows.. """

import logging
from uuid import uuid4

from attrs import define, field, make_class
from django.db import models
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from jsonfield.fields import JSONField
from model_utils.models import SoftDeletableModel, TimeStampedModel

from .exceptions import UnitOfWorkException
from .serialization import BaseInputOutput

logger = logging.getLogger(__name__)


@define
class Empty(BaseInputOutput):
    pass


class AbstractUnitOfWork(TimeStampedModel, SoftDeletableModel):
    """
    An abstract model that encapsulates the following:
    * input data
    * ``process_input()`` function to do the actual work
    * output data

    .. no_pii: This model has no PII
    """

    class Meta:
        abstract = True

    input_class = Empty
    output_class = Empty
    exception_class = UnitOfWorkException

    uuid = models.UUIDField(
        primary_key=True,
        default=uuid4,
        editable=False,
        unique=True,
    )
    input_data = JSONField(
        blank=True,
        null=False,
        default=None,
    )
    output_data = JSONField(
        blank=True,
        null=True,
        default=None,
    )
    # Stamped and saved by execute() *before* process_input() runs, so an attempt that
    # crashed mid-call is distinguishable from one that never started. Set with neither
    # succeeded_at nor failed_at means an external (possibly billed) call may already
    # have been issued with its outcome unrecorded. See execute().
    call_issued_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    succeeded_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    failed_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    exception_message = models.TextField(
        null=True,
        blank=True,
    )

    @property
    def input_object(self):
        return self.input_class.from_dict(self.input_data)

    @property
    def output_object(self):
        if self.output_data:
            return self.output_class.from_dict(self.output_data)
        return None

    def process_input(self, accumulated_output=None, **kwargs):  # pylint: disable=unused-argument
        """
        Should be implemented to do some operation on ``self.input_object``
        and return a resulting instance of ``self.output_object``.

        Params:
          accumulated_output (obj): An optional accumulator object to which
            the resulting output can be added.

        Returns:
          An instance of ``self.output_class``.
        """
        return self.output_object

    def execute(self, accumulated_output=None, **kwargs):
        """
        Executes this unit of work via ``self.process_input()``,
        then stores the output (as a dictionary for json serialization)
        and time of successful execution.
        On any exception, the exception time and message are stored,
        and a ``self.exception_class`` is raised from the responsible exception.

        Before ``process_input()`` is called, ``call_issued_at`` is stamped and durably
        saved on its own. ``process_input()`` is where an external -- and possibly billed
        -- call is issued, and neither ``succeeded_at`` nor ``failed_at`` is persisted
        until it returns, so a crash in that window would otherwise leave a record
        indistinguishable from one that was never attempted. Writing the marker first
        means the attempt survives the crash even though its outcome does not.

        Params:
          accumulated_output (obj): An optional accumulator object, which will be
            passed along to ``process_input()``, which should be implemented
            in a way that adds the successful output to the accumulator.

        Returns:
          An instance of ``self.output_class``.
        """
        logger.info(
            'Executing %s (uuid=%s) with input_data=%s',
            self.__class__.__name__, self.uuid, self.input_data,
        )
        self.call_issued_at = timezone.now()
        # update_fields so this flushes only the marker, leaving any other in-progress
        # mutation on ``self`` to be written by the save() in the finally block below.
        self.save(update_fields=['call_issued_at'])
        try:
            result = self.process_input(
                accumulated_output=accumulated_output,
                **kwargs,
            )
            self.output_data = result.to_dict()
            self.succeeded_at = timezone.now()
            logger.info(
                'Successfully executed %s (uuid=%s), output_data=%s',
                self.__class__.__name__, self.uuid, self.output_data,
            )
        except Exception as exc:
            self.failed_at = timezone.now()
            self.exception_message = str(exc)
            logger.exception(
                'Failed to execute %s (uuid=%s): %s',
                self.__class__.__name__, self.uuid, exc,
            )
            raise self.exception_class(str(exc)) from exc
        finally:
            self.save()
        return result

    def __str__(self):
        return str(self.uuid) + str(self.input_class) + str(self.output_class)


class AbstractWorkflowStep(AbstractUnitOfWork):
    """
    An abstract step of a workflow. The workflow_record_identifier and
    preceding_step_identifier help to maintain linkages between steps and within workflows.
    However, since we want workflows to be composable and modular, we're required
    to allow any workflow step type to be included in the list for one *or more*
    workflow types. So these can't be strict foreign keys.
    """
    class Meta:
        abstract = True

    workflow_record_uuid = models.UUIDField(
        null=False,
        help_text='UUID of the workflow record',
    )
    preceding_step_uuid = models.UUIDField(
        null=True,
        help_text='UUID of the preceding workflow step record, if any',
    )


class AbstractWorkflow(AbstractUnitOfWork):
    """
    An abstract workflow model.

    A step class may opt out of running on a given execution by defining::

        @classmethod
        def should_execute(cls, accumulated_output, workflow):
            return ...

    Returning ``False`` skips the step: **no step record is created**, so a skipped step
    is distinguishable from one that ran and produced nothing. Subsequent steps still
    execute and still receive the accumulated output of the steps that did run, with the
    skipped step's output key left unset.

    A step that does not define ``should_execute`` always executes, so a workflow whose
    steps all omit it behaves exactly as it did before the hook existed.

    A subclass whose steps issue costly or billed calls can override
    ``handle_unresolved_call()`` to turn a re-run's unresolved-call warning into something
    stronger -- the default is to log and proceed unchanged.
    """
    class Meta:
        abstract = True

    steps = []

    @staticmethod
    def step_should_execute(workflow_step_class, accumulated_output, workflow):
        """
        Whether ``workflow_step_class`` should run, defaulting to ``True``.

        Kept as a separate method so the default-on behaviour is testable directly and so
        a subclass can change the convention without reimplementing the loop.
        """
        should_execute = getattr(workflow_step_class, 'should_execute', None)
        if should_execute is None:
            return True
        return bool(should_execute(accumulated_output, workflow))

    def handle_unresolved_call(self, step_record, workflow_step_class):
        """
        Called when a *reused* step record shows an issued-but-unresolved call --
        ``call_issued_at`` is set but neither ``succeeded_at`` nor ``failed_at`` is, meaning
        a previous attempt may have already issued (and been billed for) an external call
        whose outcome was never recorded.

        Defaults to logging a warning and letting execution proceed exactly as it did
        before this hook existed. A subclass whose steps make costly or billed calls can
        override this to refuse re-execution instead (e.g. raising a dedicated exception
        that requires a human to confirm it's safe to retry before clearing the marker).

        Kept as a separate method, mirroring ``step_should_execute``, so the default
        behaviour is testable directly and a subclass can change the convention without
        reimplementing the loop.
        """
        logger.warning(
            'Workflow %s (uuid=%s): step %s (step_uuid=%s) has an unresolved call issued at %s '
            '-- re-executing may re-issue a call that already succeeded',
            self.__class__.__name__, self.uuid, workflow_step_class.__name__,
            step_record.uuid, step_record.call_issued_at,
        )

    @cached_property
    def input_class(self):
        """
        For constructing workflow input/output classes, we use the attrs.make_class() helper
        to dynamically create a class with fields corresponding to the ``KEY`` fields of the *step*
        input/output classes. This helps maintain some semblance of a rigid interface
        at the boundaries of a given workflow and the steps of which the workflow is composed.
        """
        class_name = self.__class__.__name__ + 'Input'
        attributes = {
            step_class.input_class.KEY: field(type=step_class.input_class, default=None)
            for step_class in self.steps
        }
        return make_class(class_name, attributes, bases=(BaseInputOutput,))

    @cached_property
    def output_class(self):
        """
        For constructing workflow input/output classes, we use the attrs.make_class() helper
        to dynamically create a class with fields corresponding to the ``KEY`` fields of the *step*
        input/output classes. This helps maintain some semblance of a rigid interface
        at the boundaries of a given workflow and the steps of which the workflow is composed.
        """
        class_name = self.__class__.__name__ + 'Output'
        attributes = {
            step_class.output_class.KEY: field(type=step_class.output_class, default=None)
            for step_class in self.steps
        }
        return make_class(class_name, attributes, bases=(BaseInputOutput,))

    def get_input_object_for_step_type(self, step_type):
        return getattr(self.input_object, step_type.input_class.KEY, None)

    def process_input(self, accumulated_output=None, **kwargs):
        """
        Processes the input for an entire workflow, which consists of:
        1. Skipping any step whose ``should_execute`` declines to run (see the class docstring).
        2. Get/creating a step record for each remaining step of the workflow.
        3. Calling ``execute()`` on each of these steps (unless they've already succeeded).
        4. On success, accumulating the step output and
        passing it along to the next step's ``process_input()`` call.

        Returns:
          An instance of ``self.output_class``, which should just be an accumulation
          of the output of each step in this workflow.
        """
        if self.succeeded_at:
            logger.info(
                '%s (uuid=%s) already succeeded at %s, skipping re-execution',
                self.__class__.__name__, self.uuid, self.succeeded_at,
            )
            return None

        accumulated_output = accumulated_output or self.output_class()

        logger.info(
            'Starting workflow %s (uuid=%s) with steps=%s',
            self.__class__.__name__, self.uuid,
            [step_class.__name__ for step_class in self.steps],
        )

        preceding_step_record = None
        for workflow_step_class in self.steps:
            if not self.step_should_execute(workflow_step_class, accumulated_output, self):
                logger.info(
                    'Workflow %s (uuid=%s): step %s opted out, no step record created',
                    self.__class__.__name__, self.uuid, workflow_step_class.__name__,
                )
                continue

            input_object = self.get_input_object_for_step_type(workflow_step_class)
            input_data = input_object.to_dict() if input_object else {}
            step_record_kwargs = {
                'workflow_record_uuid': self.uuid,
                'defaults': {
                    'input_data': input_data,
                }
            }
            if preceding_step_record:
                step_record_kwargs['defaults']['preceding_step_uuid'] = preceding_step_record.uuid

            step_record, created = workflow_step_class.objects.get_or_create(**step_record_kwargs)
            logger.info(
                'Workflow %s (uuid=%s): step %s record %s (created=%s, step_uuid=%s)',
                self.__class__.__name__, self.uuid, workflow_step_class.__name__,
                'created' if created else 'reused', created, step_record.uuid,
            )
            preceding_step_record = step_record

            unresolved_call = (
                not created and
                step_record.call_issued_at and
                not step_record.succeeded_at and
                not step_record.failed_at
            )
            if unresolved_call:
                self.handle_unresolved_call(step_record, workflow_step_class)

            if step_record.succeeded_at:
                logger.info(
                    'Workflow %s (uuid=%s): step %s (step_uuid=%s) already succeeded at %s, skipping',
                    self.__class__.__name__, self.uuid, workflow_step_class.__name__,
                    step_record.uuid, step_record.succeeded_at,
                )
                setattr(
                    accumulated_output,
                    workflow_step_class.output_class.KEY,
                    step_record.output_object,
                )
                continue

            step_output = step_record.execute(accumulated_output=accumulated_output)
            setattr(
                accumulated_output,
                workflow_step_class.output_class.KEY,
                step_output,
            )

        logger.info(
            'Completed workflow %s (uuid=%s)',
            self.__class__.__name__, self.uuid,
        )
        return accumulated_output
