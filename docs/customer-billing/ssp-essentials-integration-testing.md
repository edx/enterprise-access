# Manual E2E Integration Testing: SSP Essentials

## Overview

This doc covers how to manually test the SSP checkout and email notification flow against the Stripe sandbox and Braze dev workspace. The pattern: trigger a real action in Stripe, replay the resulting events locally with a management command, and verify that the right Celery task fired and delivered to Braze. You're confirming that the full stack (`Stripe event -> handler -> Celery task -> Braze campaign`) works against live external APIs.

Two systems:

- **Stripe sandbox:** test-mode Stripe account for creating real checkout sessions, subscriptions, and invoices without charging anyone.
- **Braze dev workspace:** staging Braze environment where campaign sends are visible without touching production users.

## Prerequisites

**Devstack.** The app server must be running. Email tasks are async Celery tasks; by default you also need the worker running (`make dev.celery` or equivalent). If running a separate worker container is inconvenient, add `CELERY_ALWAYS_EAGER = True` to `settings/private.py` to execute tasks synchronously inline instead.

**Local DB seed.** You need at least one active `SspProduct` record pointing at a valid Stripe price lookup key, and a test user in the LMS with an enterprise admin role for a test enterprise. `get_enterprise_admins` fetches LMS enterprise data to build Braze recipients; if it returns empty, the task fails before reaching Braze.

**`settings/private.py`.** Add the following (create the file if it doesn't exist -- it's gitignored):

```python
# Stripe sandbox -- test mode secret key
# Developers -> API keys in the sandbox dashboard:
# https://dashboard.stripe.com/acct_1RtEfJQ60jNALKNU/test/dashboard
STRIPE_API_KEY = 'sk_test_...'

# Run Celery tasks inline without a separate worker (optional but convenient locally)
CELERY_ALWAYS_EAGER = True

# Braze dev workspace
# Settings -> API Keys: https://dashboard-06.braze.com/dashboard/[dev-environment-id]
BRAZE_API_URL = 'https://rest.iad-06.braze.com'
BRAZE_API_KEY = '...'

# SSP Essentials campaign UUIDs -- find these in the dev workspace under Messaging -> Campaigns.
# Ask the team for the correct UUIDs once the essentials campaigns are created in the dev workspace.
BRAZE_ENTERPRISE_PROVISION_SIGNUP_CONFIRMATION_CAMPAIGN = '...'
BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN = '...'
BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN = '...'
BRAZE_ENTERPRISE_PROVISION_TRIAL_ENDING_SOON_CAMPAIGN = '...'
BRAZE_TRIAL_CANCELLATION_CAMPAIGN = '...'
BRAZE_PAID_CANCELLATION_CAMPAIGN = '...'
BRAZE_SSP_CANCELATION_FINALIZATION_CAMPAIGN = '...'
BRAZE_BILLING_ERROR_CAMPAIGN = '...'
BRAZE_SSP_SUBSCRIPTION_REINSTATED_CAMPAIGN = '...'
```

The email tasks currently send to a single campaign per email type regardless of which `SspProduct` the customer purchased. As part of SSP Essentials, they will be updated to branch on the `SspProduct` associated with the `CheckoutIntent`, routing to product-specific campaigns. Until those essentials campaigns exist in the dev workspace and the branching logic is implemented, confirm with the team which campaign UUIDs to use for a given testing milestone.

## Happy path walkthrough: payment receipt

`invoice.paid` triggers `send_payment_receipt_email`, making this the most self-contained event-to-email path to test.

The steps below describe how to test without interacting with the frontend. If the frontend is implemented and testable,
you can use it directly in place of the first 2 steps.

**1. Create a checkout session.** POST to the BFF checkout intent endpoint with your test user's JWT:

```bash
curl -X POST http://localhost:18000/api/v1/bff/checkout/intent/ \
  -H "Authorization: JWT <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "enterprise_slug": "test-co",
    "enterprise_name": "Test Co",
    "quantity": 10,
    "ssp_product_slug": "teams-yearly",
    "country": "US",
    "terms_metadata": {"version": "1.0"}
  }'
```

**2. Complete checkout in Stripe.** Open the checkout URL from the response in a browser. Use test card `4242 4242 4242 4242`, any future expiry, any CVC. Stripe creates a subscription and immediately generates a paid invoice for $0 (trial).

**3. Note the current timestamp.** Grab a Unix timestamp from a minute before you started (e.g. `date -v-2M +%s` on Mac). This is your `--since` value to avoid pulling unrelated older events.

**4. Replay events locally.**

```bash
python manage.py fetch_and_handle_stripe_events \
  --event-types invoice.created invoice.paid \
  --since <timestamp>
```

Run `python manage.py fetch_and_handle_stripe_events --help` for all options.

**5. Verify.** Three things to check:

- App/worker logs: look for `Successfully sent payment receipt confirmation email for enterprise Test Co`. If you see `Payment receipt confirmation email not sent: No invoice summary found`, `invoice.created` wasn't processed first -- include both event types and re-run.
- Braze dev dashboard: open the `BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN` campaign, Analytics -> Sends. The count should increment.
- Django admin: confirm the `StripeEventSummary` record for this invoice has `invoice_amount_paid` populated.

## Signup confirmation email

`send_enterprise_provision_signup_confirmation_email` fires at the end of the provisioning workflow (`provisioning/models.py`), not via a Stripe event. To test it, either trigger the full provisioning workflow or call the task directly from a Django shell:

```python
from enterprise_access.apps.customer_billing.tasks import send_enterprise_provision_signup_confirmation_email
from datetime import datetime, timedelta
send_enterprise_provision_signup_confirmation_email.delay(
    subscription_start_date=datetime.utcnow(),
    subscription_end_date=datetime.utcnow() + timedelta(days=30),
    number_of_licenses=10,
    activation_link=None,
    organization_name='Test Co',
    enterprise_slug='test-co',
)
```

The task calls `validate_trial_subscription` first and returns early (no exception, just a log line) if no valid trialing subscription exists for the slug. The subscription must be in `trialing` state in Stripe when you call it.

## Simulating time-based events with Stripe test clocks

Some email tasks fire on transitions that only happen over time: `trial_will_end` fires 72 hours before the trial ends, and the trial-to-active transition happens when the trial period expires. Use Stripe's test clock feature to fast-forward rather than waiting. This can be done from the subscription record page in the Stripe dashboard.

Stripe fires the appropriate events (`customer.subscription.trial_will_end`, `customer.subscription.updated`) when the clock advances. Replay them with `fetch_and_handle_stripe_events` as usual.

## Other email tasks

All tasks below are triggered by replaying the corresponding Stripe event type via `fetch_and_handle_stripe_events`.

| Task | Triggering Stripe event | `private.py` settings key |
|---|---|---|
| `send_trial_ending_reminder_email_task` | `customer.subscription.trial_will_end` | `BRAZE_ENTERPRISE_PROVISION_TRIAL_ENDING_SOON_CAMPAIGN` |
| `send_trial_end_and_subscription_started_email_task` | `customer.subscription.updated` (trial -> active) | `BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN` |
| `send_payment_receipt_email` | `invoice.paid` | `BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN` |
| `send_trial_cancellation_email_task` | `customer.subscription.updated` (cancel_at set, trialing) | `BRAZE_TRIAL_CANCELLATION_CAMPAIGN` |
| `send_paid_cancellation_email_task` | `customer.subscription.updated` (cancel_at set, active) | `BRAZE_PAID_CANCELLATION_CAMPAIGN` |
| `send_finalized_cancelation_email_task` | `customer.subscription.deleted` | `BRAZE_SSP_CANCELATION_FINALIZATION_CAMPAIGN` |
| `send_billing_error_email_task` | `customer.subscription.updated` (-> past_due) | `BRAZE_BILLING_ERROR_CAMPAIGN` |
| `send_reinstatement_email_task` | `customer.subscription.updated` (cancel_at cleared) | `BRAZE_SSP_SUBSCRIPTION_REINSTATED_CAMPAIGN` |

The `customer.subscription.updated` handler dispatches to different tasks based on the state transition in the event payload. If your sandbox subscription isn't in the expected state when you replay, no task dispatches and no error appears in the logs.

## Common failure modes

**Worker not running.** Tasks queue silently; nothing sends, no error in the management command output. Start the worker or set `CELERY_ALWAYS_EAGER = True` in `private.py`.

**No valid trial subscription.** `send_enterprise_provision_signup_confirmation_email` logs `Email not sent: No valid trial subscription found` and returns. Verify the subscription is in `trialing` state in the Stripe sandbox before calling the task.

**No enterprise admins.** `get_enterprise_admins` logs an error and the task raises. Add your test user as an enterprise admin in LMS Django admin for the test enterprise slug.

**Stale price cache.** If you add or change products in the sandbox, `get_all_stripe_prices` returns cached data until TTL expires. Clear it from a Django shell: `from edx_django_utils.cache import TieredCache; TieredCache.dangerous_clear_all_tiers()`.

**`invoice.paid` before `invoice.created`.** `send_payment_receipt_email` requires a `StripeEventSummary` record created by the `invoice.created` handler. Include both event types in `--event-types` when replaying.
