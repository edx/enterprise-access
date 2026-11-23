# Self-service annual renewals (Teams and Essentials)

When Stripe bills a paid self-service subscription for its next year, enterprise-access moves the customer
onto next year's License Manager plan. The code is in `customer_billing/stripe_event_handlers.py`.

## Feature switch

All of this is behind the `enterprise_access.ssp_paid_renewal` WaffleSwitch (`enterprise_access/toggles.py`,
off by default; `FEATURE_SSP_PAID_RENEWAL` in the tech spec). With it off, a paid renewal invoice only sends the
payment receipt and the renewal notice and logs an error, `invoice.created` and `invoice.payment_failed` don't
touch year 1, and `past_due` deactivates every renewal's plan, as before this feature. Turning it off is the
rollback.

## When an invoice is an annual renewal

`_is_annual_renewal_invoice()` returns true for an invoice with `billing_reason == 'subscription_cycle'`
when the same Stripe subscription already has an earlier `invoice.paid` that charged money. The first paid
invoice at trial end also has `subscription_cycle`, but no earlier paid invoice, so it still goes through
the trial->paid path.

## Event flow

| Event | What happens |
| --- | --- |
| `invoice.created` (renewal) | Year 1 is extended to `period.start + 7 days`. Year 1 ends at the period start, before the invoice can be paid. |
| `invoice.payment_failed` (renewal) | The same extension. It is an absolute date, so Stripe's retries don't keep pushing it out. Sets `CheckoutIntent.in_renewal_grace_period = True`. |
| `customer.subscription.updated` -> `past_due` | Only renewals that haven't been processed get their plans deactivated (`cancel_all_future_plans(include_processed=False)`). Year 1 stays active for the grace period. |
| `invoice.paid` (renewal) | `_process_annual_renewal()` runs (see below). Once year 2 exists, `in_renewal_grace_period` is cleared. |
| `customer.subscription.deleted` | Clears `in_renewal_grace_period` (Stripe cancels the subscription after its retries of a failed renewal charge run out). |

`CheckoutIntent.in_renewal_grace_period` marks the rare case of a failed renewal charge: the customer is in the
grace week, still on year 1. `invoice.created` alone (the normal short gap before payment) doesn't set it.

The flag is only cleared by a processed renewal or `customer.subscription.deleted`. If Stripe's dunning
settings end a failed subscription as `unpaid` or leave it `past_due` instead of canceling it, the flag stays
`True` after year 1 has expired. Anything that reads it (such as a grace banner) should also check that the
grace week (`period.start + 7 days`) hasn't passed.

## Processing a paid renewal

1. `resolve_renewal_product()` checks the invoice's Stripe Prices: each one must resolve to the checkout
   intent's product and nothing else. There is **no fallback** to `teams-yearly`. If a Price resolves to no
   product, another product or more than one, it logs an `outcome=halted` audit line (see "Audit logs") and
   raises `AnnualRenewalError`, so Stripe retries. Inactive (grandfathered) products are accepted.
2. It creates a License Manager `SubscriptionPlanRenewal` with no renewed plan. `prior` is the renewed plan
   of the most recently processed `SelfServiceSubscriptionRenewal`. Effective date is the invoice line's
   `period.start` (year 1's original end date) and expiration is its `period.end` (Stripe's term end, which is
   also the next renewal invoice's `period.start`). The license count comes
   from the line quantity. The renewed plan's title is set by `_renewed_plan_title()` (see "Plan titles").
3. It creates a `SelfServiceSubscriptionRenewal` with `stripe_invoice_id`, then processes the renewal,
   activates year 2 and sets year 1's expiration back to the period start (undoing any grace extension).
4. Only then does it queue the payment receipt and the renewal notice, and then it marks the record processed.
   A failed attempt, including a broker error while queuing an email, raises before the processed checkpoint,
   so Stripe's retry sends the emails. A replay of a processed renewal sends nothing. A retry can repeat an email
   that was already queued: the receipt if queuing the notice failed, or both if the checkpoint save failed
   (see architecture pattern #27).

If `customer.subscription.deleted` has already canceled the subscription (its renewals are `is_canceled`, or a
`customer.subscription.deleted` summary exists for its Stripe subscription id), a renewal invoice paid or delivered
afterwards is not processed. The summary check matters because a stale `customer.subscription.updated` (active),
handled after the deletion, sets `is_canceled` back to False; a deleted Stripe subscription never comes back. Such an
invoice is not processed: it logs `outcome=halted` and the webhook succeeds,
since a retry can't change it. The customer may have paid, so CS refunds or reinstates by hand.
A late `invoice.created` or `invoice.payment_failed` for a canceled subscription doesn't extend year 1 or set
`in_renewal_grace_period` either (`outcome=grace_skipped_canceled`).

Only subscription item lines are read (`parent.type == 'subscription_item_details'`). The term line is the first
one that isn't a proration; one-off invoice items such as adjustments never set the seats, dates or product. An
invoice without a term line halts (`outcome=halted`). Stripe lists pending invoice items before the subscription
lines, so the webhook accepts an invoice as an SSP invoice if any line is a subscription item, not just the first.

Idempotency is keyed on `stripe_invoice_id`. A processed renewal is skipped. An unprocessed one resumes at
the processing step without creating another License Manager renewal. The resume sends the License Manager
renewal unchanged, so a renewal that fails on its data (see "Runbook") needs a manual fix before a retry can
succeed.

## Audit logs and monitoring

Every renewal step logs one line in a fixed format (`_log_renewal()`), for the audit trail and for Datadog
monitors:

```
[SSPAnnualRenewal] outcome=<outcome> checkout_intent_uuid=... enterprise_slug=... invoice_id=... <details>
```

| `outcome` | Level | Meaning |
|---|---|---|
| `halted` | ERROR | Renewal stopped on purpose (missing/mismatched product, no paid plan, ...). `reason=` says why. |
| `failed` | ERROR | Unexpected error (e.g. a License Manager API error). `step=` is `process` or `grace_extension`. |
| `switch_off` | ERROR | A renewal was paid but `ssp_paid_renewal` is off, so it must be handled by hand. |
| `created` | INFO | License Manager renewal created (`renewal_id`, `license_manager_renewal_id`, `product`, `licenses`). |
| `processed` | INFO | Year 2 is active (`prior_plan_uuid`, `renewed_plan_uuid`). |
| `already_processed` | INFO | Replayed `invoice.paid`, skipped. |
| `grace_extended` | INFO | Year 1 kept active until `expiration_date` (renewal invoice created or charge failed). |
| `grace_skipped_already_renewed` | INFO | Grace extension skipped, the invoice is already renewed. |
| `grace_skipped_canceled` | INFO | Grace extension skipped, the subscription is already canceled. |
| `grace_period_started` / `grace_period_ended` | INFO | `in_renewal_grace_period` was set / cleared. |

`halted`, `failed` and `switch_off` all mean the customer may have paid without getting next year's plan.
Stripe retries the webhook, so the same invoice can log them more than once. A monitor on
`"[SSPAnnualRenewal]" status:error` covers all three.

## Plan titles

License Manager requires plan titles to be **unique per customer agreement**. Processing a renewal whose
title is already taken fails with a 500 (a MySQL duplicate-key error), and the customer has paid but gets no
new plan.

Both products follow the ENT-12202 naming convention used for the plans Salesforce provisions, with the
renewal invoice's seat count and the month and year the renewed term **ends**:

- **Essentials:** `[Company Name] [# licenses] SUBS [Month YYYY] - Essentials [Academy Name] PAID`
- **Teams:** `[Company Name] [# licenses] SUBS [Month YYYY] - Teams PAID`

For example, a year 2 running Nov 2027 to Nov 2028 is `Acme 10 SUBS November 2028 - Teams PAID`. The month
and year make each term unique. The end month matches what the admin portal shows: for Essentials it builds
this title itself from the Stripe billing period and ignores the License Manager title; for Teams it shows the
License Manager title. Without a title, License Manager would default to `{prior title} - Renewal {year}`,
which grows each year (`Acme - Renewal 2027 - Renewal 2028`).

## Runbook

Each case below logs `outcome=failed step=process` on every Stripe retry. The customer has paid, and year 1
expires at `period.start + 7 days`, so fix it within the grace week. After the fix, resend the `invoice.paid`
event from the Stripe dashboard (or wait for Stripe's next retry). The retry resumes the existing License
Manager renewal.

- **Duplicate plan title** (License Manager 500, MySQL duplicate-key error on the title). A plan with the same
  title already exists under the customer agreement, e.g. one created by hand. Rename the conflicting plan in
  the License Manager Django admin (subscription plans), or change the renewal's `renewed_plan_title` through
  the API below.
- **"Cannot renew for fewer than the number of original activated licenses"** (License Manager 500). The
  renewal invoice's seat count is below the licenses year 1 copies forward (assigned plus activated). Either
  the customer frees up licenses, or CS raises the renewal's `number_of_licenses` through the API below and
  adjusts the Stripe subscription quantity to match.

Change an SSP renewal with `PATCH /api/v1/provisioning-admins/subscription-plan-renewals/<id>/` on License
Manager, where `<id>` is the `license_manager_renewal_id` in the `outcome=created` audit line. For example,
`{"renewed_plan_title": "..."}` or `{"number_of_licenses": 15}`. It needs the `enterprise_provisioning_admin`
role, and accepts a JWT or a logged-in session (the License Manager API docs page). **Don't use the License
Manager Django admin to edit the renewal:** its form rejects every SSP renewal during the grace week, because
the effective date is in the past and before year 1's extended expiration, and SSP renewals have no
Salesforce opportunity ID.

`invoice.created` and `invoice.payment_failed` with no processed paid plan to keep active log
`outcome=halted` but don't fail the webhook, since a retry can't fix that. `invoice.paid` halts on the same
problem and does fail, so it keeps alerting.

## Gotchas

- **Invoice lines have no `price` object.** Since Stripe API `2025-03-31.basil`, a line only carries the
  Price ID under `pricing.price_details.price`. To read the `ssp_product_slug` metadata, the Price is
  fetched with `stripe.Price.retrieve`. A Price without that metadata is matched to an `SspProduct` by
  `stripe_price_lookup_key`.
- **License Manager builds year 2 from year 1.** When `renewed_subscription_plan` is null, processing
  copies year 1's `product_id`, `enterprise_catalog_uuid` (so the same catalog query and academy
  association) and `is_active`. The renewal create API has no product field; unknown fields are dropped
  without an error.
- **Provisioning doesn't link invoices.** `GetCreateSubscriptionPlanRenewalStep` leaves `stripe_invoice_id`
  empty: at provisioning the latest `invoice.paid` is the $0 trial invoice, and linking it would stop
  `invoice.created` from linking the real trial->paid invoice (tech spec KD-17). Annual renewals set the field
  when they create their row from the paid invoice.
