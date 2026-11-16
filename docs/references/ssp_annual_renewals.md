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

## Processing a paid renewal

1. `resolve_renewal_product()` finds the `SspProduct` from the invoice's Stripe Price. It must be the
   checkout intent's product. There is **no fallback** to `teams-yearly`. If the product is missing,
   unknown, ambiguous or mismatched, it logs an `outcome=halted` audit line (see "Audit logs") and
   raises `RenewalProductError`, so Stripe retries. Inactive (grandfathered) products are accepted.
2. It creates a License Manager `SubscriptionPlanRenewal` with no renewed plan. `prior` is the renewed plan
   of the most recently processed `SelfServiceSubscriptionRenewal`. Effective date is the invoice line's
   `period.start` (year 1's original end date) and expiration is one year later. The license count comes
   from the line quantity. The renewed plan's title is set by `_renewed_plan_title()` (see "Plan titles").
3. It creates a `SelfServiceSubscriptionRenewal` with `stripe_invoice_id`, then processes the renewal,
   activates year 2, sets year 1's expiration back to the period start (undoing any grace extension) and
   marks the record processed.
4. Only then does it send the payment receipt and the renewal notice. A failed attempt raises before the
   emails, so Stripe's retry sends them once, and a replay of a processed renewal sends nothing.

If `customer.subscription.deleted` has already canceled the subscription (its renewals are `is_canceled`), a
renewal invoice paid or delivered afterwards is not processed: it logs `outcome=halted` and the webhook succeeds,
since a retry can't change it. The customer may have paid, so CS refunds or reinstates by hand.

Only subscription item lines are read (`parent.type == 'subscription_item_details'`). The term line is the first
one that isn't a proration; one-off invoice items such as adjustments never set the seats, dates or product.

Idempotency is keyed on `stripe_invoice_id`. A processed renewal is skipped. An unprocessed one resumes at
the processing step without creating another License Manager renewal. The resume first sets the current
title on that License Manager renewal, so a renewal License Manager rejected over its title can
complete on Stripe's retry.

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
| `title_updated` | INFO | A retried Essentials renewal had its License Manager title reset before processing. |
| `processed` | INFO | Year 2 is active (`prior_plan_uuid`, `renewed_plan_uuid`). |
| `already_processed` | INFO | Replayed `invoice.paid`, skipped. |
| `grace_extended` | INFO | Year 1 kept active until `expiration_date` (renewal invoice created or charge failed). |
| `grace_skipped_already_renewed` | INFO | Grace extension skipped, the invoice is already renewed. |
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
