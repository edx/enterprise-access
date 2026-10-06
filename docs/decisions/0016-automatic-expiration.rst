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
  ``ALLOCATED`` assignments with the new rule.

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
