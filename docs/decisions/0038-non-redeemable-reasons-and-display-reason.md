# 0038 Non-redeemable reasons by budget type, and the one a learner sees

## Status

**Proposed** (October 2026)

Sections 1–4 record how budgets are evaluated today and the `display_reason` ranking added in
[edx/enterprise-access#326](https://github.com/edx/enterprise-access/pull/326). Section 5 proposes
evaluating budgets in three stages. It keeps that messaging rule, makes it follow from the
evaluation order instead of a separate ranking, and drops remote calls that can't change the
outcome. Section 5 isn't implemented yet.

## Context

A learner credit budget is a `SubsidyAccessPolicy` record. Every eligibility check on a budget
either passes or returns a short reason slug, and those slugs drive the message a learner or admin
sees. The slugs are defined in `subsidy_access_policy/constants.py`, raised in
`subsidy_access_policy/models.py` and turned into learner-facing text in
`api/v1/views/subsidy_access_policy.py` (`_get_user_message_for_reason`). Nothing documented
which budget type raises which reason, in what order, or which reason wins when a customer has
several budgets.

That gap surfaced in [ENT-12416](https://2u-internal.atlassian.net/browse/ENT-12416). A learner over their per-learner spend limit was told
*"You can't enroll right now because your funds expired."* The customer had a live,
spend-capped budget alongside older budgets that were still active and unretired but funded by
expired subsidies. `can-redeem` evaluates every active, unretired budget, sorts expired subsidies
first (soonest expiration), and used to display the first reason it found, so the expired budget's
reason masked the one that actually blocked the learner.

The same evaluation also costs more than it needs to. Every budget runs the full chain of checks,
and the chain reaches an uncached remote call to enterprise-subsidy (`subsidy_client.can_redeem`
in `SubsidyAccessPolicy.can_redeem`) before it learns whether the subsidy is active, so expired
budgets pay for that call too. The endpoint also keeps evaluating budgets after one is
redeemable, although it only uses the first. On the last 30 days of production redemptions, an
evaluation made 4.02 of these calls per course run on average, and 22% of them went to expired
budgets (see [Performance](#performance)).

### Budget types

`PolicyTypes` (`subsidy_access_policy/constants.py`) lists every concrete budget type. All
three are proxy models of `SubsidyAccessPolicy`.

| Policy type | Priority | Limits it enforces | Type-specific reasons |
|---|---|---|---|
| `AssignedLearnerCreditAccessPolicy` | 0 | `spend_limit`; content must be assigned by an admin | `reason_learner_not_assigned_content`, `reason_learner_assignment_cancelled`, `reason_learner_assignment_failed`, `reason_learner_assignment_expired`, `reason_learner_assignment_reversed` |
| `PerLearnerSpendCreditAccessPolicy` | 1 | `spend_limit`; optional `per_learner_spend_limit` (USD cents, net of committed reversals) | `learner_max_spend_reached`. When Browse & Request (BnR) is enabled, `can_redeem` instead runs as an assigned budget and raises the assignment reasons above. |
| `PerLearnerEnrollmentCreditAccessPolicy` | 1 | `spend_limit`; optional `per_learner_enrollment_limit` (count of transactions) | `learner_max_enrollments_reached` |

Priority comes from `AssignedCreditPolicyMixin` and `CreditPolicyMixin`.
`SUBSCRIPTION_POLICY_TYPE_PRIORITY` (2) is defined but no policy type uses it.

## Decision

We record the full reason catalogue and evaluation order here, as the reference for anyone adding
a budget type or a reason. We choose `display_reason` by ranking reasons instead of taking the
first one. And we propose evaluating budgets in three stages, so the evaluation order itself
produces that ranking and skips work that can't change the outcome.

### 1. How one budget decides today

`SubsidyAccessPolicy.can_redeem` runs the shared checks in a fixed order, and each subclass adds
its own check only after they all pass. The first failing check is the budget's reason. Checks are
ordered from the learner's state, to the content, to the budget's money, as the method's docstring
describes.

```mermaid
%%{init: {"flowchart": {"padding": 24, "nodeSpacing": 28, "rankSpacing": 36, "wrappingWidth": 340}, "themeVariables": {"fontFamily": "Arial, Helvetica, sans-serif", "fontSize": "14px"}}}%%
flowchart TB
    START(["policy.can_redeem(learner, content)"]) --> G1

    subgraph G1["Shared checks: learner and content"]
        direction LR
        a["Policy active and<br/>not retired?<br/>✗ policy_expired"] --> b["Learner linked to<br/>the customer? ¹<br/>✗ learner_not_in_enterprise"]
        b --> c["Learner in one of the<br/>policy's groups? ¹<br/>✗ learner_not_in_enterprise_group"]
        c --> d["Content in catalog,<br/>metadata found?<br/>✗ content_not_in_catalog"]
        d --> e["Before the enroll-by<br/>date? ²<br/>✗ beyond_enrollment_deadline"]
    end

    subgraph G2["Shared checks: money"]
        direction LR
        f["Subsidy active now? ³<br/>✗ subsidy_expired"] --> g["Subsidy balance covers<br/>the price? ³<br/>✗ not_enough_value_in_subsidy"]
        g --> h["Policy spend_limit<br/>covers the price?<br/>✗ policy_spend_limit_reached"]
    end

    subgraph G3["Type-specific check, last"]
        direction LR
        i["PerLearnerEnrollment:<br/>under per_learner_enrollment_limit?<br/>✗ learner_max_enrollments_reached"]
        j["PerLearnerSpend, BnR off:<br/>net spend + price within the limit?<br/>✗ learner_max_spend_reached"]
        k["Assigned, or BnR on:<br/>allocated assignment?<br/>✗ reason_learner_not_assigned_content<br/>✗ reason_learner_assignment_cancelled /<br/>failed / expired / reversed"]
        i ~~~ j ~~~ k
    end

    G1 --> G2 --> G3 --> OK(["Redeemable"])

    classDef expiry fill:#fde2e1,stroke:#c0392b,color:#1b1b1b
    classDef notApplicable fill:#fff3cd,stroke:#b7950b,color:#1b1b1b
    classDef blocking fill:#d6eaf8,stroke:#2471a3,color:#1b1b1b
    classDef ok fill:#d5f5e3,stroke:#1e8449,color:#1b1b1b
    class a,f expiry
    class c,d notApplicable
    class b,e,g,h,i,j,k blocking
    class OK ok
```

Each box is one check and the reason it fails with, in the order they run. ¹ Skipped by
`can-redeem` unless staff pass an `lms_user_id` override. ² Unless late redemption is allowed.
³ Answered by enterprise-subsidy's remote `can_redeem` call. Colours match the `display_reason`
ranks in section 3: red is expiry, yellow is a budget that doesn't apply, blue is a budget that
applies but blocks the learner.

### 2. Reason catalogue

*Raised by* lists the budget types that can produce the reason. *Where* lists the operations that
raise it: `can-redeem` and `redeem` (learner enrolling, `can_redeem`), `allocate` (admin
assigning, `AssignedLearnerCreditAccessPolicy.can_allocate`), `approve` (admin approving BnR
requests, `can_approve` and `can_allocate_all`) and `can-request` (learner opening a BnR
request).

| Reason slug | Category | Raised when | Raised by | Where | Display rank |
|---|---|---|---|---|---|
| `policy_expired` | Expiry | The policy is inactive or retired (`is_redemption_enabled` is false). | All types | redeem, allocate, approve. `can-redeem` only evaluates active, unretired policies, so it never shows this one. | 2 |
| `subsidy_expired` | Expiry | The subsidy is outside its active window, either expired or not started yet. | All types | can-redeem, redeem, allocate, approve, can-request | 2 |
| `learner_not_in_enterprise` | Learner membership | The learner has no enterprise user record for the customer. | All types | redeem; can-redeem only with a staff `lms_user_id` override | 0 |
| `learner_not_in_enterprise_group` | Not applicable | The policy is restricted to groups and the learner is in none of them. | All types | redeem; can-redeem only with a staff `lms_user_id` override | 1 |
| `content_not_in_catalog` | Not applicable | The policy's catalog doesn't contain the content, or the subsidy has no metadata for it. | All types | can-redeem, redeem, allocate, approve (per request), can-request | 1 |
| `beyond_enrollment_deadline` | Deadline | Now is past the content's enroll-by date and late redemption isn't allowed. | All types | can-redeem, redeem | 0 |
| `not_enough_value_in_subsidy` | Out of budget | The subsidy's remaining balance can't cover the price (for allocation and approval: the already-allocated amount plus the new price). | All types | can-redeem, redeem, allocate, approve | 0 |
| `policy_spend_limit_reached` | Out of budget | The policy's `spend_limit` can't cover the price (for allocation and approval: spent plus allocated plus the new price). | All types | can-redeem, redeem, allocate, approve | 0 |
| `learner_max_spend_reached` | Limit reached | The learner's spend, net of committed reversals, plus the price exceeds `per_learner_spend_limit`. | `PerLearnerSpendCreditAccessPolicy` (BnR off) | can-redeem, redeem | 0 |
| `learner_max_enrollments_reached` | Limit reached | The learner's transaction count is at or above `per_learner_enrollment_limit`. | `PerLearnerEnrollmentCreditAccessPolicy` | can-redeem, redeem | 0 |
| `reason_learner_not_assigned_content` | Not assigned | The learner has no allocated assignment for the content. An `accepted` assignment also maps here; redemption checks already return earlier in that case. | `AssignedLearnerCreditAccessPolicy`; `PerLearnerSpendCreditAccessPolicy` with BnR on | can-redeem, redeem | 1 |
| `reason_learner_assignment_cancelled` | Not assigned | The learner's assignment was cancelled. | Same as above | can-redeem, redeem | 0 |
| `reason_learner_assignment_failed` | Not assigned | The learner's assignment errored. | Same as above | can-redeem, redeem | 0 |
| `reason_learner_assignment_expired` | Not assigned | The learner's assignment expired. | Same as above | can-redeem, redeem | 0 |
| `reason_learner_assignment_reversed` | Not assigned | The learner's assignment was reversed. | Same as above | can-redeem, redeem | 0 |
| `REASON_BNR_NOT_ENABLED` | Not applicable | An admin approves a request on a policy without an active BnR config. The constant holds the message ("This policy does not support BNR."), not a slug. | All types | approve | n/a |

Admin and BnR flows raise a subset of these, in their own order:

```mermaid
%%{init: {"flowchart": {"padding": 24, "nodeSpacing": 28, "rankSpacing": 36, "wrappingWidth": 340}, "themeVariables": {"fontFamily": "Arial, Helvetica, sans-serif", "fontSize": "14px"}}}%%
flowchart TB
    subgraph AL["allocate: AssignedLearnerCreditAccessPolicy.can_allocate"]
        direction LR
        A1["policy_expired"] --> A2["content_not_in_catalog"] --> A3["subsidy_expired"] --> A4["not_enough_value_in_subsidy"] --> A5["policy_spend_limit_reached"]
    end
    subgraph AP["approve: can_approve, then can_allocate_all"]
        direction LR
        P0["BnR not enabled"] --> P1["policy_expired"] --> P2["subsidy_expired"] --> P3["content_not_in_catalog or<br/>PriceValidationError<br/>(per request)"] --> P4["not_enough_value_in_subsidy"] --> P5["policy_spend_limit_reached"]
    end
    subgraph CR["can-request: SubsidyAccessPolicyRedeemViewset.can_request"]
        direction LR
        C1["no active policies"] --> C2["no BnR-enabled policies"] --> C3["subsidy_expired"] --> C4["content_not_in_catalog"] --> C5["existing open request"]
    end
    AL ~~~ AP ~~~ CR
```

The `can-request` checks run across all of the customer's BnR budgets at once rather than per
budget, and three of them return plain text rather than a slug.

### 3. Which reason the learner sees

`can-redeem` evaluates every budget for the content and groups budgets by reason. When none is
redeemable it returns all the reasons, plus a single `display_reason` that the learner portal
shows as-is.

```mermaid
%%{init: {"flowchart": {"padding": 24, "nodeSpacing": 28, "rankSpacing": 36, "wrappingWidth": 340}, "themeVariables": {"fontFamily": "Arial, Helvetica, sans-serif", "fontSize": "14px"}}}%%
flowchart TB
    Q(["GET can-redeem, per content key"]) --> EV["Skip content the learner already redeemed.<br/>Sort the active, unretired budgets: type priority,<br/>then soonest expiration, then lowest balance.<br/>Run can_redeem on each."]
    EV --> X{"Any budget<br/>redeemable?"}
    X -- yes --> OK(["Use the first redeemable budget"])
    X -- no --> RK["Group budgets by reason into reasons[].<br/>display_reason = the first reason<br/>with the lowest rank."]
    RK --> R0["<b>Rank 0: applies but blocks</b><br/>limits, out of budget, deadline,<br/>assignment state, not in enterprise"]
    RK --> R1["<b>Rank 1: doesn't apply</b><br/>not in catalog, not assigned,<br/>not in group"]
    RK --> R2["<b>Rank 2: expired</b><br/>subsidy_expired, policy_expired"]

    classDef expiry fill:#fde2e1,stroke:#c0392b,color:#1b1b1b
    classDef notApplicable fill:#fff3cd,stroke:#b7950b,color:#1b1b1b
    classDef blocking fill:#d6eaf8,stroke:#2471a3,color:#1b1b1b
    classDef ok fill:#d5f5e3,stroke:#1e8449,color:#1b1b1b
    class R2 expiry
    class R1 notApplicable
    class R0 blocking
    class OK ok
```

The ranking lives in `_display_reason_rank` (`api/v1/views/subsidy_access_policy.py`):

* **Rank 0, applies but blocks.** The budget would fund this learner and content but something is
  in the way: a per-learner limit, the budget or subsidy is out of money, the enroll-by date has
  passed, or the learner's assignment is in a terminal state. This is what the learner can act on.
* **Rank 1, doesn't apply.** The budget was never going to fund this learner and content. Assigned
  budgets sort first, so without this rank an assigned budget with nothing assigned would mask a
  browse-and-enroll budget's limit.
* **Rank 2, expired.** The budget is no longer usable at all. It's shown only when every budget
  is expired.

`min()` returns the first reason with the lowest rank, so the sort order above still decides
between reasons of the same rank. The `reasons` list and the response shape are unchanged, and
`redeem` and `allocate`, which only ever pass a single reason, are unaffected. Section 5 proposes
producing this ranking from the evaluation order instead.

### 4. Learner-facing messages

`_get_user_message_for_reason` maps slugs to `MissingSubsidyAccessReasonUserMessages`. The
"no admins" variants are used when the customer has no admin contact.

| Reason slug | Message |
|---|---|
| `subsidy_expired` | `ORGANIZATION_EXPIRED_FUNDS`: "...because your funds expired." |
| `policy_expired`, `not_enough_value_in_subsidy`, `policy_spend_limit_reached` | `ORGANIZATION_NO_FUNDS`: "...because your organization doesn't have enough funds." |
| `learner_max_spend_reached`, `learner_max_enrollments_reached` | `LEARNER_LIMITS_REACHED`: "...because of limits set by your organization." |
| `content_not_in_catalog` | `CONTENT_NOT_IN_CATALOG` |
| `beyond_enrollment_deadline` | `BEYOND_ENROLLMENT_DEADLINE` |
| `learner_not_in_enterprise` | `LEARNER_NOT_IN_ENTERPRISE` |
| `reason_learner_not_assigned_content`, `reason_learner_assignment_failed` | `LEARNER_NOT_ASSIGNED_CONTENT` |
| `reason_learner_assignment_cancelled` | `LEARNER_ASSIGNMENT_CANCELED` |
| `learner_not_in_enterprise_group`, `reason_learner_assignment_expired`, `reason_learner_assignment_reversed` | **None.** The learner portal has no fallback for these slugs either. |

### 5. Proposed: evaluate budgets in three stages

Sections 1 and 3 describe two orderings that don't agree: the order a budget runs its checks, and
the rank used to choose between budgets. The ranking compensates after the fact. If the check
order *is* the priority order, the ranking needs no table of its own, and work that can't change
the outcome can be skipped.

Every check belongs to one of three stages, run in this order:

1. **Live?** Is the budget usable at all?
2. **Applies?** Does it cover this learner and this content?
3. **Allowed now?** Can it pay for this enrollment right now?

A budget stops at its first failing check. Budgets move through the stages together, in sort
order, and stage 3 stops at the first budget that passes. When none passes, the learner sees
**the first reason from the furthest stage any budget reached.**

```mermaid
%%{init: {"flowchart": {"padding": 24, "nodeSpacing": 28, "rankSpacing": 36, "wrappingWidth": 340}, "themeVariables": {"fontFamily": "Arial, Helvetica, sans-serif", "fontSize": "14px"}}}%%
flowchart TB
    ALL(["Active, unretired budgets, in sort order"]) --> L
    L["<b>1 · Live?</b><br/>policy_expired<br/>subsidy_expired"] -- "live budgets" --> A
    A["<b>2 · Applies to this learner and content?</b><br/>learner_not_in_enterprise<br/>learner_not_in_enterprise_group<br/>content_not_in_catalog<br/>reason_learner_not_assigned_content"] -- "applicable budgets" --> C
    C["<b>3 · Allowed now?</b><br/><i>first budget to pass wins</i><br/>beyond_enrollment_deadline<br/>not_enough_value_in_subsidy<br/>policy_spend_limit_reached<br/>learner_max_spend_reached<br/>learner_max_enrollments_reached<br/>reason_learner_assignment_cancelled,<br/>_failed, _expired or _reversed"] -- "first budget that passes" --> OK(["Redeemable"])

    classDef expiry fill:#fde2e1,stroke:#c0392b,color:#1b1b1b
    classDef notApplicable fill:#fff3cd,stroke:#b7950b,color:#1b1b1b
    classDef blocking fill:#d6eaf8,stroke:#2471a3,color:#1b1b1b
    classDef ok fill:#d5f5e3,stroke:#1e8449,color:#1b1b1b
    class L expiry
    class A notApplicable
    class C blocking
    class OK ok
```

Within each stage box, checks run top to bottom. This one diagram and one rule replace the section 1
flowchart and the section 3 ranking. Each stage holds exactly one rank from section 3: stage 1 is
rank 2, stage 2 is rank 1, stage 3 is rank 0. The one difference is `learner_not_in_enterprise`,
which is in stage 2 here and rank 0 in section 3. It fails for every budget of the customer alike,
so the reason shown doesn't change. Whenever each budget fails only one check, `display_reason`
is the same as section 3's.

**Design rules**

* **Stage 1 reads the subsidy record, not the remote call.** `subsidy_record()` is
  request-cached, and the sort already loads it for any customer with two or more budgets. A
  budget is skipped only when `is_active` is `False`. If the lookup failed (`{}`), the state is
  unknown and the budget goes on to stage 3, whose remote call stays authoritative. If that call
  reports the subsidy inactive, the reason is `subsidy_expired`, which still ranks as stage 1.
  Otherwise a blip in enterprise-subsidy would tell learners their funds expired.
* **Within a stage, checks keep today's order**, so a budget's own reason changes only where a
  check moves to an earlier stage (see [Consequences](#consequences)).
* **Stop at the first redeemable budget.** The endpoint only uses `redeemable_policies[0]`, so
  budgets after it don't need evaluating.
* **One ordered check table.** Each check declares its stage and which budget types it applies
  to. That replaces the `super().can_redeem()` chains, the Browse & Request `copy_context_to`
  path in `can_redeem`, and the two hand-kept reason sets behind section 3. Tests enforce that
  every `REASON_*` slug belongs to exactly one stage, that stages never go backwards in the table,
  and that every reason has a learner message.
* **Optional: one remote call per subsidy.** The remote call takes `(subsidy, learner, content)`
  and doesn't depend on the budget, so budgets that share a subsidy can share its answer within a
  request. It must stay request-scoped: balances change between requests, and `redeem` keeps its
  own authoritative call under the policy lock.
* **One audit log line per evaluation**, listing each budget's furthest stage and reason, and the
  reason shown. Explaining a message like the one in ENT-12416 then takes a log search, not
  replica queries.

A sketch of the shape:

```python
class Stage(IntEnum):
    LIVE = 1     # is the budget usable at all?
    APPLIES = 2  # does it cover this learner and this content?
    ALLOWED = 3  # can it pay for this enrollment right now?

# Ordered. Stages never go backwards (a test enforces it); within a stage, today's order is kept.
CHECKS = (
    Check(Stage.LIVE, policy_disabled),                # policy_expired
    Check(Stage.LIVE, subsidy_known_inactive),         # subsidy_expired, from the cached record
    Check(Stage.APPLIES, learner_excluded),            # learner_not_in_enterprise / _group
    Check(Stage.APPLIES, content_not_in_catalog),
    Check(Stage.APPLIES, no_assignment, applies_to=uses_assignments),
    Check(Stage.ALLOWED, past_enroll_by),
    Check(Stage.ALLOWED, subsidy_cannot_pay),          # the one remote call
    Check(Stage.ALLOWED, policy_spend_exceeded),
    Check(Stage.ALLOWED, learner_spend_exceeded, applies_to=spend_capped_without_bnr),
    Check(Stage.ALLOWED, learner_enrollments_exceeded, applies_to=enrollment_capped),
    Check(Stage.ALLOWED, assignment_not_allocated, applies_to=uses_assignments),
)

def evaluate(sorted_policies, request):
    failures, candidates = [], list(sorted_policies)
    for stage in Stage:
        survivors = []
        for policy in candidates:
            reason = first_failure(policy, stage, request)  # this stage's checks, in order
            if reason:
                failures.append((stage_of(reason), policy, reason))
            elif stage is Stage.ALLOWED:
                return policy, failures                     # first budget to pass everything
            else:
                survivors.append(policy)
        candidates = survivors
    return None, failures

def display_failure(failures):
    furthest = max(stage for stage, _, _ in failures)
    return next(f for f in failures if f[0] == furthest)
```

**Testing and verification**

Existing workflows must keep working, and the performance claims must be checked by tests, not
just measured once.

*Keeping existing workflows working*

* **The contract doesn't change.** `can_redeem` keeps returning
  `(redeemable, reason, existing_transactions)`, with the transactions from the remote call that
  `redeem` uses for idempotency. Every caller keeps working unmodified: the `can-redeem` and
  `redeem` endpoints, Browse & Request (`assignment_request_can_redeem`), the content-assignment
  admin redemption action, and `ForcedPolicyRedemption`.
* **The existing suite proves step 1.** The refactor keeps today's order, so the 24 existing
  `can_redeem` tests (5 model, 19 view) and their reason assertions must pass with no edits. Any
  edit there is a behaviour change and belongs in step 2.
* **Differential tests.** A ddt matrix of budget scenarios (type; live or expired; in the catalog
  or not; assignment state; limits reached or not) runs both the old and the new evaluation. It
  asserts the same redeemable budget and the same `display_reason`. The only exceptions are the
  cases listed under [Consequences](#consequences), and each of those gets its own assertion.
* **Workflow tests, end to end through the views:** `can-redeem` then `redeem` for each budget
  type, Browse & Request approval then redemption, accepting an assignment, forced redemption,
  and `credits_available`, which doesn't use `can_redeem` and must not change.
* **A per-user waffle flag.** Step 2 ships behind a `WaffleFlag`,
  `enterprise_access.staged_policy_evaluation`, off for everyone by default. This is a critical
  path, so the flag lets us validate the new evaluation for specific users before anyone else
  sees it (see *Validating behind the flag* below). Turning it off for
  everyone restores today's evaluation without a deploy. The old evaluation stays in the code
  until the flag has been on for everyone for a full release cycle.
* **Stage QA.** Seed one customer with an expired budget, a live assigned budget with nothing
  assigned, and a spend-capped budget at its limit, which reproduces ENT-12416. Confirm the
  learner sees the limit message, and that redeeming still works once they're under the limit.

*Proving the performance*

* **Call-count tests, in CI.** With the subsidy client mocked, each Big-O row becomes an assertion
  on the number of remote `can_redeem` calls per request: $`\Theta(K)`$ when the first
  applicable budget redeems, none when every budget has expired, $`P_{\text{app}}`$ per content
  key when the learner is blocked everywhere, and $`S_{\text{app}}`$ with the per-subsidy cache.
  A scaling test parameterised over $`P \in \{1, 5, 20\}`$ and $`K \in \{1, 3\}`$ asserts
  that adding expired budgets adds no remote calls. Any change that brings back a call per budget
  fails a test.
* **Query-count tests.** `CaptureQueriesContext` around a `can-redeem` request shows that
  moving the assignment lookup to stage 2 doesn't add database queries beyond the request-cached
  ones.
* **Repeatable replay.** The replica replay in [Performance](#performance) is written up with its
  read-only queries under `docs/references/`, and re-run for the same window after step 2, so the
  before and after numbers come from the same method.
* **Production metrics, by cohort.** Each `can-redeem` request records whether the flag was on
  for it, and how many remote `can_redeem` calls it made, as custom attributes
  (`set_custom_attribute`, already used in this repo). Datadog APM can then compare the flagged
  and unflagged cohorts side by side, at the same time and on the same traffic mix, instead of one
  week against another. Success means a mean of no more than about 1.6 remote calls per content
  key on the enroll path for the flagged cohort (the replay measured 1.49), with no rise in its
  5xx responses or `redeem` failures.
* **No synthetic load test.** The improvement is fewer calls per request, which the call-count
  tests prove deterministically and APM confirms under real traffic.

**Validating behind the flag**

The flag is evaluated against the user making the request, which gives two ways to validate a
specific learner's outcome in production:

* **Through a staff account.** Enable the flag for one staff user in Django admin. That user calls
  `can-redeem` with `lms_user_id` set to the learner, which staff are already allowed to do. The
  learner's eligibility is computed with the new evaluation, while the learner, who isn't
  flagged, keeps today's behaviour. This checks a real learner's result without exposing them to
  it, and can be compared directly with the same call made with the flag off.
* **For the learner themselves.** Enable the flag for the learner's user, so their course pages
  use the new evaluation. The learner from ENT-12416 is the natural first case: they should see
  the limit message, and redeem normally once they're under the limit.

Why a flag here, when `enterprise_access/toggles.py` deliberately uses switches for the learner
pathway pipeline:

* **Those switches guard a paid, unreleased pipeline.** A flag would have let anyone enable it
  with a `?flag=1` query string, and the pipeline's offline harness has no request to evaluate a
  flag against.
* **Neither risk applies here.** `WAFFLE_OVERRIDE` isn't enabled in this repo's settings, or in
  any edx repo that code search covers, so query strings can't turn the flag on. Both
  evaluations pick the same redeemable budget, so the flag changes the reason shown and the number
  of remote calls, not which budget pays.
* **Callers with no request stay on today's evaluation.** `WaffleFlag.is_enabled()` is off when
  there's no request, so anything that runs outside a request keeps the old path until the flag
  is removed. That includes forced redemptions run by the Celery task in
  `subsidy_access_policy/tasks.py`. The content-assignment admin action, and forced redemptions
  started from the Django admin, run inside the admin's request, so they follow that admin's
  flag.

**Rollout**

The implementation plan,
[`docs/plans/staged-policy-evaluation/impl-plan.md`](../plans/staged-policy-evaluation/impl-plan.md),
owns the execution details: each chunk's exit criteria, the test plan, ops readiness and the
rollout gates. It delivers this design as stacked PRs, each cut on one logical seam:

1. Characterization tests that pin today's `can_redeem` workflows and remote-call counts.
2. The flag, plus cohort and call-count attributes, so production collects a baseline first.
3. Refactor onto the ordered check table, keeping today's order.
4. Staged evaluation behind the flag.
5. The audit log line.
6. Call reduction: stop at the first redeemable budget, plus the per-subsidy cache.
7. The three missing messages, which wait on product copy.
8. Roll the flag out: one staff user, the learner from ENT-12416, the staff group, pilot groups,
   then everyone.
9. After a release cycle at 100%, remove the old evaluation and the flag.

`allocate` already runs live, applies, allowed, except that it checks the catalog before the
subsidy's expiry. `approve` and `can-request` already check expiry before the catalog. They can
share stages 1 and 2 later; their stage 3 amounts differ.

## Performance

**Overall improvement.** The staged evaluation makes far fewer calls to enterprise-subsidy, which
is where `can-redeem` spends its time. Replaying the last 30 days of production redemptions:

* **When the learner can enroll,** uncached remote `can_redeem` calls drop from about 4 per course
  run to about 1.5 (63% fewer, 69% with the per-subsidy cache). The 95th percentile drops from 10
  calls to 2.
* **When every budget blocks the learner,** calls drop by 22% (67% with the cache).
* **Customers whose budgets have all expired** make no remote calls at all.

The cost stops growing with every budget whose catalog contains the course, expired or not, and
grows only with the live budgets that apply, up to the first one that passes. Local work doesn't
change. The one added cost is a single cached lookup per request for customers with only one
budget. The rest of this section gives the Big-O comparison and how the numbers were measured.

### Big-O comparison

Remote calls dominate the cost of `can-redeem`, and local work is the same in both designs.
Evaluation is sequential, so time spent waiting on enterprise-subsidy grows with the number of
calls.

| Symbol | Meaning |
|---|---|
| $`K`$ | Content keys in the request. The course page sends every available run (`canRedeem(courseRunKeys)` in the learner portal). |
| $`P`$ | Active, unretired budgets the customer has, which is the set `can-redeem` evaluates. |
| $`P_{\text{cat}}`$ | Budgets whose catalog contains the content and whose enroll-by date hasn't passed. These reach the remote call today, expired or not. |
| $`P_{\text{live}}`$ | Budgets whose subsidy is active now. |
| $`P_{\text{app}}`$ | Live budgets that pass stage 2. |
| $`r`$ | Position, among the $`P_{\text{app}}`$ budgets in sort order, of the first one that passes stage 3 ($`r = P_{\text{app}}`$ when none does). |
| $`S`$, $`S_{\text{app}}`$ | Distinct subsidies among all $`P`$ budgets, and among the $`P_{\text{app}}`$ budgets. |
| $`\ell`$ | One round trip to enterprise-subsidy's `can_redeem`. |

| Cost per `can-redeem` request | Today | Three stages | Stages + per-subsidy cache |
|---|---|---|---|
| Uncached remote `can_redeem` calls | $`\Theta(K \cdot P_{\text{cat}})`$, at most $`O(K \cdot P)`$ | $`O(K \cdot \min(r, P_{\text{app}}))`$ | $`O(K \cdot \min(r, S_{\text{app}}))`$ |
| ...when the first applicable budget is redeemable | $`\Theta(K \cdot P_{\text{cat}})`$ | $`\Theta(K)`$ | $`\Theta(K)`$ |
| ...when every budget has expired | $`\Theta(K \cdot P_{\text{cat}})`$ | 0 | 0 |
| ...when the learner is blocked by every budget | $`\Theta(K \cdot P_{\text{cat}})`$ | $`\Theta(K \cdot P_{\text{app}})`$ | $`\Theta(K \cdot S_{\text{app}})`$ |
| Catalog lookups (TieredCache, remote on a miss) | $`\Theta(K \cdot P)`$ | $`\Theta(K \cdot P_{\text{live}})`$ | $`\Theta(K \cdot P_{\text{live}})`$ |
| Policy spend aggregates (request-cached per budget) | $`O(P_{\text{live}})`$ | $`O(P_{\text{app}})`$ | $`O(P_{\text{app}})`$ |
| Subsidy-record lookups (request-cached per subsidy) | $`\Theta(S)`$ when $`P \ge 2`$, none when $`P = 1`$ | $`\Theta(S)`$ | $`\Theta(S)`$ |
| Local work (sort and checks) | $`O(P \log P + K \cdot P)`$ | $`O(P \log P + K \cdot P)`$ | $`O(P \log P + K \cdot P)`$ |
| Choosing `display_reason` | $`O(P)`$ | $`O(P)`$ | $`O(P)`$ |

The dominant term improves by a factor of $`P_{\text{cat}} / \min(r, P_{\text{app}})`$: budgets
that contain the content, divided by the live, applicable budgets checked before one passes. Time
waiting on enterprise-subsidy falls in the same proportion, from
$`\Theta(K \cdot P_{\text{cat}} \cdot \ell)`$ to $`O(K \cdot \min(r, P_{\text{app}}) \cdot \ell)`$.

### Measured on production data

To check these bounds against real traffic, we replayed the last 30 days of production
redemptions through both designs using the read replicas:

* **Workload:** 3,246 committed redemptions from `enterprise_subsidy`, each treated as one
  evaluation of one content key at the redeeming customer.
* **Budgets:** each customer's current active, unretired budgets from `enterprise_access`, with
  subsidy liveness from `enterprise_subsidy`.
* **Catalog membership:** from `enterprise_catalog`, following `get_matching_content`: a
  catalog contains a run if it holds the run itself, the run's course, or (for a course key) one
  of its runs.

| Remote `can_redeem` calls per content key | Today | Three stages | Stages + per-subsidy cache |
|---|---|---|---|
| Learner can enroll: mean / p95 / max | 4.02 / 10 / 22 | 1.49 / 2 / 7 | 1.26 / 2 / 7 |
| Learner can enroll: total over the sample | 12,703 | 4,717 (−63%) | 3,985 (−69%) |
| Learner blocked by every budget: total over the sample | 12,703 | 9,956 (−22%) | 4,236 (−67%) |

* Stopping at the first redeemable budget alone, without stage 1, gives 7,363 (−42%). Skipping
  expired budgets supplies the rest.
* 2,747 of today's 12,703 calls (22%) go to expired budgets, which can only answer "inactive".
* The stages make fewer calls in 48% of evaluations. In the rest both make the same number,
  usually one: the median evaluation has a single budget that contains the course.
* Across all customers: 998 evaluated budgets at 527 customers, 594 of them (60%) on expired
  subsidies. 273 customers have only expired budgets, so their evaluations make no remote calls
  under the stages. The largest customer has 22 budgets, 7 of them live.
* Spot check against ENT-12416: for every course that customer's learners redeemed, today makes
  4 calls, 3 of them to expired budgets. The stages make 1.
* On the enroll path, waiting on enterprise-subsidy drops from about $`4.02\,\ell`$ to
  $`1.49\,\ell`$ per content key, times $`K`$ per request. $`\ell`$ wasn't measured here; Datadog APM
  spans for the `can_redeem` client call can confirm it after rollout.
* Customers with a single budget (286 of 527) are the exception. The sort skips the subsidy
  record for them today, so stage 1 adds one request-cached lookup per request: $`\Theta(1)`$, not $`\Theta(K)`$.
  For a live budget that's one extra call per request. For an expired one, it replaces $`K`$ remote
  calls with that one lookup. Caching the subsidy's active window across requests would remove
  it.

Limitations of the measurement:

* Redemptions stand in for `can-redeem` traffic, so the workload leans toward learners who could
  enroll and toward popular courses. Course page views that don't end in an enrollment aren't in
  the replicas.
* Assigned budgets count as reaching stage 3 even when the learner may have no assignment there,
  so the measured savings are a lower bound.
* Enroll-by deadlines and restricted runs are ignored, and membership is computed from the
  replica tables rather than through the enterprise-catalog API.
* Current budget and subsidy state is applied to the last 30 days. 89 of the 3,246 redemptions
  (2.7%) were left out because the budget that redeemed is no longer active.
* Counts are per content key. Multiply by $`K`$ for a whole request.

## Consequences

### Benefits

* **Messaging.** A learner blocked by a live budget sees that budget's reason, such as
  `learner_max_spend_reached`, instead of an expired or inapplicable budget's reason. Section 3
  delivers this now; section 5 keeps the outcome and makes it follow from the evaluation order.
* **Performance.** On the last 30 days of production redemptions, section 5 cuts uncached remote
  calls to enterprise-subsidy by 63% (69% with the per-subsidy cache) for learners who can enroll,
  and by 22% (67%) for learners blocked by every budget. Customers whose budgets have all expired
  make none. The best case improves from $`\Theta(K \cdot P_{\text{cat}})`$ to $`\Theta(K)`$.
* **Maintainability.** One ordered check table is the logic, the priority and the documentation,
  and the catalogue in section 2 can be generated from it. Budget types declare which checks
  apply to them instead of overriding `can_redeem`.
* **Auditability.** One rule explains every reason shown, and one log line per evaluation records
  it.
* **Consistency.** `approve` and `can-request` already check expiry before the catalog. The
  stages bring `can_redeem`, and later `allocate`, in line with them.

### Trade-offs and risks

* **A budget's own reason changes when it fails more than one check:**
  * An expired budget reports `subsidy_expired`, never `content_not_in_catalog` or
    `beyond_enrollment_deadline`. Today those checks run first.
  * An assigned budget with no assignment reports `reason_learner_not_assigned_content` instead
    of a deadline or funds reason.
  * `redeem`, which evaluates one budget, returns its reason in the same new order.

  The `reasons` list keeps its shape and order, and the budget a learner redeems with doesn't
  change.
* **Unknown subsidy state.** Treating a failed subsidy lookup as expired would show "your funds
  expired" during an enterprise-subsidy outage, so stage 1 skips a budget only on an explicit
  `is_active` of `False`.
* **Single-budget customers** pay one extra request-cached subsidy lookup per request (see
  [Performance](#performance)).
* **Assignment lookups move earlier.** Assigned budgets look up the learner's assignment in
  stage 2, before the remote call. A few more cheap, request-cached database queries run for
  budgets that today fail a funds check first.
* **Fewer per-budget log lines.** Budgets after the first redeemable one aren't evaluated, so
  their `[can_redeem]` log lines disappear. The audit log line replaces them.
* **Caching scope.** The per-subsidy cache must stay request-scoped and must never feed `redeem`'s
  authoritative check.
* **Two evaluations in the code for a while.** The refactor keeps today's order inside the new
  table, and the old evaluation stays behind the flag until the cleanup step. Keep that window short, one
  release cycle after the flag reaches everyone, so the two don't drift from each other or from
  this ADR.
* **A flagged learner can see a different reason from an unflagged one** for the same budgets
  while the rollout is partial. That's the point of the flag, but support should know which
  cohort a learner is in when explaining a message; the custom attribute records it.
* **Choosing a stage is a product decision.** A new reason needs a stage, and that choice
  decides when learners see it.

### Known gaps, not addressed by this ADR

* Three slugs have no learner-facing message (section 4). With the ranking, an expired or
  reversed assignment can now be chosen over an expired budget's message, so these are worth
  filling in.
* `policy_expired` reads "doesn't have enough funds", not "expired".
* Active, unretired budgets funded by expired subsidies are still evaluated on every
  `can-redeem` call; section 5 skips them before any remote call. Retiring them is an operational clean-up
  that also fixes the message for a customer immediately.
* `per_learner_spend_limit` isn't checked on the BnR path: with BnR on, `can_redeem` runs as
  an assigned budget and `can_allocate_all` checks only the subsidy balance and
  `spend_limit`.
* For a learner's own request, `can-redeem` passes `skip_customer_user_check=True`
  (`evaluate_policies`), so `learner_not_in_enterprise` and
  `learner_not_in_enterprise_group` only come from `redeem` or a staff `lms_user_id`
  override. `can-redeem` doesn't reflect a budget's group restriction for the learner.
* `learner_max_enrollments_reached` counts every transaction for the learner, including
  reversed ones, so a refunded enrollment still uses up the enrollment limit.

## Alternatives Considered

### For choosing the reason shown (section 3)

* **Skip only expiration reasons.** Smaller, and it fixes a customer whose only other budget is
  browse-and-enroll. It doesn't fix a customer with a live assigned budget: assigned budgets sort
  first, so "this course is not assigned to you" would still mask the per-learner limit.
* **Stop evaluating budgets on expired subsidies.** Removes the masking at the source, but then a
  customer whose budgets have *all* expired would get no reason at all, instead of the correct
  "your funds expired". Section 5 gets the performance benefit of this without losing that
  message.
* **Re-sort policies by liveness before evaluating.** Changes which budget redeems when several
  are redeemable, which is a larger behavioural change than choosing a message.

### For evaluating budgets (section 5)

* **Keep only the ranking (section 3).** No further code change. But the check order and the
  ranking stay separate, and every expired budget still costs a remote call.
* **Move the expiry check to the top of `can_redeem`, without a check table.** A small diff that
  captures most of the expired-budget saving. But the assignment check stays after the funds
  checks (it lives in the subclass, after `super()`), the two orderings remain, and there's still
  no single place to audit.
* **Run the remote calls in parallel.** Cuts wall-clock time to about one round trip per stage,
  but not the number of calls or the load on enterprise-subsidy, and it adds concurrency inside a
  Django request. Complementary to section 5.
* **A batch `can_redeem` endpoint in enterprise-subsidy.** One round trip for every budget and
  run, but it's a cross-service API change. Also complementary.
* **Use the cached subsidy record's balance instead of the remote call.** Removes uncached calls
  from `can-redeem` entirely. But the displayed result could then disagree with `redeem`'s
  authoritative check, and we haven't verified that the two use the same price source.
* **A rules-engine library.** More machinery than an ordered tuple of checks needs.

## References

* ADR 0004 — add access policy functionality
* ADR 0021 — transaction aggregates (per-learner spend, net of committed reversals)
* [ENT-12416](https://2u-internal.atlassian.net/browse/ENT-12416) — the learner support request that prompted this
* `subsidy_access_policy/constants.py` — `PolicyTypes`, the `REASON_*` slugs and
  `MissingSubsidyAccessReasonUserMessages`
* `subsidy_access_policy/models.py` — `SubsidyAccessPolicy.can_redeem` and the per-type
  overrides, `can_allocate`, `can_allocate_all`, `can_approve`
* `api/v1/views/subsidy_access_policy.py` — `evaluate_policies`,
  `_get_reasons_for_no_redeemable_policies`, `_display_reason_rank`, `can_request`
* `subsidy_access_policy/utils.py` — `sort_subsidy_access_policies_for_redemption`
* `subsidy_access_policy/subsidy_api.py` — `get_redemptions_by_content_and_policy_for_learner`
  (one transactions call per subsidy)
* [edx/enterprise-access#326](https://github.com/edx/enterprise-access/pull/326) — the
  `display_reason` ranking in section 3
* enterprise-catalog `EnterpriseCatalog.get_matching_content` — the catalog membership rule used
  in the measurement
* frontend-app-learner-portal-enterprise `src/components/app/data/queries/queries.ts` — the course
  page sends every available run to `can-redeem`
