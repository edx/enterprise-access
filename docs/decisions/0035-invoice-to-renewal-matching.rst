0035 Robust Invoice-to-Renewal Matching
========================================

Status
------

Proposed - March 2026

Context
-------

The ``customer_billing`` app processes Stripe ``invoice.paid`` webhook events to drive subscription
renewal workflows. A critical step in this process is finding the correct
``SelfServiceSubscriptionRenewal`` record that corresponds to a given ``invoice.paid`` event.

Current approach
~~~~~~~~~~~~~~~~

Today, ``_handle_invoice_paid_status_updated()`` and ``_process_trial_to_paid_renewal()`` locate
the renewal record by filtering on only the most recent "unprocessed" renewal
(unprocessed means we have not yet called `renew_subscription()
<https://github.com/edx/license-manager/blob/92f6abae/license_manager/apps/subscriptions/api.py#L74>`_
to renew the plan):

.. code-block:: python

   renewal = SelfServiceSubscriptionRenewal.objects.filter(
       checkout_intent=checkout_intent,
       processed_at__isnull=True,
   ).first()

This happens to work today **only** because we don't have any logic implemented
yet which generates the 2nd renewal, so it's currently impossible for an
unprocessed renewal to represent anything other than the first renewal.

Available data points
~~~~~~~~~~~~~~~~~~~~~

Stripe invoice events carry information that may be used for matching:

========================  ====================================================
Data point                Source
========================  ====================================================
Stripe Invoice ID         ``invoice.id``
Stripe Subscription ID    ``invoice.parent.subscription_details.subscription``
CheckoutIntent UUID       ``invoice.parent.checkout_intent_uuid``
Billing period start/end  ``invoice.lines.data[0].period.start`` / ``.end``
========================  ====================================================

The ``StripeEventSummary`` model already extracts and indexes ``stripe_invoice_id`` and
``stripe_subscription_id`` from invoice events (see ``populate_with_summary_data()``), but no attempt is
made to find and link that with a ``SelfServiceSubscriptionRenewal`` object. Likewise,
``SelfServiceSubscriptionRenewal`` has no field which can be used to link back to an invoice.

Therefore, there is no join path between an invoice event and the specific renewal it should
trigger.

Important note on event timing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Stripe ``invoice.upcoming`` events do **not** contain an invoice ID — they are a preview
notification only. The earliest event that carries an actual invoice object (and therefore an
invoice ID) is ``invoice.created``. This ADR uses ``invoice.created`` as the linkage event.

Decision
--------

Add ``stripe_invoice_id`` and ``effective_date`` fields to ``SelfServiceSubscriptionRenewal``. Use
``invoice.created`` events to link invoices to renewals so that ``invoice.paid`` handlers can
perform a direct lookup.

Schema change
~~~~~~~~~~~~~

Add two nullable, indexed fields to ``SelfServiceSubscriptionRenewal``:

.. code-block:: python

   stripe_invoice_id = models.CharField(
       max_length=255,
       null=True,
       blank=True,
       db_index=True,
       help_text="The Stripe invoice ID that this renewal corresponds to.",
   )
   effective_date = models.DateTimeField(
       null=True,
       blank=True,
       db_index=True,
       help_text=(
           "The datetime at which this renewal is expected to take effect, "
           "derived from the period end of the previous subscription cycle. "
           "This is a local cache of the like-named column on the "
           "license-manager SubscriptionPlanRenewal model."
       ),
   )

When to populate each field
~~~~~~~~~~~~~~~~~~~~~~~~~~~

- **``effective_date``**: Set at **renewal creation time**. Since ``SelfServiceSubscriptionRenewal``
  records are created at the beginning of the previous period (long before the invoice exists),
  ``effective_date`` is available immediately.
- **``stripe_invoice_id``**: Set by the **``invoice.created`` handler** when Stripe generates the
  invoice for the upcoming billing cycle.

Populating the ``effective_date`` field
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

At renewal creation time, we'll be in a code context which already has access to the current
subscription period metadata, including the period expiration (i.e. end) datetime. Simply use that
to populate ``effective_date``.

During trial provisioning, this can be accomplished with direct access to the workflow:

.. code-block:: python

   renewal_effective_date = accumulated_output.create_trial_subscription_plan_output.expiration_date

During subsequent renewals, we can similarly introspect either the expiration_date of the previous
plan or the start_date of the new plan.

Populating the ``stripe_invoice_id`` field
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A new ``invoice.created`` handler will perform the linkage using date math, e.g.:

.. code-block:: python

   stripe_subscription_id = invoice.parent.subscription_details.subscription
   invoice_period_start = invoice.lines.data[0].period.start
   renewal = SelfServiceSubscriptionRenewal.objects.filter(
       stripe_subscription_id=stripe_subscription_id,
       effective_date__date=invoice_period_start.date(),  # date math prevents mismatches off by milliseconds.
   ).first()
   renewal.stripe_invoice_id = invoice.id

Implementation Notes:

- **Immutability guard**: If the renewal already has a ``stripe_invoice_id`` set, log and
  return without overwriting. This also helps achieve idempotency.
- The ``persist_stripe_event()`` function and the ``on_stripe_event`` wrapper
  validation must be updated to handle ``invoice.created`` events the same way
  they handle ``invoice.paid`` events

Updated lookup pattern
~~~~~~~~~~~~~~~~~~~~~~

The ``invoice.paid`` handler can now look up the renewal directly:

.. code-block:: python

   renewal = SelfServiceSubscriptionRenewal.objects.filter(
       stripe_invoice_id=invoice.id,
   ).first()

   if not renewal:
       logger.error(
           "No SelfServiceSubscriptionRenewal found for checkout_intent %s "
           "with stripe_invoice_id %s",
           checkout_intent.id, invoice.id,
       )
       return

Backfill strategy
~~~~~~~~~~~~~~~~~

Existing ``SelfServiceSubscriptionRenewal`` records (all trial-to-paid) need backfills which are
greatly simplified by the fact that there is only ONE renewal so far for any customer in prod.

**``stripe_invoice_id``** — Populate with the only paid invoice for the customer.
**``effective_date``** — Populate with the period.end of the trial invoice for the customer.

Both can be implemented within a single data migration.

Alternatives Considered
-----------------------

1. Match by renewal record creation timestamp
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use the invoice's ``period.start``/``period.end`` to correlate with renewal creation timestamps.

**Rejected**: There's no guarantee or contract about when SelfServiceSubscriptionRenewal records are
created, which makes this approach brittle.

2. Match by sorting + zipping
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Both renewals and invoice.paid events can be separately sorted and zipped during runtime. Then,
just find the renewal which lines up with the invoice.paid event corresponding to the invoice in
hand.

**Rejected**: This is just indirect and susceptible to off-by-one errors.

3. Use Stripe metadata to embed renewal ID in the invoice
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Set ``metadata.renewal_id`` on the Stripe subscription or invoice so that incoming webhook events
carry the renewal ID directly.

**Rejected**: Honestly, I couldn't figure out how to do this and gave up. Invoices are
auto-generated by the stripe subscription, and we can't control metadata injection at that point.
We can inject metadata on the subscription itself pretty easily, but that's not helpful because
subscriptions to invoices are one-to-many.

4. Use ``invoice.upcoming`` for linkage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Link the renewal when the ``invoice.upcoming`` event arrives.

**Rejected**: Stripe ``invoice.upcoming`` events do not contain an actual invoice object or
invoice ID — they are a notification-only event. The earliest event carrying an invoice ID is
``invoice.created``.

Consequences
------------

Positive
~~~~~~~~

- **Deterministic matching**: Invoice-to-renewal lookup becomes an exact-match query instead of
  a heuristic.
- **Auditable**: The stored ``stripe_invoice_id`` creates a permanent, queryable link for
  debugging and reconciliation.

Risks
~~~~~

- If Stripe delivers ``invoice.created`` and ``invoice.paid`` events out of order (e.g.,
  ``invoice.created`` delivery retry #3 occurs after ``invoice.paid`` try #1), then the ``invoice.paid`` event
  handler won't have a link to a renewal that it can depend on. We should probably make sure the
  ``invoice.paid`` handler throws a 500 error in these situations to force a retry.
