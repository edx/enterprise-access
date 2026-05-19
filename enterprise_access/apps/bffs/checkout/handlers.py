"""
Handlers for the Checkout BFF endpoints.
"""
import logging
from datetime import datetime
from typing import Dict

import stripe
from django.conf import settings
from pytz import UTC

from enterprise_access.apps.api_client.lms_client import LmsApiClient
from enterprise_access.apps.bffs.api import (
    get_and_cache_enterprise_customer_users,
    transform_enterprise_customer_users_data
)
from enterprise_access.apps.bffs.checkout.context import (
    CheckoutContext,
    CheckoutSuccessContext,
    CheckoutValidationContext
)
from enterprise_access.apps.bffs.checkout.serializers import CheckoutIntentModelSerializer
from enterprise_access.apps.bffs.handlers import BaseHandler
from enterprise_access.apps.customer_billing.api import validate_free_trial_checkout_session
from enterprise_access.apps.customer_billing.embargo import get_embargoed_countries
from enterprise_access.apps.customer_billing.models import CheckoutIntent, EnterpriseAcademy
from enterprise_access.apps.customer_billing.pricing_api import get_all_active_stripe_prices, get_ssp_product_pricing
from enterprise_access.apps.customer_billing.stripe_api import (
    get_stripe_checkout_session,
    get_stripe_customer,
    get_stripe_invoice,
    get_stripe_payment_intent,
    get_stripe_payment_method,
    get_stripe_subscription
)
from enterprise_access.utils import cents_to_dollars

logger = logging.getLogger(__name__)


class CheckoutIntentAwareHandlerMixin:
    """
    Mixin to help fetch CheckoutIntents for the requesting user.
    """
    def _get_checkout_intent(self) -> Dict | None:
        """
        Load checkout intent data (from database) for the given user.
        """
        checkout_intent_instance = CheckoutIntent.for_user(self.context.user)
        checkout_intent_data = None
        if checkout_intent_instance:
            checkout_intent_data = CheckoutIntentModelSerializer(checkout_intent_instance).data
        return checkout_intent_data


class CheckoutContextHandler(CheckoutIntentAwareHandlerMixin, BaseHandler):
    """
    Handler for the checkout context endpoint.

    Responsible for gathering:
    - Enterprise customer information for authenticated users
    - Pricing options for self-service subscriptions
    - Field constraints for the checkout form
    """
    context: CheckoutContext

    def __init__(self, context: CheckoutContext):
        """
        Initialize with the request context.

        Args:
            context: The handler context object containing request information
        """
        super().__init__(context)
        self.lms_client = LmsApiClient()

    def load_and_process(self):
        """
        Load data and process it for the response.

        This method:
        1. Extracts stripeProductId from request (if present)
        2. Resolves and validates the Stripe product
        3. Updates CheckoutIntent with stripe_product_id
        4. Checks if the user is authenticated
        5. If authenticated, fetches associated enterprise customers
        6. Fetches pricing options from Stripe
        7. Gathers field constraints from settings
        8. Populates the context with all data
        """
        resolved_product = None
        try:
            # Extract stripeProductId from request body
            request_data = getattr(self.context.request, 'data', None)
            if request_data is None:
                request_data = getattr(self.context.request, 'POST', {})
            stripe_product_id = request_data.get('stripeProductId')

            # If stripeProductId is provided, resolve and validate it
            if stripe_product_id and self.context.user and self.context.user.is_authenticated:
                resolved_product = self.resolve_stripe_product(stripe_product_id)
                if resolved_product:
                    # Update the most specific matching CheckoutIntent first, then fall back to latest non-expired.
                    requested_enterprise_slug = (
                        request_data.get('enterpriseSlug') or
                        request_data.get('enterprise_slug')
                    )
                    checkout_intent = CheckoutIntent.for_user(
                        self.context.user,
                        enterprise_slug=requested_enterprise_slug,
                        stripe_product_id=stripe_product_id,
                    )
                    if not checkout_intent:
                        checkout_intent = CheckoutIntent.for_user(
                            self.context.user,
                            enterprise_slug=requested_enterprise_slug,
                        )
                    if not checkout_intent:
                        checkout_intent = CheckoutIntent.for_user(self.context.user)

                    if checkout_intent:
                        checkout_intent.stripe_product_id = stripe_product_id
                        checkout_intent.clean()
                        checkout_intent.save()
                        logger.info(
                            "Updated CheckoutIntent %s with stripe_product_id %s",
                            checkout_intent.uuid,
                            stripe_product_id
                        )
                else:
                    logger.warning(
                        "Failed to resolve Stripe product %s for user %s",
                        stripe_product_id,
                        self.context.user.email
                    )
                    self.add_error(
                        user_message="Invalid Stripe product ID.",
                        developer_message=f"Could not resolve or validate Stripe product: {stripe_product_id}",
                    )

            self.context.pricing = self._get_pricing_data()
            self.context.pricing['resolved_product'] = resolved_product
            self.context.field_constraints = self._get_field_constraints()
            self.context.checkout_intent = self._get_checkout_intent()
            if self.context.user:
                self._load_enterprise_customers()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error loading/processing checkout context handler for request user %s",
                self.context.user,
            )
            self.add_error(
                user_message="Could not load and/or process checkout context data",
                developer_message=f"Unable to load and/or process checkout context data: {exc}",
            )

    def _load_enterprise_customers(self):
        """
        Load enterprise customer information for the authenticated user.
        """
        try:
            # Check if the user is authenticated
            if not self.context.user.is_authenticated:
                logger.debug("User is not authenticated, skipping enterprise customer lookup")
                return

            # Get enterprise customer users for the authenticated user
            enterprise_customer_users_data = get_and_cache_enterprise_customer_users(
                self.context.request,
                traverse_pagination=True,
            )

            # Transform the data
            transformed_data = transform_enterprise_customer_users_data(
                enterprise_customer_users_data,
                self.context.request,
                enterprise_customer_slug=None,
                enterprise_customer_uuid=None,
            )

            # Format data according to our API contract
            formatted_customers = []

            for customer_user in transformed_data.get('all_linked_enterprise_customer_users', []):
                customer = customer_user.get('enterprise_customer', {})
                if customer:
                    slug = customer.get('slug')
                    admin_portal_url = f'{settings.ENTERPRISE_ADMIN_PORTAL_URL}/{slug}' if slug else ''
                    formatted_customers.append({
                        'customer_uuid': customer.get('uuid'),
                        'customer_name': customer.get('name'),
                        'customer_slug': slug,
                        'stripe_customer_id': customer.get('stripe_customer_id', ''),
                        'is_self_service': customer.get('is_self_service', False),
                        'admin_portal_url': admin_portal_url,
                    })

            self.context.existing_customers_for_authenticated_user = formatted_customers
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error loading enterprise customers for user: %s",
                exc
            )
            self.add_error(
                user_message="Could not fetch existing customer data for user",
                developer_message=f"Unable to load customer data for user: {exc}",
            )

    def _get_pricing_data(self) -> Dict:
        """
        Get pricing data from Stripe for self-service subscription plans and Essentials academies.

        Returns:
            Dict containing default lookup key, Teams prices, and academy details with prices
        """
        try:
            # Fetch Teams subscription pricing
            pricing_data = get_ssp_product_pricing()
            prices = []
            for _, price_data in pricing_data.items():
                prices.append({
                    'id': price_data.get('id'),
                    'product': price_data.get('product', {}).get('id'),
                    'lookup_key': price_data.get('lookup_key'),
                    'recurring': price_data.get('recurring', {}),
                    'currency': price_data.get('currency'),
                    'unit_amount': price_data.get('unit_amount'),
                    'unit_amount_decimal': str(price_data.get('unit_amount_decimal'))
                })

            # Fetch Essentials academy pricing only when the feature flag is enabled.
            academies = self._get_academy_pricing_data()

            return {
                'default_by_lookup_key': settings.DEFAULT_SSP_PRICE_LOOKUP_KEY,
                'prices': prices,
                'academies': academies,
                'resolved_product': None,
            }
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Error fetching pricing data: %s", exc)
            self.add_error(
                user_message="Could not load pricing data.",
                developer_message=f"Could not load pricing data: {exc}",
            )
            return {
                'default_by_lookup_key': settings.DEFAULT_SSP_PRICE_LOOKUP_KEY,
                'prices': [],
                'academies': [],
                'resolved_product': None,
            }

    def _get_academy_pricing_data(self) -> list:
        """
        Fetch active Essentials academies and enrich with Stripe pricing data.

        Returns:
            List of academy objects with pricing information
        """
        if not getattr(settings, 'ENABLE_ESSENTIALS_CHECKOUT', False):
            return []

        try:
            # Fetch all active Stripe prices
            all_stripe_prices = get_all_active_stripe_prices(
                timeout=settings.STRIPE_PRICE_DATA_CACHE_TIMEOUT
            )

            # Build lookup maps: product_id -> prices and lookup_key -> prices
            prices_by_product_id = {}
            prices_by_lookup_key = {}
            for price in all_stripe_prices:
                product = price.get('product') or {}
                product_id = product.get('id')
                lookup_key = price.get('lookup_key')

                serialized_price = {
                    'id': price.get('id'),
                    'product': product_id,
                    'lookup_key': lookup_key,
                    'recurring': price.get('recurring'),
                    'currency': price.get('currency'),
                    'unit_amount': price.get('unit_amount'),
                    'unit_amount_decimal': str(price.get('unit_amount_decimal', ''))
                }

                if product_id:
                    if product_id not in prices_by_product_id:
                        prices_by_product_id[product_id] = []
                    prices_by_product_id[product_id].append(serialized_price)

                if lookup_key:
                    if lookup_key not in prices_by_lookup_key:
                        prices_by_lookup_key[lookup_key] = []
                    prices_by_lookup_key[lookup_key].append(serialized_price)

            # Fetch active academies from database
            academies_list = []
            academies = EnterpriseAcademy.objects.filter(is_active=True).order_by('display_order', 'name')

            for academy in academies:
                # Resolve prices by product_id or lookup_key
                academy_prices = []
                if academy.stripe_product_id:
                    academy_prices = prices_by_product_id.get(academy.stripe_product_id, [])

                if not academy_prices and academy.stripe_price_lookup_key:
                    academy_prices = prices_by_lookup_key.get(academy.stripe_price_lookup_key, [])

                academies_list.append({
                    'id': academy.product_key or academy.slug,
                    'name': academy.name,
                    'long_name': academy.long_name or academy.name,
                    'description': academy.description,
                    'marketing_url': academy.marketing_url,
                    'thumbnail_url': academy.thumbnail_url,
                    'tags': academy.tags or [],
                    'stripe_product_id': academy.stripe_product_id,
                    'prices': academy_prices,
                })

            return academies_list
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Error fetching academy pricing data: %s", exc)
            return []

    def _get_field_constraints(self) -> Dict:
        """
        Get field constraints from settings.

        Returns:
            Dict containing constraints for form fields
        """
        # Get quantity constraints from SSP_PRODUCTS setting
        quantity_constraints = {'min': 5, 'max': 30}  # Default values
        for product_config in settings.SSP_PRODUCTS.values():
            if 'quantity_range' in product_config:
                min_val, max_val = product_config['quantity_range']
                quantity_constraints = {'min': min_val, 'max': max_val}
                break
        return {
            'quantity': quantity_constraints,
            'enterprise_slug': {
                'min_length': 1,
                'max_length': 255,
                'pattern': '^[a-z0-9-]+$'
            },
            'embargoed_countries': get_embargoed_countries(),
            'full_name': {
                'min_length': 1,
                'max_length': 150,
            },
            'admin_email': {
                'min_length': 6,
                'max_length': 253,
                'pattern': '^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$',  # enforce the format of X@Y.Z without spaces
            },
            'country': {
                'min_length': 2,
                'max_length': 2,
                'pattern': '^[A-Z]{2}$'
            },
            'company_name': {
                'min_length': 1,
                'max_length': 255,
            }
        }

    def resolve_stripe_product(self, stripe_product_id: str) -> Dict | None:
        """
        Retrieve and validate a Stripe product by ID.

        This method:
        1. Fetches the Stripe product
        2. Validates that metadata.name matches a known academy
        3. Returns the product with metadata

        Args:
            stripe_product_id (str): The Stripe Product ID to resolve

        Returns:
            Dict: Product metadata including name, product_type, catalog_query_uuid, etc.
            None: If product is invalid or metadata cannot be validated

        Raises:
            stripe.error.InvalidRequestError: If Stripe product doesn't exist
            Exception: For other Stripe API errors
        """
        if not stripe_product_id:
            return None

        try:
            # Retrieve the Stripe product
            product = stripe.Product.retrieve(stripe_product_id)

            # Extract metadata
            metadata = product.get('metadata') or {}
            product_name = metadata.get('name')
            product_type = metadata.get('product_type')

            all_prices = get_all_active_stripe_prices(timeout=settings.STRIPE_PRICE_DATA_CACHE_TIMEOUT)
            resolved_prices = [
                {
                    'id': price.get('id'),
                    'product': (price.get('product') or {}).get('id'),
                    'lookup_key': price.get('lookup_key'),
                    'recurring': price.get('recurring'),
                    'currency': price.get('currency'),
                    'unit_amount': price.get('unit_amount'),
                    'unit_amount_decimal': str(price.get('unit_amount_decimal', '')),
                }
                for price in all_prices
                if (price.get('product') or {}).get('id') == stripe_product_id
            ]

            # Validate that product_name corresponds to an academy
            if product_type == 'essentials' and product_name:
                if not getattr(settings, 'ENABLE_ESSENTIALS_CHECKOUT', False):
                    logger.warning(
                        "Stripe product %s is an Essentials product but ENABLE_ESSENTIALS_CHECKOUT is disabled",
                        stripe_product_id,
                    )
                    return None

                # Check if the academy exists
                academy = EnterpriseAcademy.objects.filter(name__iexact=product_name).first()
                if academy:
                    resolved_product = {
                        'stripe_product_id': product.get('id'),
                        'name': product_name,
                        'product_type': product_type,
                        'prices': resolved_prices,
                        'metadata': metadata,
                    }
                    if academy.catalog_query_uuid:
                        catalog_query_uuid = str(academy.catalog_query_uuid)
                        resolved_product['catalog_query_uuid'] = catalog_query_uuid
                        resolved_product['catalog_query_id'] = catalog_query_uuid
                        resolved_product['edx_catalog_id'] = catalog_query_uuid
                    else:
                        resolved_product['catalog_query_uuid'] = None
                        logger.warning(
                            "Stripe product %s matched academy '%s' without catalog_query_uuid configured",
                            stripe_product_id,
                            product_name,
                        )
                    return resolved_product
                else:
                    logger.warning(
                        "Stripe product %s references unknown academy: %s",
                        stripe_product_id,
                        product_name
                    )
                    return None

            # Handle Teams product or other product types
            if product_type in (None, 'teams', ''):
                return {
                    'stripe_product_id': product.get('id'),
                    'name': product.get('name'),
                    'product_type': product_type,
                    'prices': resolved_prices,
                    'metadata': metadata,
                }

            logger.warning(
                "Stripe product %s has unknown product_type: %s",
                stripe_product_id,
                product_type
            )
            return None

        except stripe.error.InvalidRequestError as exc:
            logger.warning("Stripe product not found: %s - %s", stripe_product_id, exc)
            return None
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("Error resolving Stripe product %s: %s", stripe_product_id, exc)
            return None


class CheckoutValidationHandler(BaseHandler):
    """
    Handler for validating checkout form fields.
    """
    context: CheckoutValidationContext

    def __init__(self, context: CheckoutValidationContext):
        super().__init__(context)
        self.user = getattr(context.request, 'user', None)
        self.authenticated_user = self.user if self.user and self.user.is_authenticated else None

    def load_and_process(self):
        """
        Process the validation request.
        """
        request_data = self.context.request.data

        # Check if admin_email is provided to check user existence
        # We intentionally initialize this to None,
        # which has the semantics of "we don't know if this user exists or not"
        user_exists_for_email = None
        if (admin_email := request_data.get('admin_email')):
            user_exists_for_email = self._check_user_existence(admin_email)

        # Create a mutable copy of the request data.
        validation_data = dict(request_data.items())
        validation_decisions = {}

        # Only validate enterprise_slug if authenticated
        if not self.authenticated_user and 'enterprise_slug' in request_data:
            validation_decisions['enterprise_slug'] = {
                'error_code': 'authentication_required',
                'developer_message': 'Authentication required to validate enterprise slugs.'
            }
            validation_data.pop('enterprise_slug')

        if validation_data:
            validation_results = validate_free_trial_checkout_session(
                user=self.authenticated_user,
                **validation_data
            )
            validation_decisions.update(validation_results)

        self.context.validation_decisions = validation_decisions
        self.context.user_authn = {
            'user_exists_for_email': user_exists_for_email
        }

    def _check_user_existence(self, email):
        """
        Check if a user exists for the given email.
        """
        try:
            lms_client = LmsApiClient()
            user_data = lms_client.get_lms_user_account(email=email)
            return bool(user_data)
        except Exception:  # pylint: disable=broad-except
            # In case of error, we don't know if the user exists
            return None


class CheckoutSuccessHandler(CheckoutContextHandler):
    """
    Handler for checkout success operations. Builds on the ``CheckoutContextHandler``
    to enhance the checkout intent record with addtional data from the stripe API.
    """
    context: CheckoutSuccessContext

    def load_and_process(self):
        """
        Loads base checkout context data, then enhances
        the checkout intent record with more data from the Stripe API.
        """
        super().load_and_process()
        if self.context.checkout_intent is None:
            return

        self._set_checkout_intent_academy_name()
        self.context.checkout_intent['first_billable_invoice'] = None

        try:
            self.enhance_with_stripe_data()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error loading checkout success handler Stripe data for request user %s",
                self.context.user,
            )
            self.add_error(
                user_message="Could not load and/or process checkout success data",
                developer_message=f"Unable to load and/or process checkout success data: {exc}",
            )

    def _set_checkout_intent_academy_name(self):
        """Attach academy_name onto checkout_intent payload when a matching academy exists."""
        checkout_intent_data = self.context.checkout_intent
        checkout_intent_data['academy_name'] = None

        stripe_product_id = checkout_intent_data.get('stripe_product_id')
        if not stripe_product_id:
            return

        academy = EnterpriseAcademy.objects.filter(stripe_product_id=stripe_product_id).only('name').first()
        if academy:
            checkout_intent_data['academy_name'] = academy.name

    def enhance_with_stripe_data(self):
        """
        Enhance checkout intent data with Stripe API data. Called for side effect.

        Returns:
            None (called for side effect)
        """
        checkout_intent_data = self.context.checkout_intent

        session_id = checkout_intent_data.get('stripe_checkout_session_id')
        if not session_id:
            logger.warning(
                "No Stripe checkout session id for checkout intent: "
                f"{checkout_intent_data.get('id')}"
            )
            return

        try:
            session = get_stripe_checkout_session(session_id).to_dict()
        except stripe.StripeError:
            logger.exception("Error retrieving Stripe checkout session: %s", session_id)
            return

        first_billable_invoice = {
            'start_time': None,
            'end_time': None,
            'last4': None,
            'card_brand': None,
            'quantity': None,
            'unit_amount_decimal': None,
            'customer_phone': None,
            'customer_name': None,
            'billing_address': None,
        }
        # THIS IS THE SIDE-EFFECT INITIALIZATION
        checkout_intent_data['first_billable_invoice'] = first_billable_invoice

        payment_method = self._get_payment_method(session)
        if payment_method:
            first_billable_invoice.update(self._get_card_billing_details(payment_method))

        invoice_id = session.get('invoice')
        subscription_id = session.get('subscription')

        invoice = self._get_invoice_record(invoice_id, subscription_id)
        if not invoice:
            return

        subscription_item = self._get_subscription_item(invoice)
        if not subscription_item:
            return

        first_billable_invoice['quantity'] = subscription_item.get('quantity')

        if unit_amount := subscription_item.get('price', {}).get('unit_amount_decimal'):
            first_billable_invoice['unit_amount_decimal'] = cents_to_dollars(unit_amount)

        first_billable_invoice.update(self._get_subscription_start_end(subscription_item))
        first_billable_invoice.update(self._get_customer_info(invoice))

    @staticmethod
    def _get_payment_method(session):
        """ Helper to fetch payment method record from Stripe. """
        payment_method_id = None

        # Try payment intent first (for paid subscriptions)
        if payment_intent_id := session.get('payment_intent'):
            try:
                payment_intent = get_stripe_payment_intent(payment_intent_id).to_dict()
                payment_method_id = payment_intent.get('payment_method')
            except stripe.StripeError:
                logger.exception("Error retrieving Stripe payment intent: %s", payment_intent_id)

        # If no payment method yet, try subscription (for trial subscriptions)
        if not payment_method_id and (subscription_id := session.get('subscription')):
            try:
                subscription = get_stripe_subscription(subscription_id).to_dict()
                payment_method_id = subscription.get('default_payment_method')
            except stripe.StripeError:
                logger.exception("Error retrieving Stripe subscription: %s", subscription_id)

        if not payment_method_id:
            logger.warning('No payment method found on stripe session %s', session.get('id'))
            return None

        try:
            return get_stripe_payment_method(payment_method_id).to_dict()
        except stripe.StripeError:
            logger.exception("Error retrieving Stripe payment method: %s", payment_method_id)
            return None

    @staticmethod
    def _get_card_billing_details(payment_method):
        """ Helper to fetch card last 4 and billing address. """
        result = {}
        if (card_metadata := payment_method.get('card', {})):
            result['last4'] = card_metadata.get('last4')
            result['card_brand'] = card_metadata.get('brand')
        if (billing_details := payment_method.get('billing_details', {})):
            result['billing_address'] = billing_details.get('address')
        return result

    @staticmethod
    def _get_invoice_record(invoice_id, subscription_id):
        """ Helper to fetch invoice record via Stripe API. """
        if not invoice_id and subscription_id:
            # If there's no invoice directly on the session, try to get it from the subscription
            try:
                subscription = get_stripe_subscription(subscription_id).to_dict()
                invoice_id = subscription.get('latest_invoice')
            except stripe.StripeError:
                logger.exception("Error retrieving Stripe subscription: %s", subscription_id)
                return None

        if not invoice_id:
            logger.warning(
                'Could not find invoice in Stripe subscription %s', subscription_id,
            )
            return None

        try:
            return get_stripe_invoice(invoice_id).to_dict()
        except stripe.StripeError:
            logger.exception("Error retrieving Stripe invoice: %s", invoice_id)
            return None

    @staticmethod
    def _get_subscription_item(invoice):
        """
        Helper to fetch a Stripe subscription item record from an invoice.
        """
        if not (lines_data := invoice.get('lines', {}).get('data', [])):
            logger.warning('No lines on invoice %s', invoice.get('id'))
            return None
        if not (subscription_item := lines_data[0]):
            logger.warning('No subscription items in invoice %s', invoice.get('id'))
            return None
        return subscription_item

    @staticmethod
    def _get_subscription_start_end(subscription_item):
        """ Returns a dict with formatted subscription item start/end time. """
        result = {}
        if (period := subscription_item.get('period', {})):
            if (start_timestamp := period.get('start')):
                result['start_time'] = datetime.fromtimestamp(start_timestamp).replace(tzinfo=UTC)
            if (end_timestamp := period.get('end')):
                result['end_time'] = datetime.fromtimestamp(end_timestamp).replace(tzinfo=UTC)
        return result

    @staticmethod
    def _get_customer_info(invoice):
        """ Helper to get dict of customer info from an invoice. """
        result = {}
        if not (customer_id := invoice.get('customer')):
            logger.warning('No customer available on invoice %s', invoice.get('id'))
            return result

        try:
            customer = get_stripe_customer(customer_id).to_dict()
            result['customer_name'] = customer.get('name')
            result['customer_phone'] = customer.get('phone')
        except stripe.StripeError:
            logger.exception("Error retrieving Stripe customer: %s", customer_id)

        if customer_address := invoice.get('customer_address'):
            logger.info(
                "Retrieved billing address from invoice customer_address: %s",
                customer_address
            )
            result['billing_address'] = customer_address

        return result
