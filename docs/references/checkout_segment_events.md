# Checkout Segment events: `order_completed` and `order_cancelled`

Introduced for **ENT-12377** (Checkout Segment Events Refactor, phase 1). The server sends the two final checkout
events from the Stripe webhooks, so they are recorded even if the customer closes the browser before the success page
loads.

## What is sent, and when

| Event | Sent on | `order_id` | `revenue` |
|---|---|---|---|
| `edx.ui.enterprise.checkout.order_completed` | `invoice.paid` for the subscription's first invoice (`billing_reason` `subscription_create`), paid at checkout; $0 with the free trial | The CheckoutIntent's Checkout Session ID, the same value the checkout MFE sends | Amount paid on that invoice |
| `edx.ui.enterprise.checkout.order_cancelled` | `customer.subscription.deleted`, trials included | The CheckoutIntent's Checkout Session ID | Amount paid on the subscription's first invoice |

Trial-end and renewal invoices (`subscription_cycle`) and license changes (`subscription_update`) never send
`order_completed`.

## Turning it on
`FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2` (Django setting, off by default; set it in edx-internal
`argocd/applications/enterprise-access/<env>-config.yaml`). `SEGMENT_KEY` must also be set. These events arrive in the
enterprise-access Segment source, not the checkout MFE's; join them to the MFE's funnel events on `order_id`.

## How it is sent
No table of its own: it relies on `StripeEventData` / `StripeEventSummary`, which every webhook already persists.
* The webhook handlers queue `send_checkout_segment_event_task`. A broker error fails the webhook, and Stripe
  redelivers it (pattern 25 in `docs/architecture-patterns.md`).
* Each order has exactly one triggering Stripe event, so the handlers queue the task once per Stripe event: a
  redelivery of an event whose `StripeEventData.handled_at` is set queues nothing. A delivery whose handler failed is
  not marked handled, so its redelivery queues the task again, and the Segment `messageId` `<event>:<order_id>` lets
  Segment drop the repeat (Segment de-duplicates a messageId over a limited window, about a day).
* Retried with backoff: Stripe network errors, 429 and 5xx, and DB `OperationalError`. Logged as an error and not
  sent: a missing required property, any other Stripe error, a user without `lms_user_id`, or an order that can't be
  identified.

## Payload
No PII: payloads are built only from the fields below, and a test lists every allowed key.

| Properties | Source |
|---|---|
| `product_id`, `price` (unit price), `payment_schedule` | The Stripe Price on the subscription's item (complete even if the price was archived since): fetched with `get_stripe_subscription` for `order_completed`, from the webhook's cancelled subscription for `order_cancelled`. Never invoice line amounts, which are the trial's |
| `total_quantity` | `order_completed`: the first invoice's quantity; `order_cancelled`: the cancelled subscription's item |
| `revenue`, `currency` | The `invoice.paid` `StripeEventSummary` of the order's first invoice (`invoice_amount_paid`, `invoice_currency`) |
| `sku`, `category`, `name`, `brand`, `variant`, `slug` | The CheckoutIntent's `SspProduct` |

`order_id`, `product_id`, `price`, `payment_schedule`, `total_quantity`, `revenue` and `currency` are required.

Attribution (`utm_*`, `referrer`) is not sent yet: a follow-up, together with the checkout MFE sending it.

## Gotchas
* The CheckoutIntent's Checkout Session ID is overwritten if the customer starts another checkout after paying, so
  an `order_cancelled` sent after that reports the newer session's ID. Rare; accepted to avoid a table.
* `order_cancelled` finds the order's first invoice as the subscription's earliest `invoice.paid` summary.
* stripe-python 16 returns `unit_amount_decimal` as a `Decimal`; kombu's JSON serializer round-trips it to the task.
* Before turning the setting on, check one stage trial checkout and cancellation in the Segment debugger.
