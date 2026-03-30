# Generated migration to make stripe_event_created_at non-nullable.

from django.db import migrations, models


def backfill_stripe_event_created_at(apps, schema_editor):
    """Fill any NULL stripe_event_created_at values with the record's created timestamp."""
    StripeEventSummary = apps.get_model('customer_billing', 'StripeEventSummary')
    StripeEventSummary.objects.filter(stripe_event_created_at__isnull=True).update(
        stripe_event_created_at=models.F('created')
    )


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0031_backfill_subscription_renewal_cancellations'),
    ]

    operations = [
        migrations.RunPython(
            backfill_stripe_event_created_at,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='stripeeventsummary',
            name='stripe_event_created_at',
            field=models.DateTimeField(db_index=True, help_text='Timestamp when the Stripe event was created'),
        ),
    ]
