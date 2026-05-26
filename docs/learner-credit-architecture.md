# Learner Credit Product Architecture

> This doc is for engineers and product managers getting oriented to the learner credit product. It opens with product framing and progressively drills down into models, APIs, and cross-service integration. For the canonical policy big-picture, see `docs/subsidy-access-policy/README.rst`; this doc connects the pieces rather than replacing that one.

## 1. Introduction

Learner credit is an enterprise-funded budget that a customer's learners can spend on educational content. An enterprise purchases a pool of credit, configures who can spend it and on what, and the platform enforces those rules at redemption time.

Three flavors exist, each fitting different customer needs:

| Flavor | Product description | Fits customers who... | Primary user actions |
|---|---|---|---|
| Per-learner spend credit | Learners browse and self-enroll within a spend cap | Want lightweight governance. Learners drive their own learning. | Learner: browse, redeem |
| Assignment-based credit | Admins pre-assign specific courses to specific learners | Want curated learning paths, career track programs | Admin: allocate. Learner: accept + enroll |
| Browse & Request (BNR) | Learners ask for courses; admins approve or decline | Want learner-initiated learning, with oversight | Learner: request. Admin: approve/decline |

All three flavors share the same underlying subsidy (the money pool) and, to a large extent, the same redemption pipeline. The differences live in how access is granted: self-serve, pre-assigned, or mediated by approval.

## 2. System context

Learner credit spans four services plus two frontends. Each service owns one thing:

```
┌────────────────────┐   ┌────────────────────┐
│ Admin Portal       │   │ Learner Portal     │
│ (frontend)         │   │ (frontend)         │
└────────┬───────────┘   └─────────┬──────────┘
         │                         │
         └────────────┬────────────┘
                      │
           ┌──────────▼──────────────┐
           │ enterprise-access       │◄──── policy brain
           │   - access policies     │      ownership: rules,
           │   - assignments         │      assignments, requests
           │   - credit requests     │
           └──────────┬──────────────┘
                      │
     ┌────────────────┼───────────────┬──────────────────┐
     │                │               │                  │
┌────▼─────────┐ ┌────▼────────┐ ┌────▼──────┐ ┌─────────▼────────┐
│ enterprise-  │ │ enterprise- │ │ LMS       │ │ Braze            │
│ subsidy      │ │ catalog     │ │ (edxapp + │ │ (notifications)  │
│              │ │             │ │ edx-ent.) │ │                  │
│ - subsidies  │ │ - content   │ │           │ │                  │
│ - ledger     │ │   metadata  │ │ - course  │ │                  │
│ - fulfillment│ │ - catalogs  │ │   enroll  │ │                  │
│  (incl. GEAG)│ │             │ │   ments   │ │                  │
│              │ │             │ │           │ │                  │
└──────────────┘ └─────────────┘ └───────────┘ └──────────────────┘
```

- **enterprise-access** (this repo) is the policy brain. It decides whether a learner can redeem, whether an assignment is valid, whether a request gets approved. It does not hold money and does not enroll learners directly.
- **enterprise-subsidy** owns the budget. Every subsidy is a ledger of transactions; a redemption creates a committed transaction that decrements the balance. Fulfillment to external systems (GetSmarter for Exec Ed) also lives here.
- **LMS (edx-platform + the edx-enterprise library)** owns course enrollments. When a redemption succeeds, enterprise-subsidy calls into the LMS to create the enrollment record.
- **enterprise-catalog** answers "does this course belong to this enterprise's catalog" and supplies content metadata and pricing.
- **Braze** sends transactional emails (assignment notifications, reminders, request-approved confirmations).

The two frontends call into enterprise-access, which aggregates data from the others via BFF (backend-for-frontend) endpoints when needed.

## 3. Business domain models

A customer's learner credit program is configured as a small tree of records:

```
   ┌─────────────────────────┐
   │ Subsidy                 │   (enterprise-subsidy)
   │                         │   The money pool.
   │ - uuid                  │   Starts at X, decrements
   │ - enterprise_uuid       │   as transactions commit.
   │ - starting_balance      │
   │ - expiration_datetime   │
   └───────────┬─────────────┘
               │ 1:many
               │
   ┌───────────▼─────────────┐
   │ SubsidyAccessPolicy     │   (enterprise-access)
   │                         │   Rules over the pool.
   │ - uuid                  │   Subclasses determine
   │ - subsidy_uuid          │   flavor:
   │ - spend_limit           │     PerLearnerSpendCreditPolicy
   │ - per_learner_spend_limit│    PerLearnerEnrollmentCreditPolicy
   │ - access_method         │    AssignedLearnerCreditPolicy
   │ - active                │
   └───────┬──────────────┬──┘
           │              │
           │ 1:1 (opt.)   │ 1:1 (opt.)
           │              │
   ┌───────▼─────┐  ┌─────▼─────────────────────────┐
   │ Assignment  │  │ LearnerCreditRequest          │
   │ Config      │  │ Configuration                 │
   │             │  │                               │
   │ - active    │  │ - active                      │
   └──────┬──────┘  └──────────────┬────────────────┘
          │ 1:many                 │ 1:many
          │                        │
   ┌──────▼─────────────┐  ┌───────▼────────────────┐
   │ LearnerContent     │  │ LearnerCreditRequest   │
   │ Assignment         │  │                        │
   │                    │  │ - 1:1 to Assignment    │
   │ - allocated/accept │  │ - state machine        │
   └────────────────────┘  └────────────────────────┘
```

A single subsidy can back multiple policies, which is how a customer splits one budget across, say, a BNR policy and an assignment policy. Each policy carries its own spend limits and access rules.

The policy subclass determines the flavor. `PerLearnerSpendCreditAccessPolicy` is the self-serve flavor; `AssignedLearnerCreditAccessPolicy` is the assignment flavor; BNR is enabled by attaching a `LearnerCreditRequestConfiguration` to a spend-credit policy.

For the policy model rationale, see [ADR 0004](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0004-add-access-policy-functionality.rst). For assignment configuration, see [ADR 0012](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0012-assignment-based-policies.rst). For grouping (segmenting learners within a policy), see [ADR 0018](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0018-access-policy-grouping.rst).

## 4. Per-learner spend credit

### Product view

A customer loads a subsidy with a budget, configures a catalog of content, and sets two limits: a total policy-wide spend cap (`spend_limit`) and an optional per-learner spend cap (`per_learner_spend_limit`). Learners browse that catalog in the learner portal and self-enroll. Each enrollment debits the subsidy.

This is the lowest-friction flavor. No admin action per enrollment, no approval queue.

### Happy-path flow

```
Learner          enterprise-access       enterprise-subsidy       LMS        Braze
  │                      │                       │                 │          │
  │  browse catalog      │                       │                 │          │
  ├─────────────────────▶│                       │                 │          │
  │                      │  can_redeem?          │                 │          │
  │                      ├──────────────────────▶│                 │          │
  │                      │◄──────────────────────┤ (yes, balance   │          │
  │  redeem              │                       │  covers it)     │          │
  ├─────────────────────▶│                       │                 │          │
  │                      │  create_transaction   │                 │          │
  │                      ├──────────────────────▶│                 │          │
  │                      │                       │  enroll         │          │
  │                      │                       ├────────────────▶│          │
  │                      │                       │◄────────────────┤          │
  │                      │◄──────────────────────┤ committed       │          │
  │                      │   enrollment email    │                 │          │
  │                      ├───────────────────────────────────────────────────▶│
  │  enrolled            │                       │                 │          │
  │◄─────────────────────┤                       │                 │          │
```

### Technical view

- **Model:** `PerLearnerSpendCreditAccessPolicy` in `enterprise_access/apps/subsidy_access_policy/models.py:1387`. Inherits from `SubsidyAccessPolicy` (line 114) and `CreditPolicyMixin`.
- **Key fields:** `spend_limit` (policy-wide cap, USD cents), `per_learner_spend_limit` (per-learner cap, USD cents), `active`.
- **Primary endpoint:** `POST /api/v1/policy-redemption/{policy_uuid}/redeem/`, served by `SubsidyAccessPolicyRedeemViewset.redeem()` in `enterprise_access/apps/api/v1/views/subsidy_access_policy.py`. It calls `policy.can_redeem()` first, then `policy.redeem()`, which delegates to `subsidy_client.create_subsidy_transaction()`.
- **Spend evaluation** happens at `can_redeem` time. The policy asks enterprise-subsidy for the learner's historical transaction total, adds the proposed price, and compares against `per_learner_spend_limit`. Policy-wide `spend_limit` is evaluated against aggregate transactions.
- **Locking:** the redeem path acquires a policy-level lock to prevent double-spends under concurrent requests. See ADRs [0005](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0005-access-policy-locks.rst) and [0007](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0007-access-policy-locks-revised.rst).
- **Frontend entry point:** `frontend-app-learner-portal-enterprise/src/components/enterprise-user-subsidy/enterprise-offers/`. Balance display and redemption trigger live here.

## 5. Assignment-based credit

### Product view

An admin picks specific courses and specific learners and allocates one to the other. The learner receives an email ("You've been assigned Course X"), clicks through, accepts, and gets enrolled. The admin-side flow is in the admin portal; the learner-side flow is in the learner portal.

This flavor fits structured learning: onboarding programs, role-specific curricula, career tracks. The budget is committed at allocation time (reserved, not yet spent), so admins can see remaining budget accurately as they allocate.

### Happy-path flow

```
Admin          enterprise-access       Braze           Learner          LMS
  │                   │                  │                │              │
  │ allocate          │                  │                │              │
  ├──────────────────▶│                  │                │              │
  │                   │  create          │                │              │
  │                   │  LearnerContent  │                │              │
  │                   │  Assignment      │                │              │
  │                   │  (ALLOCATED)     │                │              │
  │                   │                  │                │              │
  │                   │  assignment      │                │              │
  │                   │  notification    │                │              │
  │                   ├─────────────────▶│                │              │
  │                   │                  │  email         │              │
  │                   │                  ├───────────────▶│              │
  │                   │                  │                │ click,       │
  │                   │                  │                │ accept       │
  │                   │◄──────────────────────────────────┤              │
  │                   │  redeem (assignment-aware path)                  │
  │                   │       [subsidy transaction commits]              │
  │                   │  enroll                                          │
  │                   ├──────────────────────────────────────────────────▶│
  │                   │  mark ACCEPTED                                   │
  │                   │                                                  │
```

### Assignment state diagram

```
                       ┌─────────────┐
                       │  ALLOCATED  │   admin allocated,
                       │             │   learner notified
                       └──────┬──────┘
                              │
               ┌──────────────┼──────────────┬────────────────┐
               │              │              │                │
               ▼              ▼              ▼                ▼
         ┌──────────┐   ┌──────────┐   ┌──────────┐     ┌──────────┐
         │ ACCEPTED │   │ CANCELLED│   │ EXPIRED  │     │ ERRORED  │
         │          │   │          │   │ (90 days)│     │ (notify  │
         │ learner  │   │ (admin   │   │          │     │  failed) │
         │ enrolled │   │  cancel) │   │          │     │          │
         └─────┬────┘   └──────────┘   └──────────┘     └──────────┘
               │
               │ (learner unenrolls,
               │  refund eligible)
               ▼
         ┌──────────┐
         │ REVERSED │  (driven by
         │          │   LEDGER_TRANSACTION_REVERSED
         │          │   event — see §8)
         └──────────┘
```

### Technical view

- **Policy model:** `AssignedLearnerCreditAccessPolicy` in `subsidy_access_policy/models.py:1581`. Enforces `access_method == AccessMethods.ASSIGNED`.
- **Configuration:** `AssignmentConfiguration` in `enterprise_access/apps/content_assignments/models.py:38`, one per policy.
- **Per-assignment record:** `LearnerContentAssignment` at the same file, line 236+. Fields include `content_key`, `content_quantity` (price in USD cents, stored negative), `lms_user_id`, `allocated_at`, `accepted_at`, `transaction_uuid` (populated after acceptance).
- **Allocate endpoint:** `POST /api/v1/policy-allocation/{policy_uuid}/allocate/`, served by `SubsidyAccessPolicyAllocateViewset.allocate()`. Takes `learner_emails`, `content_key`, `content_price_cents`.
- **Acceptance:** the same `/policy-redemption/{uuid}/redeem/` endpoint handles both self-serve and assignment-based redemptions. The policy's `access_method` branches into assignment-aware logic: the redeem step looks up the `LearnerContentAssignment`, checks it's `ALLOCATED`, commits the subsidy transaction, and marks the assignment `ACCEPTED`.

The reservation semantics of allocation (budget consumed at allocation, not at acceptance) are implemented in the subsidy ledger via a pending transaction pattern. See [ADR 0012](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0012-assignment-based-policies.rst) for the full mechanics.

- **Braze campaigns:** allocation notification, reminder, and expiration emails are sent from `content_assignments/tasks.py` via `BrazeCampaignSender`.
- **Auto-expiration:** assignments stuck in `ALLOCATED` for 90 days transition to `EXPIRED` via a scheduled task. See ADRs [0015](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0015-expiration-improvements.rst) and [0016](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0016-automatic-expiration.rst).
- **Admin frontend entry:** `frontend-app-admin-portal/src/components/learner-credit-management/`, with the assignment-modal subdirectory for allocation UI.
- **Learner frontend entry:** assignment routes in `frontend-app-learner-portal-enterprise/src/routes.tsx`, with acceptance wired through the stateful-enroll components.

## 6. Browse & Request (BNR)

### Product view

BNR sits in between per-learner spend and assignment: learners choose their own content, but admins get the final say. The learner submits a request, the admin sees it in a queue, and the admin approves or declines. An approval allocates the assignment and triggers the same acceptance flow as in §5.

This flavor fits customers who want learner-initiated learning but also want cost governance and oversight.

### Happy-path flow

```
Learner       enterprise-access     Braze      Admin       enterprise-access (again)
  │                  │                │          │                  │
  │ submit request   │                │          │                  │
  ├─────────────────▶│                │          │                  │
  │                  │ LearnerCredit  │          │                  │
  │                  │ Request        │          │                  │
  │                  │ (REQUESTED)    │          │                  │
  │                  │                │          │                  │
  │                  │ admin alert    │          │                  │
  │                  ├───────────────▶│          │                  │
  │                  │                │  email   │                  │
  │                  │                ├─────────▶│                  │
  │                  │                │          │ approve          │
  │                  │◄────────────────────────────────────────────▶│
  │                  │                │          │ (APPROVED        │
  │                  │                │          │  + Assignment    │
  │                  │                │          │  ALLOCATED)      │
  │                  │ approval email │          │                  │
  │                  ├───────────────▶│          │                  │
  │                  │                │  email   │                  │
  │                  │                ├─────────▶│                  │
  │  [now takes the assignment acceptance path from §5]             │
  │    learner clicks → accept → redeem → enroll → ACCEPTED         │
```

### Request state diagram

```
               ┌──────────────┐
               │  REQUESTED   │  (learner submitted)
               └──────┬───────┘
                      │
            ┌─────────┼─────────┐
            │         │         │
            ▼         ▼         ▼
      ┌──────────┐┌──────────┐┌──────────┐
      │ APPROVED ││ DECLINED ││CANCELLED │
      │ + alloc. ││          ││ (learner │
      │ created  ││          ││  cancel) │
      └─────┬────┘└──────────┘└──────────┘
            │
            ▼
       ┌──────────┐     ┌──────────┐
       │ ACCEPTED │──┬──│ REVERSED │
       │          │  │  │          │
       └──────────┘  │  └──────────┘
                     │
                  ┌──▼───────┐
                  │  ERROR   │  (redemption failed)
                  └──────────┘
```

### The non-obvious piece

BNR does not have its own redemption pipeline. Approval is allocation: when an admin approves a `LearnerCreditRequest`, enterprise-access creates a `LearnerContentAssignment` linked 1:1 to the request and from that point on the learner's experience is identical to §5. Everything the assignment pipeline does (email, acceptance, redemption, reversal) is re-used. This is why `LearnerCreditRequest.assignment` is a `OneToOneField`.

### Technical view

- **Model:** `LearnerCreditRequest` in `enterprise_access/apps/subsidy_request/models.py:382`. Inherits from the abstract `SubsidyRequest` (line 41).
- **Configuration:** `LearnerCreditRequestConfiguration` at line 326, attached to a `PerLearnerSpendCreditAccessPolicy` via a `OneToOneField`. Its `active` flag is the BNR feature toggle for a policy.
- **Audit trail:** `LearnerCreditRequestActions` at line 638+ records each state transition with timestamps, actor, and error details on failures.
- **Create endpoint:** `POST /api/v1/learner-credit-requests/`, served by `LearnerCreditRequestViewSet.create()` in `enterprise_access/apps/api/v1/views/browse_and_request.py`.
- **Approve endpoint:** `POST /api/v1/learner-credit-requests/{uuid}/approve/`. Internally calls `assignments_api.allocate_assignment_for_requests()`, which creates the `LearnerContentAssignment` and wires it to the request.
- **Decline endpoint:** `POST /api/v1/learner-credit-requests/{uuid}/decline/`. Records the decline action; no allocation.
- **Unique constraint:** a learner cannot have two open requests for the same course in the same enterprise (see the model's `Meta` constraints at line 416+).
- **Related docs:** `docs/request_and_approve.rst` has sequence diagrams for the same flow from a different angle. [ADR 0002](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0002-auto-enrollment-post-request-approval.rst) covers auto-enrollment post-approval.
- **Admin frontend entry:** `frontend-app-admin-portal/src/components/subsidy-requests/` and the BNR tab under `learner-credit-management/`.
- **Learner frontend entry:** `frontend-app-learner-portal-enterprise/src/components/enterprise-subsidy-requests/`.

## 7. Exec Ed (GEAG / GetSmarter) fulfillment

This flow layers on top of any of §§4–6. In enterprise-subsidy it's called GEAG or GetSmarter; in enterprise-access we don't model it specially.

### Product view

Executive education courses are delivered by 2U / GetSmarter, not by Open Course Marketplace (OCM) in the LMS. When a learner redeems learner credit against an Exec Ed course, enterprise-subsidy does two things: allocates a seat with GetSmarter's external system, and creates the local enrollment record. Both have to succeed for the transaction to commit. From the learner's perspective, the experience is the same; the branching is invisible.

### Happy-path flow (differences vs. OCM)

```
  redemption request
         │
         ▼
  enterprise-subsidy: SubsidyAPI.redeem()
         │
         ▼
  is_geag_fulfillment(content)?
         │
     ┌───┴────┐
     │        │
    yes      no
     │        │
     ▼        ▼
  ┌────────────────────┐   ┌─────────────────┐
  │ GEAGFulfillment    │   │ (skip external) │
  │ Handler.fulfill()  │   │                 │
  │                    │   │                 │
  │  - call GetSmarter │   │                 │
  │    API             │   │                 │
  │  - create          │   │                 │
  │    ExternalTxn     │   │                 │
  │    Reference       │   │                 │
  └──────────┬─────────┘   └────────┬────────┘
             │                      │
             └──────────┬───────────┘
                        ▼
               LMS enrollment
                        │
                        ▼
              commit transaction
              (with external_reference
               if GEAG)
```

### Technical view

- **Branching decision:** `is_geag_fulfillment()` at `enterprise-subsidy/enterprise_subsidy/apps/fulfillment/api.py:36-45`. Delegates to `ContentMetadataApi.get_product_source()`; if the source is `ProductSources.TWOU`, route to GEAG.
- **Dispatch site:** `SubsidyAPI.redeem()` at `enterprise-subsidy/enterprise_subsidy/apps/subsidy/models.py:632-640`.
- **Handler:** `GEAGFulfillmentHandler` at `enterprise-subsidy/enterprise_subsidy/apps/fulfillment/api.py:48-260`. Responsible for the GetSmarter API call, retry handling, and recording the external reference.
- **External client:** `GetSmarterEnterpriseApiClient.create_enterprise_allocation()` (from the `getsmarter_api_clients` library), called at line 189 of the handler. OAuth2 credentials live in Django settings.
- **External reference record:** `ExternalTransactionReference` links the internal ledger transaction to the GetSmarter allocation. Used on reversal to know which external allocation to cancel.
- **Where enterprise-access fits:** it doesn't. The policy layer is content-source-agnostic. If you're tracing an Exec Ed bug, look in enterprise-subsidy, not here.
- **Unenrollment:** GEAG allocations are cancelled via `cancel_transaction_external_fulfillment()` in `enterprise_subsidy/apps/transaction/api.py:56`, invoked from the reversal flow described in §8.

## 8. Unenrollment and refund flow

This is the most confusing corner of the system. Events cross service boundaries in both directions, and a single user action (a learner clicking unenroll) fans out into three services' state machines. Read this section before touching any of it.

### Trigger

A learner unenrolls from an LMS course that was originally enrolled via learner credit.

### Event chain

```
  LMS
   │
   │ emits LEARNER_CREDIT_COURSE_ENROLLMENT_REVOKED
   │ (openedx-events via Kafka)
   │
   ▼
  enterprise-subsidy: handle_lc_enrollment_revoked()
  (apps/transaction/signals/handlers.py:87)
   │
   ├─ existing reversal? → stop (loop guard)
   │
   ├─ GEAG transaction? → cancel_transaction_external_fulfillment()
   │                       (apps/transaction/api.py:56)
   │                       tells GetSmarter to cancel the allocation
   │
   ├─ unenrollment_can_be_refunded()?
   │     │
   │     no → log and stop
   │     │
   │     yes
   │     ▼
   │   reverse_transaction()
   │     │
   │     ├─ create Reversal row in the ledger
   │     │  (restores subsidy balance)
   │     │
   │     └─ emit LEDGER_TRANSACTION_REVERSED
   │         (apps/core/event_bus.py:82)
   │
   ▼
  enterprise-access: update_assignment_status_for_reversed_transaction()
  (content_assignments/signals.py:56)
   │
   ├─ look up LearnerContentAssignment by transaction_uuid
   │
   ├─ mark assignment REVERSED
   │
   └─ if linked LearnerCreditRequest: mark it REVERSED too
```

### Idempotency and loop prevention

The enterprise-subsidy handler checks for an existing `Reversal` on the transaction before acting. This matters because reversal events can originate from multiple places: an LMS-initiated unenrollment, an admin-initiated reversal via the ECS (event-consumption service) admin tools, or an automated refund workflow. Without the guard, overlapping events would create duplicate reversals.

Rule to follow if adding new handlers in this chain: they must be idempotent. Check your target state and no-op if it's already been reached.

### Admin-initiated reversal

Same second half of the chain. An admin triggers a reversal in enterprise-subsidy directly, which creates the `Reversal` row and emits `LEDGER_TRANSACTION_REVERSED`. From there, the enterprise-access side behaves identically.

### Related references

- [ADR 0026](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0026-stripe-event-consumption-delivery.rst): Stripe event consumption and delivery. Same event-bus patterns, adjacent concern.
- `docs/segment_events.rst`: Segment analytics events for the same lifecycle.

## 9. Cross-cutting concerns

### Spend accounting

enterprise-subsidy is the source of truth for spend. Every committed transaction decrements the subsidy balance; every reversal restores it. enterprise-access caches aggregate spend per policy and per learner for `can_redeem` evaluations, but the cache is always derived. If you see a balance discrepancy, trust the subsidy ledger, not the cache. See [ADR 0021](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0021-transaction-aggregates.rst) for transaction aggregates.

### Policy retirement vs. active toggle

Policies cannot be hard-deleted because historical transactions reference them. Flipping `active=False` hides a policy from new redemption attempts but preserves historical visibility. For the motivation and mechanics, see [ADR 0017](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0017-policy-retirement.rst).

### Policy grouping

A policy can be scoped to a subset of learners within an enterprise via `PolicyGroupAssociation` and `EnterpriseGroup`. This is how one enterprise can run "Engineering team gets a $2k budget" and "Sales team gets a $500 budget" off separate policies backed by separate subsidies. See [ADR 0018](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0018-access-policy-grouping.rst).

### Locking during redemption

Redemption acquires a policy-level lock to prevent race conditions on the spend-limit check. See ADRs [0005](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0005-access-policy-locks.rst) and [0007](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0007-access-policy-locks-revised.rst) for the revised design.

### Forced redemption

An admin escape hatch that bypasses normal eligibility checks. Used sparingly, for support tickets where a learner should have been eligible but wasn't. See [ADR 0019](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0019-forced-redemption.rst).

## 10. Glossary

- **Subsidy.** The money pool. Lives in enterprise-subsidy, backed by a ledger.
- **Ledger.** The append-only transaction log that determines a subsidy's balance.
- **Transaction.** A single charge against a subsidy. States: pending, committed, reversed.
- **Reversal.** A transaction that offsets another, restoring subsidy balance. Created on refund.
- **Policy.** A rule set over a subsidy: who can spend, up to how much, for what content.
- **Budget.** Product-facing synonym for "a policy's slice of a subsidy." Shows up in the admin UI.
- **Assignment.** A specific course allocated to a specific learner under an assignment-based policy.
- **Allocation.** The act of creating an assignment. Reserves spend against the subsidy.
- **Redemption.** The act of a learner spending against a policy. Creates a committed transaction and an LMS enrollment.
- **BNR (Browse & Request).** Flavor where learners request and admins approve before the assignment is created.
- **OCM (Open Course Marketplace).** The standard Open edX course delivery path (non-Exec-Ed).
- **GEAG / GetSmarter.** The 2U executive-education delivery path. Requires external fulfillment from enterprise-subsidy.

## 11. Further reading

**Product and flow docs**
- `docs/subsidy-access-policy/README.rst`: canonical big-picture diagram and policy mechanics.
- `docs/request_and_approve.rst`: BNR flow diagrams.
- `docs/architecture-overview.md`: broader service context beyond learner credit.

**Architecture Decision Records**
- [0002](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0002-auto-enrollment-post-request-approval.rst): Auto-enrollment post-request-approval.
- [0004](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0004-add-access-policy-functionality.rst): Access policy model design.
- [0005](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0005-access-policy-locks.rst), [0007](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0007-access-policy-locks-revised.rst): Access policy locking.
- [0012](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0012-assignment-based-policies.rst): Assignment-based policies.
- [0015](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0015-expiration-improvements.rst), [0016](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0016-automatic-expiration.rst): Assignment expiration.
- [0017](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0017-policy-retirement.rst): Policy retirement.
- [0018](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0018-access-policy-grouping.rst): Policy grouping.
- [0019](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0019-forced-redemption.rst): Forced redemption.
- [0021](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0021-transaction-aggregates.rst): Transaction aggregates.
- [0026](https://github.com/edx/enterprise-access/tree/main/docs/decisions/0026-stripe-event-consumption-delivery.rst): Stripe event consumption and delivery.

**Patterns**
- `docs/architecture-patterns.md`: reusable patterns across the codebase.

**Cross-repo entry points**
- enterprise-subsidy: `enterprise_subsidy/apps/subsidy/models.py` (Subsidy), `enterprise_subsidy/apps/fulfillment/api.py` (GEAG), `enterprise_subsidy/apps/transaction/signals/handlers.py` (revocation handler).
- edx-enterprise: `enterprise/models.py` (`EnterpriseCourseEnrollment`, `LearnerCreditEnterpriseCourseEnrollment`); `enterprise/api/v1/views/enterprise_subsidy_fulfillment.py` (fulfillment views).
- frontend-app-admin-portal: `src/components/learner-credit-management/`, `src/components/subsidy-requests/`.
- frontend-app-learner-portal-enterprise: `src/components/enterprise-user-subsidy/enterprise-offers/`, `src/components/enterprise-subsidy-requests/`.
