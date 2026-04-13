Subscription and Renewal Lifecycle
==================================

Overview
--------

The customer billing domain manages the complete lifecycle of self-service enterprise subscriptions,
from initial checkout through annual renewals. This system orchestrates interactions between Stripe (payments),
Salesforce (CRM), License Manager (subscription management), and Braze (email notifications)
to provide a seamless subscription experience.

**Key Principles:**

* **One CheckoutIntent per Customer**: Each enterprise customer has a single CheckoutIntent that serves as the long-lived subscription tracker
* **Event-Driven Architecture**: Stripe webhooks drive state transitions and trigger downstream processing
* **Service Orchestration**: Enterprise Access acts as the central coordinator between external systems
* **Audit Trail**: All events and state changes are persisted for debugging and compliance

Architecture Overview
---------------------

The subscription lifecycle spans five main services, each with distinct responsibilities:

**Stripe (Payment Processing)**

* Manages payment methods, invoicing, and billing cycles
* Sends webhooks for customer-driven subscription state changes
* Stores subscription metadata linking back to enterprise record (the ``CheckoutIntent`` identifier)

**Salesforce (CRM & Opportunity Management)**

* Tracks sales opportunities and revenue recognition
* Creates Opportunity Line Items (OLIs) for accounting
* Initiates provisioning requests via APIs

**Enterprise Access (Orchestration)**

* Processes Stripe webhooks and maintains customer subscription state
* Provides provisioning API for Salesforce integration
* Provides REST and BFF APIs for integration with our frontends
* Orchestrates License Manager ``SubscriptionPlan`` and renewal operations
* Triggers Braze email campaigns at key lifecycle events

**License Manager (Subscription & License Management)**

* Manages subscription plans and license allocation (i.e. the core edX Enterprise subscription domain records)
* Processes subscription renewals and transitions

**Braze (Email Notifications)**

* Handles transactional email delivery to enterprise admins
* Campaigns are triggered via API from Enterprise Access Celery tasks

Core Models and Relationships
-----------------------------

**CheckoutIntent**

The central subscription tracker that maintains the complete lifecycle of an enterprise customer's subscription.
Originally designed for checkout sessions, it has evolved into the permanent record linking all
self-service-subscription-related activities for a given customer.

Key Fields:

* ``enterprise_uuid``: Links to the enterprise customer in downstream systems
* ``stripe_customer_id``: Links to the Stripe customer record
* ``state``: Tracks the overall subscription state (CREATED -> PAID -> FULFILLED)
* ``workflow``: Links to the provisioning workflow that created the subscription

**StripeEventData**

Persists the raw payload from all Stripe webhook events that we handle. Ultimately facilitates core business logic,
audit, and debugging purposes.

Key Fields:

* ``event_id``: Stripe's unique event identifier
* ``event_type``: The type of Stripe event (e.g., 'invoice.paid', 'customer.subscription.updated')
* ``checkout_intent``: Links the event to the relevant subscription
* ``data``: Complete JSON payload from Stripe

**StripeEventSummary**

Extracts and normalizes key data from Stripe events for easier querying and analysis, especially for
cross-service data-linkage.

Key Fields:

* ``subscription_plan_uuid``: Links to the License Manager subscription plan
* ``stripe_subscription_id``: Stripe's subscription identifier
* ``subscription_status``: Current Stripe subscription status (trialing, active, past_due, canceled)
* ``subscription_cancel_at``: Timestamp when subscription is scheduled to cancel (if any)

**SelfServiceSubscriptionRenewal**

Tracks the processing of subscription renewals,
particularly the transition from trial to paid and subsequent annual renewals.

Key Fields:

* ``subscription_plan_renewal_id``: UUID of the renewal record in License Manager
* ``stripe_event_data``: Links to the specific Stripe event that triggered the renewal
* ``stripe_invoice_id``: Links to the Stripe invoice for this renewal period
* ``processed_at``: Timestamp when the renewal was successfully processed
* ``is_canceled``: Whether the subscription is currently canceled
* ``subscription_cancel_at``: Timestamp when the subscription is scheduled to cancel

Subscription Lifecycle States
-----------------------------

**Trial Creation and Provisioning**

When a customer completes self-service checkout,
Stripe creates a subscription in trial status and sends an ``invoice.paid`` event (amount=$0). This triggers:

1. CheckoutIntent state transitions from CREATED -> PAID -> FULFILLED
2. Salesforce receives the webhook and creates Account/Contact/Opportunity records
3. Salesforce calls the ``/provisioning`` API to create our internal enterprise customer and subscription records
   (for both the trial and 1st paid plan) via API calls to downstream services

*Braze Emails:* Signup confirmation email sent after provisioning completes.

* ``BRAZE_ENTERPRISE_PROVISION_SIGNUP_CONFIRMATION_CAMPAIGN`` - Signup confirmation with subscription dates, amounts, and names.

**Trial Period (72 Hours Before End)**

Stripe sends a ``customer.subscription.trial_will_end`` event 72 hours before the trial ends.

*Braze Emails:* ``BRAZE_ENTERPRISE_PROVISION_TRIAL_ENDING_SOON_CAMPAIGN`` - Reminder email with
subscription details, renewal amount, and management link.

**Trial-to-Paid Transition**

When the trial period ends, Stripe automatically:

1. Transitions the subscription to ``active`` status
2. Creates and charges the first invoice (amount > $0), sending an ``invoice.created`` event.
3. Charges the first invoice (amount > $0), sending an ``invoice.paid`` event.

On ``invoice.paid`` (amount > $0), Enterprise Access:

1. Links the invoice to the corresponding ``SelfServiceSubscriptionRenewal`` (matched by ``stripe_invoice_id``)
2. Calls License Manager's ``/api/v1/provisioning-admins/subscription-plan-renewals/{id}/process/`` endpoint
3. License Manager processes the renewal from trial -> paid subscription plan
4. Marks the ``SelfServiceSubscriptionRenewal`` as processed
5. Activates the paid subscription plan in License Manager

*Braze Emails:*

* ``BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN`` - Payment receipt with amount, license count, billing details
* ``BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN`` - Confirmation that trial has ended and paid subscription is active

**Active Subscription Management**

During the active subscription period:

* Subsequent ``invoice.paid`` events (amount > $0) trigger payment receipt emails
* Salesforce creates paid Opportunity Line Items
* Salesforce calls ``/api/v1/provisioning/subscription-plan-oli-update`` API to associate the OLI with the paid subscription plan

*Braze Emails:* ``BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN`` on each successful payment.

**Payment Errors (past_due)**

We rely on Stripe's Pending Updates feature to help prevent subscriptions from becoming active
before a payment is *successfully* processed. When a payment fails, the Stripe subscription enters a ``past_due``
state via ``customer.subscription.updated``. When we observe this state:

* All *future* License Manager subscription plans related to the Stripe subscription are deactivated (``is_active=False``)
* We do this regardless of whether the corresponding renewal has been processed

*Braze Emails:* ``BRAZE_BILLING_ERROR_CAMPAIGN`` - Notifies admins of payment failure with link to update payment method.

**Subscription Cancellation (Scheduled)**

When a customer schedules cancellation (e.g., via Stripe billing portal), Stripe sends
``customer.subscription.updated`` with ``cancel_at`` set to a future timestamp.

On cancellation scheduling:

* ``SelfServiceSubscriptionRenewal`` records are updated with ``subscription_cancel_at`` timestamp
* **Note:** License Manager subscription plans are NOT modified at this point - the subscription
  remains active until the actual cancellation date

*Braze Emails:*

* **Trial subscriptions:** ``BRAZE_TRIAL_CANCELLATION_CAMPAIGN`` - Confirms scheduled cancellation during trial with end date
* **Active subscriptions:** ``BRAZE_PAID_CANCELLATION_CAMPAIGN`` - Confirms scheduled cancellation during paid plan with end date

**Subscription Reinstatement**

When a customer reverses a scheduled cancellation (removes the ``cancel_at`` date), Stripe sends
``customer.subscription.updated`` with ``cancel_at`` cleared to ``null``.

On reinstatement:

* ``SelfServiceSubscriptionRenewal`` records are updated: ``is_canceled=False``, ``subscription_cancel_at=None``
* **Note:** Because License Manager plans were never modified during cancellation scheduling,
  no License Manager changes are needed on reinstatement

*Braze Emails:* ``BRAZE_SSP_SUBSCRIPTION_REINSTATED_CAMPAIGN`` - Confirms subscription has been restored.

**Subscription Termination (Finalized)**

When the subscription actually ends (either at the scheduled ``cancel_at`` date or immediately),
Stripe sends ``customer.subscription.deleted``.

On subscription deletion:

* All future License Manager subscription plans are deactivated via ``cancel_all_future_plans()``
* ``SelfServiceSubscriptionRenewal`` records are updated: ``is_canceled=True``, ``subscription_cancel_at=None``
* Cancellation reason/feedback is tracked to Segment analytics

*Braze Emails:*

* **Previously active subscriptions only:** ``BRAZE_SSP_CANCELATION_FINALIZATION_CAMPAIGN`` - Final confirmation that subscription has ended
* **Trial subscriptions:** No finalization email (they already received the trial cancellation email)

**Annual Renewals**

i.e. the second and ensuing paid periods. TBD on the actual flow, here.

Braze Campaign Summary
----------------------

Key: ``[BEP] = BRAZE_ENTERPRISE_PROVISION``

+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| Event/Trigger                        | Braze Campaign Setting                              | Description                                            |
+======================================+=====================================================+========================================================+
| Provisioning complete                | ``[BEP]_SIGNUP_CONFIRMATION_CAMPAIGN``              | Signup confirmation with trial details                 |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| 72 hours before trial ends           | ``[BEP]_TRIAL_ENDING_SOON_CAMPAIGN``                | Trial ending reminder with renewal info                |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| Trial ends, paid subscription starts | ``[BEP]_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN``   | Confirmation of paid subscription start                |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| Invoice paid (amount > $0)           | ``[BEP]_PAYMENT_RECEIPT_CAMPAIGN``                  | Payment receipt with billing details                   |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| Subscription becomes past_due        | ``BRAZE_BILLING_ERROR_CAMPAIGN``                    | Payment failure notification                           |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| Trial cancellation scheduled         | ``BRAZE_TRIAL_CANCELLATION_CAMPAIGN``               | Scheduled cancellation confirmation during trial       |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| Paid plan cancellation scheduled     | ``BRAZE_PAID_CANCELLATION_CAMPAIGN``                | Scheduled cancellation confirmation during paid period |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| Cancellation reversed (reinstated)   | ``BRAZE_SSP_SUBSCRIPTION_REINSTATED_CAMPAIGN``      | Subscription restored confirmation                     |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+
| Active subscription deleted          | ``BRAZE_SSP_CANCELATION_FINALIZATION_CAMPAIGN``     | Final cancellation confirmation                        |
+--------------------------------------+-----------------------------------------------------+--------------------------------------------------------+

Event Processing Flows
----------------------

**Stripe Webhook Handlers**

See ``stripe_event_handlers.py`` for implementation details:

* ``invoice.created``: Links Stripe invoice to ``SelfServiceSubscriptionRenewal`` by matching subscription ID and effective date
* ``invoice.paid``: Triggers payment receipt, trial-to-paid processing (for amount > $0), or CheckoutIntent state update (for amount = $0)
* ``customer.subscription.created``: Enables pending updates, initializes cancellation state
* ``customer.subscription.updated``: Handles status changes, cancellation scheduling/reinstatement, payment method changes
* ``customer.subscription.deleted``: Deactivates plans, tracks cancellation analytics
* ``customer.subscription.trial_will_end``: Triggers trial ending reminder email

**Salesforce API Integration**

``POST /provisioning``:

* Initiates enterprise customer provisioning workflow
* Creates initial trial subscription plan in License Manager
* Links Salesforce Opportunity Line Item to trial subscription plan

``POST /oli-update``:

* Updates existing paid subscription plan with Salesforce OLI references
* Used when Salesforce creates paid OLIs

Data Relationships Across Services
----------------------------------

**Key Identifiers**

``stripe_customer_id``:

* Links CheckoutIntent to Stripe Customer records
* Used to correlate webhook events with enterprise customers
* Enables lookup of original CheckoutIntent for Year 2+ renewals

``enterprise_uuid``:

* The ``EnterpriseCustomer.uuid`` field

``checkout_intent_{id,uuid}``:

* Stored in Stripe subscription metadata
* Enables webhook events to find the correct CheckoutIntent

``salesforce_opportunity_line_item``:

* Links subscription plans to Salesforce accounting records
* Ensures revenue recognition and financial reporting alignment
* Used for idempotent API operations

Error Scenarios
---------------

**API Integration Failures**

* License Manager API timeouts during renewal processing
* Salesforce API failures during provisioning requests
* Braze API failures during email sending (retried via Celery)

**Data Consistency Issues**

* Missing CheckoutIntent records for webhook events
* Failed renewal processing leaving ``SelfServiceSubscriptionRenewal`` records in unprocessed state
* Out-of-order webhook delivery (e.g., ``invoice.paid`` before ``invoice.created``)

Future Considerations
--------------------

**Database Normalization Improvements**

Pros of Better Normalization:

* Cleaner separation of concerns between checkout sessions and long-lived subscriptions
* More explicit modeling of subscription lifecycle states
* Easier querying and reporting on subscription metrics

Cons of Current Approach:

* ``CheckoutIntent`` serves dual purposes (checkout + subscription tracking)
* Some fields may be irrelevant for long-lived subscription management
* Potential confusion about the model's primary purpose

Potential Improvements:

* Create dedicated ``Subscription`` model linked to CheckoutIntent
* Normalize subscription state tracking separate from checkout state
* Consider separating audit/event data from operational data

**Consolidated External System Integration**

Currently, two external systems (Salesforce, Stripe)
can trigger actions in enterprise-access through webhooks and API calls.
Furthermore, Stripe can trigger Salesforce record creation and other actions prior
to Salesforce, in turn, triggering actions in enterprise-access.

**Email Campaign Gaps**

* Payment recovery success email (when payment succeeds after past_due)
