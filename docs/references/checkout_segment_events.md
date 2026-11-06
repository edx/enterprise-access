# Server-side checkout Segment events (ENT-12377)

Behind the `FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2` Django setting (default `False`). That is the name of the
frontend-app-enterprise-checkout flag that gates the browser-side checkout events (ENT-12328). Off: no event is
recorded or sent, and events already recorded wait unsent until it is back on.

| Event | Recorded when | `order_id` | `revenue` | Product, price, `payment_schedule`, `total_quantity` |
|---|---|---|---|---|
| `edx.ui.enterprise.checkout.order_completed` | `invoice.paid` with `invoice.total > 0` and `billing_reason` in (`subscription_create`, `subscription_cycle`). The send task then checks Stripe's payment history and sends the event only if this was the order's first paid invoice (`find_order_invoice`); a renewal is `skipped`. SSP checkouts always start with a $0 trial, so in practice this is the `subscription_cycle` invoice issued when the trial ends. | Stripe Checkout Session ID (`CheckoutIntent.stripe_checkout_session_id`) | that invoice's `total` | that invoice's non-proration subscription item line, interval from the Stripe price (archived prices included) |
| `edx.ui.enterprise.checkout.order_cancelled` | `customer.subscription.deleted`, always. The send task then asks Stripe for the order's first paid invoice, and skips the event if there is none (the subscription never collected money, e.g. it was cancelled during its trial). | same Checkout Session ID | the order's first paid invoice `total`, i.e. the same as on `order_completed` | the cancelled subscription's item, read from Stripe without the cache (`get_stripe_subscription_uncached`): the state at cancellation, after license changes |

## Why these identifiers
- `order_id` is the Stripe Checkout Session ID because the checkout frontend already sends that ID as `order_id`
  (`buildCheckoutOrderProperties` in frontend-app-enterprise-checkout), Stripe has no Order object in this flow, and
  `CheckoutIntent.mark_as_paid` refuses to change it once the intent is paid. It is known when either webhook
  arrives, it is the same for both events, and it does not change after renewals or license changes. A CheckoutIntent
  without one (not created through Stripe Checkout) is logged as an error and gets no event; no other ID is substituted.
- Revenue stays an invoice total (discounts and proration included), never price * quantity.
- Cancellation product/quantity come from the subscription, never from an invoice: the last paid invoice can be a
  license change made only of proration lines, and the CheckoutIntent's quantity is the quantity at checkout.

## How it works (transactional outbox)
- `SegmentEventEmission` is the outbox and the idempotency record: one row per (`event_name`, `order_id`), unique
  (migration 0043). Statuses: `pending` -> `dispatched` -> `sent` | `skipped` | `failed`.
- The webhook handlers call `segment_outbox.record_order_completed_segment_event` /
  `record_order_cancelled_segment_event` at the end, after their billing work. They save the row (`pending`) with
  `get_or_create`, then, on commit, `dispatch_segment_event` publishes `send_checkout_segment_event_task(emission_id)`
  and marks the row `dispatched`. A redelivered webhook finds the same row.
- If saving the row fails (a database error, e.g. a lock wait timeout), the error is raised: the webhook returns an
  error and the Stripe event is not marked handled, so Stripe redelivers it. The redelivery repeats the billing work
  (handlers are idempotent; `persist_stripe_event` reuses the event's `StripeEventData` row) and records the event.
  Swallowing the error would acknowledge the webhook with nothing left to send the event from.
- If the broker is down, publishing fails, the row stays `pending` (with `last_error`), and the webhook still returns
  200. The `dispatch_checkout_segment_events` management command publishes `pending` rows. It must run on a schedule
  (e.g. every 10 minutes); without it, events recorded during a broker outage are never sent. This repo has no
  scheduler: the schedule is a cronjob in `edx/edx-internal`, `argocd/applications/enterprise-access/{stage,prod}-config.yaml`
  (see Rollout).
- The command also puts `dispatched` rows that have not finished after `--stale-after-minutes` (default 120, longer
  than the task's own Celery retries) back to `pending`, and marks them `failed` once they were dispatched
  `--max-dispatch-attempts` times (default 5). A failed publish is not counted as an attempt.
- The task builds the payload outside the row lock, then locks the row, re-checks that it is not final, sends with
  `send_event_synchronously`, and marks it `sent` in the same transaction. Retried with backoff: Stripe connection
  errors and timeouts, 429 and 5xx (including when listing the paid invoices or fetching the price for
  `payment_schedule`), retryable Segment failures and DB `OperationalError`. Marked `failed` without retry: a required
  property is missing (`IncompleteOrderPayloadError`), the CheckoutIntent is gone or has no `lms_user_id`, Stripe
  rejects the request (`InvalidRequestError`), Segment rejects the event (4xx other than 429). Marked `skipped`: the
  recorded `order_completed` invoice was not the order's first payment, or a cancelled subscription never paid.
- Why `order_completed` is confirmed in the task: the handler can't tell a trial-end payment from a renewal locally.
  `_is_annual_renewal_invoice` counts *any other* paid invoice summary, so a later license change whose webhook is
  handled first (e.g. while the trial-end delivery is being retried) makes the first payment look like a renewal, and
  summaries are best-effort. Stripe's history is complete and ordered by `created`. The recorded invoice is added to
  Stripe's list in case the list doesn't show it yet. `order_cancelled` reuses the invoice of a `sent`
  `order_completed` only; a `skipped` one holds a renewal's invoice.
- Required properties (`REQUIRED_ORDER_PROPERTIES`): `order_id`, `product_id`, `price`, `payment_schedule`,
  `total_quantity`, `revenue`, `currency`. An event missing one is never sent. `sku`, `slug`, `name`, `category`,
  `brand`, `variant` and the attribution keys are optional.
- Status changes are conditional updates (`SegmentEventEmission.finish`, the dispatcher's `.update()`s), so a stale
  copy or a second worker never moves a final row back. The dispatcher locks rows with `skip_locked`, so concurrent
  dispatchers (or the webhook and the command) never publish a row at the same time.
- The Segment `messageId` is `<event_name>:<order_id>`. Segment drops a messageId it has seen in the last 24 hours,
  so a resend after a success that was not recorded is not counted twice.
- No payment method is sent: the invoice is fetched without `expand`, so reading it would need extra Stripe calls.

## Attribution (`utm_*`, `referrer`)
- Stored in `CheckoutIntent.attribution` by the CheckoutIntent create API and only sent on `order_completed`.
- `CheckoutIntentCreateRequestSerializer.validate_attribution` rejects (400) attribution that is not a flat object of
  strings: nested objects, lists, numbers and booleans would otherwise be stringified and stored. The error message
  names the keys, never the values.
- `utils.sanitize_attribution` then keeps only the known keys with non-empty string values. Values over 100
  characters are dropped, not truncated, and the length is checked before any pattern runs, so a huge value is never
  scanned.
- **UTM values must be campaign labels** (`sanitize_utm_value`): only letters (any language), digits, spaces and
  `_ - . + | ,`. That excludes every character of an email address, URL, query string or encoded value (`@ % / : = &
  ? #`). Values that still look like identifiers are dropped too: 7 or more digits, together or split by spaces, dots,
  plus signs or hyphens (phone, account and order numbers); 20 or more letters and digits in a row (tokens, hashes);
  UUIDs. Campaign names should therefore avoid dates written as one number (`20261106`, `2026-11-06`): use
  `2026_11_06` or `nov26`.
- The referrer is reduced to its origin, `scheme://host[:port]/`: http(s) only, no username/password, query, fragment
  or path. Paths can identify a person (`/u/jane-doe`, reset tokens) and no path pattern is safe in general; browsers
  already send only the origin for cross-site referrers by default.
- Unsafe values are dropped rather than rejected, because they come from the link the customer followed.
- The payload builder applies `sanitize_attribution` again when sending, so stored values that predate the rules (or
  were edited in admin) are never forwarded unchecked.
- **Remaining risk, needs privacy sign-off:** no format can tell a person's name from a campaign label (`jane_doe`
  looks like `spring_sale`), and `utm_term` can carry a searcher's own query if an ad platform inserts it (e.g. a
  query-string macro). Closing that fully needs either an approved allowlist of campaign values or dropping
  `utm_term`/`utm_content`.
- `CheckoutIntentReadOnlySerializer` includes every field, so the lifecycle event in `signals.py` removes
  `attribution` before calling `track_event`.

## Gotchas
- Checkout events use `send_event_synchronously`, not `track_event`. analytics-python's default client hands
  events to a background thread that retries for minutes and then drops a failed upload without telling the caller,
  and `analytics.flush()` returns either way, so neither can confirm delivery. `send_event_synchronously` posts once,
  in the task, with a 10s timeout (`SYNC_SEND_TIMEOUT_SECONDS`, far below the 240s Celery soft time limit), and raises
  if Segment does not return 200. The row lock is held for that one bounded request only.
- `send_event_synchronously` returns False when `SEGMENT_KEY` is unset; the row then stays `dispatched` and the
  command re-publishes it until it is sent or `failed`.
- `payment_schedule` reads the price with `stripe_api.get_stripe_price`, not `pricing_api.get_stripe_price_data`,
  which rejects inactive prices: archiving a price doesn't change the subscriptions that use it, and an order that
  was paid must still be reported. Transient Stripe errors are retried; a missing or non-recurring price gives no
  schedule, so the event fails as incomplete.
- `get_stripe_subscription` caches for 60 seconds. Anything recorded for good about a subscription's current state
  (the cancellation payload) uses `get_stripe_subscription_uncached`.
- In tests and eager devstacks the task runs inside `delay()`. `dispatch_segment_event` marks the row `dispatched`
  before publishing for that reason; marking it afterwards would overwrite the task's `sent`.
- `TestCase` does not run `on_commit` callbacks: wrap webhook dispatches in `self.captureOnCommitCallbacks(execute=True)`.
- Renewal detection still relies on `StripeEventSummary` rows, created unconditionally by the `StripeEventData`
  post-save signal in `customer_billing/signals.py` (not gated by `ENABLE_STRIPE_EVENT_SUMMARIES`, which nothing
  reads). The cancellation path does not: it asks Stripe.
- Invalid webhook signatures are rejected by the existing authentication with 403 (not 400).

## Open questions (product / analytics)
1. **Zero-dollar completions.** Every SSP checkout starts with a $0 trial, so `order_completed` is sent when the trial
   converts to paid (about 14 days after checkout), not at checkout. Should it instead (or also) be sent at checkout
   with `revenue: 0`?
2. **Trial cancellations.** A subscription cancelled before it ever paid sends no `order_cancelled` (the row is
   `skipped`). Should it send one, with `revenue: 0`?
3. **Cancellation revenue.** `order_cancelled.revenue` is the order's first paid invoice total (the same as on
   `order_completed`). Should it be the last paid invoice, or everything the subscription collected?
4. **`order_id`.** Confirm that the Checkout Session ID is what the ticket means by "Stripe Order ID" (it is what the
   checkout frontend sends).

## Rollout
1. Set `SEGMENT_KEY` per environment (default None disables sending).
2. Before turning the setting on, schedule the dispatcher in each environment: add a cronjob next to
   `edx-enterprise-access-mark-stalled-checkout-intents` in `edx/edx-internal`
   `argocd/applications/enterprise-access/{stage,prod}-config.yaml`:
   ```yaml
   - name: edx-enterprise-access-dispatch-checkout-segment-events
     schedule: "*/10 * * * *" # Every 10 minutes
     # Publishes checkout Segment events whose send task never reached the broker, and re-publishes ones Celery gave
     # up on (see docs/references/checkout_segment_events.md). Does nothing while the setting is off.
     command: ./manage.py dispatch_checkout_segment_events
     requests:
       cpu: "300m"
   ```
3. Frontend: send `utm_*` and `referrer` (flat strings) to the CheckoutIntent create API, and stop any browser-side
   completion event that the backend replaces, so orders are not double counted. Today the frontend attaches order
   properties (`order_id` = Checkout Session ID, `revenue`) to `checkout_step_completed.billing_details`.
4. Turn on `FEATURE_SSP_CHECKOUT_SEGMENT_EVENTS_V2` on stage first and verify the events in the Segment debugger,
   including attribution, a cancelled trial (no `order_cancelled`), and a cancellation after a license change.
