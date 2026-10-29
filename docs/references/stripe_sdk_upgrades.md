# Upgrading the stripe SDK

Unit tests mock every Stripe call, so a green suite says nothing about whether Stripe still accepts what
we send or still returns what we read. This page covers what CI checks on an SDK upgrade and what a
person still has to check.

## How API changes reach us

- A Stripe API version never changes after release.
- We don't set `stripe.api_version`, so every stripe SDK release sends the API version it pins
  (`stripe.api_version`). Bumping `stripe` in `requirements/` (usually via the weekly requirements bot
  PR) is how a new API version reaches production.
- Webhook payloads are rendered at the **webhook endpoint's** API version, set in the Stripe Dashboard,
  not the SDK's. Changing it is a separate decision that no test here can see.
- Breaking changes are not limited to new named releases (`clover` → `dahlia` → `endive`). Monthly
  versions can remove fields too, e.g. stripe 15.6.0 stayed on `dahlia` and removed
  `PaymentAttemptRecord...ThreeDSecure.cryptogram`.

## What CI catches

| Test | Catches |
| --- | --- |
| `test_stripe_sdk_contract.py::test_installed_sdk_pins_a_reviewed_api_version` | Any change to the pinned API version. It fails until someone reviews it and updates `REVIEWED_STRIPE_API_VERSION`. |
| `test_stripe_sdk_contract.py::test_stripe_call_sites_match_installed_sdk` | Every `stripe.X` reference and `stripe.<Resource>.<method>(...)` call in app code: removed or renamed classes and methods (stripe 16 renamed `Reversal`), including in `except` clauses and annotations; path params passed by keyword once they became positional-only (stripe 16), including on class-or-instance methods like `Coupon.delete`; and literal params or nested keys the SDK no longer declares. When validated against stripe 15.6.1 → 16.0.0, it flagged a generated call for every call-level change between the two (501) and none of them on 15.6.1. It does not check param value types, enum values, `StripeClient` (v2) calls, or response fields. |
| `test_stripe_api.py::test_create_params_are_supported_by_installed_stripe_sdk` | Params built in a dict and passed as `**kwargs`, which the static check can't see. Checks the captured `checkout.Session.create` kwargs against `SessionCreateParams`. |

Add a test like the last one for any new Stripe call whose params are built dynamically. Use
`unsupported_stripe_params` from `customer_billing/tests/utils.py`.

## What a person still checks when the version tripwire fails

1. Read the warning (⚠️) entries in the
   [stripe-python CHANGELOG](https://github.com/stripe/stripe-python/blob/master/CHANGELOG.md) for every
   release between the reviewed version and the new one.
2. Search `customer_billing` (models, `stripe_event_handlers.py`, `tasks.py`, views) for every removed or
   retyped field. These are **response** fields we read; none of the tests above cover them.
3. If a named release changed, smoke-test self-service checkout and a Stripe webhook in stage.
4. Check that the Stripe Dashboard webhook endpoint's API version is still compatible with what
   `stripe_event_handlers.py` reads.
5. Update `REVIEWED_STRIPE_API_VERSION`.

## stripe 16 / `2026-09-30.endive` (October 2026)

- Checkout Session `payment_method_types` was replaced by `allowed_payment_method_types`. The new param
  filters the dynamically eligible payment methods instead of listing them, so card-only checkout needs
  `card` to stay enabled in the Dashboard payment method settings.
- Path params became positional-only (`stripe.Subscription.modify(sub_id, ...)`, never `id=sub_id`).
- No fields were removed from the SDK resource classes our code references (`checkout.Session`, `Charge`,
  `Customer`, `Event`, `Invoice`, `PaymentIntent`, `PaymentMethod`, `Price`, `Product`, `Subscription`).
  Resources we only see inside webhook payloads were not compared.
