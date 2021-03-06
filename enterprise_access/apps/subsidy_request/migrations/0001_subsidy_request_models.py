# Generated by Django 3.2.11 on 2022-01-25 19:43

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
import simple_history.models
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CouponCodeRequest',
            fields=[
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False, unique=True)),
                ('lms_user_id', models.IntegerField()),
                ('course_id', models.CharField(blank=True, max_length=128, null=True)),
                ('enterprise_customer_uuid', models.UUIDField()),
                ('state', models.CharField(choices=[('pending_review', 'Pending Review'), ('approved_pending', 'Approved - Pending'), ('approved_fulfilled', 'Approved - Fulfilled'), ('denied', 'Denied')], default='pending_review', max_length=25)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('reviewer_lms_user_id', models.IntegerField(blank=True, null=True)),
                ('denial_reason', models.CharField(blank=True, max_length=255, null=True)),
                ('coupon_id', models.IntegerField(blank=True, null=True)),
                ('coupon_code', models.CharField(blank=True, max_length=128, null=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='LicenseRequest',
            fields=[
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False, unique=True)),
                ('lms_user_id', models.IntegerField()),
                ('course_id', models.CharField(blank=True, max_length=128, null=True)),
                ('enterprise_customer_uuid', models.UUIDField()),
                ('state', models.CharField(choices=[('pending_review', 'Pending Review'), ('approved_pending', 'Approved - Pending'), ('approved_fulfilled', 'Approved - Fulfilled'), ('denied', 'Denied')], default='pending_review', max_length=25)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('reviewer_lms_user_id', models.IntegerField(blank=True, null=True)),
                ('denial_reason', models.CharField(blank=True, max_length=255, null=True)),
                ('subscription_plan_uuid', models.UUIDField(blank=True, null=True)),
                ('license_uuid', models.UUIDField(blank=True, null=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='HistoricalLicenseRequest',
            fields=[
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False)),
                ('lms_user_id', models.IntegerField()),
                ('course_id', models.CharField(blank=True, max_length=128, null=True)),
                ('enterprise_customer_uuid', models.UUIDField()),
                ('state', models.CharField(choices=[('pending_review', 'Pending Review'), ('approved_pending', 'Approved - Pending'), ('approved_fulfilled', 'Approved - Fulfilled'), ('denied', 'Denied')], default='pending_review', max_length=25)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('reviewer_lms_user_id', models.IntegerField(blank=True, null=True)),
                ('denial_reason', models.CharField(blank=True, max_length=255, null=True)),
                ('subscription_plan_uuid', models.UUIDField(blank=True, null=True)),
                ('license_uuid', models.UUIDField(blank=True, null=True)),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField()),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'historical license request',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': 'history_date',
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name='HistoricalCouponCodeRequest',
            fields=[
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('uuid', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False)),
                ('lms_user_id', models.IntegerField()),
                ('course_id', models.CharField(blank=True, max_length=128, null=True)),
                ('enterprise_customer_uuid', models.UUIDField()),
                ('state', models.CharField(choices=[('pending_review', 'Pending Review'), ('approved_pending', 'Approved - Pending'), ('approved_fulfilled', 'Approved - Fulfilled'), ('denied', 'Denied')], default='pending_review', max_length=25)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('reviewer_lms_user_id', models.IntegerField(blank=True, null=True)),
                ('denial_reason', models.CharField(blank=True, max_length=255, null=True)),
                ('coupon_id', models.IntegerField(blank=True, null=True)),
                ('coupon_code', models.CharField(blank=True, max_length=128, null=True)),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField()),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'historical coupon code request',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': 'history_date',
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
    ]
