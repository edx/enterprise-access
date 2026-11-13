"""
Backfill EnterpriseStripeCustomer from CheckoutIntent (ENT-12509).

Kept apart from 0043's schema change: MySQL can't roll back DDL, so a backfill that failed inside 0043 would leave the
tables behind with 0043 unrecorded, and every later ``migrate`` would fail on the existing table. Here a failure rolls
back this migration's own transaction, and ``migrate`` can simply run it again.
"""
from django.db import migrations
from django.utils import timezone

BATCH_SIZE = 500


def backfill_enterprise_stripe_customers(apps, schema_editor):
    """
    Map each enterprise to the Stripe customer on its newest CheckoutIntent (by ``created``) whose stripe_customer_id
    is neither NULL nor empty, the customer billing management already used, and give each new mapping its initial
    history record. Runs a fixed number of queries. Enterprises already mapped are skipped, so this can run again
    without duplicating mappings or history.
    """
    CheckoutIntent = apps.get_model('customer_billing', 'CheckoutIntent')
    EnterpriseStripeCustomer = apps.get_model('customer_billing', 'EnterpriseStripeCustomer')
    HistoricalEnterpriseStripeCustomer = apps.get_model('customer_billing', 'HistoricalEnterpriseStripeCustomer')

    already_mapped = set(EnterpriseStripeCustomer.objects.values_list('enterprise_customer_uuid', flat=True))
    candidates = CheckoutIntent.objects.filter(
        enterprise_uuid__isnull=False,
    ).exclude(
        stripe_customer_id__isnull=True,
    ).exclude(
        stripe_customer_id='',
    ).order_by('enterprise_uuid', '-created').values_list('enterprise_uuid', 'stripe_customer_id')

    newest_customer_by_enterprise = {}
    for enterprise_uuid, stripe_customer_id in candidates.iterator():
        if enterprise_uuid not in already_mapped:
            newest_customer_by_enterprise.setdefault(enterprise_uuid, stripe_customer_id)

    EnterpriseStripeCustomer.objects.bulk_create(
        [
            EnterpriseStripeCustomer(
                enterprise_customer_uuid=enterprise_uuid,
                stripe_customer_id=stripe_customer_id,
                source='checkout_intent_backfill',
            )
            for enterprise_uuid, stripe_customer_id in newest_customer_by_enterprise.items()
        ],
        batch_size=BATCH_SIZE,
    )

    # bulk_create() skips simple-history's post_save hook, and the history manager's bulk helpers aren't available to
    # migration models, so the '+' records are written here. MySQL doesn't return the new primary keys, so read the
    # new rows back: every mapping except those that existed before this run.
    history_date = timezone.now()
    new_mappings = EnterpriseStripeCustomer.objects.exclude(enterprise_customer_uuid__in=already_mapped)
    HistoricalEnterpriseStripeCustomer.objects.bulk_create(
        [
            HistoricalEnterpriseStripeCustomer(
                history_date=history_date,
                history_type='+',
                history_change_reason='Backfilled from CheckoutIntent (ENT-12509)',
                **{field.attname: getattr(mapping, field.attname) for field in mapping._meta.fields},
            )
            for mapping in new_mappings.iterator()
        ],
        batch_size=BATCH_SIZE,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0043_enterprisestripecustomer'),
    ]

    operations = [
        # Reversing leaves the mappings in place: they map the same customers CheckoutIntent does, and reversing 0043
        # drops the tables.
        migrations.RunPython(backfill_enterprise_stripe_customers, reverse_code=migrations.RunPython.noop),
    ]
