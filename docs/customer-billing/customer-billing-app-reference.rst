Customer Billing App Reference
==============================

Overview
--------

The ``customer_billing`` Django app owns the self-service purchase (SSP) layer: initial checkout,
subscription lifecycle state, Stripe webhook processing, and email notifications. It acts as the
central coordinator between Stripe, License Manager, LMS, Braze, and Salesforce.

Source: ``enterprise_access/apps/customer_billing/``

Models
------

**SspProduct** (``models.py:49``)

Universal product catalog entry. ``slug`` is the cross-service key stored in Stripe Price metadata
(provisioned via Terraform) and passed to Salesforce. Two product categories: Teams and
Academy-specific. Academy display metadata (title, description, thumbnail) is fetched lazily from
``enterprise-catalog`` via ``academy_api.py`` and instance-cached.

**CheckoutIntent** (``models.py:158``)

State machine tracking the full checkout and subscription lifecycle. One per user/product pair
(enforced via ``unique_user_ssp_product`` constraint). Reserves the enterprise slug and name for 24
hours (matching Stripe's checkout session TTL). State machine:

.. code-block:: text

    CREATED → PAID → FULFILLED                  (happy path)
    CREATED → EXPIRED                           (24-hour timeout, no payment)
    PAID    → ERRORED_BACKOFFICE                (Salesforce integration failure)
    PAID    → ERRORED_PROVISIONING              (provisioning workflow failure)
    PAID    → ERRORED_FULFILLMENT_STALLED       (workflow never started/completed)
    ERRORED_* → FULFILLED                       (recovery path)

Links to ``provisioning.ProvisionNewCustomerWorkflow`` once fulfilled, and stores
``stripe_customer_id`` and ``stripe_checkout_session_id`` after checkout creation.

**SelfServiceSubscriptionRenewal** (``models.py:818``)

Tracks trial-to-paid transitions and annual renewals. Bridges ``CheckoutIntent`` to
``license-manager`` ``SubscriptionPlanRenewal`` records. The ``stripe_invoice_id`` field is linked
during ``invoice.created`` processing so the ``invoice.paid`` handler can look up the renewal
directly by invoice ID rather than time-based matching — this guards against out-of-order webhook
delivery.

**StripeEventData** (``models.py:913``)

Raw Stripe event payload storage. PII-flagged (``email_address``) — scrubbed after 90 days via
management command. Linked to ``CheckoutIntent`` via Stripe customer ID cross-referenced with
subscription metadata (``checkout_intent_uuid`` preferred; ``checkout_intent_id`` as legacy
fallback). ``handled_at`` is set after all handler logic completes successfully.

**StripeEventSummary** (``models.py:965``)

Normalized projection of ``StripeEventData`` with extracted subscription and invoice fields.
Populated via ``post_save`` signal on ``StripeEventData``. Primary purpose: lets handlers
reconstruct "previous state" for change detection (e.g., detecting when ``cancel_at`` is newly set)
without parsing raw JSON each time. Also stores ``upcoming_invoice_amount_due`` fetched from Stripe
at subscription creation, used in the trial-ending reminder email.

Key Modules
-----------

**api.py** — Checkout entry point

``CheckoutSessionInputValidator`` validates each field independently:

* ``admin_email``: format + LMS user registration check
* ``enterprise_slug``: format + ``CheckoutIntent`` reservation check + LMS conflict check
* ``company_name``: non-null + ``CheckoutIntent`` reservation check + LMS conflict check
* ``quantity``: positive integer within range defined by ``pricing_api``
* ``ssp_product_slug`` / ``stripe_price_id``: validated against active Stripe prices

``create_free_trial_checkout_session`` orchestrates the checkout setup:

1. Validate all inputs
2. Create ``CheckoutIntent`` (reserves slug/name atomically)
3. Create Stripe checkout session via ``stripe_api``
4. Store Stripe session ID and customer ID on the intent

**stripe_api.py** — Stripe SDK wrappers

Thin wrappers around the Stripe SDK, all decorated with ``@stripe_cache()`` using ``TieredCache``.
Covers checkout sessions, subscriptions, invoices, payment methods, customers, and upcoming invoice
preview. ``create_subscription_checkout_session`` embeds ``checkout_intent_uuid`` and
``checkout_intent_id`` in Stripe subscription metadata for later webhook lookup.

**stripe_event_handlers.py** — Webhook dispatch

``StripeEventHandler`` uses a decorator-based registry (``@on_stripe_event``). Every handler:
validates event type, persists ``StripeEventData``, runs business logic, marks event handled.

+------------------------------------------+--------------------------------------------------------------+
| Event                                    | Action                                                       |
+==========================================+==============================================================+
| ``invoice.paid`` (total = 0)             | Mark ``CheckoutIntent`` as PAID                              |
+------------------------------------------+--------------------------------------------------------------+
| ``invoice.paid`` (total > 0)             | Process trial→paid renewal, send receipt email               |
+------------------------------------------+--------------------------------------------------------------+
| ``invoice.created``                      | Link invoice to ``SelfServiceSubscriptionRenewal`` by date   |
+------------------------------------------+--------------------------------------------------------------+
| ``customer.subscription.created``        | Enable ``pending_if_incomplete``, update Stripe customer ID  |
+------------------------------------------+--------------------------------------------------------------+
| ``customer.subscription.updated``        | Detect status/cancellation/payment-method changes            |
+------------------------------------------+--------------------------------------------------------------+
| ``customer.subscription.deleted``        | Cancel future plans, send finalized cancellation email       |
+------------------------------------------+--------------------------------------------------------------+
| ``customer.subscription.trial_will_end`` | Send 72-hour trial ending reminder                           |
+------------------------------------------+--------------------------------------------------------------+

``invoice.paid`` and ``invoice.created`` are filtered by ``_valid_invoice_event_type``:
only events whose first line item has type ``subscription_item_details`` are processed.
Other invoice events (one-off, non-SSP) return early without error.

**pricing_api.py**

Fetches active ``SspProduct`` pricing from Stripe by lookup key. Used by
``CheckoutSessionInputValidator`` for quantity range validation.

**academy_api.py**

Fetches and caches Academy metadata from ``enterprise-catalog``. Called by ``SspProduct``
property accessors (``academy_title``, ``academy_description``, etc.).

**embargo.py**

Country-based embargo check. Gates checkout by the customer's country field on ``CheckoutIntent``.

**signals.py**

Post-save signal on ``StripeEventData`` that creates/updates the corresponding
``StripeEventSummary`` record.

Celery Tasks (tasks.py)
-----------------------

All tasks use ``LoggedTaskWithRetry`` as base class. All send Braze API-triggered campaign emails to
enterprise admins fetched from the LMS. Admin lookup is done via ``get_enterprise_admins`` which
calls ``LmsApiClient.get_enterprise_customer_data``.

+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| Task                                                     | Trigger                           | Braze Campaign Setting                       |
+==========================================================+===================================+==============================================+
| ``send_enterprise_provision_signup_confirmation_email``  | Provisioning workflow complete    | ``[BEP]_SIGNUP_CONFIRMATION_CAMPAIGN``       |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| ``send_trial_ending_reminder_email_task``                | ``trial_will_end`` (72 hr before) | ``[BEP]_TRIAL_ENDING_SOON_CAMPAIGN``         |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| ``send_trial_end_and_subscription_started_email_task``   | ``invoice.paid`` (total > 0,      | ``[BEP]_TRIAL_END_SUBSCRIPTION_STARTED``     |
|                                                          | first paid invoice)               | ``_CAMPAIGN``                                |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| ``send_payment_receipt_email``                           | ``invoice.paid`` (total > 0)      | ``[BEP]_PAYMENT_RECEIPT_CAMPAIGN``           |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| ``send_billing_error_email_task``                        | Subscription → ``past_due``       | ``BRAZE_BILLING_ERROR_CAMPAIGN``             |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| ``send_trial_cancellation_email_task``                   | ``cancel_at`` newly set,          | ``BRAZE_TRIAL_CANCELLATION_CAMPAIGN``        |
|                                                          | status = ``trialing``             |                                              |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| ``send_paid_cancellation_email_task``                    | ``cancel_at`` newly set,          | ``BRAZE_PAID_CANCELLATION_CAMPAIGN``         |
|                                                          | status = ``active``               |                                              |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| ``send_reinstatement_email_task``                        | ``cancel_at`` cleared to null     | ``BRAZE_SSP_SUBSCRIPTION_REINSTATED``        |
|                                                          |                                   | ``_CAMPAIGN``                                |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+
| ``send_finalized_cancelation_email_task``                | ``subscription.deleted``,         | ``BRAZE_SSP_CANCELATION_FINALIZATION``       |
|                                                          | prior status = ``active``         | ``_CAMPAIGN``                                |
+----------------------------------------------------------+-----------------------------------+----------------------------------------------+

Key: ``[BEP] = BRAZE_ENTERPRISE_PROVISION``

Notes:

* ``send_payment_receipt_email`` and ``send_trial_end_and_subscription_started_email_task`` both
  fire on the same ``invoice.paid`` event. Receipt fires first; the renewal handler may raise to
  force a Stripe retry, which could produce a duplicate receipt — mitigated by a Braze dedup window.
* ``send_payment_receipt_email`` falls back to the ``CheckoutIntent.user`` email if the enterprise
  isn't provisioned yet (LMS returns no admins).
* ``send_trial_ending_reminder_email_task`` fetches live data from Stripe (trialing subscription,
  payment method, upcoming invoice amount) to populate email properties.
* The three cancellation tasks delegate to ``_send_cancelation_campaign``; only the Braze campaign
  ID and timestamp semantics differ.

Management Commands
-------------------

+------------------------------------+------------------------------------------------------------------+
| Command                            | Purpose                                                          |
+====================================+==================================================================+
| ``cleanup_checkout_intents``       | Expire ``CREATED`` intents past ``expires_at``                   |
+------------------------------------+------------------------------------------------------------------+
| ``mark_stalled_checkout_intents``  | Flag ``PAID`` intents stuck > 3 min as                           |
|                                    | ``ERRORED_FULFILLMENT_STALLED``                                  |
+------------------------------------+------------------------------------------------------------------+
| ``fetch_and_handle_stripe_events`` | Backfill/replay Stripe events by fetching from the Stripe API    |
+------------------------------------+------------------------------------------------------------------+
| ``backfill_subscription_renewals`` | Data migration helper for ``SelfServiceSubscriptionRenewal``     |
+------------------------------------+------------------------------------------------------------------+
| ``populate_stripe_event_summaries``| Backfill ``StripeEventSummary`` records from existing event data |
+------------------------------------+------------------------------------------------------------------+

External Service Dependencies
------------------------------

+---------------------+-----------------------------------------------------------------------+
| Service             | Role                                                                  |
+=====================+=======================================================================+
| Stripe              | Checkout sessions, subscriptions, invoices, billing portal, webhooks  |
+---------------------+-----------------------------------------------------------------------+
| license-manager     | Subscription plan creation, renewal processing, plan                  |
|                     | activation/deactivation                                               |
+---------------------+-----------------------------------------------------------------------+
| enterprise-catalog  | Academy product metadata                                              |
+---------------------+-----------------------------------------------------------------------+
| LMS                 | User account lookup, enterprise customer slug/name conflict detection |
+---------------------+-----------------------------------------------------------------------+
| Braze               | All transactional email notifications                                 |
+---------------------+-----------------------------------------------------------------------+
| Segment             | Analytics (``subscription.canceled``, checkout intent lifecycle)      |
+---------------------+-----------------------------------------------------------------------+
| Salesforce          | Downstream from provisioning workflow; failure surfaces as            |
|                     | ``ERRORED_BACKOFFICE`` on ``CheckoutIntent``                          |
+---------------------+-----------------------------------------------------------------------+
