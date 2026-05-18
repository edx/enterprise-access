import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0031_enterpriseacademy_historicalenterpriseacademy'),
        ('provisioning', '0006_getcreatefirstpaidsubscriptionplanstep_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveField(
            model_name='enterpriseacademy',
            name='enterprise_catalog_uuid',
        ),
        migrations.RemoveField(
            model_name='historicalenterpriseacademy',
            name='enterprise_catalog_uuid',
        ),
        migrations.AddField(
            model_name='enterpriseacademy',
            name='catalog_query_uuid',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Related enterprise catalog query UUID.',
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='historicalenterpriseacademy',
            name='catalog_query_uuid',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Related enterprise catalog query UUID.',
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='checkoutintent',
            name='stripe_product_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Stripe Product ID for the selected academy or Teams subscription product.',
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='historicalcheckoutintent',
            name='stripe_product_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Stripe Product ID for the selected academy or Teams subscription product.',
                max_length=255,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name='checkoutintent',
            name='user',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name='checkoutintent',
            constraint=models.UniqueConstraint(
                fields=('user', 'enterprise_slug', 'stripe_product_id'),
                name='unique_checkout_intent_user_slug_stripe_product',
            ),
        ),
    ]