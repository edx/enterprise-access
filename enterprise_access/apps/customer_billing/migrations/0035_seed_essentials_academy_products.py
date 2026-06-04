from datetime import datetime, timezone
from uuid import UUID

from django.db import migrations


SEEDED_AT = datetime(2026, 6, 4, tzinfo=timezone.utc)

ESSENTIALS_ACADEMY_PRODUCTS = [
    {
        'slug': 'ai-academy-yearly',
        'stripe_price_lookup_key': 'essentials_artificial_intelligence_subscription_license_yearly',
        'academy_uuid': '3307f1bb-8b2d-43af-a5d5-030e2f8c81bd',
        'catalog_query_uuid': 'e50133c8b65c447280264ed9963b3b26',
        'license_manager_product_id_trial': None,
        'license_manager_product_id_paid': None,
        'is_active': True,
    },
    {
        'slug': 'communication-academy-yearly',
        'stripe_price_lookup_key': 'essentials_communication_subscription_license_yearly',
        'academy_uuid': 'd1679114-23aa-4d68-b101-0fb15833647e',
        'catalog_query_uuid': '8454340461064d8eacb12c01e4978e76',
        'license_manager_product_id_trial': None,
        'license_manager_product_id_paid': None,
        'is_active': True,
    },
    {
        'slug': 'data-academy-yearly',
        'stripe_price_lookup_key': 'essentials_data_subscription_license_yearly',
        'academy_uuid': '6681055b-1c0c-4f29-8e08-a599c926b073',
        'catalog_query_uuid': '52c5808872c44d6dba635ff1787f5b6e',
        'license_manager_product_id_trial': None,
        'license_manager_product_id_paid': None,
        'is_active': True,
    },
    {
        'slug': 'leadership-academy-yearly',
        'stripe_price_lookup_key': 'essentials_leadership_subscription_license_yearly',
        'academy_uuid': 'b54820a7-76eb-45cd-964c-25685d0b677c',
        'catalog_query_uuid': '0aa7de4d4c2844c48a41b29dc4a4af1f',
        'license_manager_product_id_trial': None,
        'license_manager_product_id_paid': None,
        'is_active': True,
    },
    {
        'slug': 'management-academy-yearly',
        'stripe_price_lookup_key': 'essentials_management_subscription_license_yearly',
        'academy_uuid': '02189bdd-89cd-4a69-8a5f-d18267f9c645',
        'catalog_query_uuid': '3ccbbe2d1e35430298c934578994fd8e',
        'license_manager_product_id_trial': None,
        'license_manager_product_id_paid': None,
        'is_active': True,
    },
    {
        'slug': 'supply-chain-academy-yearly',
        'stripe_price_lookup_key': 'essentials_supply_chain_subscription_license_yearly',
        'academy_uuid': '5937443b-ae93-4595-a7fe-ba18541704ef',
        'catalog_query_uuid': '79b39d050b55478e9e004bb03e4b45db',
        'license_manager_product_id_trial': None,
        'license_manager_product_id_paid': None,
        'is_active': True,
    },
    {
        'slug': 'sustainability-academy-yearly',
        'stripe_price_lookup_key': 'essentials_sustainability_subscription_license_yearly',
        'academy_uuid': '4c026942-e8a4-4eff-b2c9-9cd0456de0b6',
        'catalog_query_uuid': 'c94ec83fe15747c9926d6d99ac7efe78',
        'license_manager_product_id_trial': None,
        'license_manager_product_id_paid': None,
        'is_active': True,
    },
    {
        'slug': 'tech-digital-transformation-academy-yearly',
        'stripe_price_lookup_key': 'essentials_tech_and_digital_transformation',
        'academy_uuid': '8d06f7b0-fb76-406f-8fdb-e750ce5def4a',
        'catalog_query_uuid': '755a1b54ac79438491b96d8f73441f7e',
        'license_manager_product_id_trial': None,
        'license_manager_product_id_paid': None,
        'is_active': True,
    },
]


def _uuid_or_none(value):
    return UUID(value) if value else None


def seed_essentials_academy_products(apps, schema_editor):
    """Seed SspProduct rows for Essentials Academy offerings."""
    SspProduct = apps.get_model('customer_billing', 'SspProduct')

    for product_data in ESSENTIALS_ACADEMY_PRODUCTS:
        product, created = SspProduct.objects.update_or_create(
            slug=product_data['slug'],
            defaults={
                'stripe_price_lookup_key': product_data['stripe_price_lookup_key'],
                'academy_uuid': _uuid_or_none(product_data['academy_uuid']),
                'catalog_query_uuid': UUID(product_data['catalog_query_uuid']),
                'license_manager_product_id_trial': product_data['license_manager_product_id_trial'],
                'license_manager_product_id_paid': product_data['license_manager_product_id_paid'],
                'is_active': product_data['is_active'],
            },
        )

        if created or product.created == SEEDED_AT:
            SspProduct.objects.filter(pk=product.pk).update(created=SEEDED_AT, modified=SEEDED_AT)


def reverse_essentials_academy_products(apps, schema_editor):
    """Delete only rows created by this migration."""
    SspProduct = apps.get_model('customer_billing', 'SspProduct')
    seeded_slugs = [product_data['slug'] for product_data in ESSENTIALS_ACADEMY_PRODUCTS]

    SspProduct.objects.filter(
        slug__in=seeded_slugs,
        created=SEEDED_AT,
        modified=SEEDED_AT,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0034_drop_ent_academy_table'),
    ]

    operations = [
        migrations.RunPython(seed_essentials_academy_products, reverse_essentials_academy_products),
    ]
