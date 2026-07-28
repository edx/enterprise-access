# Late Enrollment for Custom Presentations

Date investigated: 2026-06-01

## Summary

Late enrollment currently bypasses only the LMS enrollment deadline. It does not bypass all of the upstream assumptions used to find, price, assign, and redeem content. Those upstream paths often resolve a course-level content key to the course's advertised course run. For one-off/custom presentations, especially language variants, there may be no future or advertised run. In that case the late enrollment path can fail before it ever reaches the LMS `force_enrollment` behavior.

The safest permanent fix is to make late enrollment operate on an explicit course run key for the requested presentation, and to add a late-enrollment-only metadata/catalog lookup path that can resolve that specific run even when the parent course has no future advertised cohort.

## Affected Examples

These examples are now in the past relative to 2026-06-01, so they are true late-enrollment scenarios:

- Curso online com certificado Fundamentos LSE MBA - Santander 2025-09-24
- LSE Conceptos Esenciales de MBA curso de certificado en linea - Santander 2025-09-24
- UCT Public Sector Financial Reporting online short course - 2026-05-11

## Code Findings

The Enterprise Access late redemption admin tool enables a temporary policy window, then instructs operators to assign the course and craft an enrollment URL. See `enterprise_access/templates/subsidy_access_policy/admin/set_late_redemption.html:32`.

`SubsidyAccessPolicy.can_redeem()` still requires the content key to be in the policy catalog before checking the enrollment deadline. Late redemption skips the deadline check only when `is_late_redemption_allowed` is true. See `enterprise_access/apps/subsidy_access_policy/models.py:857` and `enterprise_access/apps/subsidy_access_policy/models.py:873`.

When redemption is allowed, Enterprise Access adds transaction metadata `allow_late_enrollment=True`. See `enterprise_access/apps/subsidy_access_policy/models.py:1042`.

Enterprise Subsidy converts that metadata into `force_enrollment=True` for the enterprise bulk enrollment endpoint. See `enterprise_subsidy/apps/api_client/enterprise.py:110`. In LMS, `force_enrollment` only bypasses expired `enrollment_end` validation, not catalog/content resolution. See `openedx/core/djangoapps/enrollments/api.py:225`.

Assignment allocation derives a `preferred_course_run_key` from content metadata. For course-level keys, this depends on the content summary's selected run. See `enterprise_access/apps/content_assignments/api.py:779`.

Enterprise Subsidy's content summary resolves course-level identifiers to `advertised_course_run_uuid`. If the supplied identifier is a course run key, it can return that specific run. If the supplied identifier is a course key and no advertised run exists, it returns an empty run payload. See `enterprise_subsidy/apps/content_metadata/api.py:190`.

edx-enterprise has the same advertised-run dependency in `DefaultEnterpriseEnrollmentIntention`: course-level content keys resolve through `get_advertised_course_run()`, and validation fails if no course run can be resolved. See `enterprise/models.py:2616` and `enterprise/models.py:2745`.

## Likely Root Cause

The late enrollment configuration is not failing at the final LMS enrollment step. It is failing earlier because the system is trying to resolve or validate a course-level key through an advertised/future/current presentation.

For standard courses, this is usually fine because there is normally another cohort or advertised run. For custom Santander-style presentations, there may be only one presentation. Once that presentation is past its normal enrollment window and no future cohort exists, course-level resolution can produce no course run, the wrong future run, or catalog/content-metadata failures.

## Recommended Fix

Implement a first-class late enrollment path based on exact course run keys.

1. Require or strongly prefer exact `course_run_key` input for late enrollment into custom presentations.
2. Do not auto-select a future advertised run for late enrollment when the operator has specified a one-off/custom presentation.
3. Allow late-enrollment metadata lookup for that exact course run even when the parent course has no advertised/future run.
4. Keep normal catalog and budget controls intact for regular learner-initiated redemption.
5. Keep `force_enrollment` limited to the LMS deadline bypass.

## Implementation Plan

1. Confirm the failing production path.
   - Check whether the Santander failures return `content_not_in_catalog`, `beyond_enrollment_deadline`, missing `course_run_key`, or an Enterprise Catalog 404.
   - Inspect Enterprise Catalog metadata for each exact run key: `course_runs`, `advertised_course_run_uuid`, `normalized_metadata_by_run`, `variant_id`, `enrollment_end`, and parent `content_key`.
   - Confirm whether operators are configuring/assigning a course key or a course run key.

2. Add explicit-run validation and messaging in Enterprise Access.
   - Update the late redemption admin guidance to say custom/one-off presentations must use the exact course run key.
   - Add validation or warnings where assignments/forced redemptions accept a course key that resolves to no `course_run_key`.
   - For custom late enrollment, fail with an actionable message instead of silently depending on `advertised_course_run_uuid`.

3. Add late-aware content metadata resolution.
   - In Enterprise Subsidy, extend the metadata summary path to handle `allow_late_enrollment` plus an exact course run key.
   - If the requested identifier is a course run key, return that run even when the parent course has no advertised run.
   - If the requested identifier is only a course key and no advertised run exists, return a clear error requiring an exact run key.

4. Add a scoped Enterprise Catalog lookup change if catalog inclusion is the blocker.
   - Add a service-to-service parameter such as `include_expired_course_runs=true` for exact course run lookups.
   - Use it only from late-enrollment/forced-redemption paths.
   - Do not broadly make archived content visible in learner browse/search.

5. Preserve financial and partner safeguards.
   - Continue checking policy/subsidy activity and remaining balance.
   - Continue using the original content price or an explicit staff-provided price for forced redemptions.
   - Keep catalog bypasses scoped to staff-enabled late enrollment windows or forced redemption records.

6. Add tests.
   - Enterprise Access: late redemption still sends `allow_late_enrollment`; assignment/forced redemption with an exact custom course run does not require a future advertised run.
   - Enterprise Subsidy: content summary for an exact run returns the requested run; course-level late resolution without an advertised run gives a clear error.
   - Enterprise Catalog: exact run metadata can be fetched by the late path without exposing archived runs to browse/search.
   - End-to-end: one course with one past custom run, no future runs, no advertised run, successful late redemption, transaction metadata includes `allow_late_enrollment`, and LMS bulk enrollment receives `force_enrollment=True`.

## Short-Term Operational Workaround

Before code changes land, try the exact course run key path, not the parent course key:

1. Enable late redemption on the relevant policy.
2. Use the exact course run key for the target presentation when assigning or forcing redemption.
3. Verify the exact run still exists in Enterprise Catalog metadata and the policy catalog can resolve it.
4. Use Forced Policy Redemption only if catalog/content metadata resolution succeeds and the required GEAG metadata is available.

If the exact run is no longer resolvable through Enterprise Catalog for the customer, an operational workaround will likely require temporarily adding that exact run or parent content to the relevant catalog. If Enterprise Catalog still excludes it because it is archived and there is no future run, code changes are required.

## Risks and Non-Goals

- Do not automatically choose the "latest" or "next" run for custom presentations. That can enroll learners into the wrong language variant or wrong partner-specific presentation.
- Do not bypass catalog checks globally. Late enrollment should remain staff/policy scoped.
- Do not rely on course-level content keys for one-off custom runs unless an advertised run exists and is confirmed to be the desired presentation.
