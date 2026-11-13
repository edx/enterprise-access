# Checkout Segment events: `order_completed` and `order_cancelled`

Introduced for **ENT-12377** (Checkout Segment Events Refactor, phase 1). The server sends the two final checkout
events from the Stripe webhooks, so they are recorded even if the customer closes the browser before the success page
loads.

## What is sent, and when

| Event | Sent when | `order_id` | `revenue` |
|---|---|---|---|
| `edx.ui.enterprise.checkout.order_completed` | `invoice.paid` for the subscription's first invoice (`billing_reason` `subscription_create`), which Stripe collects at checkout. With the 14-day free trial its total is $0. | The purchase's Checkout Session ID (`CheckoutIntent.stripe_checkout_session_id`), the same value the checkout MFE sends | That invoice's total: $0 for a trial |
| `edx.ui.enterprise.checkout.order_cancelled` | `customer.subscription.deleted`, for every deletion, trials included | The order_id of the subscription's `order_completed` | The total of the same first invoice |

Trial-end and renewal invoices (`subscription_cycle`) and license changes (`subscription_update`) never send
`order_completed`.

## The setting (off by default)
`FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2` (a Django setting). Off: nothing is queued or sent. Turn it on in the
enterprise-access config in edx-internal (`argocd/applications/enterprise-access/<env>-config.yaml`). The checkout MFE
flag of the same name is configured separately (`frontends/frontend-app-enterprise-checkout/`) and does not turn this
one on. `SEGMENT_KEY` must also be set (it is in stage and prod).

The checkout MFE never sends `order_completed` or `order_cancelled`, so nothing on the browser side needs turning off.
The MFE sends the same `order_id` on `edx.ui.enterprise.checkout.checkout_step_completed.billing_details`. The
backend's `SEGMENT_KEY` is a different Segment source (write key) from the MFE's, so these two events arrive in the
enterprise-access source, not alongside the MFE's funnel events.

## How it is sent
* The webhook handlers queue `send_checkout_segment_event_task` (`customer_billing/tasks.py`). A broker error is not
  caught: the webhook fails and Stripe redelivers it (see pattern 25 in `docs/architecture-patterns.md`).
  * `invoice_paid`, $0 branch: queued after `mark_as_paid`, so a broker outage never holds up the checkout.
  * `invoice_paid`, paid branch: queued before the receipt email, so a redelivery doesn't send it twice.
  * `subscription_deleted`: queued before the cancellation work and emails.
* The task records a `SegmentEventEmission` row, unique per (event, order_id), so each event is sent at most once per
  order however often the task is queued. Status: PENDING, then SENT or FAILED.
* The row is locked while sending. `track.segment.send_event_synchronously` posts the event in the task with a 10s
  timeout and raises unless Segment returns 200, unlike `track_event`, whose background thread drops failures. The
  Segment `messageId` is `<event>:<order_id>`, so Segment drops a resend after a success that wasn't recorded.
* Retried (`LoggedTaskWithRetry`, about half an hour): Stripe network errors, 429 and 5xx; Segment network errors,
  429 and 5xx; DB `OperationalError`. The row stays PENDING. If Celery gives up, queue the task again by hand with
  the same arguments.
* Set to FAILED, never sent: a required property is missing, Stripe rejects the request (`InvalidRequestError`), or
  Segment rejects it (another 4xx).

## Payload
No PII: payloads are built only from the fields below, and a test lists every allowed key.

| Properties | Source |
|---|---|
| `product_id`, `price` (unit price), `payment_schedule` | The Stripe Price: for `order_completed`, the price of the invoice's subscription item line (a trial line's amounts are the trial's, not the price); for `order_cancelled`, the cancelled subscription's item from the webhook (its final state, after license changes) |
| `total_quantity` | The invoice's subscription item line, or the cancelled subscription's item |
| `revenue`, `currency` | The order's first invoice |
| `sku`, `category`, `name`, `brand`, `variant`, `slug` | The CheckoutIntent's `SspProduct` |
| `utm_*`, `referrer` | `CheckoutIntent.attribution`, on `order_completed` only |

`order_id`, `product_id`, `price`, `payment_schedule`, `total_quantity`, `revenue` and `currency` are required. An
event missing one is set to FAILED rather than sent incomplete.

## Attribution
The CheckoutIntent create API accepts `attribution`. `customer_billing.utils.sanitize_attribution` keeps only
`utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term` and `referrer`, with string values of at most 100
characters:
* UTM values must be campaign labels: letters, digits, spaces and `_ - . + | ,`, so email addresses, URLs and query
  strings are dropped. Values with 10 or more digits (phone numbers) are dropped too.
* The referrer is reduced to its origin.
* Dropped values are logged by key, never by value.

A create call that sends attribution replaces the intent's attribution whole (never merged), matching the checkout
MFE, where a later link with UTM values replaces what it stored. A call without attribution, or whose values are all
dropped, keeps the existing one. Attribution never appears in API responses or in the CheckoutIntent lifecycle event.
The checkout MFE doesn't send it yet (follow-up).

## Gotchas
* stripe-python 16 returns `unit_amount_decimal` as a `Decimal`. The cancelled subscription's items reach the task
  through kombu's JSON serializer, which round-trips `Decimal`.
* The CheckoutIntent's Checkout Session ID is overwritten if the customer starts another checkout after paying.
  That's why `order_cancelled` takes its order_id and invoice from the `order_completed` row.
* An order placed before the setting was on has no `order_completed` row. Its `order_cancelled` reads the first
  invoice from the Checkout Session's `invoice` field.
* Before turning the setting on, check one stage trial checkout in the Segment debugger. This confirms that the trial
  invoice's subscription line carries `pricing.price_details.price` and that `order_cancelled` finds the invoice.
