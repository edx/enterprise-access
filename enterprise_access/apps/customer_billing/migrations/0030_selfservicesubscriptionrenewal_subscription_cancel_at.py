from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customer_billing', '0029_historicalselfservicesubscriptionrenewal_is_canceled_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalselfservicesubscriptionrenewal',
            name='subscription_cancel_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    'Timestamp when the subscription is scheduled to be canceled. '
                    'Set from Stripe cancel_at on subscription_updated events; '
                    'cleared on subscription deletion or when cancellation is reversed.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='selfservicesubscriptionrenewal',
            name='subscription_cancel_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    'Timestamp when the subscription is scheduled to be canceled. '
                    'Set from Stripe cancel_at on subscription_updated events; '
                    'cleared on subscription deletion or when cancellation is reversed.'
                ),
            ),
        ),
    ]
