"""
Backfill CheckoutIntent.ssp_product to teams-yearly for existing rows.
"""
from django.db import migrations


TEAMS_YEARLY_SLUG = 'teams-yearly'


def backfill_checkoutintent_ssp_product(apps, schema_editor):
    """
    Set ssp_product to teams-yearly for all CheckoutIntent rows where it is NULL.
    """
    CheckoutIntent = apps.get_model('customer_billing', 'CheckoutIntent')
    SspProduct = apps.get_model('customer_billing', 'SspProduct')

    null_ssp_product_qs = CheckoutIntent.objects.filter(ssp_product__isnull=True)
    if not null_ssp_product_qs.exists():
        return

    teams_yearly_product = SspProduct.objects.filter(slug=TEAMS_YEARLY_SLUG).first()
    if not teams_yearly_product:
        raise ValueError(
            f"SspProduct with slug '{TEAMS_YEARLY_SLUG}' must exist before running this migration."
        )

    null_ssp_product_qs.update(
        ssp_product_id=teams_yearly_product.slug,
    )


def reverse_backfill_checkoutintent_ssp_product(apps, schema_editor):
    """
    Set ssp_product back to NULL for rows set to teams-yearly.
    """
    CheckoutIntent = apps.get_model('customer_billing', 'CheckoutIntent')

    CheckoutIntent.objects.filter(ssp_product_id=TEAMS_YEARLY_SLUG).update(ssp_product_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0035_checkoutintent_ssp_product'),
    ]

    operations = [
        migrations.RunPython(
            backfill_checkoutintent_ssp_product,
            reverse_backfill_checkoutintent_ssp_product,
        ),
    ]
