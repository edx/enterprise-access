""" Admin configuration for content_assignment models. """

from django.contrib import admin, messages
from django.db import transaction
from djangoql.admin import DjangoQLSearchMixin
from simple_history.admin import SimpleHistoryAdmin

from enterprise_access.apps.content_assignments import models
from enterprise_access.apps.content_assignments.constants import (
    AssignmentActions,
    AssignmentActorTypes,
    AssignmentSources,
    LearnerContentAssignmentStateChoices
)
from enterprise_access.apps.subsidy_access_policy.exceptions import SubsidyAccessPolicyException, SubsidyAPIHTTPError
from enterprise_access.utils import localized_utcnow


@admin.register(models.AssignmentConfiguration)
class AssignmentConfigurationAdmin(DjangoQLSearchMixin, SimpleHistoryAdmin):
    """
    Admin configuration for AssignmentConfigurations.
    """
    list_display = (
        'uuid',
        'enterprise_customer_uuid',
        'active',
        'modified',
    )
    search_fields = (
        'uuid',
        'enterprise_customer_uuid',
    )
    list_filter = ('active',)
    ordering = ['-modified']
    readonly_fields = (
        'created',
        'modified',
    )


class ActionInline(admin.TabularInline):
    """
    Inline admin for linking actions into their related assignment record.
    """
    model = models.LearnerContentAssignmentAction

    fields = (
        'action_type',
        'completed_at',
        'error_reason',
    )

    ordering = ['-modified']

    show_change_link = True

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request, obj):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('assignment')


@admin.register(models.LearnerContentAssignment)
class LearnerContentAssignmentAdmin(DjangoQLSearchMixin, SimpleHistoryAdmin):
    """
    Admin configuration for LearnerContentAssignments.
    """
    list_display = (
        'uuid',
        'get_assignment_configuration_uuid',
        'get_enterprise_customer_uuid',
        'learner_email',
        'lms_user_id',
        'content_key',
        'state',
        'content_quantity',
        'modified',
    )
    ordering = ['-modified']
    search_fields = (
        'uuid',
        'learner_email',
        'lms_user_id',
        'assignment_configuration__uuid',
        'assignment_configuration__enterprise_customer_uuid',
    )
    list_filter = ('state',)
    readonly_fields = (
        'created',
        'modified',
        'lms_user_id',
        'get_enterprise_customer_uuid',
        'parent_content_key',
        'is_assigned_course_run',
    )
    autocomplete_fields = ['assignment_configuration']

    list_select_related = ('assignment_configuration',)

    inlines = [ActionInline]

    actions = ['force_redeem_assignments']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('assignment_configuration')

    @admin.display(ordering='uuid', description='Assignment configuration UUID')
    def get_assignment_configuration_uuid(self, obj):
        return obj.assignment_configuration.uuid

    @admin.display(ordering='enterprise_customer_uuid', description='Enterprise customer uuid')
    def get_enterprise_customer_uuid(self, obj):
        return obj.assignment_configuration.enterprise_customer_uuid

    @admin.action(description='Force redeem selected assignments', permissions=['change'])
    def force_redeem_assignments(self, request, queryset):
        """
        Admin action to force-redeem selected assignments through the real redemption path
        (``SubsidyAccessPolicy.redeem()``), so a genuine ledger transaction and ``transaction_uuid``
        are created exactly as they would be for a learner-initiated redemption -- attributed here
        to the requesting admin instead. Assignments not already ALLOCATED are first reset to
        ALLOCATED locally (recording an ALLOCATED audit row) so that redemption's precondition is
        met; the REDEEMED audit row is then written by ``redeem()`` itself. Already-ACCEPTED
        assignments are skipped rather than reset and re-redeemed, which would double-spend
        against the policy's ledger.
        """
        actor_lms_user_id = getattr(request.user, 'lms_user_id', None)
        redeemed_count = 0
        failures = []

        for assignment in queryset:
            if assignment.state == LearnerContentAssignmentStateChoices.ACCEPTED:
                failures.append(f'{assignment.uuid}: already accepted, skipping to avoid double-spend')
                continue

            policy = assignment.assignment_configuration.policy if assignment.assignment_configuration else None
            if not policy or not assignment.lms_user_id:
                failures.append(f'{assignment.uuid}: missing subsidy access policy or lms_user_id')
                continue

            try:
                if assignment.state != LearnerContentAssignmentStateChoices.ALLOCATED:
                    # Atomic: if save() fails, the ALLOCATED audit row it writes must roll back too.
                    with transaction.atomic():
                        assignment.state = LearnerContentAssignmentStateChoices.ALLOCATED
                        assignment.allocated_at = localized_utcnow()
                        assignment.accepted_at = None
                        assignment.errored_at = None
                        assignment.cancelled_at = None
                        assignment.expired_at = None
                        assignment.reversed_at = None
                        assignment.transaction_uuid = None
                        assignment.save()
                        assignment.add_audit_action(
                            action_type=AssignmentActions.ALLOCATED,
                            actor_type=AssignmentActorTypes.ADMIN,
                            source=AssignmentSources.DJANGO_ADMIN,
                            actor_lms_user_id=actor_lms_user_id,
                        )

                with policy.lock():
                    can_redeem, reason, existing_transactions = policy.can_redeem(
                        assignment.lms_user_id,
                        assignment.content_key,
                        skip_enrollment_deadline_check=True,
                    )
                    if not can_redeem:
                        raise SubsidyAccessPolicyException(reason)
                    policy.redeem(
                        assignment.lms_user_id,
                        assignment.content_key,
                        existing_transactions,
                        actor_lms_user_id=actor_lms_user_id,
                        actor_type=AssignmentActorTypes.ADMIN,
                        source=AssignmentSources.DJANGO_ADMIN,
                    )
                redeemed_count += 1
            except (SubsidyAccessPolicyException, SubsidyAPIHTTPError) as exc:
                failures.append(f'{assignment.uuid}: {exc}')

        message = f'Successfully redeemed {redeemed_count} assignment(s).'
        if failures:
            message += f' {len(failures)} failed: ' + '; '.join(failures)
            self.message_user(request, message, level=messages.WARNING)
        else:
            self.message_user(request, message)


@admin.register(models.LearnerContentAssignmentAction)
class LearnerContentAssignmentActionAdmin(DjangoQLSearchMixin, SimpleHistoryAdmin):
    """
    Admin configuration for LearnerContentAssignmentAction.
    """
    list_display = (
        'uuid',
        'get_assignment',
        'action_type',
        'completed_at',
        'error_reason',
        'modified',
    )
    ordering = ['-modified']
    search_fields = (
        'uuid',
        'assignment__uuid',
        'traceback',
    )
    list_filter = ('action_type', 'error_reason')
    readonly_fields = (
        'created',
        'modified',
        'traceback',
    )
    autocomplete_fields = ['assignment']

    list_select_related = ('assignment',)

    @admin.display(ordering='uuid', description='Assignment UUID')
    def get_assignment(self, obj):
        return obj.assignment.uuid

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'assignment',
        )
