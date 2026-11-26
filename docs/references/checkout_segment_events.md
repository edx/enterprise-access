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
Once it is on, `order_cancelled` is sent for every subscription cancelled, including ones bought before it was turned
on. Those orders have no `order_completed`, from the server or the MFE, so expect cancellations without a matching
completion in the funnel for a while after the switch.

## How it is sent
No table of its own: it relies on `StripeEventData` / `StripeEventSummary`, which every webhook already persists.
* The webhook handlers queue `send_checkout_segment_event_task`. A broker error fails the webhook, and Stripe
  redelivers it (pattern 27 in `docs/architecture-patterns.md`).
* Each order has exactly one triggering Stripe event, so the handlers queue the task once per Stripe event: a
  redelivery of an event whose `StripeEventData.handled_at` is set queues nothing. The task is queued as the
  handler's last step, so a delivery whose handler fails queues nothing: Stripe redelivers it for up to three days,
  longer than Segment de-duplicates a `messageId` (about a day). The `messageId` `<event>:<order_id>` covers the rare
  repeat when only the queuing itself fails.
* The task posts the event to Segment itself (`track_event_or_raise`, a `sync_mode` analytics client), not through
  `track_event`, whose client only queues events for a background thread and swallows errors: an event queued that
  way is lost if Segment stays unreachable or the worker exits first, and the task would never know.
* Retried with backoff (`TASK_MAX_RETRIES`): Stripe network errors, 429 and 5xx, DB `OperationalError`, an order
  invoice's `invoice.paid` `StripeEventSummary` that isn't readable yet (its signal logs and swallows its own errors),
  an Essentials product's Academy title that can't be read (the catalog call failed), and Segment being unreachable,
  timing out or answering 429 or 5xx. The retries run out within about 25 minutes.
* Logged as an error with the prefix `[CheckoutSegmentEventDropped]` and not sent (alert on the prefix): a missing
  required property, any other Stripe error, a user without `lms_user_id`, an order that can't be identified, Segment
  rejecting the event with another 4xx (e.g. a wrong `SEGMENT_KEY`), and an event whose retries ran out (logged by
  the task's `on_failure`).

## Resending a dropped event
Replaying the Stripe event doesn't resend it: the admin "Handle the selected events" action and
`fetch_and_handle_stripe_events` both see `handled_at` set and queue nothing. Once the cause is fixed, queue the task
from a Django shell with the arguments the webhook handler would have used:

```python
from enterprise_access.apps.customer_billing.constants import CheckoutSegmentEvents
from enterprise_access.apps.customer_billing.tasks import send_checkout_segment_event_task

# order_completed: the subscription's first (subscription_create) invoice
send_checkout_segment_event_task.delay(
    CheckoutSegmentEvents.ORDER_COMPLETED, checkout_intent_id, 'sub_...', invoice_id='in_...',
)
# order_cancelled: the cancelled subscription's id and items, from the customer.subscription.deleted event's data
send_checkout_segment_event_task.delay(
    CheckoutSegmentEvents.ORDER_CANCELLED, checkout_intent_id, 'sub_...',
    subscription_data={'id': 'sub_...', 'items': event_data['data']['object']['items']},
)
```

Segment drops a `messageId` it has seen in about the last day, so resending an event it did receive within that
window is harmless; after that it would be counted twice.

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
* `order_cancelled` finds the order's first invoice as the subscription's earliest `invoice.paid` summary
  (`StripeEventSummary.get_first_invoice_paid`).
* stripe-python 17 returns `unit_amount_decimal` as a `Decimal`; kombu's JSON serializer round-trips it to the task.
* Before turning the setting on, check one stage trial checkout and cancellation in the Segment debugger.
