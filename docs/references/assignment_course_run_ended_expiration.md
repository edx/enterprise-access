# Assignment expiration: `COURSE_RUN_ENDED`

Introduced for **ENT-12316**. See also ADR `docs/decisions/0016-automatic-expiration.rst`.

## What it does
An `ALLOCATED` assignment now also expires when **every known run of its course has an end date and the latest one
has passed**. That date competes with subsidy expiry, the enrollment deadline and the 90-day timeout; the earliest
wins and is recorded as the reason (`NINETY_DAYS_PASSED`, `ENROLLMENT_DATE_PASSED`, `SUBSIDY_EXPIRED` or
`COURSE_RUN_ENDED`) on the `scheduled_job` `EXPIRED` audit action. A run with no (or an unparseable) end date
disables the reason for that course.

## The switch (off by default)
`enterprise_access.assignment_course_run_ended_expiration` (`enterprise_access/toggles.py`), a `WaffleSwitch`.

| State | Expiration | PII clearing |
|---|---|---|
| Off | Course run end dates are ignored; no catalog-agnostic lookups are made | Rows already recorded as `COURSE_RUN_ENDED` are **not** cleared |
| On | `COURSE_RUN_ENDED` competes as above | `COURSE_RUN_ENDED` is PII-clearable, like `NINETY_DAYS_PASSED` |

PII clearing is irreversible. Keep the switch off until product has signed off (see the ADR's "Pending product
sign-off"); turning it off again is the rollback.

## Where the metadata comes from
Content whose last run ended usually drops out of the policy's catalog. So:

* **Enrollment deadline:** always from the policy catalog's metadata, so content that left the catalog is not
  expired as `ENROLLMENT_DATE_PASSED` (which is never PII-cleared).
* **Course run end date:** from the policy catalog, else the catalog-agnostic lookup
  (`get_catalog_agnostic_content_metadata_for_assignment`), which tries the content key then the parent key.
  Only a 404 means "no metadata". Any other catalog error propagates, so the nightly job skips that assignment
  and retries it next run instead of expiring it without its end date. Nothing failed is cached.
* **Reminders:** the Braze reminder uses the same inputs for `action_required_by_timestamp`, and the fallback
  metadata for display fields (start date, partners, card image). If no metadata exists anywhere the reminder is
  skipped, with an errored `REMINDED` audit action, instead of raising.

## Nightly job (`automatically_expire_assignments`)
* Pages over a frozen list of assignment ids (expiring an assignment removes it from the filter, so offset paging
  over the live queryset skips records).
* `expire_assignment()` locks the assignment row and re-checks it is still `ALLOCATED` before writing, so a learner
  who accepts mid-run is not overwritten. State change, credit request update and audit action are one
  transaction; the expiration email is queued **on commit**.
* Each failure is isolated and reported; the command exits non-zero (`CommandError`) at the end.
* **Email reconciliation:** expired, non-B&R assignments with no expiration email action 1 to 7 days after expiring
  get the email re-queued (covers a broker outage at the moment of expiry). B&R is excluded because its email task
  records no action. A failure re-queuing one email does not stop the others, and is reported by the command.

## PII clearing (`clear_pii_for_expired_assignments`)
Decides from the `expiration_reason` recorded when the assignment expired (bulk-loaded), not from today's catalog
data. Legacy rows without a recorded reason are recomputed **without** course run end dates.

## Known gaps
* B&R expiration emails record no action, so B&R PII is never cleared.
* Learner-facing dates from the API serializer use only policy-catalog metadata, so they can differ from the
  nightly job for content that left the catalog.
* An assignment for a specific run does not expire as `COURSE_RUN_ENDED` if the course has a newer or undated run.
* If queuing the expiration email fails after commit, the reconciliation above covers non-B&R assignments only.
