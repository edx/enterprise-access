# Generated migration for seeding SspProduct rows from SSP_PRODUCT_BACKFILL_DATA setting.

from uuid import UUID

from django.conf import settings
from django.db import migrations


def seed_ssp_products(apps, schema_editor):
    """Seed SspProduct rows from SSP_PRODUCT_BACKFILL_DATA setting."""
    SspProduct = apps.get_model('customer_billing', 'SspProduct')

    backfill_data = settings.SSP_PRODUCT_BACKFILL_DATA

    if not backfill_data:
        return

    for product_data in backfill_data:
        slug = product_data['slug']

        # Convert catalog_query_uuid to UUID if it's a string
        catalog_query_uuid = product_data['catalog_query_uuid']
        if isinstance(catalog_query_uuid, str):
            catalog_query_uuid = UUID(catalog_query_uuid)

        SspProduct.objects.get_or_create(
            slug=slug,
            defaults={
                'stripe_price_lookup_key': product_data['stripe_price_lookup_key'],
                'catalog_query_uuid': catalog_query_uuid,
                'academy_uuid': product_data.get('academy_uuid'),
                'license_manager_product_id_trial': product_data.get('license_manager_product_id_trial'),
                'license_manager_product_id_paid': product_data.get('license_manager_product_id_paid'),
                'is_active': product_data.get('is_active', True),
            }
        )


def reverse_ssp_products(apps, schema_editor):
    """Remove rows created by this migration."""
    SspProduct = apps.get_model('customer_billing', 'SspProduct')
    backfill_data = getattr(settings, 'SSP_PRODUCT_BACKFILL_DATA', [])

    for product_data in backfill_data:
        SspProduct.objects.filter(slug=product_data['slug']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0032_add_ssp_product'),
    ]

    operations = [
        migrations.RunPython(seed_ssp_products, reverse_ssp_products),
    ]
