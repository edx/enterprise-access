""" Admin configuration for subsidy_request models. """

import logging

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import ObjectDoesNotExist

from enterprise_access.apps.subsidy_request import models
from enterprise_access.apps.subsidy_request.utils import (
    get_data_from_jwt_payload,
    get_user_from_request_session,
)

logger = logging.getLogger(__name__)


class BaseSubsidyRequestAdmin:
    """ Base admin configuration for the subsidy request models. """

    list_display = (
        'uuid',
        'lms_user_id',
        'enterprise_customer_uuid',
        'course_id',
    )

    list_filter = (
        'enterprise_customer_uuid',
        'state',
    )

    read_only_fields = (
        'uuid',
        'denial_reason',
        'state',
        'reviewer_lms_user_id',
        'reviewed_at',
    )

    fields = (
        'lms_user_id',
        'course_id',
        'enterprise_customer_uuid',
        'denial_reason',
        'reviewer_lms_user_id',
        'reviewed_at',
        'state',
    )


@admin.register(models.LicenseRequest)
class LicenseRequestAdmin(BaseSubsidyRequestAdmin, admin.ModelAdmin):
    """ Admin configuration for the LicenseRequest model. """

    read_only_fields = (
        'subscription_plan_uuid',
        'license_uuid',
    )

    fields = (
        'subscription_plan_uuid',
        'license_uuid',
    )

    class Meta:
        """
        Meta class for ``LicenseRequestAdmin``.
        """

        model = models.LicenseRequest

    def get_readonly_fields(self, request, obj=None):
        return super().read_only_fields + self.read_only_fields

    def get_fields(self, request, obj=None):
        return super().fields + self.fields


@admin.register(models.CouponCodeRequest)
class CouponCodeRequestAdmin(BaseSubsidyRequestAdmin, admin.ModelAdmin):
    """ Admin configuration for the CouponCodeRequest model. """

    read_only_fields = (
        'coupon_id',
        'coupon_code',
    )

    fields = (
        'coupon_id',
        'coupon_code',
    )

    class Meta:
        """
        Meta class for ``CouponCodeRequestAdmin``.
        """

        model = models.CouponCodeRequest

    def get_readonly_fields(self, request, obj=None):
        return super().read_only_fields + self.read_only_fields

    def get_fields(self, request, obj=None):
        return super().fields + self.fields


@admin.register(models.SubsidyRequestCustomerConfiguration)
class SubsidyRequestCustomerConfigurationAdmin(admin.ModelAdmin):
    """ Admin configuration for the SubsidyRequestCustomerConfiguration model. """
    writable_fields = [
        'subsidy_requests_enabled',
        'subsidy_type',
        'pending_request_reminder_frequency',
        
    ]
    exclude = ['changed_by']

    def get_readonly_fields(self, request, obj=None):
        """
        Override to only display some fields on creation of object in admin, as well
        as limit what is editable after creation.
        """
        if obj:
            return [
                'enterprise_customer_uuid',
                'last_changed_by',
            ]
        else:
            return []

    def last_changed_by(self, obj):
        return 'LMS User: {} ({})'.format(
            obj.changed_by.lms_user_id,
            obj.changed_by.email,
        )

    def save_model(self, request, obj, form, change):
        """
        Override save_model method to keep our change records up to date.
        """
        current_user = get_user_from_request_session(request)
        jwt_data = get_data_from_jwt_payload(request, ['user_id'])
        # Make sure we update the user object's lms_user_id if it's not set
        # or if it has changed to keep our DB up to date, because we have
        # no way to predict if the user has hit a rest endpoint and had
        # their info prepopulated already.
        lms_user_id = jwt_data['user_id']
        if not current_user.lms_user_id or current_user.lms_user_id != lms_user_id:
            current_user.lms_user_id = lms_user_id
            current_user.save()

        obj.changed_by = current_user

        super().save_model(request, obj, form, change)
