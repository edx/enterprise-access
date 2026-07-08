# Generated migration for adding marketing_url to SspProduct and backfilling from SSP_PRODUCT_BACKFILL_DATA.

from django.conf import settings
from django.db import migrations, models


def seed_marketing_urls(apps, schema_editor):
    """Backfill marketing_url on existing SspProduct rows from SSP_PRODUCT_BACKFILL_DATA setting."""
    SspProduct = apps.get_model('customer_billing', 'SspProduct')
    backfill_data = settings.SSP_PRODUCT_BACKFILL_DATA
    if not backfill_data:
        return
    for product_data in backfill_data:
        marketing_url = product_data.get('marketing_url')
        if marketing_url:
            SspProduct.objects.filter(slug=product_data['slug']).update(marketing_url=marketing_url)


def reverse_marketing_urls(apps, schema_editor):
    """Clear marketing_url for products in SSP_PRODUCT_BACKFILL_DATA."""
    SspProduct = apps.get_model('customer_billing', 'SspProduct')
    backfill_data = getattr(settings, 'SSP_PRODUCT_BACKFILL_DATA', [])
    for product_data in backfill_data:
        if product_data.get('marketing_url'):
            SspProduct.objects.filter(slug=product_data['slug']).update(marketing_url=None)


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0037_alter_checkoutintent_sspproduct_nonnull_and_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='sspproduct',
            name='marketing_url',
            field=models.URLField(
                blank=True,
                help_text='Marketing URL for this product. Overrides academy_marketing_url when present.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='historicalsspproduct',
            name='marketing_url',
            field=models.URLField(
                blank=True,
                help_text='Marketing URL for this product. Overrides academy_marketing_url when present.',
                null=True,
            ),
        ),
        migrations.RunPython(seed_marketing_urls, reverse_marketing_urls),
    ]
