"""
REST API views for the billing provider (Stripe) integration.
"""
import logging
import uuid

import stripe
from django.conf import settings
from django.http import HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, OpenApiTypes, extend_schema, extend_schema_view
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from rest_framework import exceptions, mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from enterprise_access.apps.api import serializers
from enterprise_access.apps.api.authentication import StripeWebhookAuthentication
from enterprise_access.apps.core.constants import (
    ALL_ACCESS_CONTEXT,
    BILLING_MANAGEMENT_ACCESS_PERMISSION,
    CHECKOUT_INTENT_READ_WRITE_ALL_PERMISSION,
    CUSTOMER_BILLING_CREATE_PORTAL_SESSION_PERMISSION,
    STRIPE_EVENT_SUMMARY_READ_PERMISSION
)
from enterprise_access.apps.customer_billing.api import (
    CreateCheckoutSessionFailedConflict,
    CreateCheckoutSessionSlugReservationConflict,
    CreateCheckoutSessionValidationError,
    create_free_trial_checkout_session
)
from enterprise_access.apps.customer_billing.models import CheckoutIntent, StripeEventSummary
from enterprise_access.apps.customer_billing.stripe_api import get_stripe_customer
from enterprise_access.apps.customer_billing.stripe_event_handlers import StripeEventHandler

from .constants import CHECKOUT_INTENT_EXAMPLES, ERROR_RESPONSES, PATCH_REQUEST_EXAMPLES

stripe.api_key = settings.STRIPE_API_KEY
logger = logging.getLogger(__name__)

CUSTOMER_BILLING_API_TAG = 'Customer Billing'
STRIPE_EVENT_API_TAG = 'Stripe Event Summary'


class CheckoutIntentPermission(permissions.BasePermission):
    """
    Check for existence of a CheckoutIntent related to the requesting user,
    but only for some views.
    """
    def has_permission(self, request, view):
        if view.action != 'create_checkout_portal_session':
            return True

        checkout_intent_pk = request.parser_context['kwargs']['pk']

        # Try UUID lookup first, then fall back to id lookup
        try:
            uuid_value = uuid.UUID(checkout_intent_pk)
            intent_record = CheckoutIntent.objects.filter(uuid=uuid_value).first()
        except (ValueError, TypeError):
            # Fall back to id lookup
            try:
                int_value = int(checkout_intent_pk)
                intent_record = CheckoutIntent.objects.filter(pk=int_value).first()
            except (ValueError, TypeError):
                return False

        if not intent_record:
            return False

        if intent_record.user != request.user:
            return False

        return True


class CustomerBillingViewSet(viewsets.ViewSet):
    """
    Viewset supporting operations pertaining to customer billing.
    """
    authentication_classes = (JwtAuthentication,)
    permission_classes = (permissions.IsAuthenticated, CheckoutIntentPermission)

    @extend_schema(
        tags=[CUSTOMER_BILLING_API_TAG],
        summary='Listen for events from Stripe.',
    )
    @action(
        detail=False,
        methods=['post'],
        url_path='stripe-webhook',
        authentication_classes=(StripeWebhookAuthentication,),
        permission_classes=(permissions.AllowAny,),
    )
    @csrf_exempt
    def stripe_webhook(self, request):
        """
        Listen for events from Stripe, and take specific actions. Typically the action is to send a confirmation email.

        Authentication is performed via Stripe signature validation in StripeWebhookAuthentication.

        TODO:
        * For a real production implementation we should implement event de-duplication:
          - https://docs.stripe.com/webhooks/process-undelivered-events
          - This is a safeguard against the remote possibility that an event is sent twice. This could happen if the
            network connection cuts out at the exact moment between successfully processing an event and responding with
            HTTP 200, in which case Stripe will attempt to re-send the event since it does not know we successfully
            received it.
        """
        # Event must be parsed and verified by the authentication class.
        event = getattr(request, '_stripe_event', None)
        if event is None:
            # This should not occur if StripeWebhookAuthentication is applied.
            return Response(
                'Stripe WebHook event missing after authentication.',
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Could throw an exception. Do NOT swallow the exception because we
        # need the error response to trigger webhook retries.
        StripeEventHandler.dispatch(event)

        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        tags=[CUSTOMER_BILLING_API_TAG],
        summary='Create a new checkout session given form data from a prospective customer.',
        request=serializers.CustomerBillingCreateCheckoutSessionRequestSerializer,
        responses={
            status.HTTP_201_CREATED: serializers.CustomerBillingCreateCheckoutSessionSuccessResponseSerializer,
            status.HTTP_422_UNPROCESSABLE_ENTITY: (
                serializers.CustomerBillingCreateCheckoutSessionValidationFailedResponseSerializer
            ),
        },
    )
    @action(
        detail=False,
        methods=['post'],
        url_path='create-checkout-session',
    )
    def create_checkout_session(self, request, *args, **kwargs):
        """
        Create a new Stripe checkout session for a free trial and return it's client_secret.

        Notes:
        * This endpoint is designed to be called AFTER logistration, but BEFORE displaying a payment entry form.  A
          Stripe "Checkout Session" object is a prerequisite to rendering the Stripe embedded component for payment
          entry.
        * The @permission_required() decorator has NOT been added. This endpoint only requires an authenticated LMS
          user, which is more permissive than our usual requirement for a user with an enterprise role.
        * This endpoint is NOT idempotent and will create new checkout sessions on each subsequent call.
          TODO: introduce an idempotency key and a new model to hold pending requests.

        Request/response structure:

            POST /api/v1/customer-billing/create_checkout_session
            >>> {
            >>>     "admin_email": "dr@evil.inc",
            >>>     "enterprise_slug": "my-sluggy"
            >>>     "quantity": 7,
            >>>     "stripe_price_id": "price_1MoBy5LkdIwHu7ixZhnattbh"
            >>> }
            HTTP 201 CREATED
            >>> {
            >>>     "checkout_session_client_secret": "cs_Hu7ixZhnattbh1MoBy5LkdIw"
            >>> }
            HTTP 422 UNPROCESSABLE ENTITY (only admin_email validation failed)
            >>> {
            >>>     "admin_email": {
            >>>         "error_code": "not_registered",
            >>>         "developer_message": "The provided email has not yet been registered."
            >>>     }
            >>> }
            HTTP 422 UNPROCESSABLE ENTITY (only enterprise_slug validation failed)
            >>> {
            >>>     "enterprise_slug": {
            >>>         "error_code": "existing_enterprise_customer_for_admin",
            >>>         "developer_message": "Slug invalid: Admin belongs to existing customer..."
            >>>     }
            >>> }
        """
        serializer = serializers.CustomerBillingCreateCheckoutSessionRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        validated_data = serializer.validated_data

        # Simplify tracking create_plan requests using k="v" machine-readable formatting.
        logger.info(
            'Handling request to create free trial plan. '
            f'enterprise_slug="{validated_data["enterprise_slug"]}" '
            f'quantity="{validated_data["quantity"]}" '
            f'stripe_price_id="{validated_data["stripe_price_id"]}"'
        )
        try:
            session = create_free_trial_checkout_session(
                user=request.user,
                **serializer.validated_data,
            )
        except CreateCheckoutSessionValidationError as exc:
            response_serializer = serializers.CustomerBillingCreateCheckoutSessionValidationFailedResponseSerializer(
                data=exc.validation_errors_by_field,
            )
            if not response_serializer.is_valid():
                return HttpResponseServerError()
            return Response(response_serializer.data, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except (CreateCheckoutSessionSlugReservationConflict, CreateCheckoutSessionFailedConflict) as exc:
            response_serializer = serializers.CustomerBillingCreateCheckoutSessionValidationFailedResponseSerializer(
                errors=exc.non_field_errors,
            )
            if not response_serializer.is_valid():
                return HttpResponseServerError()
            return Response(response_serializer.data, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        response_serializer = serializers.CustomerBillingCreateCheckoutSessionSuccessResponseSerializer(
            data={'checkout_session_client_secret': session.client_secret},
        )
        if not response_serializer.is_valid():
            return HttpResponseServerError()
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=[CUSTOMER_BILLING_API_TAG],
        summary='Create a new Customer Portal Session from the Admin portal MFE.',
    )
    @action(
        detail=False,
        methods=['get'],
        url_path='create-enterprise-admin-portal-session',
    )
    # # UUID in path is used as the "permission object" for role-based auth.
    @permission_required(
        CUSTOMER_BILLING_CREATE_PORTAL_SESSION_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid') or kwargs.get(
            'enterprise_customer_uuid')
    )
    def create_enterprise_admin_portal_session(self, request, **kwargs):
        """
        Create a new Customer Portal Session for the Admin Portal MFE.  Response dict contains "url" key
        that should be attached to a button that the customer clicks.

        Response structure defined here: https://docs.stripe.com/api/customer_portal/sessions/create
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            msg = "enterprise_customer_uuid parameter is required."
            logger.error(msg)
            return Response(msg, status=status.HTTP_400_BAD_REQUEST)

        checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
        origin_url = request.META.get("HTTP_ORIGIN")

        if not checkout_intent:
            msg = f"No checkout intent for id, for enterprise_uuid: {enterprise_uuid}"
            logger.error(f"No checkout intent for id, for enterprise_uuid: {enterprise_uuid}")
            return Response(msg, status=status.HTTP_404_NOT_FOUND)

        stripe_customer_id = checkout_intent.stripe_customer_id
        enterprise_slug = checkout_intent.enterprise_slug

        if not (stripe_customer_id or enterprise_slug):
            msg = f"No stripe customer id or enterprise slug associated to enterprise_uuid:{enterprise_uuid}"
            logger.error(msg)
            return Response(msg, status=status.HTTP_404_NOT_FOUND)

        try:
            customer_portal_session = stripe.billing_portal.Session.create(
                customer=stripe_customer_id,
                return_url=f"{origin_url}/{enterprise_slug}",
            )
        except stripe.StripeError as e:
            # TODO: Long term we should be explicit to different types of Stripe error exceptions available
            # https://docs.stripe.com/api/errors/handling, https://docs.stripe.com/error-handling
            msg = f"StripeError creating billing portal session for CheckoutIntent {checkout_intent}: {e}"
            logger.exception(msg)
            return Response(msg, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except Exception as e:  # pylint: disable=broad-except
            msg = f"General exception creating billing portal session for CheckoutIntent {checkout_intent}: {e}"
            logger.exception(msg)
            return Response(msg, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        # TODO: pull out session fields actually needed, and structure a response.
        return Response(
            customer_portal_session,
            status=status.HTTP_200_OK,
            content_type='application/json',
        )

    @extend_schema(
        tags=[CUSTOMER_BILLING_API_TAG],
        summary='Create a new Customer Portal Session from the enterprise checkout MFE.',
    )
    @action(
        detail=True,
        methods=['get'],
        url_path='create-checkout-portal-session',
    )
    def create_checkout_portal_session(self, request, pk=None):
        """
        Create a new Customer Portal Session for the enterprise checkout MFE.  Response dict contains "url" key
        that should be attached to a button that the customer clicks.

        Response structure defined here: https://docs.stripe.com/api/customer_portal/sessions/create
        """
        origin_url = request.META.get("HTTP_ORIGIN")

        # Try UUID lookup first, then fall back to id lookup
        try:
            uuid_value = uuid.UUID(pk)
            checkout_intent = CheckoutIntent.objects.filter(uuid=uuid_value).first()
        except (ValueError, TypeError):
            # Fall back to id lookup
            try:
                int_value = int(pk)
                checkout_intent = CheckoutIntent.objects.filter(pk=int_value).first()
            except (ValueError, TypeError):
                return Response(
                    'Invalid lookup value: must be either a valid UUID or integer ID',
                    status=status.HTTP_400_BAD_REQUEST
                )

        if not checkout_intent:
            msg = f"No checkout intent for id, for requesting user {request.user.id}"
            logger.error(msg)
            return Response(msg, status=status.HTTP_404_NOT_FOUND)

        stripe_customer_id = checkout_intent.stripe_customer_id
        if not stripe_customer_id:
            msg = f"No stripe customer id associated to CheckoutIntent {checkout_intent}"
            logger.error(msg)
            return Response(msg, status=status.HTTP_404_NOT_FOUND)

        if not checkout_intent:
            msg = f"No checkout intent for id {pk}"
            logger.error(f"No checkout intent for id {pk}")
            return Response(msg, status=status.HTTP_404_NOT_FOUND)

        stripe_customer_id = checkout_intent.stripe_customer_id
        enterprise_slug = checkout_intent.enterprise_slug

        if not (stripe_customer_id or enterprise_slug):
            msg = f"No stripe customer id or enterprise slug associated to checkout_intent_id:{pk}"
            logger.error(f"No stripe customer id or enterprise slug associated to checkout_intent_id:{pk}")
            return Response(msg, status=status.HTTP_404_NOT_FOUND)

        try:
            customer_portal_session = stripe.billing_portal.Session.create(
                customer=stripe_customer_id,
                return_url=f"{origin_url}/billing-details/success",
            )
        except stripe.StripeError as e:
            # TODO: Long term we should be explicit to different types of Stripe error exceptions available
            # https://docs.stripe.com/api/errors/handling, https://docs.stripe.com/error-handling
            msg = f"StripeError creating billing portal session for CheckoutIntent {checkout_intent}: {e}"
            logger.exception(msg)
            return Response(msg, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except Exception as e:  # pylint: disable=broad-except
            msg = f"General exception creating billing portal session for CheckoutIntent {checkout_intent}: {e}"
            logger.exception(msg)
            return Response(msg, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        # TODO: pull out session fields actually needed, and structure a response.
        return Response(
            customer_portal_session,
            status=status.HTTP_200_OK,
            content_type='application/json',
        )


@extend_schema_view(
    list=extend_schema(
        summary='List CheckoutIntents',
        description=(
            'Retrieve a list of CheckoutIntent records for the authenticated user. '
            'This endpoint returns only the CheckoutIntent records that belong to the '
            'currently authenticated user, unless the user is staff, in which case '
            '**all** records are returned.'
        ),
        responses={
            200: OpenApiResponse(
                response=serializers.CheckoutIntentReadOnlySerializer,
                description='Successful response with paginated results',
                examples=CHECKOUT_INTENT_EXAMPLES,
            ),
            **{k: v for k, v in ERROR_RESPONSES.items() if k in [401, 403, 429]},
        },
        tags=['Customer Billing'],
        operation_id='list_checkout_intents',
    ),
    retrieve=extend_schema(
        summary='Retrieve CheckoutIntent',
        description=(
            'Retrieve a specific CheckoutIntent by either ID or UUID. '
            'This endpoint is designed to support polling from the frontend to check '
            'the fulfillment state after a successful Stripe checkout. '
            'Users can only retrieve their own CheckoutIntent records. '
            'Supports lookup by either:\n'
            '- Integer ID (e.g., `/api/v1/checkout-intents/123/`)\n'
            '- UUID (e.g., `/api/v1/checkout-intents/550e8400-e29b-41d4-a716-446655440000/`)\n'
        ),
        responses={
            200: OpenApiResponse(
                response=serializers.CheckoutIntentReadOnlySerializer,
                description='Successful response',
                examples=CHECKOUT_INTENT_EXAMPLES,
            ),
            **ERROR_RESPONSES,
        },
        tags=['Customer Billing'],
        operation_id='retrieve_checkout_intent',
    ),
    partial_update=extend_schema(
        summary='Update CheckoutIntent State',
        description=(
            'Update the state of a CheckoutIntent. '
            'This endpoint is used to transition the CheckoutIntent through its lifecycle states. '
            'Only valid state transitions are allowed. '
            'Users can only update their own CheckoutIntent records. '
            'Supports lookup by either:\n'
            '- Integer ID (e.g., `/checkout-intents/123/`)\n'
            '- UUID (e.g., `/checkout-intents/550e8400-e29b-41d4-a716-446655440000/`)\n'
            '\n'
            '## Allowed State Transitions\n'
            '```\n'
            'created → paid\n'
            'created → errored_stripe_checkout\n'
            'paid → fulfilled\n'
            'paid → errored_provisioning\n'
            'errored_stripe_checkout → paid\n'
            'errored_provisioning → paid\n'
            '```\n'
            '## Integration Points\n'
            '- **Stripe Webhook**: Transitions from `created` to `paid` after successful payment\n'
            '- **Fulfillment**: Transitions from `paid` to `fulfilled` after provisioning\n'
            '- **Error Recovery**: Allows retry from error states back to `paid`\n\n'
        ),
        parameters=[
            OpenApiParameter(
                name='id',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                required=True,
                description='ID or UUID of the CheckoutIntent to update',
            ),
        ],
        request=serializers.CheckoutIntentUpdateRequestSerializer,
        examples=PATCH_REQUEST_EXAMPLES,
        responses={
            200: OpenApiResponse(
                response=serializers.CheckoutIntentReadOnlySerializer,
                description='Successfully updated',
                examples=CHECKOUT_INTENT_EXAMPLES,
            ),
            **ERROR_RESPONSES,
        },
        tags=['Customer Billing'],
        operation_id='update_checkout_intent',
    ),
)
class CheckoutIntentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for CheckoutIntent model.

    Provides list, retrieve, and partial_update actions for CheckoutIntent records.
    Users can only access their own CheckoutIntent records, unless the user is staff,
    in which case all records can be accessed.

    This ViewSet intentionally does not utilize edx-rbac for permission checking,
    because most use cases involve requesting users who are not yet expected
    to have been granted any enterprise roles. Instead, we manage authorization
    via the ``get_queryset()`` method.

    Supports lookup by either 'id' (integer) or 'uuid' (UUID).
    """
    authentication_classes = (JwtAuthentication,)
    permission_classes = (permissions.IsAuthenticated,)
    lookup_field = 'id'

    # Only allow GET and PATCH operations
    http_method_names = ['get', 'patch', 'post', 'head', 'options']

    def get_serializer_class(self):
        """
        Use different serializers for different actions.
        """
        if self.action in ['partial_update', 'update']:
            return serializers.CheckoutIntentUpdateRequestSerializer
        elif self.action in ['create']:
            return serializers.CheckoutIntentCreateRequestSerializer
        return serializers.CheckoutIntentReadOnlySerializer

    def get_queryset(self):
        """
        Filter queryset to only include CheckoutIntent records
        belonging to the authenticated user, unless the requesting user
        has permission to read and write *all* CheckoutIntent records.
        """
        user = self.request.user
        base_queryset = CheckoutIntent.objects.filter(user=user)
        if user.is_staff:
            base_queryset = CheckoutIntent.objects.all()
        return base_queryset.select_related('user')

    def get_object(self):
        """
        Override get_object to support lookup by either id or uuid.

        Attempts to parse the lookup value as UUID first, then falls back to integer id.
        This allows clients to use either field for retrieving CheckoutIntent objects.
        """
        queryset = self.filter_queryset(self.get_queryset())
        lookup_value = self.kwargs[self.lookup_url_kwarg or self.lookup_field]

        try:
            uuid_value = uuid.UUID(lookup_value)
            filter_kwargs = {'uuid': uuid_value}
        except (ValueError, TypeError):
            try:
                int_value = int(lookup_value)
                filter_kwargs = {'id': int_value}
            except (ValueError, TypeError) as exc:
                raise exceptions.ValidationError(
                    'Lookup value must be either a valid UUID or integer ID'
                ) from exc

        try:
            obj = queryset.get(**filter_kwargs)
        except CheckoutIntent.DoesNotExist as exc:
            raise exceptions.NotFound('CheckoutIntent not found') from exc

        self.check_object_permissions(self.request, obj)
        return obj


def stripe_event_summary_permission_detail_fn(request, *args, **kwargs):
    """
    Helper to use with @permission_required on retrieve endpoint.

    Args:
        uuid (str): UUID representing an SubscriptionPlan object.
    """
    if not (subs_plan_uuid := request.query_params.get('subscription_plan_uuid')):
        raise exceptions.ValidationError(detail='subscription_plan_uuid query param is required')

    summary = StripeEventSummary.objects.filter(
        subscription_plan_uuid=subs_plan_uuid,
    ).select_related(
        'checkout_intent',
    ).first()
    if not (summary and summary.checkout_intent):
        return None
    return summary.checkout_intent.enterprise_uuid


class StripeEventSummaryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    ViewSet for StripeEventSummary model.

    Provides retrieve action for StripeEventSummary records.
    """
    authentication_classes = (JwtAuthentication,)
    permission_classes = (permissions.IsAuthenticated,)

    def get_serializer_class(self):
        """
        Return read only serializer.
        """
        return serializers.StripeEventSummaryReadOnlySerializer

    def get_queryset(self):
        """
        Either return full queryset, or filter by all objects associated with
        a subscription_plan_uuid
        """
        subscription_plan_uuid = self.request.query_params.get('subscription_plan_uuid')
        if not subscription_plan_uuid:
            raise exceptions.ValidationError(detail='subscription_plan_uuid query param is required')
        return StripeEventSummary.objects.filter(
            subscription_plan_uuid=subscription_plan_uuid,
        ).select_related(
            'checkout_intent',
        )

    @extend_schema(
        tags=[STRIPE_EVENT_API_TAG],
        summary='Retrieves stripe event summaries.',
        responses={
            status.HTTP_200_OK: serializers.StripeEventSummaryReadOnlySerializer,
            status.HTTP_403_FORBIDDEN: None,
        },
    )
    @permission_required(
        STRIPE_EVENT_SUMMARY_READ_PERMISSION,
        fn=stripe_event_summary_permission_detail_fn,
    )
    def list(self, request, *args, **kwargs):
        """
        Lists ``StripeEventSummary`` records, filtered by given subscription plan uuid.
        """
        return super().list(request, *args, **kwargs)

    @action(
        detail=False,
        methods=['get'],
        url_path='first-invoice-upcoming-amount-due',
    )
    def first_upcoming_invoice_amount_due(self, request, *args, **kwargs):
        """
        Deprecated first-invoice-upcoming-amount-due endpoint.

        Temporary passthrough to aid with transitioning to get-stripe-subscription-plan-info.
        """
        return self.get_stripe_subscription_plan_info(request, *args, **kwargs)

    @action(
        detail=False,
        methods=['get'],
        url_path='get-stripe-subscription-plan-info',
    )
    def get_stripe_subscription_plan_info(self, request, *args, **kwargs):
        """
        Given a license-manager SubscriptionPlan uuid, returns information needed for the
        Subscription management page on admin portal, like the upcoming subscription price
        and if the subscription has been cancelled
        """
        subscription_plan_uuid = self.request.query_params.get('subscription_plan_uuid')
        if not subscription_plan_uuid:
            raise exceptions.ValidationError(detail='subscription_plan_uuid query param is required')
        created_event_summary = StripeEventSummary.objects.filter(
            event_type='customer.subscription.created',
            subscription_plan_uuid=subscription_plan_uuid,
        ).order_by('-stripe_event_created_at').first()
        updated_event_summary = StripeEventSummary.objects.filter(
            event_type='customer.subscription.updated',
            subscription_plan_uuid=subscription_plan_uuid,
        ).order_by('-stripe_event_created_at').first()

        canceled_date, currency, upcoming_invoice_amount_due = None, None, None

        if updated_event_summary:
            canceled_date = updated_event_summary.subscription_cancel_at

        if created_event_summary:
            currency = created_event_summary.currency
            upcoming_invoice_amount_due = created_event_summary.upcoming_invoice_amount_due

        response_serializer = serializers.StripeSubscriptionPlanInfoResponseSerializer(
            data={
                'upcoming_invoice_amount_due': upcoming_invoice_amount_due,
                'currency': currency,
                'canceled_date': canceled_date,
            },
        )
        if not subscription_plan_uuid:
            raise exceptions.NotFound("No associated subscription plan uuid was found")
        if not (updated_event_summary or created_event_summary):
            raise exceptions.NotFound("No Stripe subscription data found for this plan")
        response_serializer.is_valid(raise_exception=True)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


BILLING_MANAGEMENT_API_TAG = 'Billing Management'


class BillingManagementViewSet(viewsets.ViewSet):
    """
    Viewset supporting operations for the Billing Management API.
    This is a new API for managing billing and subscription information.
    """
    authentication_classes = (JwtAuthentication,)
    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='Placeholder endpoint for billing management API.',
        description='This endpoint serves as a placeholder for the billing management API.',
    )
    @action(
        detail=False,
        methods=['get'],
        url_path='health-check',
    )
    def health_check(self, request):
        """
        Health check endpoint for the billing management API.
        """
        return Response(
            {'status': 'healthy'},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='Get customer billing address',
        description='Retrieve the billing address for a Stripe customer associated with an enterprise.',
        parameters=[
            OpenApiParameter(
                name='enterprise_customer_uuid',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the enterprise customer',
            ),
        ],
        responses={
            status.HTTP_200_OK: serializers.BillingAddressResponseSerializer,
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Missing or invalid enterprise_customer_uuid'),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description='Permission denied'),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description='Enterprise customer or Stripe customer not found'),
            status.HTTP_422_UNPROCESSABLE_ENTITY: OpenApiResponse(description='Stripe API call failed'),
        },
    )
    @action(
        detail=False,
        methods=['get'],
        url_path='address',
    )
    @permission_required(
        BILLING_MANAGEMENT_ACCESS_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid')
    )
    def get_address(self, request, **kwargs):
        """
        Retrieve the billing address for a Stripe customer.

        The enterprise_customer_uuid query parameter is used to:
        1. Find the CheckoutIntent for the enterprise
        2. Extract the Stripe customer ID from the CheckoutIntent
        3. Retrieve the customer's billing address from Stripe
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            return Response(
                {'error': 'enterprise_customer_uuid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find the CheckoutIntent for this enterprise
        try:
            checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
            if not checkout_intent or not checkout_intent.stripe_customer_id:
                logger.warning(
                    f'No checkout intent with stripe customer ID found for enterprise_uuid: {enterprise_uuid}'
                )
                return Response(
                    {'error': 'Stripe customer not found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Get the Stripe customer
            stripe_customer = get_stripe_customer(checkout_intent.stripe_customer_id)
            if not stripe_customer:
                return Response(
                    {'error': 'Stripe customer not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Extract address fields from Stripe customer object
            address_data = {
                'name': stripe_customer.get('name'),
                'email': stripe_customer.get('email'),
                'phone': stripe_customer.get('phone'),
            }

            # Extract address from Stripe's address object if it exists
            if stripe_customer.get('address'):
                address = stripe_customer['address']
                address_data.update({
                    'country': address.get('country'),
                    'address_line_1': address.get('line1'),
                    'address_line_2': address.get('line2'),
                    'city': address.get('city'),
                    'state': address.get('state'),
                    'postal_code': address.get('postal_code'),
                })

            # Serialize and return the response
            response_serializer = serializers.BillingAddressResponseSerializer(data=address_data)
            if not response_serializer.is_valid():
                logger.error(f'Failed to serialize billing address: {response_serializer.errors}')
                return Response(
                    {'error': 'Failed to process billing address'},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY
                )

            return Response(response_serializer.data, status=status.HTTP_200_OK)

        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error retrieving customer {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': f'Stripe API error: {str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f'Unexpected error retrieving billing address for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': 'An unexpected error occurred'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='Update customer billing address',
        description='Update the billing address for a Stripe customer associated with an enterprise.',
        parameters=[
            OpenApiParameter(
                name='enterprise_customer_uuid',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the enterprise customer',
            ),
        ],
        request=serializers.BillingAddressUpdateRequestSerializer,
        responses={
            status.HTTP_200_OK: serializers.BillingAddressResponseSerializer,
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Missing UUID or invalid request data'),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description='Permission denied'),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description='Enterprise customer or Stripe customer not found'),
            status.HTTP_422_UNPROCESSABLE_ENTITY: OpenApiResponse(description='Stripe API call failed'),
        },
    )
    @action(
        detail=False,
        methods=['post'],
        url_path='address',
    )
    @permission_required(
        BILLING_MANAGEMENT_ACCESS_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid')
    )
    def update_address(self, request, **kwargs):
        """
        Update the billing address for a Stripe customer.

        The enterprise_customer_uuid query parameter is used to:
        1. Find the CheckoutIntent for the enterprise
        2. Extract the Stripe customer ID from the CheckoutIntent
        3. Update the customer's billing address in Stripe
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            return Response(
                {'error': 'enterprise_customer_uuid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate request data
        request_serializer = serializers.BillingAddressUpdateRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response(request_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        validated_data = request_serializer.validated_data

        try:
            # Find the CheckoutIntent for this enterprise
            checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
            if not checkout_intent or not checkout_intent.stripe_customer_id:
                logger.warning(
                    f'No checkout intent with stripe customer ID found for enterprise_uuid: {enterprise_uuid}'
                )
                return Response(
                    {'error': 'Stripe customer not found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Update the Stripe customer with the new address information
            stripe_customer = stripe.Customer.modify(
                checkout_intent.stripe_customer_id,
                name=validated_data.get('name'),
                email=validated_data.get('email'),
                phone=validated_data.get('phone'),
                address={
                    'line1': validated_data.get('address_line_1'),
                    'line2': validated_data.get('address_line_2', ''),
                    'city': validated_data.get('city'),
                    'state': validated_data.get('state'),
                    'postal_code': validated_data.get('postal_code'),
                    'country': validated_data.get('country'),
                },
            )

            # Extract address fields from updated Stripe customer object
            address_data = {
                'name': stripe_customer.get('name'),
                'email': stripe_customer.get('email'),
                'phone': stripe_customer.get('phone'),
            }

            # Extract address from Stripe's address object if it exists
            if stripe_customer.get('address'):
                address = stripe_customer['address']
                address_data.update({
                    'country': address.get('country'),
                    'address_line_1': address.get('line1'),
                    'address_line_2': address.get('line2'),
                    'city': address.get('city'),
                    'state': address.get('state'),
                    'postal_code': address.get('postal_code'),
                })

            # Serialize and return the response
            response_serializer = serializers.BillingAddressResponseSerializer(data=address_data)
            if not response_serializer.is_valid():
                logger.error(f'Failed to serialize updated billing address: {response_serializer.errors}')
                return Response(
                    {'error': 'Failed to process updated billing address'},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY
                )

            return Response(response_serializer.data, status=status.HTTP_200_OK)

        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error updating customer {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': f'Stripe API error: {str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f'Unexpected error updating billing address for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': 'An unexpected error occurred'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='List payment methods',
        description='Retrieve all saved payment methods for a Stripe customer associated with an enterprise.',
        parameters=[
            OpenApiParameter(
                name='enterprise_customer_uuid',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the enterprise customer',
            ),
        ],
        responses={
            status.HTTP_200_OK: serializers.PaymentMethodsListResponseSerializer,
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Missing or invalid enterprise_customer_uuid'),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description='Permission denied'),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description='Enterprise customer or Stripe customer not found'),
            status.HTTP_422_UNPROCESSABLE_ENTITY: OpenApiResponse(description='Stripe API call failed'),
        },
    )
    @action(
        detail=False,
        methods=['get'],
        url_path='payment-methods',
    )
    @permission_required(
        BILLING_MANAGEMENT_ACCESS_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid')
    )
    def list_payment_methods(self, request, **kwargs):
        """
        List all payment methods for a Stripe customer.

        The enterprise_customer_uuid query parameter is used to:
        1. Find the CheckoutIntent for the enterprise
        2. Extract the Stripe customer ID from the CheckoutIntent
        3. Retrieve the customer's payment methods from Stripe
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            return Response(
                {'error': 'enterprise_customer_uuid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Find the CheckoutIntent for this enterprise
            checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
            if not checkout_intent or not checkout_intent.stripe_customer_id:
                logger.warning(
                    f'No checkout intent with stripe customer ID found for enterprise_uuid: {enterprise_uuid}'
                )
                return Response(
                    {'error': 'Stripe customer not found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Get the Stripe customer to find the default payment method
            stripe_customer = stripe.Customer.retrieve(checkout_intent.stripe_customer_id)
            default_payment_method_id = stripe_customer.get('invoice_settings', {}).get('default_payment_method')

            # List all payment methods for the customer
            payment_methods_response = stripe.PaymentMethod.list(
                customer=checkout_intent.stripe_customer_id,
                type='card',  # Start with cards, could expand to support other types
                limit=100,  # Reasonable limit for pagination
            )

            # Transform payment methods into response format
            payment_methods = []
            for pm in payment_methods_response.data:
                payment_method_data = {
                    'id': pm.get('id'),
                    'type': pm.get('type'),
                    'is_default': pm.get('id') == default_payment_method_id,
                }

                # Add card-specific fields if available
                if pm.get('card'):
                    card = pm['card']
                    payment_method_data.update({
                        'last4': card.get('last4'),
                        'brand': card.get('brand'),
                        'exp_month': card.get('exp_month'),
                        'exp_year': card.get('exp_year'),
                    })
                # Add bank account specific fields if applicable
                elif pm.get('us_bank_account'):
                    bank_account = pm['us_bank_account']
                    payment_method_data['last4'] = bank_account.get('last4')

                payment_methods.append(payment_method_data)

            # Serialize and return the response
            response_data = {'payment_methods': payment_methods}
            response_serializer = serializers.PaymentMethodsListResponseSerializer(data=response_data)
            if not response_serializer.is_valid():
                logger.error(f'Failed to serialize payment methods: {response_serializer.errors}')
                return Response(
                    {'error': 'Failed to process payment methods'},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY
                )

            return Response(response_serializer.data, status=status.HTTP_200_OK)

        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error retrieving payment methods for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': f'Stripe API error: {str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f'Unexpected error retrieving payment methods for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': 'An unexpected error occurred'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='Set default payment method',
        description='Set a payment method as the default for a Stripe customer associated with an enterprise.',
        parameters=[
            OpenApiParameter(
                name='enterprise_customer_uuid',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the enterprise customer',
            ),
            OpenApiParameter(
                name='id',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                required=True,
                description='ID of the payment method to set as default',
            ),
        ],
        request=serializers.SetDefaultPaymentMethodRequestSerializer,
        responses={
            status.HTTP_200_OK: OpenApiResponse(description='Payment method set as default successfully'),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Missing or invalid enterprise_customer_uuid'),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description='Permission denied'),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description='Enterprise customer, Stripe customer, or payment method not found'),
            status.HTTP_422_UNPROCESSABLE_ENTITY: OpenApiResponse(description='Stripe API call failed'),
        },
    )
    @action(
        detail=False,
        methods=['post'],
        url_path='payment-methods/(?P<payment_method_id>[^/]+)/set-default',
    )
    @permission_required(
        BILLING_MANAGEMENT_ACCESS_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid')
    )
    def set_default_payment_method(self, request, payment_method_id=None, **kwargs):
        """
        Set a payment method as the default for a Stripe customer.

        The enterprise_customer_uuid query parameter is used to:
        1. Find the CheckoutIntent for the enterprise
        2. Extract the Stripe customer ID from the CheckoutIntent
        3. Verify the payment method belongs to this customer
        4. Set it as the default payment method
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            return Response(
                {'error': 'enterprise_customer_uuid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not payment_method_id:
            return Response(
                {'error': 'payment_method_id is required in the URL path'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Find the CheckoutIntent for this enterprise
            checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
            if not checkout_intent or not checkout_intent.stripe_customer_id:
                logger.warning(
                    f'No checkout intent with stripe customer ID found for enterprise_uuid: {enterprise_uuid}'
                )
                return Response(
                    {'error': 'Stripe customer not found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            stripe_customer_id = checkout_intent.stripe_customer_id

            # Verify the payment method exists and belongs to this customer
            payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
            if not payment_method or payment_method.get('customer') != stripe_customer_id:
                logger.warning(
                    f'Payment method {payment_method_id} does not belong to customer {stripe_customer_id}'
                )
                return Response(
                    {'error': 'Payment method not found or does not belong to this customer'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Set the payment method as the default
            stripe.Customer.modify(
                stripe_customer_id,
                invoice_settings={'default_payment_method': payment_method_id},
            )

            return Response(
                {'message': 'Payment method set as default successfully'},
                status=status.HTTP_200_OK
            )

        except stripe.error.InvalidRequestError as e:
            logger.warning(f'Invalid Stripe request for payment method {payment_method_id}: {str(e)}')
            return Response(
                {'error': 'Payment method not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error setting default payment method for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': f'Stripe API error: {str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f'Unexpected error setting default payment method for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': 'An unexpected error occurred'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='Delete payment method',
        description='Remove a payment method from a Stripe customer associated with an enterprise.',
        parameters=[
            OpenApiParameter(
                name='enterprise_customer_uuid',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the enterprise customer',
            ),
            OpenApiParameter(
                name='id',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                required=True,
                description='ID of the payment method to delete',
            ),
        ],
        responses={
            status.HTTP_200_OK: OpenApiResponse(description='Payment method deleted successfully'),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Missing or invalid enterprise_customer_uuid'),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description='Permission denied'),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description='Enterprise customer, Stripe customer, or payment method not found'),
            status.HTTP_409_CONFLICT: OpenApiResponse(description='Cannot delete only payment method or must change default first'),
            status.HTTP_422_UNPROCESSABLE_ENTITY: OpenApiResponse(description='Stripe API call failed'),
        },
    )
    @action(
        detail=False,
        methods=['delete'],
        url_path='payment-methods/(?P<payment_method_id>[^/]+)',
    )
    @permission_required(
        BILLING_MANAGEMENT_ACCESS_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid')
    )
    def delete_payment_method(self, request, payment_method_id=None, **kwargs):
        """
        Delete a payment method from a Stripe customer.

        The enterprise_customer_uuid query parameter is used to:
        1. Find the CheckoutIntent for the enterprise
        2. Extract the Stripe customer ID from the CheckoutIntent
        3. Verify the payment method belongs to this customer
        4. Check constraints (only payment method, or is default with others)
        5. Detach the payment method from the customer
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            return Response(
                {'error': 'enterprise_customer_uuid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not payment_method_id:
            return Response(
                {'error': 'payment_method_id is required in the URL path'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Find the CheckoutIntent for this enterprise
            checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
            if not checkout_intent or not checkout_intent.stripe_customer_id:
                logger.warning(
                    f'No checkout intent with stripe customer ID found for enterprise_uuid: {enterprise_uuid}'
                )
                return Response(
                    {'error': 'Stripe customer not found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            stripe_customer_id = checkout_intent.stripe_customer_id

            # Verify the payment method exists and belongs to this customer
            payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
            if not payment_method or payment_method.get('customer') != stripe_customer_id:
                logger.warning(
                    f'Payment method {payment_method_id} does not belong to customer {stripe_customer_id}'
                )
                return Response(
                    {'error': 'Payment method not found or does not belong to this customer'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Get the customer to check payment method count and default
            stripe_customer = stripe.Customer.retrieve(stripe_customer_id)
            default_payment_method_id = stripe_customer.get('invoice_settings', {}).get('default_payment_method')

            # List all payment methods for the customer
            payment_methods_response = stripe.PaymentMethod.list(
                customer=stripe_customer_id,
                type='card',
                limit=100,
            )

            # Count total payment methods (including other types if they exist)
            # For now, we check only card type, but in future might need to include other types
            total_payment_methods = len(payment_methods_response.data)

            # Check if this is the only payment method
            if total_payment_methods <= 1:
                return Response(
                    {'error': 'Cannot delete the only payment method on the account'},
                    status=status.HTTP_409_CONFLICT
                )

            # Check if this is the default and others exist
            if payment_method_id == default_payment_method_id:
                return Response(
                    {'error': 'Set another method as default before deleting this one'},
                    status=status.HTTP_409_CONFLICT
                )

            # Detach the payment method from the customer
            stripe.PaymentMethod.detach(payment_method_id)

            return Response(
                {'message': 'Payment method deleted successfully'},
                status=status.HTTP_200_OK
            )

        except stripe.error.InvalidRequestError as e:
            logger.warning(f'Invalid Stripe request for payment method {payment_method_id}: {str(e)}')
            return Response(
                {'error': 'Payment method not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error deleting payment method {payment_method_id} for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': f'Stripe API error: {str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f'Unexpected error deleting payment method for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': 'An unexpected error occurred'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='List transactions',
        description='Retrieve paginated invoice/transaction history for a Stripe customer associated with an enterprise.',
        parameters=[
            OpenApiParameter(
                name='enterprise_customer_uuid',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the enterprise customer',
            ),
            OpenApiParameter(
                name='page_token',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Stripe pagination cursor for continuing a paginated list',
            ),
            OpenApiParameter(
                name='limit',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Number of transactions to return per page (default 10, max 25)',
            ),
        ],
        responses={
            status.HTTP_200_OK: serializers.TransactionsListResponseSerializer,
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Missing or invalid enterprise_customer_uuid or parameters'),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description='Permission denied'),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description='Enterprise customer or Stripe customer not found'),
            status.HTTP_422_UNPROCESSABLE_ENTITY: OpenApiResponse(description='Stripe API call failed'),
        },
    )
    @action(detail=False, methods=['get'], url_path='transactions')
    @permission_required(
        BILLING_MANAGEMENT_ACCESS_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid')
    )
    def list_transactions(self, request, **kwargs):
        """
        List transactions/invoices for a Stripe customer.

        The enterprise_customer_uuid query parameter is used to:
        1. Find the CheckoutIntent for the enterprise
        2. Extract the Stripe customer ID from the CheckoutIntent
        3. Retrieve paginated invoices from Stripe
        4. Fetch charge information for receipt URLs
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            return Response(
                {'error': 'enterprise_customer_uuid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get and validate pagination parameters
        page_token = request.query_params.get('starting_after')
        try:
            limit = int(request.query_params.get('limit', 10))
            if limit < 1 or limit > 25:
                limit = 10
        except (ValueError, TypeError):
            limit = 10

        try:
            # Find the CheckoutIntent for this enterprise
            checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
            if not checkout_intent or not checkout_intent.stripe_customer_id:
                logger.warning(
                    f'No checkout intent with stripe customer ID found for enterprise_uuid: {enterprise_uuid}'
                )
                return Response(
                    {'error': 'Stripe customer not found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            stripe_customer_id = checkout_intent.stripe_customer_id

            # Retrieve invoices from Stripe with pagination
            invoices_response = stripe.Invoice.list(
                customer=stripe_customer_id,
                limit=limit,
                starting_after=page_token,
            )

            # Transform invoices to transaction format
            transactions = []
            for invoice in invoices_response.data:
                transaction = {
                    'id': invoice.get('id'),
                    'date': invoice.get('created'),  # Unix timestamp, will be converted by serializer
                    'amount': invoice.get('amount_paid'),  # Amount in cents
                    'currency': invoice.get('currency', 'usd').lower(),
                    'status': self._normalize_invoice_status(invoice.get('status')),
                    'description': invoice.get('description'),
                    'invoice_pdf_url': invoice.get('hosted_invoice_url'),
                }

                # Attempt to get receipt_url from associated charge
                try:
                    if invoice.get('charge'):
                        charge = stripe.Charge.retrieve(invoice.get('charge'))
                        transaction['receipt_url'] = charge.get('receipt_url')
                except stripe.error.StripeError as e:
                    logger.warning(f'Could not retrieve charge {invoice.get("charge")} for receipt URL: {str(e)}')
                    transaction['receipt_url'] = None

                transactions.append(transaction)

            # Determine if there are more results
            next_page_token = None
            if invoices_response.get('has_more'):
                # Get the last invoice's ID for pagination
                if transactions:
                    next_page_token = transactions[-1]['id']

            # Serialize and return response
            response_data = {
                'transactions': transactions,
                'next_page_token': next_page_token,
            }

            serializer = serializers.TransactionsListResponseSerializer(response_data)
            return Response(serializer.data, status=status.HTTP_200_OK)

        except stripe.error.InvalidRequestError as e:
            logger.warning(f'Invalid Stripe request for customer {stripe_customer_id}: {str(e)}')
            return Response(
                {'error': 'Invalid request to Stripe API'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error retrieving transactions for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': f'Stripe API error: {str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f'Unexpected error retrieving transactions for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': 'An unexpected error occurred'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @staticmethod
    def _normalize_invoice_status(stripe_status):
        """
        Normalize Stripe invoice status to canonical form.

        Stripe returns statuses like 'draft', 'open', 'paid', 'uncollectible', 'void'.
        We normalize to: 'paid', 'open', 'void', 'uncollectible'
        """
        if stripe_status in ['paid']:
            return 'paid'
        elif stripe_status in ['draft', 'open']:
            return 'open'
        elif stripe_status in ['void']:
            return 'void'
        elif stripe_status in ['uncollectible']:
            return 'uncollectible'
        return 'open'  # Default to open

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='Get subscription status',
        description='Retrieve the current subscription status and plan type for a Stripe customer associated with an enterprise.',
        parameters=[
            OpenApiParameter(
                name='enterprise_customer_uuid',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the enterprise customer',
            ),
        ],
        responses={
            status.HTTP_200_OK: serializers.SubscriptionResponseSerializer,
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Missing or invalid enterprise_customer_uuid'),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description='Permission denied'),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description='Enterprise customer or Stripe customer not found'),
            status.HTTP_422_UNPROCESSABLE_ENTITY: OpenApiResponse(description='Stripe API call failed'),
        },
    )
    @action(detail=False, methods=['get'], url_path='subscription')
    @permission_required(
        BILLING_MANAGEMENT_ACCESS_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid')
    )
    def get_subscription(self, request, **kwargs):
        """
        Get subscription status and plan type for a Stripe customer.

        Returns null subscription if no active subscription exists.
        The plan_type is derived from Stripe product/price metadata using the 'plan_type' key.
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            return Response(
                {'error': 'enterprise_customer_uuid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Find the CheckoutIntent for this enterprise
            checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
            if not checkout_intent or not checkout_intent.stripe_customer_id:
                logger.warning(
                    f'No checkout intent with stripe customer ID found for enterprise_uuid: {enterprise_uuid}'
                )
                return Response(
                    {'error': 'Stripe customer not found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            stripe_customer_id = checkout_intent.stripe_customer_id

            # Retrieve subscriptions for the customer
            subscriptions_response = stripe.Subscription.list(
                customer=stripe_customer_id,
                limit=1,  # Only get the most recent subscription
                status='active',
            )

            # If no active subscription, return null
            if not subscriptions_response.data:
                serializer = serializers.SubscriptionResponseSerializer(None)
                return Response(serializer.data, status=status.HTTP_200_OK)

            subscription = subscriptions_response.data[0]

            # Extract subscription details
            sub_data = {
                'id': subscription.get('id'),
                'status': subscription.get('status'),
                'plan_type': self._get_plan_type_from_subscription(subscription),
                'cancel_at_period_end': subscription.get('cancel_at_period_end', False),
                'current_period_end': subscription.get('current_period_end'),
                'yearly_amount': self._get_yearly_amount(subscription),
                'license_count': self._get_license_count(subscription),
            }

            serializer = serializers.SubscriptionResponseSerializer(sub_data)
            return Response(serializer.data, status=status.HTTP_200_OK)

        except stripe.error.InvalidRequestError as e:
            logger.warning(f'Invalid Stripe request for customer {stripe_customer_id}: {str(e)}')
            return Response(
                {'error': 'Invalid request to Stripe API'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error retrieving subscription for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': f'Stripe API error: {str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f'Unexpected error retrieving subscription for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': 'An unexpected error occurred'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    @staticmethod
    def _get_plan_type_from_subscription(subscription):
        """
        Derive plan_type from Stripe subscription's price/product metadata.

        Looks for 'plan_type' key in price metadata first, then product metadata.
        Returns 'Other' if no matching metadata is found.

        Expected plan_type values: 'Teams', 'Essentials', 'LearnerCredit', 'Other'
        """
        try:
            # Get the price object from the subscription
            items = subscription.get('items', {}).get('data', [])
            if not items:
                return 'Other'

            price_id = items[0].get('price', {}).get('id')
            if not price_id:
                return 'Other'

            # Retrieve the price to access metadata
            price = stripe.Price.retrieve(price_id)
            
            # Check price metadata first
            price_metadata = price.get('metadata', {})
            if 'plan_type' in price_metadata:
                return price_metadata.get('plan_type', 'Other')

            # Check product metadata
            product_id = price.get('product')
            if product_id:
                product = stripe.Product.retrieve(product_id)
                product_metadata = product.get('metadata', {})
                if 'plan_type' in product_metadata:
                    return product_metadata.get('plan_type', 'Other')

            return 'Other'

        except stripe.error.StripeError as e:
            logger.warning(f'Could not retrieve plan type from Stripe metadata: {str(e)}')
            return 'Other'
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Unexpected error deriving plan_type: {str(e)}')
            return 'Other'

    @staticmethod
    def _get_yearly_amount(subscription):
        """
        Calculate yearly amount from subscription items.

        Returns the total yearly recurring revenue for the subscription.
        """
        try:
            items = subscription.get('items', {}).get('data', [])
            if not items:
                return 0

            total_amount = 0
            for item in items:
                price = item.get('price', {})
                quantity = item.get('quantity', 1)
                
                # Get the unit amount
                unit_amount = price.get('unit_amount', 0)
                
                # Calculate yearly amount based on billing period
                billing_period = price.get('recurring', {}).get('interval')
                if billing_period == 'year':
                    total_amount += unit_amount * quantity
                elif billing_period == 'month':
                    total_amount += (unit_amount * 12) * quantity
                elif billing_period == 'week':
                    total_amount += (unit_amount * 52) * quantity

            return total_amount

        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Could not calculate yearly amount: {str(e)}')
            return 0

    @staticmethod
    def _get_license_count(subscription):
        """
        Get license count from subscription items.

        Uses the quantity from subscription items as license count.
        Returns the sum of quantities across all items.
        """
        try:
            items = subscription.get('items', {}).get('data', [])
            if not items:
                return 0

            total_licenses = 0
            for item in items:
                quantity = item.get('quantity', 0)
                total_licenses += quantity

            return total_licenses

        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Could not get license count: {str(e)}')
            return 0

    @extend_schema(
        tags=[BILLING_MANAGEMENT_API_TAG],
        summary='Cancel subscription',
        description='Request cancellation of a subscription at the end of the current billing period. Only available for Teams and Essentials plans.',
        parameters=[
            OpenApiParameter(
                name='enterprise_customer_uuid',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the enterprise customer',
            ),
        ],
        responses={
            status.HTTP_200_OK: serializers.CancelSubscriptionResponseSerializer,
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Missing or invalid enterprise_customer_uuid'),
            status.HTTP_403_FORBIDDEN: OpenApiResponse(description='Permission denied or plan type does not support cancellation'),
            status.HTTP_404_NOT_FOUND: OpenApiResponse(description='Enterprise customer, Stripe customer, or active subscription not found'),
            status.HTTP_409_CONFLICT: OpenApiResponse(description='Subscription is already scheduled for cancellation'),
            status.HTTP_422_UNPROCESSABLE_ENTITY: OpenApiResponse(description='Stripe API call failed'),
        },
    )
    @action(detail=False, methods=['post'], url_path='subscription/cancel')
    @permission_required(
        BILLING_MANAGEMENT_ACCESS_PERMISSION,
        fn=lambda request, **kwargs: request.GET.get('enterprise_customer_uuid')
    )
    def cancel_subscription(self, request, **kwargs):
        """
        Cancel a subscription at the end of the current billing period.

        Only Teams and Essentials plans can be cancelled. Returns 403 for other plan types.
        Returns 409 if the subscription is already scheduled for cancellation.
        """
        enterprise_uuid = request.query_params.get('enterprise_customer_uuid')
        if not enterprise_uuid:
            return Response(
                {'error': 'enterprise_customer_uuid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Find the CheckoutIntent for this enterprise
            checkout_intent = CheckoutIntent.objects.filter(enterprise_uuid=enterprise_uuid).first()
            if not checkout_intent or not checkout_intent.stripe_customer_id:
                logger.warning(
                    f'No checkout intent with stripe customer ID found for enterprise_uuid: {enterprise_uuid}'
                )
                return Response(
                    {'error': 'Stripe customer not found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            stripe_customer_id = checkout_intent.stripe_customer_id

            # Retrieve the active subscription
            subscriptions_response = stripe.Subscription.list(
                customer=stripe_customer_id,
                limit=1,
                status='active',
            )

            if not subscriptions_response.data:
                return Response(
                    {'error': 'No active subscription found for this enterprise'},
                    status=status.HTTP_404_NOT_FOUND
                )

            subscription = subscriptions_response.data[0]

            # Check if already cancelled
            if subscription.get('cancel_at_period_end', False):
                return Response(
                    {'error': 'Subscription is already scheduled for cancellation'},
                    status=status.HTTP_409_CONFLICT
                )

            # Get plan_type to verify cancellation eligibility
            plan_type = self._get_plan_type_from_subscription(subscription)
            if plan_type not in ['Teams', 'Essentials']:
                return Response(
                    {'error': 'Subscription cancellation is not available for your plan type'},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Cancel the subscription at period end
            updated_subscription = stripe.Subscription.modify(
                subscription.get('id'),
                cancel_at_period_end=True,
            )

            # Build response with updated subscription data
            sub_data = {
                'id': updated_subscription.get('id'),
                'status': updated_subscription.get('status'),
                'plan_type': self._get_plan_type_from_subscription(updated_subscription),
                'cancel_at_period_end': updated_subscription.get('cancel_at_period_end', False),
                'current_period_end': updated_subscription.get('current_period_end'),
            }

            serializer = serializers.CancelSubscriptionResponseSerializer(sub_data)
            return Response(serializer.data, status=status.HTTP_200_OK)

        except stripe.error.InvalidRequestError as e:
            logger.warning(f'Invalid Stripe request for customer {stripe_customer_id}: {str(e)}')
            return Response(
                {'error': 'Invalid request to Stripe API'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error cancelling subscription for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': f'Stripe API error: {str(e)}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f'Unexpected error cancelling subscription for {enterprise_uuid}: {str(e)}')
            return Response(
                {'error': 'An unexpected error occurred'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
