0016 Automatic Cancellation (Expiration)
****************************************

Status
======
Accepted - January 2024 (amended October 2026, see below)

Context
=======

How expiration currently works
------------------------------
There's currently a management command (run via a cron) named ``automatically_exire_assignments.py``
that toggles the state of ``LearnerContentAssignment`` records ("Assignments")
to ``CANCELLED`` under any of the following conditions, as long as the current state
of the assignment is ``ALLOCATED``:

1. The current date is more than 90 days after the most recent notification action - this action
   time is used as a proxy for understanding *when* the assignment *was last allocated*. This helps
   deal with the edge case where an assignment is allocated, cancelled, and re-allocated later. Note
   that a reminder action on an assignment record *does not* reset this specific expiry time.
2. The current date is greater than the inferred enrollment deadline for the assigned course.
3. The current date is greater than the expiration date of the subsidy associated with the Assignment's
   access policy record.

Decision
========
The above management command will now remove Personally-Identifiable Information ("PII") from assignments
that are automatically moved from ``ALLOCATED`` to ``CANCELLED`` under condition (1) above - that is, only
for such assignments whose last notification time was more than 90 days ago.
This PII includes the learner email address.

Scrubbing the learner email
---------------------------
Note that, to remove learner email PII, we change the value to a "tombstone" - ``retired_user@retired.invalid``.
This is done so that the ``learner_email`` database column can continue to have a non-null constraint.

Amendment - October 2026: ``COURSE_RUN_ENDED`` expiration reason
================================================================
A fourth condition now moves ``ALLOCATED`` assignments to ``EXPIRED``:

4. All known course runs for the assigned course have ended. Conditions (1) through (4) are all computed,
   and the earliest resulting date determines when, and why, the assignment expires. Condition (4) can
   therefore expire an assignment before the 90-day timeout of condition (1) is reached.

The reasons recorded on the assignment's expiration audit action are, respectively: ``NINETY_DAYS_PASSED``,
``ENROLLMENT_DATE_PASSED``, ``SUBSIDY_EXPIRED``, and ``COURSE_RUN_ENDED``.

PII is now also cleared for assignments that expired as ``COURSE_RUN_ENDED``. Previously only
``NINETY_DAYS_PASSED`` qualified. This moves PII clearing earlier for those assignments: shortly after the
course runs end rather than 90 days after allocation.

Details that affect which assignments have PII cleared:

* The PII-clearing decision uses the ``expiration_reason`` recorded when the assignment expired, rather
  than recomputing it from today's catalog data. Assignments that expired as ``ENROLLMENT_DATE_PASSED``
  or ``SUBSIDY_EXPIRED`` therefore never have PII cleared, even if their content later leaves the catalog.
  (Legacy rows with no recorded reason still fall back to recomputing it, but without the course run end
  date, i.e. exactly as they were evaluated before ``COURSE_RUN_ENDED`` existed. A legacy row that really
  expired as ``SUBSIDY_EXPIRED`` can therefore never be recomputed as ``COURSE_RUN_ENDED`` and have its PII
  cleared.)
* Content that is no longer in the policy's catalog is looked up in a catalog-agnostic way, but only to
  determine course run end dates. The enrollment deadline is still derived from the policy catalog's
  metadata, so such assignments expire as ``COURSE_RUN_ENDED`` (and have PII cleared) rather than as
  ``ENROLLMENT_DATE_PASSED``.
* No backfill is required: the daily ``automatically_expire_assignments`` job evaluates existing
  ``ALLOCATED`` assignments with the new rule. Assignments that already expired before this change are not
  re-evaluated; their PII eligibility follows the legacy rules above.
* Expiring an assignment is atomic: the state change, the credit request update and the ``scheduled_job`` audit
  action succeed or fail together. If any of them fails, everything is rolled back and the assignment stays
  ``ALLOCATED``, so the next nightly run retries it. The expiration email is queued after the commit, so no email
  is sent for a rolled-back expiration. If queuing it then fails (for example the Celery broker is down), the
  assignment stays ``EXPIRED`` without an email, and so is never eligible for PII clearing; the error is reported
  by the command, but nothing re-queues the email automatically. The command keeps processing other assignments,
  then exits non-zero (``CommandError``) listing the ones that failed.
* The catalog-agnostic lookup only happens while the switch is on, so a catalog outage cannot affect expiration
  with the feature off. With it on, a failure other than a 404 skips that assignment (reported as failed) rather
  than expiring it without its course run end date, so the next nightly run retries it.
* ``expire_assignment()`` locks the assignment row and re-checks that it is still ``ALLOCATED`` before writing, so
  a learner who accepts while the nightly job is running is not overwritten as ``EXPIRED``.
* Braze emails built by ``BrazeCampaignSender`` (reminders, and every other assignment and Browse & Request email)
  no longer fail for content that has left the policy's catalog: ``BrazeCampaignSender.course_metadata`` falls back
  to the catalog-agnostic lookup for display fields and for the course run end date used in the action-required-by
  date (as the nightly job does); the enrollment deadline always comes from the policy catalog. If no metadata
  exists at all a reminder is skipped (``ContentMetadataUnavailableError``) with a log line and an errored
  ``REMINDED`` audit action, instead of raising and marking the assignment errored. This closes the window between
  a course ending and the nightly job running.
* The ``COURSE_RUN_ENDED`` reason is gated by the ``enterprise_access.assignment_course_run_ended_expiration``
  waffle switch, off by default. While it is off, course run end dates are ignored and PII is not cleared for rows
  already recorded as ``COURSE_RUN_ENDED``. Turning it off again is the rollback for that reason only.
* The rest of this amendment is **not** gated by the switch and takes effect on deploy: the PII-clearing decision
  reading the recorded ``expiration_reason``, the ``BrazeCampaignSender`` fallback and reminder skipping, and the
  nightly job's frozen-id paging, per-assignment error isolation (with a non-zero exit) and atomic expiration.

Pending product sign-off
------------------------
The following consequences of this amendment are irreversible (PII clearing cannot be undone) and have **not**
yet been approved by product. The switch above must stay off until they are:

* PII is cleared earlier (right after the last course run ends) for assignments that used to expire at day 90.
* PII is now cleared for assignments that, before this change, expired as ``SUBSIDY_EXPIRED`` and kept their
  email indefinitely, whenever all course runs ended before the subsidy did.
* Course-level assignments expire once all *known* runs have ended, even if a new run might be announced later.

Consequences
============
* Assignments that were cancelled by the admin or that fell into an ``ERRORED`` state will not
  currently have PII cleared by this cron-based management command.
* The Assignment data schema does not yet fully support the improvements proposed in
  `<0015-expiration-improvements.rst>`_. The implementation of these improvements will
  allow us to also take a more nuanced approach about automatically expiring assignment records
  in non-allocated states, or to retire PII fields in other ways.

Alternatives Considered
=======================

Hook into the edX User Retirement Pipeline
------------------------------------------
We have not yet rejected, but not yet committed to, integrating ``LearnerContentAssignment``
record retirement with the edX User Retirement Pipeline. This would involve exposing
some API view to scrub PII from certain assignment records associated with a
registered edX user who has requested that their account be retired. This is mostly relevant
in the case of ``ACCEPTED`` assignments, or assignments that have fallen into an ``ERRORED``
state prior to being automatically expired.
