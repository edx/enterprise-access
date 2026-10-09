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

- **404, not 403, for another enterprise's subsidy:** the permission check only covers `enterprise_customer_uuid`,
  and enterprise-subsidy is called with all-access credentials. The view proves the subsidy belongs to the
  enterprise by finding a `SubsidyAccessPolicy` with both values, and answers 404 so it doesn't reveal that another
  customer's subsidy exists.
- **Shared roles:** `SUBSIDY_ACCESS_POLICY_TRANSACTIONS_EXPORT_PERMISSION` reuses the predicate of
  `SUBSIDY_ACCESS_POLICY_ALLOCATION_PERMISSION`, so `CONTENT_ASSIGNMENTS_ADMIN_ROLE` (including an explicit database
  assignment) can export learner emails. Enterprise admins get it that way, because there is no policy-admin
  feature role. Narrowing it needs a dedicated feature role in `SYSTEM_TO_FEATURE_ROLE_MAPPING`.
- **Streaming:** the upstream response (`stream=True`) is relayed by `UpstreamCsvStream`, whose `close()` Django
  registers, so the connection is released on upstream errors, when the client aborts, and even if the response is
  discarded before its first chunk. A mid-stream failure is logged and re-raised so the download aborts instead of
  looking complete. `Content-Length` is never forwarded, and `Cache-Control: no-store` is set (learner emails).
- **Timeout and workers:** `OAuthAPIClient` only times out its token fetch, so the call sets
  `SUBSIDY_TRANSACTIONS_EXPORT_TIMEOUT`: a 10s connect timeout, so an unreachable upstream fails fast, and a
  120s *read* timeout between chunks rather than for the whole download. Each
  download holds a synchronous worker throughout, and one longer than the worker timeout is truncated. The
  12/hour throttle is per user, so it limits how often one admin exports, not how many exports run at once.
- **Content negotiation:** `text/csv` is renderable, so `Accept: text/csv` isn't refused with a 406.
- **Logging:** each export is audit-logged with the user id, enterprise, subsidy and policy. This code never logs
  the `search` value (it may be a learner email), and logs upstream failures without a traceback, since the chained
  `requests` error names the full upstream URL. Request URLs, query string included, still appear in web-server
  access logs and DEBUG-level `urllib3` logs, as for any GET endpoint.
