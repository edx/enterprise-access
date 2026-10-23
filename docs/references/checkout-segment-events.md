# Server-side checkout Segment events (ENT-12377)

Behind the `enterprise_access.checkout_segment_events_v2` WaffleSwitch (default off; off = nothing is sent).

| Event | Trigger | Order ID |
|---|---|---|
| `edx.ui.enterprise.checkout.order_completed` | `invoice.paid` handler, on the first paid invoice of a subscription: `invoice.total > 0`, `billing_reason` in (`subscription_create`, `subscription_cycle`), and not an annual renewal (`_is_annual_renewal_invoice`). Covers the `subscription_cycle` invoice issued when a trial ends. Skips `subscription_update`, `manual` and missing billing reasons. | Stripe invoice ID |
| `edx.ui.enterprise.checkout.order_cancelled` | `customer.subscription.deleted` handler, only if the subscription has a recorded paid invoice (`invoice.paid` with a non-zero amount) | the subscription's last paid invoice |

## Open question: order_cancelled order ID (product decision pending)
`order_cancelled` uses the subscription's last paid invoice (from `StripeEventSummary`) as `order_id` and for
`revenue`. It never uses the subscription's `latest_invoice`: when a renewal charge fails and the subscription is
then cancelled, `latest_invoice` is the unpaid renewal, and its total was never collected. After a renewal or
license change, the last paid invoice differs from the one on the original `order_completed`. Options:
- (a) Store the original order_completed invoice ID on `CheckoutIntent` and reuse it. Pro: the two events
  join on `order_id`. Con: needs a migration and backfill for existing intents, one more field to keep in
  sync, and revenue then reflects the original order rather than the last charge.
- (b) Keep the last paid invoice. Pro: no schema change, revenue is the most recent charge actually collected.
  Con: `order_id` does not match `order_completed` once the subscription has renewed or changed.

## How it works
- Handlers call `enqueue_checkout_segment_event` (never raises). The Celery task
  `send_checkout_segment_event_task` retrieves the invoice with `get_stripe_invoice` (unexpanded, cached for a
  minute; a finalized invoice's total and lines don't change), builds the payload in `segment_payloads.py`, and
  sends it with `send_event_synchronously` (see Gotchas).
- Webhook signature validation happens in `StripeWebhookAuthentication`, before any handler runs.
- Idempotency: `SegmentEventEmission` (unique on `event_name` + `order_id`, migration 0042). The task locks the
  row, sends, and sets `emitted_at` only after Segment accepted the event, so a failed send stays retryable
  and concurrent deliveries emit once. `order_id` is the Stripe invoice ID (for `order_cancelled`, the
  subscription's last paid invoice; see the open question above).
- Payload fields are set explicitly in `segment_payloads.py`; no PII. `APPROVED_PAYLOAD_KEYS` in
  `test_checkout_segment_events.py` lists every key a payload may carry, so a new key fails the tests until it is
  reviewed and added there. `revenue` is the invoice `total` (discounts and proration
  included), never price * quantity. `variant` (Academy title) is only set for Essentials.
  `payment_schedule` comes from the cached Stripe price's recurring interval (`get_stripe_price_data`) and is
  omitted if it cannot be fetched. Product, quantity and price come from the invoice's subscription item line,
  never a proration or credit line. Stripe proration lines share the subscription item parent type and are only
  told apart by `parent.subscription_item_details.proration`. If there is no such line (e.g. a license-change
  invoice made only of prorations), product and price are omitted and the quantity is the CheckoutIntent's.
- No payment method is sent. The invoice is fetched without `expand`, so a payment's PaymentIntent is only an ID,
  and reading the method would need extra Stripe calls.
- The referrer's query string and fragment are stripped once, when the CheckoutIntent create API saves the
  attribution (`CheckoutIntentCreateRequestSerializer.validate_attribution`), so they are never stored. A referrer
  that can't be parsed as a URL is dropped rather than failing the request. The payload sends the stored value.
- Attribution (`utm_*`, `referrer`) lives in `CheckoutIntent.attribution` (JSON), accepted by the
  CheckoutIntent create API, and is only sent on `order_completed`.

## Gotchas
- Checkout events use `send_event_synchronously`, not `track_event`. analytics-python's default client hands
  events to a background thread that retries for minutes and then drops a failed upload without telling the
  caller, and `analytics.flush()` returns either way, so neither can confirm delivery. `send_event_synchronously`
  posts once, in the task, with a 10s timeout (`SYNC_SEND_TIMEOUT_SECONDS`, far below the 240s Celery soft time
  limit) and raises if Segment does not return 200. Network errors, timeouts, 429 and 5xx raise
  `SegmentSendError` and are retried with backoff; other 4xx and invalid arguments are not retried, because they
  would fail the same way every time. The emission row lock is held for that one bounded request only.
- The Segment `messageId` is `<event_name>:<invoice_id>`. Segment drops a messageId it has seen in the last 24
  hours, so if the worker dies after Segment accepted the event but before `emitted_at` is saved, the retry is
  not counted twice.
- `send_event_synchronously` returns False when `SEGMENT_KEY` is unset; the emission is then not marked as sent.
- Renewal detection and the paid-cancellation check rely on `StripeEventSummary` rows, created unconditionally
  by the `StripeEventData` post-save signal in `customer_billing/signals.py` (not gated by
  `ENABLE_STRIPE_EVENT_SUMMARIES`, which nothing reads).
- `order_cancelled` is queued by `enqueue_order_cancelled_segment_event`, which never raises and runs after the
  cancellation logic, so analytics errors cannot stop `cancel_all_future_plans`.
- The webhook handlers run outside a DB transaction (no `ATOMIC_REQUESTS`), so tasks are queued with `.delay()`.
- `order_cancelled` is sent only if the subscription has a recorded paid invoice. Trials, subscriptions whose
  first charge failed, and subscriptions whose invoices were all $0 are excluded, whatever their last status.
- Invalid webhook signatures are rejected by the existing authentication with 403 (not 400).

## Rollout
1. Set `SEGMENT_KEY` per environment (default None disables sending).
2. Turn off the browser-side `edx.ui.enterprise.checkout.order_completed` event when the backend switch is
   turned on, to avoid double counting.
3. Enable `enterprise_access.checkout_segment_events_v2` on stage first and verify the events in the Segment
   debugger, including attribution and the trial-cancellation exclusion.
