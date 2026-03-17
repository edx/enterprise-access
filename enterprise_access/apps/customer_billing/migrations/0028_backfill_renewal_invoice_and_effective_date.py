"""
Backfill stripe_invoice_id and effective_date on existing SelfServiceSubscriptionRenewal records.

Context: ADR 0035 introduced these fields to enable deterministic invoice-to-renewal matching.
Existing renewal records (all trial-to-paid) need to be backfilled. Since there is only ONE
renewal per customer in production, the backfill logic is straightforward:

- stripe_invoice_id: copied from the first invoice.paid StripeEventSummary with amount > 0
  for the same checkout_intent.
- effective_date: extracted from the trial invoice's (amount_paid=0) line-item period.end
  in the raw StripeEventData payload for the same checkout_intent.
"""
import logging
from datetime import datetime, timezone

from django.db import migrations, models

logger = logging.getLogger(__name__)


def backfill_renewal_fields(apps, schema_editor):
    """
    Populate stripe_invoice_id and effective_date on SelfServiceSubscriptionRenewal
    records that are missing them.
    """
    SelfServiceSubscriptionRenewal = apps.get_model(
        'customer_billing', 'SelfServiceSubscriptionRenewal',
    )
    StripeEventSummary = apps.get_model(
        'customer_billing', 'StripeEventSummary',
    )

    renewals = SelfServiceSubscriptionRenewal.objects.filter(
        models.Q(stripe_invoice_id__isnull=True) | models.Q(effective_date__isnull=True),
    )

    for renewal in renewals:
        checkout_intent = renewal.checkout_intent
        updated_fields = []

        # --- stripe_invoice_id ---
        if not renewal.stripe_invoice_id:
            paid_summary = StripeEventSummary.objects.filter(
                checkout_intent=checkout_intent,
                event_type='invoice.paid',
                invoice_amount_paid__gt=0,
            ).first()
            if paid_summary and paid_summary.stripe_invoice_id:
                renewal.stripe_invoice_id = paid_summary.stripe_invoice_id
                updated_fields.append('stripe_invoice_id')

        # --- effective_date ---
        if not renewal.effective_date:
            trial_summary = StripeEventSummary.objects.filter(
                checkout_intent=checkout_intent,
                event_type='invoice.paid',
                invoice_amount_paid=0,
                stripe_event_data__isnull=False,
            ).first()
            if trial_summary:
                try:
                    raw_data = trial_summary.stripe_event_data.data
                    period_end_ts = raw_data['data']['object']['lines']['data'][0]['period']['end']
                    renewal.effective_date = datetime.fromtimestamp(period_end_ts, tz=timezone.utc)
                    updated_fields.append('effective_date')
                except (KeyError, IndexError, TypeError, ValueError):
                    logger.warning(
                        'Could not extract period.end from trial invoice event for renewal %s',
                        renewal.id,
                    )

        if updated_fields:
            updated_fields.append('modified')
            renewal.save(update_fields=updated_fields)


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0027_add_renewal_invoice_and_effective_date'),
    ]

    operations = [
        migrations.RunPython(backfill_renewal_fields, reverse_code=migrations.RunPython.noop),
    ]
