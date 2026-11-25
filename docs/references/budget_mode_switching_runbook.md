# Moving a customer between learner credit budget modes: support runbook

This runbook is for support staff (ECS) who move a customer between the three learner credit budget modes. A budget is a subsidy access policy: it says which subsidy pays, for which catalog, and how learners get access. Steps name the Django admin page or the API call. The last section, for engineers, says where each statement comes from in the code.

The procedures describe what the code on `main` does today. Eight pull requests that change this area are still open. Every step that depends on one of them says so.

## What is live and what is not (as of 2026-11-25)

None of these pull requests is merged. Until one is, its "after" behaviour does not exist.

| Pull request | Status | Effect on this procedure once merged and deployed |
|---|---|---|
| edx/enterprise-access#413 | Open | A learner is no longer shown "funds expired" from an ended budget while a live one exists. The learner dashboard's request card shows only while a B&R budget can still take requests. |
| edx/enterprise-access#414 | Open | New nightly command `expire_requests_for_ended_budgets` moves requests awaiting review (`requested`, `error`) on a retired or expired B&R budget to `expired`. Waffle switch `enterprise_access.expire_requests_for_ended_budgets`, off by default; while off, the nightly run only logs. A run with `--enterprise-customer-uuid` or `--policy-uuid` acts even while the switch is off. `--dry-run` only logs. |
| edx/edx-internal#15171 | Open | Schedules #414 daily at 01:00 UTC. Merges only after #414 is deployed. |
| edx/enterprise-access#419 | Open | `automatically_expire_assignments` also expires allocated assignments on a retired budget (reason `BUDGET_RETIRED`). A linked approved request moves to `expired`. No learner email. Waffle switch `enterprise_access.expire_assignments_on_retired_budgets`, off by default; product must agree before it is turned on. |
| edx/enterprise-access#421 | Open | Saving an active request configuration gives its budget an assignment configuration if it has none. The order of the enable steps stops mattering. |
| edx/enterprise-access#422 | Open | Unticking `active` on a request configuration is refused while any of its requests is `requested`, `pending`, `approved` or `error`. |
| edx/frontend-app-learner-portal-enterprise#223 | Open | On a course page, a way to enroll (for example an assignment) wins over an old pending request: the learner sees Enroll, not "Awaiting approval". |
| edx/frontend-app-admin-portal#209 | Open | On a retired or expired budget, the Requests tab disables Approve and keeps Decline. |

## Terms

- **Admin Assign**: the customer admin assigns courses to named learners. Each assignment holds funds until the learner enrolls.
- **Browse & Enroll**: learners pick courses from the catalog and enroll directly, up to a per-learner limit.
- **Browse & Request (B&R)**: learners pick courses and send a request. A customer admin approves or declines it. An approval creates an assignment underneath, so an approved request holds funds like an Admin Assign assignment.
- **Request configuration**: a `LearnerCreditRequestConfiguration` row. Attached to a Browse & Enroll budget with `active` ticked, it turns that budget into B&R.
- **Assignment configuration**: an `AssignmentConfiguration` row. Every assignment, including the one behind an approved request, belongs to one. A budget without one cannot hold assignments.
- **Retire** (`retired` ticked): the budget stops all new enrollments and requests but stays visible to admins.
- **Deactivate** (`active` unticked on the budget): the budget is hidden from admins and learners. This is a soft delete for budgets made by mistake.

## The three modes

| Mode | Budget class (Django admin model) | Request configuration | Assignment configuration |
|---|---|---|---|
| Admin Assign | Assigned learner credit access policy | None | Always; created when the budget is saved |
| Browse & Enroll | Per learner spend credit access policy | None, or one with `active` unticked | Usually none (one may remain from an earlier spell as B&R) |
| Browse & Request | Per learner spend credit access policy | Attached, `active` ticked | Required; created when the budget is saved while B&R is on |

A third budget class caps the number of enrollments per learner instead of spend: Per learner enrollment credit access policy. It is always Browse & Enroll. Its admin page has no "Learner credit request config" and it never gets an assignment configuration, so B&R cannot be turned on for it. Moving it to B&R means a new per learner spend budget.

To tell which mode a budget is in:

1. In Django admin, search for the budget UUID under Subsidy access policy > Assigned learner credit access policies (`/admin/subsidy_access_policy/assignedlearnercreditaccesspolicy/`). If it is there, it is Admin Assign.
2. Otherwise search under Subsidy access policy > Per learner enrollment credit access policies (`/admin/subsidy_access_policy/perlearnerenrollmentcreditaccesspolicy/`). If it is there, it is Browse & Enroll and you can stop.
3. Otherwise open it under Subsidy access policy > Per learner spend credit access policies (`/admin/subsidy_access_policy/perlearnerspendcreditaccesspolicy/`). All three lists filter by `active`, `retired` and access method. Their search box matches the budget UUID, display name, customer UUID, catalog UUID or subsidy UUID; the lists also offer DjangoQL search.
4. On that budget's page, look at "Learner credit request config". Empty means Browse & Enroll. If set, open the configuration: `active` ticked means B&R, unticked means Browse & Enroll.
5. At the bottom of the budget page, the "Extra" section shows the REST API serialization. Check `policy_type`, `bnr_enabled` and `assignment_configuration`. A B&R budget must show `bnr_enabled: true` and a non-null `assignment_configuration`.

The same fields come from `GET /api/v1/subsidy-access-policies/<budget uuid>/`.

## Rules you must not break

- Do not try to change a budget's class. `policy_type` is not editable in Django admin and the update API does not accept it. Moving to or from Admin Assign always means a new budget.
- Do not untick `active` on a request configuration while any of its requests is `requested`, `pending`, `approved` or `error`. Approved requests stop counting against the budget, so it shows more money available than it has, and learners can enroll directly at once. Retire the budget instead, or close the requests first (see "Closing a request by hand"). After #422 the admin refuses the untick.
- Detaching is the same as unticking. Clearing "Learner credit request config" on the budget's page, or deleting the configuration, also turns B&R off, because B&R needs an attached, active configuration. The same harm follows, and #422 refuses neither. After #414, its job finds requests through the configuration attached to a budget, so a detached configuration's requests would never expire.
- Do not deactivate (untick `active` on) a budget that has spend. The model refuses it with "Cannot deactivate this policy while it has existing spend." Retire it instead: that keeps its history visible to admins.
- Do not turn B&R on while licence or coupon requests are enabled for the customer. Only one subsidy type can take requests at a time; saving the request configuration or the budget fails with "Only one subsidy type can have Browse & Request enabled at a time." Check Subsidy request > Subsidy request customer configurations (`/admin/subsidy_request/subsidyrequestcustomerconfiguration/`) and turn `subsidy_requests_enabled` off there first, with the customer's agreement.
- Do not reuse a request configuration on a second budget. A configuration belongs to one budget. Create a new one.
- Do not retire a budget while learners still hold assignments or approved requests on it, unless the customer agrees, in writing, to lose them. A retired budget refuses every enrollment, so those learners can no longer enroll with them.

## Closing a request by hand

The customer admin does this in the budget's Requests tab in the admin portal. The API calls are under `/api/v1/learner-credit-requests/`.

| State | What the customer admin can do | API call |
|---|---|---|
| `requested` | Decline it, or approve it | `POST decline/` (`subsidy_request_uuid`), `POST decline-all/`, `POST approve/` (`policy_uuid`, `learner_credit_request_uuids`) |
| `error` | Approve it again. Decline refuses it. If the approval works, the request is `approved` and can then be cancelled | `POST approve/`, `POST approve-all/` |
| `approved` | Cancel it, or wait until the learner enrolls (`accepted`) | `POST cancel/` (`request_uuid`), `POST cancel-all/`, `POST bulk-cancel/` |

- Decline accepts only `requested`. Cancel is for `approved`: the single `cancel/` call does not check the state, but it cancels the request's assignment, and a request that was never approved has none, so the call fails and the request stays as it was. Do not use it on `requested` or `error`.
- On `main` no code moves a learner credit request to `pending` or `error`; only licence and coupon code requests take those states. A failed approval leaves the request in its state and records the failure in its history. Expect no `pending` or `error` learner credit requests; one that exists came from older data or a manual change.
- If an `error` request's approval fails again, it cannot be closed by hand. It stays open until #414 expires it.
- Every decline and every cancel emails the learner. `decline/` and `decline-all/` queue a decline email for each declined request whatever `send_notification` says: the field defaults to false, but the view only logs it. `cancel/`, `cancel-all/` and `bulk-cancel/` queue a cancel email for each cancelled request. Warn the customer admin before they close many requests. (The admin portal's decline also sends `send_notification: true`; checked in a local checkout from 2026-07-22, which may be behind.)

## Step: create the new budget (To confirm with ECS)

Support normally creates budgets in the support tool (frontend-app-support-tools). That tool is not in this repository and was not read, so its screens are not described here. ECS must confirm the usual path.

This repository offers two ways:

1. Django admin: "Add" on Assigned learner credit access policies (Admin Assign) or Per learner spend credit access policies (Browse & Enroll and B&R). Fill customer UUID, display name, catalog UUID, subsidy UUID, spend limit and, for per learner spend, the per-learner spend limit. Tick `active`.
2. API: `POST /api/v1/subsidy-access-policies/` with `policy_type` (`AssignedLearnerCreditAccessPolicy` or `PerLearnerSpendCreditAccessPolicy`), `enterprise_customer_uuid`, `catalog_uuid`, `subsidy_uuid`, `access_method` (`assigned` or `direct`), `active` and the limits.

Do not rely on the API to make a second budget on the same subsidy and catalog. If an active budget of the same class already exists with the same customer, subsidy, catalog and access method, the API returns that budget with HTTP 200 instead of creating one. A retired budget still has `active` ticked, so it counts. HTTP 201 means a new budget was created. If you get 200, use Django admin.

## Move 1: Admin Assign → Browse & Request

1. List the old budget's allocated assignments (see "Checking the result"). Ask the customer admin to let learners accept them, or to cancel them in the admin portal (API: `POST /api/v1/assignment-configurations/<uuid>/admin/assignments/cancel-all/`).
2. Check the customer has no licence or coupon requests enabled (see the rules).
3. Create the new budget as Browse & Enroll (Per learner spend credit access policy). See "create the new budget". Do not attach a request configuration yet.
4. Turn on B&R for the new budget with Move 3, steps 2 to 5.
5. Retire only when the old budget has no allocated assignment left (the Django admin list in query 1 of "Checking the result", searched by the old budget's assignment configuration UUID, shows none; the query's code counts retired budgets only), or when the customer has agreed, in writing, to lose them. Then, on the old budget's page (Assigned learner credit access policies), tick `retired` and save. Do not untick `active`. Assignments still allocated when the budget is retired can no longer be used to enroll, and they hold their funds until the nightly job below expires them.

Left open on the old budget: allocated assignments. Today the nightly `automatically_expire_assignments` command expires each one at the earliest of: 90 days after allocation, the course's enrollment deadline, or the subsidy's expiry. A fourth rule, course run end, applies only while the `assignment_course_run_ended_expiration` switch is on. There is no switch for the other three. After #419 with `enterprise_access.expire_assignments_on_retired_budgets` on, the next nightly run expires them with reason `BUDGET_RETIRED` and no email.

## Move 2: Browse & Request → Admin Assign

1. List the old budget's open requests and its approved requests that have not been redeemed (see "Checking the result").
2. Ask the customer admin to close every `requested` and `error` request (see "Closing a request by hand"). Approved requests are assignments; learners can still accept them until the budget is retired, or the admin can cancel them.
3. Create the new budget as Admin Assign (Assigned learner credit access policy). See "create the new budget". Saving it creates its assignment configuration.
4. Retire only when the old budget has no allocated assignment and no approved request left (the Django admin lists in queries 1 and 2 of "Checking the result" show none for it), or when the customer has agreed, in writing, to lose them. Then, on the old budget's page (Per learner spend credit access policies), tick `retired` and save. Do not untick `active` on the budget. Approved requests still open when the budget is retired can no longer be used to enroll, and their assignments hold the funds until the nightly job expires them.
5. Optional. Leave the request configuration attached and `active` ticked, unless the customer wants the learner dashboard request card gone before #413 is live: today a retired budget with an active configuration still shows the card. To remove it, wait until the old budget has no request in `requested`, `pending`, `approved` or `error`, then untick `active` on its configuration and leave it attached. Skip this step while any such request remains. The cost: the admin portal shows a budget's Requests tab only while B&R is on, so the admin can no longer see that budget's request history there.

Left open on the old budget:
- Requests awaiting review (`requested`, `error`). Today nothing closes them: they wait forever and block the same learner from requesting the same course again. After #414 with `enterprise_access.expire_requests_for_ended_budgets` on (and the #15171 schedule), the next 01:00 UTC run moves them to `expired`. Before the switch is on, an engineer can run the command for this budget with `--policy-uuid`.
- Approved requests not yet redeemed. They expire with their assignment under the same rules as Move 1, and the request moves to `expired` with it. After #419 with its switch on, they expire at the next nightly run.
- Today the admin portal still offers Approve on the retired budget's requests. Tell admins to decline instead. After #209, Approve is disabled there.

## Move 3: Browse & Enroll → Browse & Request (same budget)

Nothing is left open: Browse & Enroll holds no assignments or requests.

1. Check the customer has no licence or coupon requests enabled (see the rules).
2. In Subsidy request > Learner credit request configurations (`/admin/subsidy_request/learnercreditrequestconfiguration/`), click "Add", tick `active`, and save.
3. Open the budget under Per learner spend credit access policies. In "Learner credit request config", choose the new configuration. Save.
4. Saving the budget is what creates its assignment configuration. Check the "Extra" section: `bnr_enabled: true` and `assignment_configuration` is not null.
5. If `assignment_configuration` is null, save the budget again without changes. That creates it.

Do not do these steps in the other order today. If the configuration is attached while unticked and `active` is ticked later on the configuration's own page, the budget takes requests but has no assignment configuration, and approvals fail. Saving the budget again repairs it. After #421, saving the active configuration repairs it, and the order stops mattering.

## Move 4: Browse & Request → Browse & Enroll (same budget)

1. List the budget's requests in `requested`, `pending`, `approved` or `error` (see "Checking the result").
2. Ask the customer admin to close every open request (see "Closing a request by hand"), or to wait until learners accept the approved ones.
3. Repeat step 1. Continue only when it returns nothing.
4. Open the budget's request configuration (Learner credit request configurations). Untick `active` and save. Leave it attached to the budget.
5. Check the budget's "Extra" section shows `bnr_enabled: false`.

If the requests cannot be closed, for example an `error` request whose approval fails again, do not untick and do not detach the configuration. Retire this budget (with the gate in Move 2, step 4), create a new Browse & Enroll budget, and treat the old one as in Move 2. After #422 the untick in step 4 is refused while any open request remains.

## Move 5: back to Browse & Request after a spell on Admin Assign

This is Move 1 again, with two extra checks for what the first B&R spell left behind.

1. Do Move 1, including the gate before its retire step. Use a new budget and a new request configuration. A configuration that belongs to the old B&R budget cannot be attached to a second budget.
2. If the old B&R budget is on the same subsidy and catalog, create the new budget in Django admin, not the API: the API returns the old retired budget instead.
3. List the customer's requests still in `requested`, `approved` or `error` (see "Checking the result"). Today any of these blocks the same learner from requesting the same course again on any budget of the customer, and so does an `accepted` one (the learner already enrolled). Ask the admin to close the stale ones on the old budget (see "Closing a request by hand"). After #414 with its switch on, `requested` and `error` requests on the retired budget expire at the next run; after #419, approved ones do too.
4. A learner who has both an old pending request and a new assignment for the same course sees "Awaiting approval" on the course page today. After #223 they see Enroll.

## What is left open after each move

| Move | Left open on the old budget | What closes it today | After the open pull requests |
|---|---|---|---|
| Admin Assign → B&R | Allocated assignments | `automatically_expire_assignments`, nightly: 90 days, enrollment deadline or subsidy expiry; course run end behind `assignment_course_run_ended_expiration` | #419: next nightly run, behind `enterprise_access.expire_assignments_on_retired_budgets` |
| B&R → Admin Assign | Requests awaiting review | Nothing; they wait forever | #414 and #15171: next 01:00 UTC run, behind `enterprise_access.expire_requests_for_ended_budgets` |
| B&R → Admin Assign | Approved, unredeemed requests | Same as allocated assignments; the request expires with its assignment | #419, as above |
| Browse & Enroll → B&R | Nothing | Not needed | Not needed |
| B&R → Browse & Enroll | Nothing, if step 3 returned nothing | Not needed | #422 enforces step 3 |
| Back to B&R | As B&R → Admin Assign, for the first B&R budget | As above | As above |

## Checking the result

What the customer admin should see (checked in frontend-app-admin-portal, learner-credit-management):
- The new budget in the admin portal, and the old one with the status "Retired" (`BUDGET_STATUSES.retired` in `data/utils.js`). A deactivated budget would be hidden; a retired one is not.
- On a B&R budget, a Requests tab. On a budget switched back to Browse & Enroll, no Requests tab: it shows only while the budget's `bnrEnabled` is true (`useBudgetDetailTabs.jsx`).

What a learner should see (checked in a local frontend-app-learner-portal-enterprise checkout from 2026-03-30, which may be behind):
- On a B&R budget, a request button on the course page. `SubsidyRequestButton.jsx` shows it only when `GET /api/v1/policy-redemption/enterprise-customer/<customer uuid>/can-request/?content_key=<course key>` returns `can_request: true`, the learner is not enrolled, has no other subsidy for the course and has no request open for it. If the call returns "You already have an active request for this course", an old request is still open.
- On Browse & Enroll, an Enroll option on the course page (expected, not checked in the frontend code).
- The dashboard Browse & Request card (`BrowseAndRequestAlert.jsx`, shown by `DashboardPage.jsx`) shows while the BFF returns `has_bnr_enabled_policy: true`, that is while the customer has an active budget with an active request configuration. The learner can dismiss it. Today that includes a retired budget; after #413 only a budget that can still take requests.

Replace `C` with the customer UUID. All three are read-only. Each says when it should return nothing.

1. Allocated assignments on a retired budget. Django admin: Content assignments > Learner content assignments (`/admin/content_assignments/learnercontentassignment/`), filter state "allocated", and search for the old budget's assignment configuration UUID (from its "Extra" section).

   ```python
   LearnerContentAssignment.objects.filter(assignment_configuration__enterprise_customer_uuid=C, assignment_configuration__subsidy_access_policy__retired=True, state='allocated')
   ```

   Empty today once the customer admin has cancelled the assignments and learners have accepted the rest, or once the nightly expiry rules have expired them (up to 90 days). After #419 with its switch on: after the next nightly run.

2. Requests still open on a retired budget. Django admin: Subsidy request > Learner credit requests (`/admin/subsidy_request/learnercreditrequest/`), filter by the customer UUID and by state (one of requested, pending, approved, error at a time). The list cannot filter by budget; open a request to see its configuration. The query cannot see subsidy expiry, which lives in the subsidy service; it covers retired budgets only.

   ```python
   LearnerCreditRequest.objects.filter(enterprise_customer_uuid=C, learner_credit_request_config__learner_credit_config__retired=True, state__in=['requested', 'pending', 'approved', 'error'])
   ```

   Empty today once the customer admin has closed the requests by hand. Approved ones also go when the nightly expiry rules expire their assignments; `requested` and `error` ones stay until someone closes them. After #414 (with its switch on and the #15171 schedule) and #419 (with its switch on): after the next nightly runs.

3. A B&R budget with no assignment configuration. Django admin has no list filter for this: open each of the customer's per learner spend budgets and check the "Extra" section, as in Move 3, step 4.

   ```python
   SubsidyAccessPolicy.objects.filter(enterprise_customer_uuid=C, learner_credit_request_config__active=True, assignment_configuration__isnull=True)
   ```

   Always empty. A row means approvals on that budget fail; save the budget again (Move 3, step 5).

## Where this comes from in the code

All citations are to `main` at the time of writing.

- Budget classes are proxy models; each manager filters on its class: `enterprise_access/apps/subsidy_access_policy/models.py:127-129` (`PolicyManager`).
- `policy_type` is not editable and `save()` sets it from the class: `enterprise_access/apps/subsidy_access_policy/models.py:160-164` (`policy_type`), `enterprise_access/apps/subsidy_access_policy/models.py:494-499` (`policy_type`). The update API leaves it out: `enterprise_access/apps/api/serializers/subsidy_access_policy.py:448-468` (`SubsidyAccessPolicyUpdateRequestSerializer`).
- Retire versus deactivate: `enterprise_access/apps/subsidy_access_policy/models.py:199-213` (`retired`), `docs/learner-credit-architecture.md:437-439` (`active=False`). A retired budget refuses redemption: `enterprise_access/apps/subsidy_access_policy/models.py:310-314` (`is_redemption_enabled`), `enterprise_access/apps/subsidy_access_policy/models.py:891-894` (`REASON_POLICY_EXPIRED`).
- Only `save()` reads `retired`, to set `retired_at`; no signal or job reacts to it: `enterprise_access/apps/subsidy_access_policy/models.py:494-497` (`retired_at`).
- Deactivation with spend is refused: `enterprise_access/apps/subsidy_access_policy/models.py:472-474` (`validate_budget_deactivation_with_spend`), `enterprise_access/apps/subsidy_access_policy/utils.py:224-255` (`ERROR_MSG_ACTIVE_WITH_SPEND`).
- B&R is a request configuration on a per learner spend budget: `enterprise_access/apps/subsidy_access_policy/models.py:221-227` (`learner_credit_request_config`), `enterprise_access/apps/subsidy_access_policy/models.py:424-428` (`bnr_enabled`), `enterprise_access/apps/subsidy_request/models.py:326-342` (`LearnerCreditRequestConfiguration`).
- The per learner spend budget creates its assignment configuration only when saved with B&R on: `enterprise_access/apps/subsidy_access_policy/models.py:1477-1494` (`create_assignment_configuration`). The Admin Assign budget always does: `enterprise_access/apps/subsidy_access_policy/models.py:1648-1660` (`create_assignment_configuration`).
- Approved requests count against the budget only while B&R is on: `enterprise_access/apps/subsidy_access_policy/models.py:1500-1514` (`bnr_enabled`).
- One subsidy type at a time for requests: `enterprise_access/apps/subsidy_access_policy/models.py:1454-1475` (`subsidy_requests_enabled`), `enterprise_access/apps/subsidy_request/models.py:348-375` (`subsidy_requests_enabled`).
- Django admin pages and fields: `enterprise_access/apps/subsidy_access_policy/admin/__init__.py:70-74` (`list_filter`), `enterprise_access/apps/subsidy_access_policy/admin/__init__.py:182-194` (`api_serialized_repr`), `enterprise_access/apps/subsidy_access_policy/admin/__init__.py:285-289` (`learner_credit_request_config`), `enterprise_access/apps/subsidy_request/admin.py:301-325` (`LearnerCreditRequestConfigurationAdmin`), `enterprise_access/apps/subsidy_request/admin.py:30-33` (`list_filter`), `enterprise_access/apps/content_assignments/admin.py:88-95` (`assignment_configuration__uuid`).
- The create API returns an existing active budget: `enterprise_access/apps/api/serializers/subsidy_access_policy.py:309-322` (`filtered_policy`), `enterprise_access/apps/api/v1/views/subsidy_access_policy.py:361-371` (`create`).
- Request states and the states that block a new request: `enterprise_access/apps/subsidy_request/constants.py:4-35` (`SubsidyRequestStates`), `enterprise_access/apps/subsidy_request/constants.py:53-58` (`LC_NON_RE_REQUESTABLE_STATES`), `enterprise_access/apps/api/v1/views/browse_and_request.py:913-924` (`ACCEPTED`), `enterprise_access/apps/api/v1/views/subsidy_access_policy.py:1085-1097` (`LC_NON_RE_REQUESTABLE_STATES`), `enterprise_access/apps/subsidy_request/models.py:416-426` (`unique_learner_course_request`).
- A request needs a live budget: `enterprise_access/apps/api/v1/views/browse_and_request.py:890-897` (`get_policies_accepting_requests`), `enterprise_access/apps/subsidy_access_policy/subsidy_api.py:251-266` (`is_redemption_enabled`).
- Assignment expiry rules and the nightly command: `enterprise_access/utils.py:133-150` (`get_automatic_expiration_date_and_reason`), `enterprise_access/apps/content_assignments/management/commands/automatically_expire_assignments.py:64-86` (`EXPIRABLE_STATES`). A linked request expires with its assignment: `enterprise_access/apps/content_assignments/api.py:1363-1367` (`credit_request`).
- The dashboard request card: `enterprise_access/apps/subsidy_access_policy/models.py:431-446` (`has_bnr_enabled_policy_for_enterprise`), `enterprise_access/apps/bffs/response_builder.py:182` (`has_bnr_enabled_policy`).
- Decline and cancel APIs: `enterprise_access/apps/api/v1/views/browse_and_request.py:1121-1127` (`cancel`), `enterprise_access/apps/api/v1/views/browse_and_request.py:1387` (`decline`), `enterprise_access/apps/api/v1/views/content_assignments/assignments_admin.py:258` (`cancel-all`).
- Which actions email the learner. The single cancel queues the email itself: `enterprise_access/apps/api/v1/views/browse_and_request.py:1167-1169` (`send_learner_credit_bnr_cancel_notification_task`). `cancel-all/` and `bulk-cancel/` call the bulk cancel, which queues one per cancelled request: `enterprise_access/apps/api/v1/views/browse_and_request.py:1323` (`cancel_learner_credit_requests`), `enterprise_access/apps/api/v1/views/browse_and_request.py:1368` (`cancel_learner_credit_requests`), `enterprise_access/apps/subsidy_request/api.py:476-483` (`send_learner_credit_bnr_cancel_notification_task`). `decline/` and `decline-all/` call the bulk decline, which queues one per declined request: `enterprise_access/apps/api/v1/views/browse_and_request.py:1408` (`decline_learner_credit_requests`), `enterprise_access/apps/api/v1/views/browse_and_request.py:1471` (`decline_learner_credit_requests`), `enterprise_access/apps/subsidy_request/api.py:546-552` (`send_learner_credit_bnr_decline_notification_task`). `send_notification` defaults to false and is only logged: `enterprise_access/apps/api/serializers/subsidy_requests.py:248-250` (`send_notification`), `enterprise_access/apps/api/v1/views/browse_and_request.py:1421-1426` (`send_notification`).
- Which state each action accepts: `enterprise_access/apps/subsidy_request/constants.py:67-70` (`DECLINABLE_STATES`), `enterprise_access/apps/api/serializers/subsidy_requests.py:288-294` (`non_declinable`), `enterprise_access/apps/subsidy_request/api.py:77` (`APPROVABLE_STATES`). The single cancel has no state check and cancels the request's assignment: `enterprise_access/apps/api/serializers/subsidy_requests.py:422-431` (`validate_request_uuid`), `enterprise_access/apps/api/v1/views/browse_and_request.py:1127-1150` (`cancel_assignments`), `enterprise_access/apps/content_assignments/api.py:1118-1121` (`assignment.state`). The assignment is set only on approval: `enterprise_access/apps/subsidy_request/api.py:266-279` (`request.assignment`).
- Only licence and coupon code requests take `pending`; a learner credit request goes straight to `approved`, and a failed approval only records an action: `enterprise_access/apps/subsidy_request/models.py:191-193` (`PENDING`), `enterprise_access/apps/subsidy_request/models.py:243-245` (`PENDING`), `enterprise_access/apps/subsidy_request/models.py:524-526` (`APPROVED`), `enterprise_access/apps/subsidy_request/api.py:282-295` (`_prepare_failed_requests_and_actions`), `enterprise_access/apps/api/tasks.py:215` (`ERROR`).
- Deleting a request configuration detaches it from its budget: `enterprise_access/apps/subsidy_access_policy/models.py:221-224` (`SET_NULL`).
- The per learner enrollment budget: `enterprise_access/apps/subsidy_access_policy/models.py:1255` (`PerLearnerEnrollmentCreditAccessPolicy`), `enterprise_access/apps/subsidy_access_policy/admin/__init__.py:196-197` (`PerLearnerEnrollmentCreditAccessPolicy`), `enterprise_access/apps/subsidy_access_policy/admin/__init__.py:217-247` (`fieldsets`).
- Admin list search: `enterprise_access/apps/subsidy_access_policy/admin/__init__.py:249-250` (`DjangoQLSearchMixin`), `enterprise_access/apps/subsidy_access_policy/admin/__init__.py:266-272` (`enterprise_customer_uuid`).
