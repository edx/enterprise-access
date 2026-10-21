# Learner Credit Top-Ups

## Overview
Enterprise admins will be able to add funds ("top up") to an existing Learner Credit budget from the admin portal.
Introduced for **ENT-12356**. Today only the **eligibility check** exists: it tells the portal which budgets may be
topped up and, when one can't, why. The quote (chunk 7) and pay (chunk 11) endpoints will be added to the same
viewset and must apply the same rules.

The rules live in one place, `enterprise_access/apps/customer_billing/api.py`:

- `evaluate_top_up_eligibility_for_policies(policies)` fetches each budget's subsidy record and evaluates it. Quote
  and pay must call this function rather than reimplementing the checks, so the portal and the backend always agree.
- `evaluate_top_up_eligibility(policy, subsidy)` applies the rules to one budget whose subsidy is already fetched.
- `is_top_up_enabled()` reads the feature flag.

Reason codes and error codes are in `customer_billing/constants.py` (`TOP_UP_INELIGIBILITY_REASONS`,
`TOP_UP_ERRORS`).

## Endpoint

`GET /api/v1/learner-credit-top-ups/eligibility/`

| Param | Required | Validation | Meaning |
|---|---|---|---|
| `enterprise_customer_uuid` | yes | UUID; also the edx-rbac context | The enterprise whose budgets are evaluated. |
| `policy_uuid` | no | UUID; must belong to the enterprise | Evaluate only this budget. |

- **Without `policy_uuid`:** returns every policy of the enterprise with `active=True`, oldest first. A retired
  policy that is still `active=True` is listed and reports `budget_not_active`.
- **With `policy_uuid`:** that budget is always evaluated, even if inactive, so the portal can show why it can't be
  topped up.

Ineligibility is a normal answer, not an error: it returns **200** with reasons.

```json
{
  "results": [
    {
      "policy_uuid": "7983a1ed-7a59-4bcd-9f75-c6896b628395",
      "is_eligible": false,
      "reasons": [
        {"error_code": "budget_expiring_soon", "message": "The budget expires in fewer than 14 days."}
      ],
      "subsidy_expiration_datetime": "2026-11-03T11:38:00Z"
    }
  ]
}
```

### Permissions
`customer_billing.learner_credit_top_up` is granted to the same roles as billing management:

- `CUSTOMER_BILLING_ADMIN_ROLE`: enterprise admins, for their own enterprise.
- `CUSTOMER_BILLING_OPERATOR_ROLE`: enterprise operators and provisioning admins, for every enterprise.

## Reason codes

A budget is eligible only when `reasons` is empty. Every failing rule is reported, in this order:

| `error_code` | When |
|---|---|
| `budget_not_active` | The policy is inactive or retired, the subsidy is not active (not started yet, or expired), or the subsidy answered 403/404 (see below). |
| `budget_expiring_soon` | Fewer than `TOP_UP_MIN_DAYS_BEFORE_EXPIRY` (14) days remain before the subsidy expires. Exactly 14 days still passes. An expired subsidy reports this as well as `budget_not_active`. |
| `account_not_eligible` | The account standing / agreement type check fails. Always passes today (see OQ-8). |

## Responses

| Status | Body | When |
|---|---|---|
| 200 | `{"results": [...]}` | Eligible or not, see the reason codes. |
| 400 | `{"<param>": ["Must be a valid UUID."]}` | `policy_uuid` is not a UUID, or `enterprise_customer_uuid` isn't one and the caller is an operator. |
| 401 | | Not authenticated. Checked before the feature flag. |
| 403 | `{"detail": "You do not have permission to perform this action."}` | No permission for `enterprise_customer_uuid`, including when it is missing, or malformed for an enterprise admin. |
| 404 | `{"detail": "Not found."}` | The feature flag is off, for every role. |
| 404 | `{"error_code": "budget_not_found", "detail": "Budget <policy_uuid> was not found for enterprise <uuid>."}` | `policy_uuid` doesn't exist or belongs to another enterprise. |
| 404 | `{"error_code": "no_budgets_found", "detail": "No active budgets were found for enterprise with UUID <uuid>."}` | The enterprise has no active budgets. Operators can query any enterprise, so this is also what a mistyped `enterprise_customer_uuid` returns for them. |
| 503 | `{"detail": "Could not verify subsidies with the Subsidy API. Please try again later."}` | enterprise-subsidy is failing (see below). Generic `SubsidyAPIUnavailableException` body; upstream details never reach the client. |

## One unreachable subsidy vs. an outage

Each budget's subsidy is read with `policy.subsidy_record(raise_on_error=True)`, following
`docs/architecture-patterns.md` §18 and the same shape as `get_policies_accepting_requests()`:

| enterprise-subsidy answers | Result |
|---|---|
| 403 or 404 for **some** subsidies | Only those budgets report `budget_not_active`; the rest are evaluated normally (200). A soft-deleted subsidy answers 403, not 404. |
| 403 or 404 for **every** listed subsidy | 503. That looks the same as our own credentials losing their role, so it is treated as an outage rather than reported as "nothing active". |
| 403 or 404 for an explicitly requested `policy_uuid` | 200 with `budget_not_active`, since a deleted subsidy would otherwise return a 503 forever. |
| 5xx, connection error or timeout | 503, even if other budgets are healthy. |

Never report a failed lookup as an inactive budget in any other case: during an outage every budget would look
inactive. `subsidy_record()` without `raise_on_error` swallows HTTP errors into `{}`, which is why it isn't used.

## Feature flag

`FEATURE_TOP_UP_LEARNER_CREDIT`, a Django setting read on every request by `is_top_up_enabled()`. The URL is always
registered; when the setting is off every role gets a 404.

| Where | Value |
|---|---|
| `settings/base.py` | `False` (the default for every environment) |
| `settings/devstack.py` | `True` |
| Stage / prod | Set in edx-internal. Must stay `False` until chunk 19 (see below). |

Changing it needs a config change and a deploy, so turning it off again is not instant. Tests use
`@override_settings(FEATURE_TOP_UP_LEARNER_CREDIT=True)`.

## Open decisions

- **KD-6: global vs. per-enterprise flag.** Today the flag is a single global setting fixed at deploy time.
  `is_top_up_enabled()` is the only place it is read, so a per-enterprise version would change only that function
  and its callers.
- **OQ-8: account standing source.** There is no account-standing data source yet, so `account_not_eligible` is
  never reported (`is_account_in_good_standing()` always returns True). Chunk 19 replaces it.
- **Until both are settled, the flag is the only production safeguard:** it must stay off in stage and prod until
  chunk 19.
- **Pay permission (before chunk 11):** `customer_billing.learner_credit_top_up` will also cover pay, and it is
  granted to provisioning admins through `CUSTOMER_BILLING_OPERATOR_ROLE`. Whether pay needs a narrower permission
  is undecided.
