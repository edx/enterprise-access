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
| 200 | CSV streamed through from enterprise-subsidy. `Content-Type` and `Content-Disposition` are passed through. |
| 400 | Missing/invalid params. Validated **before** the upstream call, so a typo is never reported as an outage. |
| 401 / 403 | Not authenticated / lacks `SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION` for `enterprise_customer_uuid`. |
| 404 | No policy links `subsidy_uuid` (and `subsidy_access_policy_uuid`, if given) to `enterprise_customer_uuid`. |
| 429 | The same user has exported more than 12 times in the past hour (`learner_credit_transactions_export` throttle scope). |
| 502 | Any upstream failure. Only a generic message is returned; upstream bodies are never passed through. |

## Gotchas

- **Why 404 and not 403 for another enterprise's subsidy:** the permission check only covers
  `enterprise_customer_uuid`, and enterprise-subsidy is called with all-access operator credentials. The view proves
  the subsidy belongs to the enterprise by looking for a `SubsidyAccessPolicy` with both values, and answers 404 so it
  doesn't reveal that another customer's subsidy exists.
- **Dedicated permission, shared roles:** `SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION` has its own
  permission name so it can be narrowed later, but it is **not** yet independent of other features: it reuses the
  same predicate as `SUBSIDY_ACCESS_POLICY_ALLOCATION_PERMISSION` (content-assignment admins/operators and policy
  operators), so anyone holding `CONTENT_ASSIGNMENTS_ADMIN_ROLE` — including via an explicit database role
  assignment — can export learner emails. Enterprise admins only reach it that way, because
  `SYSTEM_ENTERPRISE_ADMIN_ROLE` maps to the policy *learner* role and there is no policy-admin feature role.
  Decoupling properly needs a dedicated feature role in `SYSTEM_TO_FEATURE_ROLE_MAPPING`.
- **Streaming:** the upstream response is opened with `stream=True` and relayed in chunks. It is always closed:
  on upstream error statuses (in `get_subsidy_transactions_export`), when the client aborts, and when the stream
  fails partway through. A mid-stream failure is logged and re-raised so the download aborts instead of looking
  complete.
- **Timeout:** `OAuthAPIClient` only applies a timeout to its token fetch, so the export call sets
  `SUBSIDY_TRANSACTIONS_EXPORT_TIMEOUT` explicitly. This is requests' *read* timeout, which bounds the gap
  between two reads rather than the total download, so a slow but steady stream can still outlive it. With
  synchronous workers each download occupies a worker for its whole duration, and a download longer than the
  deployed worker timeout is killed, giving the admin a truncated file. The endpoint is throttled
  (`learner_credit_transactions_export`, 12/hour), but note `ScopedRateThrottle` keys on the user, so it caps how
  often one admin can export rather than how many exports run at once.
- **Streaming and the upstream connection:** the upstream response is relayed by `UpstreamCsvStream`, whose
  `close()` Django registers as a resource closer, so the connection is released even if the response is
  discarded before its first chunk. `Content-Length` is not forwarded: the upstream streams its response and
  never sends one. `Cache-Control: no-store` is set because the file contains learner emails.
- **Content negotiation:** the view renders `text/csv` as well as JSON, so a client sending
  `Accept: text/csv` is not refused with a 406 during negotiation.
- **Logging:** the `search` value never reaches the logs, because upstream matches it against learner emails;
  the audit line records only whether a search was used. Upstream failures are logged without a traceback
  (no `logger.exception`), because the chained `requests` error's message includes the full upstream URL, query
  string and `search` included.
- **Audit:** each export logs the requesting user id, enterprise, subsidy and policy.
