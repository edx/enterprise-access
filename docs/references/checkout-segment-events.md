# Server-side checkout Segment events (ENT-12377)

Behind `FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2` (Django setting, default `False`; off = nothing is sent).

| Event | Trigger | Order ID |
|---|---|---|
| `edx.ui.enterprise.checkout.order_completed` | `invoice.paid` handler, on the first paid invoice of a subscription: `invoice.total > 0`, `billing_reason` in (`subscription_create`, `subscription_cycle`), and not an annual renewal (`_is_annual_renewal_invoice`). Covers the `subscription_cycle` invoice issued when a trial ends. Skips `subscription_update`, `manual` and missing billing reasons. | Stripe invoice ID |
| `edx.ui.enterprise.checkout.order_cancelled` | `customer.subscription.deleted` handler | subscription's `latest_invoice` |

## Open question: order_cancelled order ID (product decision pending)
`order_cancelled` uses the subscription's `latest_invoice` as `order_id` and for `revenue`. After a renewal or
license change that invoice differs from the one on the original `order_completed`. Options:
- (a) Store the original order_completed invoice ID on `CheckoutIntent` and reuse it. Pro: the two events
  join on `order_id`. Con: needs a migration and backfill for existing intents, one more field to keep in
  sync, and revenue then reflects the original order rather than the last charge.
- (b) Keep `latest_invoice`. Pro: no schema change, revenue is the most recent charge. Con: `order_id` does
  not match `order_completed` once the subscription has renewed or changed.

## How it works
- Handlers call `enqueue_checkout_segment_event` (never raises). The Celery task
  `send_checkout_segment_event_task` retrieves the invoice from Stripe (expanding the paying charge),
  builds the payload in `segment_payloads.py`, and sends it with `track_event`.
- Webhook signature validation happens in `StripeWebhookAuthentication`, before any handler runs.
- Idempotency: cache key `checkout-segment-event:<event_name>:<order_id>` (30 days), claimed right before
  sending and released if sending raises. A cache flush could therefore allow a rare duplicate.
- Payload is built from an explicit allowlist; no PII. `revenue` is the invoice `total` (discounts and proration
  included), never price * quantity. `variant` (Academy title) is only set for Essentials.
  `payment_method` is only set when the expanded charge is available and the invoice is not $0.
- Attribution (`utm_*`, `referrer`) lives in `CheckoutIntent.attribution` (JSON), accepted by the
  CheckoutIntent create API, and is only sent on `order_completed`.

## Gotchas
- `track_event` swallows Segment exceptions by default (and no-ops without `SEGMENT_KEY`). The task passes
  `raise_on_error=True` so a Segment failure releases the dedupe key and Celery retries.
- Renewal detection relies on `StripeEventSummary` (`ENABLE_STRIPE_EVENT_SUMMARIES`); without summaries a renewal
  looks like a first paid invoice.
- The Stripe expand path `payments.data.payment.payment_intent.latest_charge` should be verified against
  the account's pinned API version on stage.

## Rollout
1. Confirm `ENABLE_STRIPE_EVENT_SUMMARIES = True` in stage and prod (the repo default in `settings/base.py` is `False`).
   Without it, renewals look like first paid invoices and send duplicate order_completed events.
2. Confirm that stage and prod `CACHES` use a shared backend (Memcached or Redis), so the duplicate check
   works across workers.
3. Turn off the browser-side `edx.ui.enterprise.checkout.order_completed` event when the backend flag is
   turned on, to avoid double counting.
4. Enable `FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2` on stage first and verify the events in the Segment
   debugger, including `payment_method` (expand path) and attribution.
