""" Admin configuration for subsidy_access_policy models. """
import json
import logging
from uuid import uuid4

from celery import chain
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import re_path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.text import Truncator  # for shortening a text
from django.utils.translation import gettext_lazy
from django_object_actions import DjangoObjectActions, action
from djangoql.admin import DjangoQLSearchMixin
from pygments import highlight
from pygments.formatters import HtmlFormatter  # pylint: disable=no-name-in-module
from pygments.lexers import JsonLexer  # pylint: disable=no-name-in-module
from simple_history.admin import SimpleHistoryAdmin

from enterprise_access.apps.api.serializers.subsidy_access_policy import SubsidyAccessPolicyResponseSerializer
from enterprise_access.apps.subsidy_access_policy import models
from enterprise_access.apps.subsidy_access_policy.admin.utils import UrlNames
from enterprise_access.apps.subsidy_access_policy.admin.views import (
    SubsidyAccessPolicyDepositFundsView,
    SubsidyAccessPolicySetLateRedemptionView
)
from enterprise_access.apps.subsidy_access_policy.api import force_redeem_with_geag_metadata
from enterprise_access.apps.subsidy_access_policy.tasks import force_redeem_task
from enterprise_access.apps.subsidy_access_policy.utils import cents_to_usd_string

from .forms import BulkForcedPolicyRedemptionForm, ForcedPolicyRedemptionForm, SubsidyAccessPolicyForm

logger = logging.getLogger(__name__)


EVERY_SPEND_LIMIT_FIELD = [
    'spend_limit',
    'policy_spend_limit_dollars',
    'per_learner_spend_limit',
    'per_learner_enrollment_limit',
]


def super_admin_enabled():
    return getattr(settings, 'DJANGO_ADMIN_POLICY_SUPER_ADMIN', False)


class BaseSubsidyAccessPolicyMixin(DjangoObjectActions, SimpleHistoryAdmin):
    """
    Mixin for common admin properties on subsidy access policy models.
    """
    list_display = (
        'uuid',
        'modified',
        'active',
        'retired',
        'display_name_or_short_description',
        'policy_spend_limit_dollars',
    )
    history_list_display = (
        'active',
        'retired',
        'spend_limit',
    )
    list_filter = (
        'active',
        'retired',
        'access_method',
    )
    ordering = ['-modified']

    readonly_fields = (
        'created',
        'modified',
        'policy_spend_limit_dollars',
        'late_redemption_allowed_until',
        'is_late_redemption_allowed',
        'api_serialized_repr',
    )

    change_actions = (
        'set_late_redemption',
        'deposit_funds',
    )

    def get_form(self, *args, **kwargs):
        """
        Expand width of certain fields so that large integers (e.g. $100k in cents) are fully visible.
        """
        form = super().get_form(*args, **kwargs)
        form.base_fields['spend_limit'].widget.attrs['style'] = 'width: 10em;'  # Wide enough for billions of dollars.
        return form

    @action(
        label='Set Late Redemption',
        description='Enable/disable the "late redemption" feature for this policy'
    )
    def set_late_redemption(self, request, obj):
        """
        Object tool handler method - redirects to set_late_redemption view.
        """
        # url names coming from get_urls are prefixed with 'admin' namespace
        set_late_redemption_url = reverse('admin:' + UrlNames.SET_LATE_REDEMPTION, args=(obj.uuid,))
        return HttpResponseRedirect(set_late_redemption_url)

    @action(
        label='Deposit Funds',
        description='Top-up the subsidy and spend_limit associated with this policy'
    )
    def deposit_funds(self, request, obj):
        """
        Object tool handler method - redirects to deposit_funds view.
        """
        # url names coming from get_urls are prefixed with 'admin' namespace
        deposit_funds_url = reverse('admin:' + UrlNames.DEPOSIT_FUNDS, args=(obj.uuid,))
        return HttpResponseRedirect(deposit_funds_url)

    def get_urls(self):
        """
        Returns the additional urls used by the custom object tools.
        """
        additional_urls = [
            re_path(
                r"^([^/]+)/set_late_redemption",
                self.admin_site.admin_view(SubsidyAccessPolicySetLateRedemptionView.as_view()),
                name=UrlNames.SET_LATE_REDEMPTION,
            ),
            re_path(
                r"^([^/]+)/deposit_funds",
                self.admin_site.admin_view(SubsidyAccessPolicyDepositFundsView.as_view()),
                name=UrlNames.DEPOSIT_FUNDS,
            ),
        ]
        return additional_urls + super().get_urls()

    @admin.display(description='REST API serialization')
    def api_serialized_repr(self, obj):
        """
        Convenience method to see what the policy details REST API
        response is.  Thanks to:
        https://daniel.feldroy.com/posts/pretty-formatting-json-django-admin
        for this styling idea.
        """
        try:
            data = SubsidyAccessPolicyResponseSerializer(obj).data
            json_string = json.dumps(data, indent=4, sort_keys=True)

            # Get the Pygments formatter
            formatter = HtmlFormatter(style='default')

            # Highlight the data
            response = highlight(json_string, JsonLexer(), formatter)

            # Get the stylesheet
            style = "<style>" + formatter.get_style_defs() + "</style><br>"

            # Safe the output
            return mark_safe(style + response)
        except Exception:  # pylint: disable=broad-except
            return ''

    def _short_description(self, obj):
        return Truncator(str(obj.description)).chars(255)

    def display_name_or_short_description(self, obj):
        if obj.display_name:
            return obj.display_name
        return self._short_description(obj)

    @admin.display(description='Policy-wide spend limit (dollars)')
    def policy_spend_limit_dollars(self, obj):
        """Returns this policy's spend_limit as a US Dollar string."""
        if obj.spend_limit is None:
            return None
        return cents_to_usd_string(obj.spend_limit)

    def get_fieldsets(self, request, obj=None):
        """
        Render the API serialization only when we're not
        adding a new policy record.
        """
        fieldsets = super().get_fieldsets(request, obj=obj)
        if obj and not obj._state.adding:  # pylint: disable=protected-access
            try:
                return fieldsets + [('Extra', {'fields': ['api_serialized_repr']})]
            except Exception:  # pylint: disable=broad-except
                return fieldsets
        return fieldsets


@admin.register(models.PerLearnerEnrollmentCreditAccessPolicy)
class PerLearnerEnrollmentCreditAccessPolicy(DjangoQLSearchMixin, BaseSubsidyAccessPolicyMixin):
    """
    Admin configuration for PerLearnerEnrollmentCreditAccessPolicy.
    """
    form = SubsidyAccessPolicyForm

    list_display = BaseSubsidyAccessPolicyMixin.list_display + (
        'per_learner_enrollment_limit',
    )
    history_list_display = BaseSubsidyAccessPolicyMixin.history_list_display + (
        'per_learner_enrollment_limit',
    )
    search_fields = (
        'uuid',
        'display_name',
        'enterprise_customer_uuid',
        'catalog_uuid',
        'subsidy_uuid',
    )

    fieldsets = [
        (
            'Base configuration',
            {
                'fields': [
                    'enterprise_customer_uuid',
                    'display_name',
                    'description',
                    'active',
                    'retired',
                    'retired_at',
                    'catalog_uuid',
                    'subsidy_uuid',
                    'late_redemption_allowed_until',
                    'created',
                    'modified',
                ]
            }
        ),
        (
            'Spend limits',
            {
                'fields': [
                    'spend_limit',
                    'policy_spend_limit_dollars',
                    'per_learner_enrollment_limit',
                ] if not super_admin_enabled() else EVERY_SPEND_LIMIT_FIELD
            }
        ),
    ]


@admin.register(models.PerLearnerSpendCreditAccessPolicy)
class PerLearnerSpendCreditAccessPolicy(DjangoQLSearchMixin, BaseSubsidyAccessPolicyMixin):
    """
    Admin configuration for PerLearnerSpendCreditAccessPolicy.
    """
    form = SubsidyAccessPolicyForm

    list_display = BaseSubsidyAccessPolicyMixin.list_display + (
        'per_learner_spend_limit_dollars',
    )
    history_list_display = BaseSubsidyAccessPolicyMixin.history_list_display + (
        'per_learner_spend_limit_dollars',
    )
    readonly_fields = BaseSubsidyAccessPolicyMixin.readonly_fields + (
        'per_learner_spend_limit_dollars',
        'retired_at',
    )
    search_fields = (
        'uuid',
        'display_name',
        'enterprise_customer_uuid',
        'catalog_uuid',
        'subsidy_uuid',
    )

    fieldsets = [
        (
            'Base configuration',
            {
                'fields': [
                    'enterprise_customer_uuid',
                    'display_name',
                    'description',
                    'active',
                    'retired',
                    'retired_at',
                    'catalog_uuid',
                    'subsidy_uuid',
                    'learner_credit_request_config',
                    'late_redemption_allowed_until',
                    'created',
                    'modified',
                ]
            }
        ),
        (
            'Spend limits',
            {
                'fields': [
                    'spend_limit',
                    'policy_spend_limit_dollars',
                    'per_learner_spend_limit',
                    'per_learner_spend_limit_dollars',
                ] if not super_admin_enabled() else EVERY_SPEND_LIMIT_FIELD
            }
        ),
    ]

    @admin.display(description='Per-learner spend limit (dollars)')
    def per_learner_spend_limit_dollars(self, obj):
        """Returns this policy's per_learner_spend_limit as a US Dollar string."""
        if obj.per_learner_spend_limit is None:
            return None
        return cents_to_usd_string(obj.per_learner_spend_limit)


@admin.register(models.AssignedLearnerCreditAccessPolicy)
class LearnerContentAssignmentAccessPolicy(DjangoQLSearchMixin, BaseSubsidyAccessPolicyMixin):
    """
    Admin configuration for AssignedLearnerCreditAccessPolicy.
    """
    form = SubsidyAccessPolicyForm

    search_fields = (
        'uuid',
        'display_name',
        'enterprise_customer_uuid',
        'catalog_uuid',
        'subsidy_uuid',
    )

    readonly_fields = BaseSubsidyAccessPolicyMixin.readonly_fields + (
        'assignment_configuration',
        'per_learner_spend_limit',
        'per_learner_enrollment_limit',
        'retired_at',
    )

    fieldsets = [
        (
            'Base configuration',
            {
                'fields': [
                    'enterprise_customer_uuid',
                    'display_name',
                    'description',
                    'active',
                    'retired',
                    'retired_at',
                    'catalog_uuid',
                    'subsidy_uuid',
                    'late_redemption_allowed_until',
                    'assignment_configuration',
                    'created',
                    'modified',
                ],
            }
        ),
        (
            'Spend limits',
            {
                'fields': [
                    'spend_limit',
                    'policy_spend_limit_dollars',
                ] if not super_admin_enabled() else EVERY_SPEND_LIMIT_FIELD
            }
        ),
    ]


@admin.register(models.PolicyGroupAssociation)
class PolicyGroupAssociationAdmin(admin.ModelAdmin):
    """
    Admin configuration for PolicyGroupAssociation
    """
    search_fields = (
        'subsidy_access_policy__uuid',
        'enterprise_group_uuid',
    )

    list_display = (
        'subsidy_access_policy',
        'enterprise_group_uuid',
    )

    autocomplete_fields = [
        'subsidy_access_policy',
    ]


@admin.register(models.SubsidyAccessPolicy)
class SubsidAccessPolicyAdmin(admin.ModelAdmin):
    """
    We need this not-particularly-useful admin class
    to let the ForcedPolicyRedemptionAdmin class refer
    to subsidy access policies, of all types, via its
    ``autocomplete_fields``.
    It's hidden from the admin index page.
    """
    fields = []
    search_fields = [
        'uuid',
        'display_name',
    ]

    def has_module_permission(self, request):
        """
        Hide this view from the admin index page.
        """
        return False

    def has_change_permission(self, request, obj=None):
        """
        For good measure, declare no change permissions on this admin class.
        """
        return False


@admin.register(models.ForcedPolicyRedemption)
class ForcedPolicyRedemptionAdmin(DjangoObjectActions, DjangoQLSearchMixin, SimpleHistoryAdmin):
    """
    Admin class for the forced redemption model/logic.
    """
    form = ForcedPolicyRedemptionForm

    changelist_actions = (
        'bulk_create_forced_redemptions',
    )

    djangoql_completion_enabled_by_default = False
    search_fields = [
        'uuid',
        'subsidy_access_policy__uuid',
        'lms_user_id',
        'course_run_key',
        'batch_uuid',
    ]

    list_display = [
        'uuid',
        'policy_uuid',
        'lms_user_id',
        'course_run_key',
        'redeemed_at',
        'errored_at',
        'batch_uuid',
    ]
    list_filter = [
        'redeemed_at',
        'errored_at',
    ]
    autocomplete_fields = [
        'subsidy_access_policy',
    ]
    readonly_fields = [
        'redeemed_at',
        'errored_at',
        'transaction_uuid',
        'traceback',
        'batch_uuid',
    ]

    def get_ordering(self, request):
        """
        Lists a bulk-created batch (``?batch_uuid=``) in creation order, which is the order its rows were queued.
        """
        if 'batch_uuid' in request.GET:
            return ('created',)
        return super().get_ordering(request)

    @action(
        label='Bulk create from JSON',
        description='Create (and redeem) many forced redemptions from a JSON list'
    )
    def bulk_create_forced_redemptions(self, request, queryset):  # pylint: disable=unused-argument
        """
        Object tool handler method - redirects to the bulk create view.
        """
        return HttpResponseRedirect(reverse('admin:' + UrlNames.BULK_CREATE_FORCED_REDEMPTIONS))

    def get_urls(self):
        """
        Returns the additional urls used by the custom object tools.
        """
        additional_urls = [
            re_path(
                r"^bulk_create/$",
                # Opt out of ATOMIC_REQUESTS, so the view's own atomic block commits the records before it
                # queues the tasks that read them.
                transaction.non_atomic_requests(self.admin_site.admin_view(self.bulk_create_view)),
                name=UrlNames.BULK_CREATE_FORCED_REDEMPTIONS,
            ),
        ]
        return additional_urls + super().get_urls()

    def bulk_create_view(self, request):
        """
        Creates one ForcedPolicyRedemption per row of a JSON list, all sharing a new ``batch_uuid``, and queues a
        chain of tasks that redeem them one at a time, as the add form would.

        Rows are validated up front, and nothing is saved unless every row is valid. There's deliberately no
        skip for earlier records (their redemption may since have been reversed); the subsidy service's redeem()
        returns any existing unreversed transaction instead of charging again.
        """
        if not self.has_add_permission(request):
            raise PermissionDenied

        form = BulkForcedPolicyRedemptionForm(request.POST or None)
        if request.method == 'POST' and form.is_valid():
            batch_uuid = uuid4()
            task_signatures = []
            with transaction.atomic():
                for row_form in form.cleaned_data['redemptions_json']:
                    obj = row_form.save(commit=False)
                    obj.batch_uuid = batch_uuid
                    obj.save()
                    self.log_addition(request, obj, [{'added': {}}])
                    task_signature = force_redeem_task.si(
                        forced_redemption_uuid=str(obj.uuid),
                        geag_metadata=row_form.get_geag_metadata(),
                        actor_lms_user_id=request.user.lms_user_id,
                        suppress_assignment_email=row_form.cleaned_data['suppress_assignment_email'],
                    )
                    task_signatures.append(task_signature)

            batch_url = reverse('admin:subsidy_access_policy_forcedpolicyredemption_changelist')
            try:
                # Rows run one at a time (they'd contend for the policy lock); each task never raises, so one
                # failure doesn't stop the chain.
                chain(*task_signatures).apply_async()
            except Exception:  # pylint: disable=broad-except
                logger.exception('[BULK FORCED REDEMPTION] batch=%s saved but could not be queued', batch_uuid)
                message = format_html(
                    'Saved batch {batch_uuid}, but could not queue it for redemption. '
                    '<a href="{batch_url}?batch_uuid={batch_uuid}">View this batch</a> and resubmit its rows.',
                    batch_uuid=batch_uuid,
                    batch_url=batch_url,
                )
                self.message_user(request, message, messages.ERROR)
                return HttpResponseRedirect(f'{batch_url}?batch_uuid={batch_uuid}')

            logger.info(
                '[BULK FORCED REDEMPTION] batch=%s queued %s rows by lms_user_id=%s',
                batch_uuid,
                len(task_signatures),
                request.user.lms_user_id,
            )
            message = format_html(
                'Queued {count} forced redemption(s) as batch {batch_uuid}; they redeem one at a time in the '
                'background. <a href="{batch_url}?batch_uuid={batch_uuid}">View this batch</a> and refresh to see '
                'progress.',
                count=len(task_signatures),
                batch_uuid=batch_uuid,
                batch_url=batch_url,
            )
            self.message_user(request, message, messages.SUCCESS)
            return HttpResponseRedirect(f'{batch_url}?batch_uuid={batch_uuid}')

        context = {
            **self.admin_site.each_context(request),
            'title': 'Bulk create forced redemptions',
            'opts': self.model._meta,
            'form': form,
        }
        return TemplateResponse(request, 'subsidy_access_policy/admin/bulk_create_forced_redemptions.html', context)

    def save_model(self, request, obj, form, change) -> None:
        """
        If this record has not been successfully redeemed yet,
        and if ``wait_to_redeem`` is false, then call ``force_redeem()`` on
        the record.
        """
        super().save_model(request, obj, form, change)
        obj.refresh_from_db()

        if obj.transaction_uuid:
            message = gettext_lazy("{} has already been redeemed".format(obj))
            self.message_user(request, message, messages.SUCCESS)
            return

        if obj.wait_to_redeem:
            message = gettext_lazy(
                "{} has wait_to_redeem set to true, redemption will not occur "
                "until this is changed to false".format(obj)
            )
            self.message_user(request, message, messages.WARNING)
            return

        form.full_clean()  # populates cleaned_data below
        try:
            force_redeem_with_geag_metadata(
                forced_redemption=obj,
                geag_metadata=form.get_geag_metadata(),
                actor_lms_user_id=request.user.lms_user_id,
                suppress_assignment_email=form.cleaned_data.get('suppress_assignment_email', False),
            )
        except Exception as exc:  # pylint: disable=broad-except
            message = gettext_lazy("{} Failure reason: {}".format(obj, exc))
            self.message_user(request, message, messages.ERROR)
            logger.exception('Force redemption failed for %s', obj)
