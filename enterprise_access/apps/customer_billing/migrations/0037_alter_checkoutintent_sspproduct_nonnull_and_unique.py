"""
Backfill any remaining NULL ssp_product rows, make CheckoutIntent.ssp_product
non-nullable, change user from OneToOneField to ForeignKey, and add a unique
constraint on (user, ssp_product).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


TEAMS_YEARLY_SLUG = 'teams-yearly'


def backfill_null_ssp_product(apps, schema_editor):
    """
    Backfill any CheckoutIntent rows that still have NULL ssp_product.
    Re-uses the same approach from migration 0036.
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

    # Use the FK id (slug) for backpopulation, matching migration 0036's approach.
    updated = null_ssp_product_qs.update(ssp_product_id=teams_yearly_product.slug)
    print(f"\n  Backfilled {updated} CheckoutIntent row(s) with ssp_product_id='{TEAMS_YEARLY_SLUG}'.")


def check_no_nulls(apps, schema_editor):
    """
    Guard: fail fast with a clear error if any NULL ssp_product rows still
    exist after the backfill above.
    """
    CheckoutIntent = apps.get_model('customer_billing', 'CheckoutIntent')
    null_count = CheckoutIntent.objects.filter(ssp_product__isnull=True).count()
    if null_count > 0:
        raise Exception(
            f"Cannot proceed: {null_count} CheckoutIntent row(s) still have "
            f"NULL ssp_product after backfill. Investigate before re-running."
        )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('customer_billing', '0036_backfill_checkoutintent_ssp_product_teams_yearly'),
    ]

    operations = [
        # 1. Backfill any remaining NULLs (re-uses logic from 0036)
        migrations.RunPython(backfill_null_ssp_product, migrations.RunPython.noop),

        # 2. Guard: fail if NULLs somehow still remain
        migrations.RunPython(check_no_nulls, migrations.RunPython.noop),

        # 3. Change user: OneToOneField → ForeignKey
        migrations.AlterField(
            model_name='checkoutintent',
            name='user',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to=settings.AUTH_USER_MODEL,
            ),
        ),

        # 4. Make ssp_product non-nullable (hardcoded default for migration stability)
        migrations.AlterField(
            model_name='checkoutintent',
            name='ssp_product',
            field=models.ForeignKey(
                default='teams-yearly',
                help_text='The SSP product associated with this checkout intent.',
                on_delete=django.db.models.deletion.PROTECT,
                to='customer_billing.sspproduct',
            ),
        ),

        # 5. Add unique constraint on (user, ssp_product)
        migrations.AddConstraint(
            model_name='checkoutintent',
            constraint=models.UniqueConstraint(
                fields=('user', 'ssp_product'),
                name='unique_user_ssp_product',
            ),
        ),
    ]