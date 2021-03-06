# Generated by Django 3.2.11 on 2022-01-31 22:32

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('subsidy_request', '0002_historicalsubsidyrequestcustomerconfiguration_subsidyrequestcustomerconfiguration'),
    ]

    operations = [
        migrations.AddField(
            model_name='couponcoderequest',
            name='is_removed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='historicalcouponcoderequest',
            name='is_removed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='historicallicenserequest',
            name='is_removed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='licenserequest',
            name='is_removed',
            field=models.BooleanField(default=False),
        ),
    ]
