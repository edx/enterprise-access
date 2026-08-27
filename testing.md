BE: Audit instrumentation for Browse & Request approval flows




Key details
Description

Description:-
Instrument the Browse & Request (B&R) approval flows in enterprise-access to write audit action rows capturing the reviewer as the acting admin. Currently, reviewer information is stored on LearnerCreditRequest but not on the assignment action timeline.

Implementation Steps:-

In the approval endpoint (POST /api/v1/learner-credit-requests/approve/), write an approved action row followed by an allocated or reallocated action row, with the reviewer as actor (actor_type=admin).

Capture the request UUID in the action metadata for traceability back to the original learner credit request.

In the approve-all endpoint (POST /api/v1/learner-credit-requests/approve-all/), handle the bulk path with correlation — one pair of action rows per approved request, all sharing a correlation ID.

Set source=browse_request_approve for single approvals and source=browse_request_approve_all for bulk.

Add/extend tests for: single approval producing approved + allocated rows with reviewer actor; bulk approve-all producing correct rows per request with shared correlation ID; request UUID presence in metadata.

Acceptance Criteria:-

Single B&R approval writes approved + allocated/reallocated action rows with reviewer as actor_lms_user_id and actor_type=admin.

Request UUID is present in action metadata.

Approve-all writes one pair of action rows per approved request with shared correlation ID.

Source is correctly set to browse_request_approve or browse_request_approve_all.

Tests cover single approval, bulk approve-all, reviewer attribution, and request UUID verification.