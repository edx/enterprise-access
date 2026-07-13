"""
customer billing serializers
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from django.conf import settings
from django_countries.serializer_fields import CountryField
from django_countries.serializers import CountryFieldMixin
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import APIException

from enterprise_access.apps.customer_billing.constants import ALLOWED_CHECKOUT_INTENT_STATE_TRANSITIONS
from enterprise_access.apps.customer_billing.embargo import get_embargoed_countries
from enterprise_access.apps.customer_billing.models import (
    CheckoutIntent,
    FailedCheckoutIntentConflict,
    SlugReservationConflict,
    SspProduct,
    StripeEventSummary
)


class RecordConflictError(APIException):
    """
    Raise this to trigger HTTP 422, as an alternative to ValidationError (HTTP 400).

    422 more accurately describes a conflicting database record, whereas 400 means there's an issue
    with the query structure or values.
    """
    status_code = 422
    default_detail = 'Encountered a conflicting record.'
    default_code = 'record_conflict_error'


BILLING_ADDRESS_FIELDS = (
    'billing_address_country',
    'billing_address_line_1',
    'billing_address_line_2',
    'billing_address_city',
    'billing_address_state',
    'billing_address_postal_code',
)
BILLING_ADDRESS_REQUIRED_FIELDS = (
    'billing_address_country',
    'billing_address_line_1',
    'billing_address_city',
    'billing_address_state',
    'billing_address_postal_code',
)


def _has_billing_address_value(value):
    return value not in (None, '')


def validate_billing_address_fields(serializer, attrs):
    """
    Require a complete billing address whenever any billing address field is supplied.
    """
    final_values = {}
    for field_name in BILLING_ADDRESS_FIELDS:
        if field_name in attrs:
            final_values[field_name] = attrs[field_name]
        elif serializer.instance is not None:
            final_values[field_name] = getattr(serializer.instance, field_name)
        else:
            final_values[field_name] = None

    if not any(_has_billing_address_value(value) for value in final_values.values()):
        return attrs

    errors = {
        field_name: 'This field is required when billing address details are provided.'
        for field_name in BILLING_ADDRESS_REQUIRED_FIELDS
        if not _has_billing_address_value(final_values[field_name])
    }
    if errors:
        raise serializers.ValidationError(errors)
    return attrs


# pylint: disable=abstract-method
class CustomerBillingCreateCheckoutSessionRequestSerializer(serializers.Serializer):
    """
    Request serializer for body of POST requests to /api/v1/customer-billing/create-checkout-session
    """
    admin_email = serializers.EmailField(
        required=True,
        help_text='The email corresponding to a registered user to assign as admin.',
    )
    enterprise_slug = serializers.SlugField(
        required=True,
        help_text='The unique slug proposed for the Enterprise Customer.',
    )
    company_name = serializers.CharField(
        required=True,
        help_text='The unique name proposed for the Enterprise Customer.',
    )
    quantity = serializers.IntegerField(
        required=True,
        min_value=1,
        help_text=(
            'Unit depends on the Stripe Price object. '
            'This could be count of subscription licenses, but could also be USD of Learner Credit.'
        )
    )
    stripe_price_id = serializers.CharField(
        required=True,
        help_text='The ID of the Stripe Price object representing the plan selection.',
    )
    ssp_product_slug = serializers.SlugField(
        required=False,
        help_text='The slug of the SSP product representing the plan selection.',
    )
    billing_address_country = CountryField(
        required=False,
        allow_null=True,
        help_text='Two-letter ISO country code for the billing address.',
    )
    billing_address_line_1 = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=255,
        help_text='First line of the billing street address.',
    )
    billing_address_line_2 = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=255,
        help_text='Second line of the billing street address (optional).',
    )
    billing_address_city = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=255,
        help_text='Billing address city.',
    )
    billing_address_state = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=255,
        help_text='Billing address state or province.',
    )
    billing_address_postal_code = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=20,
        help_text='Billing address postal code.',
    )

    def validate(self, attrs):
        """
        Require a complete billing address when any billing address field is supplied.
        """
        return validate_billing_address_fields(self, attrs)


# pylint: disable=abstract-method
class CustomerBillingCreateCheckoutSessionSuccessResponseSerializer(serializers.Serializer):
    """
    Response serializer for response body from POST /api/v1/customer-billing/create-checkout-session

    Specifically for HTTP 201 CREATED responses.
    """
    checkout_session_client_secret = serializers.CharField(
        required=True,
        help_text=(
            'Secret identifier for the newly created Stripe checkout session. Pass this to the '
            'frontend stripe component.'
        ),
    )


class ErrorDetailSerializer(serializers.Serializer):
    """
    Common pattern for serialized error details.

    Can be used for both field-level validation errors and general error messages.
    """
    error_code = serializers.CharField(
        required=True,
        help_text='Error code for the error.',
    )
    developer_message = serializers.CharField(
        required=True,
        help_text='System message (not intended for user display) describing the error.',
    )


# pylint: disable=abstract-method
class CustomerBillingCreateCheckoutSessionValidationFailedResponseSerializer(serializers.Serializer):
    """
    Response serializer for response body from POST /api/v1/customer-billing/create-checkout-session

    Specifically for HTTP 422 UNPROCESSABLE ENTITY responses.
    """
    admin_email = ErrorDetailSerializer(
        required=False,
        help_text='Validation results for admin_email if validation failed. Absent otherwise.',
    )
    enterprise_slug = ErrorDetailSerializer(
        required=False,
        help_text='Validation results for enterprise_slug if validation failed. Absent otherwise.',
    )
    quantity = ErrorDetailSerializer(
        required=False,
        help_text='Validation results for quantity if validation failed. Absent otherwise.',
    )
    stripe_price_id = ErrorDetailSerializer(
        required=False,
        help_text='Validation results for stripe_price_id if validation failed. Absent otherwise.',
    )
    ssp_product_slug = ErrorDetailSerializer(
        required=False,
        help_text='Validation results for ssp_product_slug if validation failed. Absent otherwise.',
    )
    company_name = ErrorDetailSerializer(
        required=False,
        help_text='Validation results for company_name if validation failed. Absent otherwise.',
    )
    errors = ErrorDetailSerializer(
        required=False,
        many=True,
        help_text='Errors if ChekoutIntent creation failed for non-field-specific reasons. Absent otherwise.',
    )


class CheckoutIntentReadOnlySerializer(CountryFieldMixin, serializers.ModelSerializer):
    """
    Serializer for reading and updating CheckoutIntent model instances.
    """
    workflow = serializers.UUIDField(source='workflow.uuid', read_only=True, allow_null=True)

    class Meta:
        model = CheckoutIntent
        fields = '__all__'
        read_only_fields = [field.name for field in CheckoutIntent._meta.get_fields()]


class CheckoutIntentUpdateRequestSerializer(CountryFieldMixin, serializers.ModelSerializer):
    """
    Write serializer for CheckoutIntent - used for PATCH operations.
    """

    class Meta:
        model = CheckoutIntent
        fields = '__all__'
        read_only_fields = [
            field.name for field in CheckoutIntent._meta.get_fields()
            if field.name not in (
                'state',
                'country',
                'billing_address_country',
                'billing_address_line_1',
                'billing_address_line_2',
                'billing_address_city',
                'billing_address_state',
                'billing_address_postal_code',
                'terms_metadata',
            )
        ]

    def validate_state(self, value):
        """
        Validate that the state transition is allowed.
        """
        instance = self.instance
        if instance:
            current_state = instance.state
            if (current_state != value) and \
               (value not in ALLOWED_CHECKOUT_INTENT_STATE_TRANSITIONS.get(current_state, [])):
                raise serializers.ValidationError(
                    f'Invalid state transition from {current_state} to {value}'
                )

        return value

    def validate_country(self, value):
        """
        Reject embargoed countries.
        """

        if value and value in get_embargoed_countries():
            raise serializers.ValidationError(
                f'Country {value} is not supported.'
            )
        return value

    def validate_terms_metadata(self, value):
        """
        Validate that terms_metadata is a dictionary/object, not a list or string.
        """
        if value is not None and not isinstance(value, dict):
            raise serializers.ValidationError(
                'terms_metadata must be a dictionary/object, not a list or string.'
            )
        return value

    def validate(self, attrs):
        """
        Perform cross-field validation, including optional billing address completeness.
        """
        attrs = super().validate(attrs)
        return validate_billing_address_fields(self, attrs)


class CheckoutIntentCreateRequestSerializer(CountryFieldMixin, serializers.ModelSerializer):
    """
    A serializer intended for creating new CheckoutIntents.
    """

    ssp_product = serializers.PrimaryKeyRelatedField(
        queryset=SspProduct.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = CheckoutIntent
        fields = '__all__'
        read_only_fields = [
            field.name for field in CheckoutIntent._meta.get_fields()
            if field.name not in [
                'enterprise_slug',
                'enterprise_name',
                'quantity',
                'country',
                'billing_address_country',
                'billing_address_line_1',
                'billing_address_line_2',
                'billing_address_city',
                'billing_address_state',
                'billing_address_postal_code',
                'terms_metadata',
                'ssp_product'
            ]
        ]

    # Put some reasonable validation bounds at this layer, and let
    # the customer_billing.api business logic handle more detailed validation
    quantity = serializers.IntegerField(min_value=1, max_value=1000)

    def validate_terms_metadata(self, value):
        """
        Validate that terms_metadata is a dictionary/object, not a list or string.
        """
        if value is not None and not isinstance(value, dict):
            raise serializers.ValidationError(
                'terms_metadata must be a dictionary/object, not a list or string.'
            )
        return value

    def validate(self, attrs):
        """
        Perform any cross-field validation.
        """
        if attrs.get('enterprise_slug') and not attrs.get('enterprise_name'):
            raise serializers.ValidationError(
                {'enterprise_name': 'enterprise_name is required when enterprise_slug is provided.'}
            )
        if attrs.get('enterprise_name') and not attrs.get('enterprise_slug'):
            raise serializers.ValidationError(
                {'enterprise_slug': 'enterprise_slug is required when enterprise_name is provided.'}
            )
        return validate_billing_address_fields(self, attrs)

    def create(self, validated_data):
        """
        Creates a new CheckoutIntent.
        """
        try:
            ssp_product = validated_data.pop('ssp_product', None)
            return CheckoutIntent.create_intent(
                user=self.context['request'].user,
                quantity=validated_data['quantity'],
                slug=validated_data.get('enterprise_slug'),
                name=validated_data.get('enterprise_name'),
                country=validated_data.get('country'),
                billing_address_country=validated_data.get('billing_address_country'),
                billing_address_line_1=validated_data.get('billing_address_line_1'),
                billing_address_line_2=validated_data.get('billing_address_line_2'),
                billing_address_city=validated_data.get('billing_address_city'),
                billing_address_state=validated_data.get('billing_address_state'),
                billing_address_postal_code=validated_data.get('billing_address_postal_code'),
                terms_metadata=validated_data.get('terms_metadata'),
                ssp_product=ssp_product,
            )

        # Catch exceptions that should return 422:
        except SlugReservationConflict as exc:
            raise RecordConflictError('enterprise_slug or enterprise_name has already been reserved.') from exc
        except FailedCheckoutIntentConflict as exc:
            raise RecordConflictError('Requesting user already has a failed CheckoutIntent.') from exc

        # All other exceptions should return 5xx.


class StripeEventSummaryReadOnlySerializer(serializers.ModelSerializer):
    """
    Serializer for reading StripeEventSummary model instances.
    """
    class Meta:
        model = StripeEventSummary
        fields = '__all__'
        read_only_fields = [field.name for field in StripeEventSummary._meta.get_fields()]


# pylint: disable=abstract-method
class StripeSubscriptionPlanInfoResponseSerializer(serializers.Serializer):
    """
    Response serializer for response body from GET /api/v1/stripe-event-summary/get-stripe-subscription-plan-info
    """
    upcoming_invoice_amount_due = serializers.IntegerField(
        allow_null=True,
        required=False,
        help_text='Upcoming invoice amount due (in cents) related to this event/subscription (if trial subscription)',
    )

    currency = serializers.CharField(
        allow_null=True,
        required=False,
        help_text='Three-letter ISO currency code associated with the subscription.',
    )

    canceled_date = serializers.DateTimeField(
        allow_null=True,
        required=False,
        help_text=(
            'Timestamp when the subscription is scheduled to be canceled. '
            'None if no cancellation is scheduled or if the subscription has already been deleted.'
        ),
    )

    checkout_intent_uuid = serializers.UUIDField(
        allow_null=True,
        required=False,
        help_text='UUID of Checkout Intent associated with the stripe event.',
    )

    is_canceled = serializers.BooleanField(
        default=False,
        required=False,
        help_text=(
            'True if the subscription is currently canceled (renewal record is_canceled=True). '
            'False if it is currently active, including after an un-canceling event.'
        ),
    )

    renewed_subscription_plan_uuid = serializers.UUIDField(
        allow_null=True,
        required=False,
        help_text=(
            "UUID of the renewed (paid) subscription plan linked to this subscription's "
            'renewal record. The front-end should suppress this plan UUID '
            'when is_canceled is true, or when canceled_date is a future date '
            '(cancellation is scheduled but not yet in effect).'
        ),
    )


# pylint: disable=abstract-method
class BillingAddressResponseSerializer(serializers.Serializer):
    """
    Response serializer for billing address from GET /api/v1/billing-management/address
    """
    name = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Full name of the billing contact',
    )
    email = serializers.EmailField(
        required=False,
        allow_null=True,
        help_text='Email address associated with the billing account',
    )
    country = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Two-letter ISO country code',
    )
    address_line_1 = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text='First line of the street address',
    )
    address_line_2 = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text='Second line of the street address (optional)',
    )
    city = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text='City of the billing address',
    )
    state = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text='State or province of the billing address',
    )
    postal_code = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text='Postal code or zip code of the billing address',
    )
    phone = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Phone number associated with the billing account',
    )


# pylint: disable=abstract-method
class BillingAddressUpdateRequestSerializer(serializers.Serializer):
    """
    Request serializer for updating billing address via POST /api/v1/billing-management/address
    """
    name = serializers.CharField(
        required=True,
        max_length=255,
        help_text='Full name of the billing contact',
    )
    email = serializers.EmailField(
        required=True,
        help_text='Email address associated with the billing account',
    )
    country = serializers.CharField(
        required=True,
        max_length=2,
        min_length=2,
        help_text='Two-letter ISO country code',
    )
    address_line_1 = serializers.CharField(
        required=True,
        max_length=255,
        help_text='First line of the street address',
    )
    address_line_2 = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        help_text='Second line of the street address (optional)',
    )
    city = serializers.CharField(
        required=True,
        max_length=255,
        help_text='City of the billing address',
    )
    state = serializers.CharField(
        required=True,
        max_length=255,
        help_text='State or province of the billing address',
    )
    postal_code = serializers.CharField(
        required=True,
        max_length=20,
        help_text='Postal code or zip code of the billing address',
    )
    phone = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=20,
        help_text='Phone number associated with the billing account',
    )

    def validate_country(self, value):
        """
        Validate that country is a valid two-letter ISO code.
        """
        if len(value) != 2 or not value.isalpha():
            raise serializers.ValidationError(
                'Country must be a valid two-letter ISO code (e.g., US, CA, GB)'
            )
        return value.upper()

    def validate_postal_code(self, value):
        """
        Validate postal code is not empty if required.
        """
        if not value or not value.strip():
            raise serializers.ValidationError('Postal code is required and cannot be empty')
        return value


# pylint: disable=abstract-method
class PaymentMethodResponseSerializer(serializers.Serializer):
    """
    Response serializer for a single payment method from GET /api/v1/billing-management/payment-methods
    """
    id = serializers.CharField(
        required=True,
        help_text='Unique identifier for the payment method in Stripe',
    )
    type = serializers.CharField(
        required=True,
        help_text='Type of payment method (e.g., card, us_bank_account)',
    )
    last4 = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Last 4 digits of the card or account number',
    )
    brand = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Card brand (e.g., visa, mastercard) - only for card type',
    )
    exp_month = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text='Card expiration month - only for card type',
    )
    exp_year = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text='Card expiration year - only for card type',
    )
    is_default = serializers.BooleanField(
        required=True,
        help_text='Whether this is the default payment method for the customer',
    )
    status = serializers.ChoiceField(
        choices=['verified', 'pending', 'failed'],
        required=True,
        help_text=(
            'Verification status: verified (ready to use), pending (awaiting verification), '
            'failed (verification failed)'
        ),
    )


# pylint: disable=abstract-method
class PaymentMethodsListResponseSerializer(serializers.Serializer):
    """
    Response serializer for list of payment methods from GET /api/v1/billing-management/payment-methods
    """
    payment_methods = PaymentMethodResponseSerializer(
        many=True,
        required=True,
        help_text='List of payment methods for the customer',
    )


# pylint: disable=abstract-method
class SetDefaultPaymentMethodRequestSerializer(serializers.Serializer):
    """
    Request serializer for setting a payment method as default via
    POST /api/v1/billing-management/payment-methods/{id}/set-default

    Note: This serializer is used only for OpenAPI/Swagger documentation generation.
    The actual endpoint receives the payment_method_id from the URL path parameter,
    not from the request body.
    """
    payment_method_id = serializers.CharField(
        required=True,
        help_text='Unique identifier of the payment method to set as default',
    )


# pylint: disable=abstract-method
class AttachPaymentMethodRequestSerializer(serializers.Serializer):
    """
    Request serializer for attaching a payment method via POST /api/v1/billing-management/payment-methods/

    The payment_method_id is a Stripe PaymentMethod ID created client-side via Stripe Elements.
    """
    payment_method_id = serializers.CharField(
        required=True,
        help_text='Stripe payment method ID (e.g., pm_xxxxx) created client-side via Stripe Elements',
    )


# pylint: disable=abstract-method
class AttachPaymentMethodResponseSerializer(serializers.Serializer):
    """
    Response serializer for successful payment method attachment
    """
    message = serializers.CharField(
        required=True,
        help_text='Success message',
    )
    payment_method_id = serializers.CharField(
        required=True,
        help_text='ID of the attached payment method',
    )


# pylint: disable=abstract-method
class TransactionResponseSerializer(serializers.Serializer):
    """
    Response serializer for a single transaction/invoice from GET /api/v1/billing-management/transactions
    """
    id = serializers.CharField(
        required=True,
        help_text='Unique identifier for the invoice/transaction in Stripe',
    )
    created = serializers.IntegerField(
        required=True,
        help_text='Invoice creation timestamp (Unix timestamp in seconds)',
    )
    amount = serializers.IntegerField(
        required=True,
        help_text='Amount in cents',
    )
    currency = serializers.CharField(
        required=True,
        max_length=3,
        help_text='Three-letter ISO currency code',
    )
    status = serializers.ChoiceField(
        choices=['paid', 'open', 'void', 'uncollectible'],
        required=True,
        help_text='Invoice status',
    )
    description = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Description or notes for the invoice',
    )
    invoice_pdf_url = serializers.URLField(
        required=False,
        allow_null=True,
        help_text='URL to download the invoice PDF',
    )
    receipt_url = serializers.URLField(
        required=False,
        allow_null=True,
        help_text='URL to view the receipt',
    )


# pylint: disable=abstract-method
class TransactionsListResponseSerializer(serializers.Serializer):
    """
    Response serializer for list of transactions from GET /api/v1/billing-management/transactions
    """
    transactions = TransactionResponseSerializer(
        many=True,
        required=True,
        help_text='List of transactions/invoices for the customer',
    )
    next_page_token = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Pagination token for next page of results, if more exist',
    )


# pylint: disable=abstract-method
class StripeSubscriptionResponseSerializer(serializers.Serializer):
    """
    Response serializer for a Stripe subscription object.

    Used for subscription-related endpoints including:
    - GET /api/v1/billing-management/subscription
    - POST /api/v1/billing-management/subscription/cancel
    - POST /api/v1/billing-management/subscription/reinstate
    """
    id = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Unique identifier for the subscription in Stripe',
    )
    status = serializers.CharField(
        required=False,
        allow_null=True,
        help_text='Status of the subscription (e.g., active, canceled)',
    )
    cancel_at_period_end = serializers.BooleanField(
        required=False,
        default=False,
        help_text='Whether the subscription is scheduled to cancel at the end of the current period',
    )
    cancel_at = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text='Unix timestamp when the subscription is scheduled to be canceled',
    )
    current_period_end = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text='Unix timestamp of the end of the current billing period',
    )
    currency = serializers.CharField(
        required=False,
        allow_null=True,
        max_length=3,
        help_text='Three-letter ISO currency code',
    )
    yearly_amount = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text='Total yearly recurring revenue in cents',
    )
    license_count = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text='Total number of licenses/seats in the subscription',
    )


# pylint: disable=abstract-method
class SspEssentialsProductResponseSerializer(serializers.Serializer):
    """Serialized SSP Essentials product — field logic lives here, not in the view."""

    name = serializers.SerializerMethodField()
    long_name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    marketing_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    lookup_key = serializers.SerializerMethodField()
    slug = serializers.SlugField(read_only=True)

    # ── helpers ──────────────────────────────────────────────

    def _price_data(self, obj):
        """Return the cached Stripe price dict for this product (or {})."""
        return self.context.get('pricing', {}).get(obj.stripe_price_lookup_key) or {}

    def _academy_field(self, obj, field):
        """Safely read an academy metadata field from the SspProduct."""
        return getattr(obj, field, None)

    @staticmethod
    def _build_public_thumbnail_url(thumbnail_url):
        """Convert relative thumbnail paths to fully-qualified public URLs."""
        if not thumbnail_url or not isinstance(thumbnail_url, str):
            return None
        if thumbnail_url.startswith(('http://', 'https://')):
            return thumbnail_url
        base_url = getattr(settings, 'SSP_ESSENTIALS_THUMBNAIL_S3_BASE_URL', None)
        if not base_url:
            return thumbnail_url
        return urljoin(f'{base_url.rstrip("/")}/', thumbnail_url.lstrip('/'))

    # ── field methods ────────────────────────────────────────

    def get_name(self, obj):
        return obj.academy_title or self._price_data(obj).get('stripe_name')

    def get_long_name(self, obj):
        return (
            obj.academy_long_name or
            obj.academy_title or
            self._price_data(obj).get('stripe_name')
        )

    def get_description(self, obj):
        return obj.academy_description or self._price_data(obj).get('stripe_description')

    def get_marketing_url(self, obj):
        return (
            obj.marketing_url or
            obj.academy_marketing_url or
            self._price_data(obj).get('stripe_marketing_url')
        )

    def get_thumbnail_url(self, obj):
        """Get and format the public thumbnail URL for the product."""
        thumbnail_url = obj.academy_thumbnail_url or self._price_data(obj).get('stripe_thumbnail_url')
        return self._build_public_thumbnail_url(thumbnail_url)

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_tags(self, obj):
        return obj.academy_tags or []

    def get_price(self, obj):
        """Get and format the product price as a string."""
        price_value = self._price_data(obj).get('unit_amount_decimal')
        if price_value is None:
            return None
        try:
            return f'{Decimal(str(price_value)):.2f}'
        except (InvalidOperation, TypeError, ValueError):
            return None

    def get_lookup_key(self, obj):
        return obj.stripe_price_lookup_key
