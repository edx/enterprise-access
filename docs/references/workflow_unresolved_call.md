# Responding to an "unresolved call" warning

What to do when you see a log line like:

```
Workflow <WorkflowClass> (uuid=<uuid>): step <StepClass> (step_uuid=<uuid>) has an
unresolved call issued at <timestamp> -- re-executing may re-issue a call that already
succeeded
```

Code: `enterprise_access/apps/workflow/models.py`, `AbstractWorkflow.process_input()`'s
step loop → `AbstractWorkflow.handle_unresolved_call()`.

## What the warning means

A step record is being re-executed (its workflow is being re-run, or `process_input()` is
being called again on an existing workflow instance) and that step's `call_issued_at` is
set but neither `succeeded_at` nor `failed_at` is. The only way to reach that state: a
previous `execute()` call got far enough to issue its external call (the body of
`process_input()`) and then the process died -- crash, OOM, a worker timeout -- before it
could record success or failure. The attempt happened; its outcome never got written down.

## Before you retry

1. Confirm there's really no recorded outcome, not just a logging gap:
   ```python
   step = StepModel.objects.get(uuid=<step_uuid>)
   step.output_data, step.exception_message, step.call_issued_at
   ```
2. If the step calls an external paid service, check that service's own logs or
   conversation history for the same timeframe before assuming it's safe to re-issue the
   call. A duplicate call to a billed service is the actual risk this warning exists to
   surface.
3. Once you've confirmed it's safe, re-running the workflow (`workflow.execute()` or
   `workflow.process_input()` from a shell) proceeds exactly as it would have without this
   warning -- today, it's observability only everywhere in this codebase; nothing blocks
   the retry.

## If a step overrides `handle_unresolved_call` to refuse instead of warn

A step class whose calls are costly or billed can override this hook (see its docstring)
to raise instead of log. If you hit a raised exception here rather than a plain warning,
the investigation above is **required**, not optional, before you can proceed -- clear the
step's marker once you've confirmed it's safe, then retry:

```python
step.call_issued_at = None
step.save(update_fields=['call_issued_at'])
```

## Today, nothing overrides this

As of this PR, every workflow in the service uses the default warn-and-proceed behavior --
including `apps/provisioning`'s `ProvisionNewCustomerWorkflow` and its steps, which make no
calls costly enough to warrant blocking a retry. The pipeline expected to actually override
this hook is the learner-pathways pipeline's Xpert-calling steps, once it merges
(`enterprise-access#281`) and `apps/pathways` exists for the override to live in. Until
then, there's nothing to act on operationally beyond the investigation above if you see
the warning.
