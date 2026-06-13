"""
Make CheckoutIntent.ssp_product non-nullable, change user to ForeignKey, and add unique constraint on (user, ssp_product).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def check_no_nulls(apps, schema_editor):
    CheckoutIntent = apps.get_model('customer_billing', 'CheckoutIntent')
    null_count = CheckoutIntent.objects.filter(ssp_product__isnull=True).count()
    if null_count > 0:
        raise Exception(
            f"Cannot proceed: {null_count} CheckoutIntent rows still have NULL ssp_product. "
            "Run the backfill migration (0036_backfill_checkoutintent_ssp_product_teams_yearly) first."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0036_backfill_checkoutintent_ssp_product_teams_yearly'),
    ]

    operations = [
        migrations.RunPython(check_no_nulls, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='checkoutintent',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='checkoutintent',
            name='ssp_product',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='customer_billing.sspproduct', null=False),
        ),
        migrations.AddConstraint(
            model_name='checkoutintent',
            constraint=models.UniqueConstraint(fields=('user', 'ssp_product'), name='unique_user_ssp_product'),
        ),
    ]
