"""
Serializers for the ``subsidy_requests`` app.
"""
import logging

from rest_framework import serializers

from enterprise_access.apps.content_assignments.models import LearnerContentAssignment
from enterprise_access.apps.subsidy_request.constants import SubsidyRequestStates
from enterprise_access.apps.subsidy_request.models import (
    CouponCodeRequest,
    LearnerCreditRequest,
    LearnerCreditRequestActions,
    LearnerCreditRequestConfiguration,
    LicenseRequest,
    SubsidyRequest,
    SubsidyRequestCustomerConfiguration
)

logger = logging.getLogger(__name__)


class SubsidyRequestSerializer(serializers.ModelSerializer):
    """
    Serializer for the abstract `SubsidyRequest` model.
    """

    email = serializers.EmailField(read_only=True, source="user.email")
    lms_user_id = serializers.IntegerField(read_only=True, source="user.lms_user_id")
    reviewer_lms_user_id = serializers.IntegerField(read_only=True, source="reviewer.lms_user_id", allow_null=True)
    course_partners = serializers.JSONField(read_only=True)

    class Meta:
        model = SubsidyRequest
        fields = [
            'uuid',
            'user',
            'lms_user_id',
            'email',
            'course_id',
            'course_title',
            'course_partners',
            'enterprise_customer_uuid',
            'state',
            'reviewed_at',
            'reviewer_lms_user_id',
            'decline_reason',
            'created',
            'modified',
        ]
        read_only_fields = [
            'uuid',
            'state',
            'lms_user_id',
            'email',
            'course_title',
            'course_partners',
            'reviewed_at',
            'reviewer_lms_user_id',
            'created',
            'modified',
        ]
        extra_kwargs = {
            'user': {'write_only': True},
        }
        abstract = True


class LicenseRequestSerializer(SubsidyRequestSerializer):
    """
    Serializer for the `LicenseRequest` model.
    """

    class Meta:
        model = LicenseRequest
        fields = SubsidyRequestSerializer.Meta.fields + [
            'subscription_plan_uuid',
            'license_uuid'
        ]
        read_only_fields = SubsidyRequestSerializer.Meta.read_only_fields + [
            'subscription_plan_uuid',
            'license_uuid'
        ]
        extra_kwargs = SubsidyRequestSerializer.Meta.extra_kwargs


class CouponCodeRequestSerializer(SubsidyRequestSerializer):
    """
    Serializer for the `CouponCodeRequest` model.
    """

    course_id = serializers.CharField(
        allow_blank=False,
        required=True,
    )

    class Meta:
        model = CouponCodeRequest
        fields = SubsidyRequestSerializer.Meta.fields + [
            'coupon_id',
            'coupon_code'
        ]
        read_only_fields = SubsidyRequestSerializer.Meta.read_only_fields + [
            'coupon_id',
            'coupon_code'
        ]
        extra_kwargs = SubsidyRequestSerializer.Meta.extra_kwargs


class SubsidyRequestCustomerConfigurationSerializer(serializers.ModelSerializer):
    """
    Serializer for the `SubsidyRequestCustomerConfiguration` model.
    """
    changed_by_lms_user_id = serializers.IntegerField(read_only=True, source="changed_by.lms_user_id", allow_null=True)

    class Meta:
        model = SubsidyRequestCustomerConfiguration
        fields = [
            'enterprise_customer_uuid',
            'subsidy_requests_enabled',
            'subsidy_type',
            'changed_by_lms_user_id'
        ]

    def update(self, instance, validated_data):
        # Pop enterprise_customer_uuid so that it's read-only for updates.
        validated_data.pop('enterprise_customer_uuid', None)
        return super().update(instance, validated_data)


class LearnerCreditRequestConfigurationSerializer(serializers.ModelSerializer):
    """
    Serializer for the `LearnerCreditRequestConfiguration` model.
    """

    class Meta:
        model = LearnerCreditRequestConfiguration
        fields = "__all__"
        read_only_fields = ["uuid", "created", "modified"]


class LearnerCreditRequestSerializer(SubsidyRequestSerializer):
    """
    Serializer for the `LearnerCreditRequest` model.
    """

    learner_credit_request_config = serializers.PrimaryKeyRelatedField(
        queryset=LearnerCreditRequestConfiguration.objects.all(),
        required=False,
        allow_null=True,
    )
    assignment = serializers.PrimaryKeyRelatedField(
        queryset=LearnerContentAssignment.objects.all(),
        required=False,
        allow_null=True,
    )
    course_price = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Cost of the content in USD Cents.",
    )
    latest_action = serializers.SerializerMethodField()
    learner_request_state = serializers.CharField(
        read_only=True,
        help_text="Computed state based on action status and error conditions. "
                  "Returns 'waiting' for approved/reminded actions without errors, "
                  "'failed' for actions with error_reason, or the actual status otherwise"
    )

    class Meta:
        model = LearnerCreditRequest
        fields = SubsidyRequestSerializer.Meta.fields + [
            "learner_credit_request_config",
            "assignment",
            "course_price",
            "latest_action",
            "learner_request_state",
        ]
        read_only_fields = SubsidyRequestSerializer.Meta.read_only_fields + [
            "latest_action",
            "learner_request_state",
        ]
        extra_kwargs = SubsidyRequestSerializer.Meta.extra_kwargs

    def get_latest_action(self, obj):
        """
        Returns the latest action for this learner credit request, if any exists.
        """
        latest_action = obj.actions.order_by('-created').first()
        if latest_action:
            return LearnerCreditRequestActionsSerializer(latest_action).data
        return None


class LearnerCreditRequestActionsSerializer(serializers.ModelSerializer):
    """
    Serializer for the `LearnerCreditRequestActions` model.
    """

    class Meta:
        model = LearnerCreditRequestActions
        fields = [
            'uuid',
            'recent_action',
            'status',
            'error_reason',
            'traceback',
            'created',
            'modified',
            'learner_credit_request',
        ]
        read_only_fields = [
            'uuid',
            'created',
            'modified',
        ]
        extra_kwargs = {
            'learner_credit_request': {'write_only': True},
        }


class LearnerCreditRequestDeclineSerializer(serializers.Serializer):
    """
    Serializer for declining a learner credit request.
    """

    subsidy_request_uuid = serializers.UUIDField(
        required=True, help_text="UUID of the learner credit request to decline"
    )
    send_notification = serializers.BooleanField(
        default=False, help_text="Whether to send decline notification email to the learner"
    )
    disassociate_from_org = serializers.BooleanField(
        default=False, help_text="Whether to unlink the user from the enterprise organization"
    )
    # Add a new optional field 'decline_reason' to capture reason for decline
    # Default value is None (null) if not provided
    decline_reason = serializers.CharField(
        required=False,     # makes the field optional
        allow_blank=True,   # allows empty string ""
        allow_null=True,    # allows null value
        help_text="Reason for declining"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._learner_credit_request = None

    def validate_subsidy_request_uuid(self, value):
        """
        Validate that the subsidy request exists and can be declined.
        """
        try:
            learner_credit_request = LearnerCreditRequest.objects.get(uuid=value)
        except LearnerCreditRequest.DoesNotExist as exc:
            raise serializers.ValidationError(f"Learner Credit Request with UUID {value} not found.") from exc

        if learner_credit_request.state not in [SubsidyRequestStates.REQUESTED]:
            raise serializers.ValidationError(
                f'Learner Credit Request with UUID {value} cannot be declined. '
                f'Current state: {learner_credit_request.state}'
            )

        # Store the fetched object for later use
        self._learner_credit_request = learner_credit_request

        return value

    def get_learner_credit_request(self):
        """
        Return the already-fetched LearnerCreditRequest object
        """
        return self._learner_credit_request

    def create(self, validated_data):
        """
        Not implemented - this serializer is for validation only
        """
        raise NotImplementedError("This serializer is for validation only")

    def update(self, instance, validated_data):
        """
        Not implemented - this serializer is for validation only
        """
        raise NotImplementedError("This serializer is for validation only")


class LearnerCreditRequestApproveRequestSerializer(serializers.Serializer):
    """
    Request Serializer to validate subsidy-request ``approve`` endpoint POST data.

    For view: LearnerCreditRequestViewSet.approve
    """
    learner_credit_request_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=True,
        allow_empty=False,
        help_text="A list of LearnerCreditRequest UUIDs to be approved."
    )
    policy_uuid = serializers.UUIDField(
        required=True,
        help_text="The UUID of the SubsidyAccessPolicy to use for this approval."
    )

    def create(self, validated_data):
        """
        Not implemented - this serializer is for validation only
        """
        raise NotImplementedError("This serializer is for validation only")

    def update(self, instance, validated_data):
        """
        Not implemented - this serializer is for validation only
        """
        raise NotImplementedError("This serializer is for validation only")


class LearnerCreditRequestApproveAllSerializer(serializers.Serializer):
    """
    Request serializer to validate the approve-all action.
    """
    policy_uuid = serializers.UUIDField(
        required=True,
        help_text="The UUID of the SubsidyAccessPolicy to use for this approval."
    )

    def create(self, validated_data):
        """
        Not implemented - this serializer is for validation only
        """
        raise NotImplementedError("This serializer is for validation only")

    def update(self, instance, validated_data):
        """
        Not implemented - this serializer is for validation only
        """
        raise NotImplementedError("This serializer is for validation only")


# pylint: disable=abstract-method
class LearnerCreditRequestCancelSerializer(serializers.Serializer):
    """
    Request serializer to validate cancel endpoint query params.

    For view: LearnerCreditRequestViewSet.cancel
    """
    request_uuid = serializers.UUIDField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._learner_credit_request = None

    def validate_request_uuid(self, value):
        """
        Validate that the learner credit request exists and store it for later use.
        """
        try:
            learner_credit_request = LearnerCreditRequest.objects.get(uuid=value)
            self._learner_credit_request = learner_credit_request
            return value
        except LearnerCreditRequest.DoesNotExist as exc:
            raise serializers.ValidationError(f"Learner credit request with uuid {value} not found.") from exc

    def get_learner_credit_request(self):
        """
        Return the already-fetched learner credit request object.
        """
        return getattr(self, '_learner_credit_request', None)


class LearnerCreditRequestRemindSerializer(serializers.Serializer):
    """
    Request serializer to validate remind endpoint for a LearnerCreditRequest.

    For view: LearnerCreditRequestViewSet.remind
    """
    learner_credit_request_uuid = serializers.UUIDField(
        required=True,
        help_text="The UUID of the LearnerCreditRequest to be reminded."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._learner_credit_request = None

    def validate_learner_credit_request_uuid(self, value):
        """
        Validate that the learner credit request exists, has an associated assignment,
        and is in a state where a reminder is appropriate.
        """
        try:
            learner_credit_request = LearnerCreditRequest.objects.select_related('assignment').get(uuid=value)
        except LearnerCreditRequest.DoesNotExist as exc:
            raise serializers.ValidationError(f"Learner credit request with uuid {value} not found.") from exc

        if learner_credit_request.state != SubsidyRequestStates.APPROVED:
            raise serializers.ValidationError(
                f"Cannot send a reminder for a request in the '{learner_credit_request.state}' state. "
                "Reminders can only be sent for 'APPROVED' requests."
            )

        if not learner_credit_request.assignment:
            raise serializers.ValidationError(
                f"The learner credit request with uuid {value} does not have an associated assignment."
            )

        self._learner_credit_request = learner_credit_request
        return value

    def get_learner_credit_request(self):
        """
        Return the already-fetched learner credit request object.
        """
        return getattr(self, '_learner_credit_request', None)


class LearnerCreditRequestBulkApproveRequestSerializer(serializers.Serializer):
    """
    Serializer for bulk approving learner credit requests.
    """
    policy_uuid = serializers.UUIDField(
        required=True,
        help_text='The UUID of the subsidy access policy to use for approval.',
    )
    enterprise_customer_uuid = serializers.UUIDField(
        required=True,
        help_text='The UUID of the enterprise customer.',
    )
    approve_all = serializers.BooleanField(
        required=False,
        default=False,
        help_text='If true, approve all pending requests for the enterprise customer.',
    )
    subsidy_request_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text='List of subsidy request UUIDs to approve.',
    )

    def validate(self, attrs):
        """
        Validate that either approve_all is True or subsidy_request_uuids is provided, but not both.
        """
        approve_all = attrs.get('approve_all', False)
        subsidy_request_uuids = attrs.get('subsidy_request_uuids', [])

        if approve_all and subsidy_request_uuids:
            raise serializers.ValidationError(
                'Cannot specify both approve_all and subsidy_request_uuids. Please choose one.'
            )

        if not approve_all and not subsidy_request_uuids:
            raise serializers.ValidationError(
                'Must specify either approve_all=True or provide subsidy_request_uuids.'
            )

        return attrs


class LearnerCreditRequestBulkDeclineSerializer(serializers.Serializer):
    """
    Serializer for bulk declining learner credit requests.

    Request Payload (per ticket spec):
    {
        "enterprise_customer_uuid": "<uuid>",  # Required for permission checking
        "request_uuids": [],
        "decline_all": false,
        "policy_uuid": "<uuid>"
    }
    """
    enterprise_customer_uuid = serializers.UUIDField(
        required=True,
        help_text='The UUID of the enterprise customer.',
    )
    policy_uuid = serializers.UUIDField(
        required=True,
        help_text='The UUID of the subsidy access policy associated with the requests.',
    )
    decline_all = serializers.BooleanField(
        required=False,
        default=False,
        help_text='If true, decline all open requests associated with a budget.',
    )
    request_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text='List of learner credit request UUIDs to decline.',
    )

    def validate(self, attrs):
        """
        Validate that either decline_all is True or request_uuids is provided, but not both.
        """
        decline_all = attrs.get('decline_all', False)
        request_uuids = attrs.get('request_uuids', [])

        if decline_all and request_uuids:
            raise serializers.ValidationError(
                'Cannot specify both decline_all and request_uuids. Please choose one.'
            )

        if not decline_all and not request_uuids:
            raise serializers.ValidationError(
                'Must specify either decline_all=True or provide request_uuids.'
            )

        return attrs
