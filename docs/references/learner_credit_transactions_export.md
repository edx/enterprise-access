# Learner Credit Spend (Transactions) CSV Export

## Overview
Enterprise admins can download a subsidy's Learner Credit spend report (who spent, on what, how much, when) as a
CSV, instead of paging through the admin portal's "Spent" table. Introduced for **ENT-10608**.

enterprise-access does not build the report itself. It is an authenticated **gateway** in front of
enterprise-subsidy, which owns the transaction ledger and renders the CSV.

```
admin portal ──► enterprise-access                       ──► enterprise-subsidy
                 GET /api/v1/subsidy-access-policies/        GET
                     transactions/export/                    /api/v2/subsidies/<subsidy_uuid>/admin/transactions/export/
                                                             (called with this service's operator credentials)
```

## Cross-service contract

**Upstream dependency:** `GET /api/v2/subsidies/<subsidy_uuid>/admin/transactions/export/` in enterprise-subsidy
(edx/enterprise-subsidy#74), which requires admin-level access to the subsidy and is always called with the v2 client. The
enterprise-subsidy change must be deployed **before** this endpoint is used; until then every call fails upstream
and is returned as a 502.

| Param (enterprise-access) | Required | Validation here | Forwarded upstream as |
|---|---|---|---|
| `enterprise_customer_uuid` | yes | UUID | `enterprise_customer_uuid` (enterprise-subsidy also scopes on it) |
| `subsidy_uuid` | yes | UUID | URL path |
| `subsidy_access_policy_uuid` | no | UUID; must belong to the enterprise + subsidy | `subsidy_access_policy_uuid` |
| `search` | no | max 320 chars | `search` |
| `start_date` | no | `YYYY-MM-DD` | `start_date` (ISO date) |
| `end_date` | no | `YYYY-MM-DD`, on/after `start_date` | `end_date` (ISO date, inclusive) |

Dates are interpreted in UTC by enterprise-subsidy.

## Responses

| Status | When |
|---|---|
| 200 | CSV streamed through from enterprise-subsidy. `Content-Type`, `Content-Disposition` and (when the upstream body isn't compressed) `Content-Length` are passed through. |
| 400 | Missing/invalid params. Validated **before** the upstream call, so a typo is never reported as an outage. |
| 401 / 403 | Not authenticated / lacks `SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION` for `enterprise_customer_uuid`. |
| 404 | No policy links `subsidy_uuid` (and `subsidy_access_policy_uuid`, if given) to `enterprise_customer_uuid`. |
| 502 | Any upstream failure. Only a generic message is returned; upstream bodies are never passed through. |

## Gotchas

- **Why 404 and not 403 for another enterprise's subsidy:** the permission check only covers
  `enterprise_customer_uuid`, and enterprise-subsidy is called with all-access operator credentials. The view proves
  the subsidy belongs to the enterprise by looking for a `SubsidyAccessPolicy` with both values, and answers 404 so it
  doesn't reveal that another customer's subsidy exists.
- **Dedicated permission:** `SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION` is granted to content-assignment
  admins/operators and policy operators (`core/rules.py`). Don't reuse another feature's permission (e.g. Browse &
  Request's `REQUESTS_ADMIN_ACCESS_PERMISSION`) for it, because the report contains learner emails.
- **Streaming:** the upstream response is opened with `stream=True` and relayed in chunks. It is always closed:
  on upstream error statuses (in `get_subsidy_transactions_export`), when the client aborts, and when the stream
  fails partway through. A mid-stream failure is logged and re-raised so the download aborts instead of looking
  complete.
- **Timeout:** `OAuthAPIClient` only applies a timeout to its token fetch, so the export call sets
  `SUBSIDY_TRANSACTIONS_EXPORT_TIMEOUT` explicitly. Keep its read timeout under gunicorn's worker timeout.
- **Audit:** each export logs the requesting user id, enterprise, subsidy and policy.
