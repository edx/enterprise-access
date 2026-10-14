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
| Off | Course run end dates are ignored; the nightly job makes no catalog-agnostic lookups | Rows already recorded as `COURSE_RUN_ENDED` are **not** cleared |
| On | `COURSE_RUN_ENDED` competes as above | `COURSE_RUN_ENDED` is PII-clearable, like `NINETY_DAYS_PASSED` |

PII clearing is irreversible. Keep the switch off until product has signed off (see the ADR's "Pending product
sign-off"); turning it off again is the rollback for `COURSE_RUN_ENDED`, and only for that.

## Not behind the switch (takes effect on deploy)
* **PII decision source:** `clear_pii_for_expired_assignments` decides from the recorded `expiration_reason`
  (below), with the switch on or off.
* **Braze emails:** `BrazeCampaignSender.course_metadata` falls back to the catalog-agnostic lookup for every email
  it builds (reminders, new assignment, nudges, cancellations, B&R approve/cancel, expiration), and reminders whose
  content has no metadata anywhere are skipped instead of raising.
* **Nightly job hardening:** frozen-id paging, per-assignment error isolation with a non-zero exit, and the atomic,
  row-locked expiration with the email queued on commit.

## Where the metadata comes from
Content whose last run ended usually drops out of the policy's catalog. So:

* **Enrollment deadline:** always from the policy catalog's metadata, so content that left the catalog is not
  expired as `ENROLLMENT_DATE_PASSED` (which is never PII-cleared).
* **Course run end date:** from the policy catalog, else the catalog-agnostic lookup
  (`get_catalog_agnostic_content_metadata_for_assignment`), which tries the content key then the parent key.
  Only a 404 means "no metadata". Any other catalog error propagates, so the nightly job skips that assignment
  and retries it next run instead of expiring it without its end date. Nothing failed is cached.
* **Braze emails:** `action_required_by_timestamp` uses the same inputs as the nightly job, and display fields
  (start date, partners, card image) use the fallback metadata. If no metadata exists anywhere a reminder is
  skipped, with an errored `REMINDED` audit action, instead of raising.

## Nightly job (`automatically_expire_assignments`)
* Pages over a frozen list of assignment ids (expiring an assignment removes it from the filter, so offset paging
  over the live queryset skips records).
* `expire_assignment()` locks the assignment row and re-checks it is still `ALLOCATED` before writing, so a learner
  who accepts mid-run is not overwritten. State change, credit request update and audit action are one
  transaction; the expiration email is queued **on commit**.
* Each failure is isolated and reported; the command exits non-zero (`CommandError`) at the end.

## PII clearing (`clear_pii_for_expired_assignments`)
Decides from the `expiration_reason` recorded when the assignment expired (bulk-loaded), not from today's catalog
data. Legacy rows without a recorded reason are recomputed **without** course run end dates. When an assignment
has expired more than once (it was reallocated in between), the reason recorded at the latest expiry wins.

## Known gaps
* B&R expiration emails record no action, so B&R PII is never cleared.
* Learner-facing dates from the API serializer use only policy-catalog metadata, so they can differ from the
  nightly job for content that left the catalog.
* An assignment for a specific run does not expire as `COURSE_RUN_ENDED` if the course has a newer or undated run.
* If queuing the expiration email fails after commit (e.g. the broker is down), the assignment stays `EXPIRED` with
  no email and so is never PII-cleared. The command reports it, but nothing re-queues the email.
* For a reallocated assignment that expires again, an expiration email sent for an *earlier* expiry still counts as
  "emailed" (as it did before this change), so its PII can be cleared before the latest expiration email is sent.
