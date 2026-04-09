""""
Handlers for bffs app.
"""
import json
import logging

from enterprise_access.apps.api_client.constants import LicenseStatuses
from enterprise_access.apps.api_client.license_manager_client import LicenseManagerUserApiClient
from enterprise_access.apps.api_client.lms_client import LmsApiClient
from enterprise_access.apps.bffs.api import (
    get_and_cache_default_enterprise_enrollment_intentions_learner_status,
    get_and_cache_subscription_licenses_for_learner,
    invalidate_default_enterprise_enrollment_intentions_learner_status_cache,
    invalidate_enterprise_course_enrollments_cache,
    invalidate_subscription_licenses_cache
)
from enterprise_access.apps.bffs.context import BaseHandlerContext, HandlerContext
from enterprise_access.apps.bffs.mixins import BaseLearnerDataMixin, LearnerDashboardDataMixin
from enterprise_access.apps.bffs.serializers import EnterpriseCustomerUserSubsidiesSerializer
from enterprise_access.toggles import enable_multi_license_entitlements_bff

logger = logging.getLogger(__name__)


class SubscriptionLicenseProcessor:
    """
    Handles subscription license data transformation.
    Preserves collection semantics while maintaining backward compatibility.
    
    This processor supports multi-license scenarios where a learner may have
    access to multiple subscription licenses across different catalogs.
    """

    def _build_catalog_index(self, licenses):
        """
        Build catalog_uuid → licenses mapping for efficient O(1) lookups.
        
        Args:
            licenses: List of subscription licenses
            
        Returns:
            Dict[str, List[License]]: Mapping of catalog UUID to licenses
        """
        catalog_index = {}
        for license in licenses:
            catalog_uuid = license.get('subscription_plan', {}).get('enterprise_catalog_uuid')
            if catalog_uuid:
                catalog_index.setdefault(catalog_uuid, []).append(license)
        return catalog_index

    def _select_best_license(self, licenses):
        """
        Deterministic tie-breaker for multiple matching licenses.

        When a learner has access to more than one subscription license for the same course
        (course is in multiple catalogs), this selects the license they first activated,
        per the ENT-11672 business rule:
          "the enrollment record is on the license they first activate that has the
           course in the catalog."

        Precedence:
        1. Earliest activation_date ASC  — first-activated license wins
        2. Latest expiration_date DESC   — longer access window as secondary criterion
        3. UUID DESC                     — stable, deterministic fallback

        Args:
            licenses: Non-empty list of candidate licenses.

        Returns:
            License: The best matching license dict.
        """
        if len(licenses) == 1:
            return licenses[0]

        def _sort_key(lic):
            activation = lic.get('activation_date') or '9999-12-31'
            expiration = (
                lic.get('subscription_plan', {}).get('expiration_date') or '0000-00-00'
            )
            uuid = lic.get('uuid') or ''
            # For DESC ordering on string fields, negate each character's ordinal value.
            # ISO-8601 date strings and UUIDs are ASCII, so ord() is safe and deterministic.
            exp_desc = tuple(-(ord(c)) for c in expiration)
            uuid_desc = tuple(-(ord(c)) for c in uuid)
            return (activation, exp_desc, uuid_desc)

        return sorted(licenses, key=_sort_key)[0]


class BaseHandler:
    """
    A base handler class that provides shared core functionality for different BFF handlers.
    The `BaseHandler` includes core methods for loading data and adding errors to the context.
    """

    def __init__(self, context: BaseHandlerContext):
        """
        Initializes the BaseHandler with a HandlerContext.
        Args:
            context (HandlerContext): The context object containing request information and data.
        """
        self.context = context

    def load_and_process(self):
        """
        Loads and processes data. This method should be extended by subclasses to implement
        specific data loading and transformation logic.
        """
        raise NotImplementedError("Subclasses must implement `load_and_process` method.")

    def add_error(self, user_message, developer_message, status_code=None):
        """
        Adds an error to the context.
        Output fields determined by the ErrorSerializer
        """
        self.context.add_error(
            user_message=user_message,
            developer_message=developer_message,
            status_code=status_code,
        )

    def add_warning(self, user_message, developer_message):
        """
        Adds an error to the context.
        Output fields determined by the WarningSerializer
        """
        self.context.add_warning(
            user_message=user_message,
            developer_message=developer_message,
        )


class BaseLearnerPortalHandler(BaseHandler, BaseLearnerDataMixin, SubscriptionLicenseProcessor):
    """
    A base handler class for learner-focused routes.

    The `BaseLearnerHandler` extends `BaseHandler` and provides shared core functionality
    across all learner-focused page routes, such as the learner dashboard, search, and course routes.
    """
    context: HandlerContext

    def __init__(self, context: HandlerContext):
        """
        Initializes the BaseLearnerPortalHandler with a HandlerContext and API clients.
        Args:
            context (HandlerContext): The context object containing request information and data.
        """
        super().__init__(context)

        # API Clients
        self.license_manager_user_api_client = LicenseManagerUserApiClient(self.context.request)
        self.lms_api_client = LmsApiClient()

    def load_and_process(self):
        """
        Loads and processes data. This is a basic implementation that can be overridden by subclasses.

        The method in this class simply calls common learner logic to ensure the context is set up.
        """
        try:
            # Verify enterprise customer attrs have learner portal enabled
            self.ensure_learner_portal_enabled()

            # Transform enterprise customer data
            self.transform_enterprise_customers()

            # Retrieve and process subscription licenses. Handles activation and auto-apply logic.
            self.load_and_process_subsidies()

            # Scope Algolia search access to the learner's current activated license catalogs.
            self.scope_secured_algolia_api_keys_to_activated_licenses()

            # Retrieve default enterprise courses and enroll in the redeemable ones
            self.load_default_enterprise_enrollment_intentions()
            self.enroll_in_redeemable_default_enterprise_enrollment_intentions()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error loading/processing learner portal handler for request user %s and enterprise customer %s",
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
            )
            self.add_error(
                user_message="Could not load and/or process common data",
                developer_message=f"Unable to load and/or process common learner portal data: {exc}",
            )

    def ensure_learner_portal_enabled(self):
        """
        Ensure the learner portal is enabled for the enterprise
        customer attributes in the context. If not, remove the enterprise
        customer data from the context and add a warning.
        """
        for customer_record_key in ('enterprise_customer', 'active_enterprise_customer', 'staff_enterprise_customer'):
            if not (customer_record := getattr(self.context, customer_record_key, None)):
                logger.warning(
                    f"No {customer_record_key} found in the context for request user {self.context.lms_user_id}"
                )
                continue

            if not customer_record.get('enable_learner_portal', False):
                logger.warning(
                    f"Learner portal is not enabled for enterprise customer {customer_record.get('uuid')}"
                )
                # Remove the enterprise customer data from the context
                self.context.data.pop(customer_record_key, None)

                # Add a warning to the context
                self.add_warning(
                    user_message="Learner portal not enabled for enterprise customer",
                    developer_message=(
                        f"[{customer_record_key}] Learner portal not enabled for enterprise "
                        f"customer {customer_record.get('uuid')} for request user {self.context.lms_user_id}"
                    ),
                )

    def transform_enterprise_customers(self):
        """
        Transform enterprise customer metadata retrieved by self.context.
        """
        for customer_record_key in ('enterprise_customer', 'active_enterprise_customer', 'staff_enterprise_customer'):
            if not (customer_record := getattr(self.context, customer_record_key, None)):
                logger.warning(
                    f"No {customer_record_key} found in the context for request user {self.context.lms_user_id}"
                )
                continue
            self.context.data[customer_record_key] = self.transform_enterprise_customer(customer_record)

        if enterprise_customer_users := self.context.all_linked_enterprise_customer_users:
            self.context.data['all_linked_enterprise_customer_users'] = [
                self.transform_enterprise_customer_user(enterprise_customer_user)
                for enterprise_customer_user in enterprise_customer_users
                if enterprise_customer_user.get('enterprise_customer').get('enable_learner_portal') is True
            ]
        else:
            logger.warning(
                f"No linked enterprise customer users found in the context for request user {self.context.lms_user_id}"
            )

    def load_and_process_subsidies(self):
        """
        Load and process subsidies for learners
        """
        empty_subsidies = {
            'subscriptions': {
                'customer_agreement': None,
            },
        }
        self.context.data['enterprise_customer_user_subsidies'] =\
            EnterpriseCustomerUserSubsidiesSerializer(empty_subsidies).data
        self.load_and_process_subscription_licenses()

    def transform_enterprise_customer_user(self, enterprise_customer_user):
        """
        Transform the enterprise customer user data.

        Args:
            enterprise_customer_user: The enterprise customer user data.
        Returns:
            The transformed enterprise customer user data.
        """
        enterprise_customer = enterprise_customer_user.get('enterprise_customer')
        return {
            **enterprise_customer_user,
            'enterprise_customer': self.transform_enterprise_customer(enterprise_customer),
        }

    def transform_enterprise_customer(self, enterprise_customer):
        """
        Transform the enterprise customer data.

        Args:
            enterprise_customer: The enterprise customer data.

        Returns:
            The transformed enterprise customer data.
        """
        # Learner Portal is enabled, so transform the enterprise customer data.
        identity_provider = enterprise_customer.get("identity_provider")
        active_integrations = enterprise_customer.get("active_integrations")
        disable_search = bool(
            not enterprise_customer.get("enable_integrated_customer_learner_portal_search", False) and
            identity_provider
        )
        show_integration_warning = bool(not disable_search and active_integrations)

        return {
            **enterprise_customer,
            'disable_search': disable_search,
            'show_integration_warning': show_integration_warning,
        }

    def load_subscription_licenses(self):
        """
        Load subscription licenses for the learner.
        """
        try:
            subscriptions_result = get_and_cache_subscription_licenses_for_learner(
                request=self.context.request,
                enterprise_customer_uuid=self.context.enterprise_customer_uuid,
                include_revoked=True,
                current_plans_only=False,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error loading subscription licenses for request user %s and enterprise customer %s",
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
            )
            self.add_error(
                user_message="Unable to retrieve subscription licenses",
                developer_message=f"Unable to fetch subscription licenses. Error: {exc}",
            )
            return

        try:
            subscriptions_data = self.transform_subscriptions_result(subscriptions_result)
            self.context.data['enterprise_customer_user_subsidies'].update({
                'subscriptions': subscriptions_data,
            })
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error transforming subscription licenses for request user %s and enterprise customer %s",
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
            )
            self.add_error(
                user_message="Unable to transform subscription licenses",
                developer_message=f"Unable to transform subscription licenses. Error: {exc}",
            )

    def _extract_subscription_license(self, subscription_licenses_by_status):
        """
        Extract subscription licenses from the subscription licenses by status.
        """
        license_status_priority_order = [
            LicenseStatuses.ACTIVATED,
            LicenseStatuses.ASSIGNED,
            LicenseStatuses.REVOKED,
        ]
        subscription_license = next(
            (
                license
                for status in license_status_priority_order
                for license in subscription_licenses_by_status.get(status, [])
            ),
            None,
        )
        return subscription_license

    def transform_subscriptions_result(self, subscriptions_result):
        """
        Transform subscription licenses data with support for multiple licenses.
        
        Returns both collection-first fields (subscription_licenses, licenses_by_catalog) 
        and legacy singular fields (subscription_license, subscription_plan) for 
        backward compatibility.
        """
        subscription_licenses = subscriptions_result.get('results', [])
        subscription_licenses_by_status = {}

        # Sort licenses by whether the associated subscription plans
        # are current; current plans should be prioritized over non-current plans.
        ordered_subscription_licenses = sorted(
            subscription_licenses,
            key=lambda license: not license.get('subscription_plan', {}).get('is_current'),
        )

        # Group licenses by status
        for subscription_license in ordered_subscription_licenses:
            status = subscription_license.get('status')
            if status not in subscription_licenses_by_status:
                subscription_licenses_by_status[status] = []
            subscription_licenses_by_status[status].append(subscription_license)

        customer_agreement = subscriptions_result.get('customer_agreement')
        subscription_license = self._extract_subscription_license(subscription_licenses_by_status)
        subscription_plan = subscription_license.get('subscription_plan') if subscription_license else None

        # Check the multi-license feature flag.
        # When ON: populate the catalog index and advertise schema v2.
        # When OFF: return empty catalog index and schema v1 for full backward compatibility.
        multi_license_flag_enabled = enable_multi_license_entitlements_bff()

        # Build catalog index for activated licenses to enable efficient course-to-license matching
        activated_licenses = subscription_licenses_by_status.get(LicenseStatuses.ACTIVATED, [])
        if multi_license_flag_enabled and activated_licenses:
            licenses_by_catalog = self._build_catalog_index(activated_licenses)
            # ENT-11672: When multi-license flag is ON, the legacy subscription_license field
            # must also use the first-activated selection rule so that it is consistent with
            # courses_by_catalog and the enrollment record business rule.
            best_activated = self._select_best_license(activated_licenses)
            subscription_license = best_activated
            subscription_plan = best_activated.get('subscription_plan') if best_activated else subscription_plan
            # v2: expose all licenses in the flat collection field
            response_subscription_licenses = subscription_licenses
        else:
            licenses_by_catalog = {}
            # v1 (flag OFF): preserve main's flat list behavior and expose all licenses.
            response_subscription_licenses = subscription_licenses

        # Determine if expiration notifications should be shown
        if not customer_agreement:
            show_expiration_notifications = False
        else:
            disable_expiration_notifications = customer_agreement.get('disable_expiration_notifications', False)
            custom_expiration_messaging = customer_agreement.get('has_custom_license_expiration_messaging_v2', False)
            show_expiration_notifications = not (disable_expiration_notifications or custom_expiration_messaging)

        return {
            'customer_agreement': customer_agreement,
            # Collection-first fields (canonical for multi-license support)
            'subscription_licenses': response_subscription_licenses,
            'subscription_licenses_by_status': subscription_licenses_by_status,
            'licenses_by_catalog': licenses_by_catalog,
            # Schema version lets the MFE know which response shape to expect.
            # 'v2' = multi-license fields populated; 'v1' = legacy single-license only.
            'license_schema_version': 'v2' if multi_license_flag_enabled else 'v1',
            # Legacy singular fields (backward compatibility - deprecated)
            'subscription_license': subscription_license,
            'subscription_plan': subscription_plan,
            'show_expiration_notifications': show_expiration_notifications,
        }

    def refresh_subscription_data(self, subscription_licenses):
        """
        Rebuilds the subscription payload in context from a flat license list.
        """
        subscriptions_data = self.transform_subscriptions_result({
            'results': subscription_licenses,
            'customer_agreement': self.customer_agreement,
        })
        self.context.data['enterprise_customer_user_subsidies']['subscriptions'].update(subscriptions_data)

    def scope_secured_algolia_api_keys_to_activated_licenses(self):
        """
        Refresh the secured Algolia key so it is limited to the learner's current activated catalogs.
        """
        # Legacy mode (flag OFF): scope search to a single selected activated license,
        # preserving "either/or" access semantics.
        # Multi-license mode (flag ON): scope search to all current activated licenses.
        if enable_multi_license_entitlements_bff():
            scoped_licenses = self.current_activated_licenses
        else:
            selected_license = self.subscriptions.get('subscription_license') or self.current_activated_license
            scoped_licenses = [selected_license] if selected_license else []

        activated_catalog_uuids = {
            license.get('subscription_plan', {}).get('enterprise_catalog_uuid')
            for license in scoped_licenses
            if license.get('subscription_plan', {}).get('enterprise_catalog_uuid')
        }
        self.context.refresh_secured_algolia_api_keys(catalog_uuids=activated_catalog_uuids)

    def _current_subscription_licenses_for_status(self, status):
        """
        Filter subscription licenses by license status and current subscription plan.
        """
        current_licenses_for_status = [
            _license for _license in self.subscription_licenses_by_status.get(status, [])
            if _license['subscription_plan']['is_current']
        ]
        return current_licenses_for_status

    @property
    def current_activated_licenses(self):
        """
        Returns list of current, activated licenses, if any, for the user.
        """
        activated_licenses = self._current_subscription_licenses_for_status(LicenseStatuses.ACTIVATED)
        return activated_licenses

    @property
    def current_activated_license(self):
        """
        Returns an activated license for the user iff the related subscription plan is current,
        otherwise returns None.
        """
        return self.current_activated_licenses[0] if self.current_activated_licenses else None

    @property
    def current_revoked_licenses(self):
        """
        Returns a revoked license for the user iff the related subscription plan is current,
        otherwise returns None.
        """
        return self._current_subscription_licenses_for_status(LicenseStatuses.REVOKED)

    @property
    def current_assigned_licenses(self):
        """
        Returns an assigned license for the user iff the related subscription plan is current,
        otherwise returns None.
        """
        return self._current_subscription_licenses_for_status(LicenseStatuses.ASSIGNED)

    def process_subscription_licenses(self):
        """
        Process loaded subscription licenses, including performing side effects such as:
            * Checking if there is an activated license
            * Checking and activating assigned licenses
            * Checking and auto applying licenses

        This method is called after `load_subscription_licenses` to handle further actions based
        on the loaded data.
        """
        if not self.subscriptions:
            # Skip process if there are no subscriptions data
            logger.warning("No subscription data found for the request user %s", self.context.lms_user_id)
            return

        if self.current_activated_license:
            # Skip processing if request user already has an activated license(s)
            logger.info("User %s already has an activated license", self.context.lms_user_id)
            return

        # Check if there are 'assigned' licenses that need to be activated
        self.check_and_activate_assigned_license()

        # Check if the user should be auto-applied a license
        self.check_and_auto_apply_license()

    def load_and_process_subscription_licenses(self):
        """
        Helper to load subscription licenses into the context then processes them
        by determining by:
            * Checking if there is an activated license
            * Checking and activating assigned licenses
            * Checking and auto applying licenses
        """
        self.load_subscription_licenses()
        self.process_subscription_licenses()

    def check_and_activate_assigned_license(self):
        """
        Check if there are assigned licenses that need to be activated.
        """
        subscription_licenses_by_status = self.subscription_licenses_by_status
        activated_licenses = []
        for subscription_license in self.current_assigned_licenses:
            activation_key = subscription_license.get('activation_key')
            if activation_key:
                try:
                    # Perform side effect: Activate the assigned license
                    activated_license = self.license_manager_user_api_client.activate_license(activation_key)

                    # Invalidate the subscription licenses cache as the cached data changed
                    # with the now-activated license.
                    invalidate_subscription_licenses_cache(
                        enterprise_customer_uuid=self.context.enterprise_customer_uuid,
                        lms_user_id=self.context.lms_user_id,
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    license_uuid = subscription_license.get('uuid')
                    logger.exception(f"Error activating license {license_uuid}")
                    self.add_error(
                        user_message="Unable to activate subscription license",
                        developer_message=f"Could not activate subscription license {license_uuid}, Error: {exc}",
                    )
                    return

                # Update the subscription_license data with the activation status and date; the activated license is not
                # returned from the API, so we need to manually update the license object we have available.
                transformed_activated_subscription_licenses = [activated_license]
                activated_licenses.append(transformed_activated_subscription_licenses[0])
            else:
                license_uuid = subscription_license.get('uuid')
                logger.error(f"Activation key not found for license {license_uuid}")
                self.add_error(
                    user_message="No subscription license activation key found",
                    developer_message=f"Activation key not found for license {license_uuid}",
                )

        # Update the subscription_licenses_by_status data with the activated licenses
        updated_activated_licenses = self.current_activated_licenses
        updated_activated_licenses.extend(activated_licenses)
        if updated_activated_licenses:
            subscription_licenses_by_status[LicenseStatuses.ACTIVATED] = updated_activated_licenses

        activated_license_uuids = {license['uuid'] for license in activated_licenses}
        remaining_assigned_licenses = [
            subscription_license
            for subscription_license in self.current_assigned_licenses
            if subscription_license['uuid'] not in activated_license_uuids
        ]
        if remaining_assigned_licenses:
            subscription_licenses_by_status[LicenseStatuses.ASSIGNED] = remaining_assigned_licenses
        else:
            subscription_licenses_by_status.pop(LicenseStatuses.ASSIGNED, None)

        self.context.data['enterprise_customer_user_subsidies']['subscriptions'].update({
            'subscription_licenses_by_status': subscription_licenses_by_status,
        })

        # Update the subscription_licenses data with the activated licenses
        activated_by_uuid = {license['uuid']: license for license in activated_licenses}
        updated_subscription_licenses = [
            activated_by_uuid.get(subscription_license.get('uuid'), subscription_license)
            for subscription_license in self.subscription_licenses
        ]
        self.refresh_subscription_data(updated_subscription_licenses)

    def check_and_auto_apply_license(self):
        """
        Check if auto-apply licenses are available and apply them to the user.
        """
        if (self.subscription_licenses or not self.context.is_request_user_linked_to_enterprise_customer):
            # Skip auto-apply if:
            #   - User has assigned/current license(s)
            #   - User has activated/current license(s)
            #   - User has revoked/current license(s)
            #   - User is not explicitly linked to the enterprise customer (e.g., staff request user)
            return

        subscription_licenses_by_status = self.subscription_licenses_by_status
        customer_agreement = self.subscriptions.get('customer_agreement') or {}
        has_subscription_plan_for_auto_apply = (
            bool(customer_agreement.get('subscription_for_auto_applied_licenses')) and
            customer_agreement.get('net_days_until_expiration') > 0
        )
        has_idp_or_univeral_link_enabled = (
            self.context.enterprise_customer.get('identity_provider') or
            customer_agreement.get('enable_auto_applied_subscriptions_with_universal_link')
        )
        is_eligible_for_auto_apply = has_subscription_plan_for_auto_apply and has_idp_or_univeral_link_enabled
        if not is_eligible_for_auto_apply:
            # Skip auto-apply if the customer agreement does not have a subscription plan for auto-apply
            return

        try:
            # Perform side effect: Auto-apply license
            auto_applied_license = self.license_manager_user_api_client.auto_apply_license(
                customer_agreement.get('uuid')
            )
            # Invalidate the subscription licenses cache as the cached data changed with the auto-applied license.
            invalidate_subscription_licenses_cache(
                enterprise_customer_uuid=self.context.enterprise_customer_uuid,
                lms_user_id=self.context.lms_user_id,
            )
            # Update the context with the auto-applied license data
            licenses = self.subscription_licenses + [auto_applied_license]
            subscription_licenses_by_status['activated'] = [auto_applied_license]
            self.refresh_subscription_data(licenses)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error auto-applying subscription license for user %s and "
                "enterprise customer %s and customer agreement %s",
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
                customer_agreement.get('uuid'),
            )
            self.add_error(
                user_message="Unable to auto-apply a subscription license.",
                developer_message=(
                    f"Could not auto-apply a subscription license for "
                    f"customer agreement {customer_agreement.get('uuid')}, Error: {exc}",
                )
            )

    def load_default_enterprise_enrollment_intentions(self):
        """
        Load default enterprise course enrollments (stubbed)
        """
        if not self.context.is_request_user_linked_to_enterprise_customer:
            # Skip loading default enterprise enrollment intentions if the request
            # user is not linked to specified enterprise customer (e.g., staff request user)
            logger.info(
                'Request user %s is not linked to enterprise customer %s. Skipping default '
                'enterprise enrollment intentions.',
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
            )
            return

        try:
            default_enterprise_enrollment_intentions =\
                get_and_cache_default_enterprise_enrollment_intentions_learner_status(
                    request=self.context.request,
                    enterprise_customer_uuid=self.context.enterprise_customer_uuid,
                )
            self.context.data['default_enterprise_enrollment_intentions'] = default_enterprise_enrollment_intentions
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error loading default enterprise enrollment intentions for user %s and enterprise customer %s",
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
            )
            self.add_error(
                user_message="Could not load default enterprise enrollment intentions",
                developer_message=f"Could not load default enterprise enrollment intentions. Error: {e}",
            )

    def _map_courses_to_licenses(self, enrollment_intentions):
        """
        Map each course to the best matching license from multiple available licenses.
        
        When a learner has access to more than one subscription license and they enroll 
        in a course that is in multiple catalogs, the enrollment uses the license they 
        first activate that has the course in the catalog (as per ENT-11672).
        
        Algorithm:
        1. For each course, find ALL licenses whose catalog contains the course.
        2. If multiple match, apply deterministic tie-breaker (_select_best_license):
           a. Earliest activation_date (first activated)
           b. Latest expiration_date (maximize access window)
           c. UUID descending order (deterministic fallback)
        
        Args:
            enrollment_intentions: List of enrollment intentions with course_run_key 
                                   and applicable_enterprise_catalog_uuids
        
        Returns:
            dict: Mapping of course_run_key to license UUID
        """
        license_uuids_by_course_run_key = {}
        
        # Get all current activated licenses
        activated_licenses = self.current_activated_licenses
        
        if not activated_licenses:
            logger.info(
                "No activated licenses found for course-to-license mapping for request user %s",
                self.context.lms_user_id,
            )
            return license_uuids_by_course_run_key
        
        # Build catalog index for efficient O(1) lookups
        licenses_by_catalog = self._build_catalog_index(activated_licenses)
        
        # For each course, find all matching licenses and select the best one
        for enrollment_intention in enrollment_intentions:
            course_run_key = enrollment_intention.get('course_run_key')
            applicable_catalog_uuids = enrollment_intention.get('applicable_enterprise_catalog_uuids', [])
            
            # Collect all licenses that have this course in their catalog
            matching_licenses = []
            for catalog_uuid in applicable_catalog_uuids:
                matching_licenses.extend(licenses_by_catalog.get(catalog_uuid, []))
            
            # Remove duplicates (a license might appear in multiple catalogs)
            unique_licenses = {lic['uuid']: lic for lic in matching_licenses}.values()
            
            if not unique_licenses:
                logger.debug(
                    "No license found for course %s (catalogs: %s) for user %s",
                    course_run_key,
                    applicable_catalog_uuids,
                    self.context.lms_user_id,
                )
                continue
            
            # Select the best license using deterministic tie-breaker
            best_license = self._select_best_license(list(unique_licenses))
            license_uuids_by_course_run_key[course_run_key] = best_license['uuid']
            
            logger.info(
                "Mapped course %s to license %s (catalog: %s, activated: %s, expires: %s) for user %s",
                course_run_key,
                best_license['uuid'],
                best_license.get('subscription_plan', {}).get('enterprise_catalog_uuid'),
                best_license.get('activation_date'),
                best_license.get('subscription_plan', {}).get('expiration_date'),
                self.context.lms_user_id,
            )
        
        return license_uuids_by_course_run_key

    def enroll_in_redeemable_default_enterprise_enrollment_intentions(self):
        """
        Enroll in redeemable courses.
        
        For multiple licenses: Maps each course to the appropriate license based on 
        catalog membership. When a course is in multiple catalogs with multiple matching 
        licenses, uses the first activated license (ENT-11672).
        """
        enrollment_statuses = self.default_enterprise_enrollment_intentions.get('enrollment_statuses', {})
        needs_enrollment = enrollment_statuses.get('needs_enrollment', {})
        needs_enrollment_enrollable = needs_enrollment.get('enrollable', [])

        if not needs_enrollment_enrollable:
            # Skip enrolling in default enterprise courses if there are no enrollable courses for which to enroll
            logger.info(
                "No default enterprise enrollment intentions courses for which to enroll "
                "for request user %s and enterprise customer %s",
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
            )
            return

        if not self.current_activated_licenses:
            # Skip enrolling in default enterprise courses if there are no activated licenses
            logger.info(
                "No activated licenses found for request user %s and enterprise customer %s. "
                "Skipping realization of default enterprise enrollment intentions.",
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
            )
            return

        # Gate multi-license course mapping behind the feature flag.
        # When OFF, fall back to the legacy single-license mapping so existing
        # behavior is completely unchanged.
        multi_license_flag_enabled = enable_multi_license_entitlements_bff()
        if multi_license_flag_enabled:
            license_uuids_by_course_run_key = self._map_courses_to_licenses(needs_enrollment_enrollable)
        else:
            license_uuids_by_course_run_key = self._map_courses_to_single_license(needs_enrollment_enrollable)

        response_payload = self._request_default_enrollment_realizations(license_uuids_by_course_run_key)

        if failures := response_payload.get('failures'):
            # Log and add error if there are failures realizing default enrollments
            failures_str = json.dumps(failures)
            logger.error(
                'Default realization enrollment failures for request user %s and '
                'enterprise customer %s: %s',
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
                failures_str,
            )
            self.add_error(
                user_message='There were failures realizing default enrollments',
                developer_message='Default realization enrollment failures: ' + failures_str,
            )

        if not self.context.data.get('default_enterprise_enrollment_realizations'):
            self.context.data['default_enterprise_enrollment_realizations'] = []

        if successful_enrollments := response_payload.get('successes', []):
            # Invalidate the default enterprise enrollment intentions and enterprise course enrollments cache
            # as the previously redeemable enrollment intentions have been processed/enrolled.
            self.invalidate_default_enrollment_intentions_cache()
            self.invalidate_enrollments_cache()

        for enrollment in successful_enrollments:
            course_run_key = enrollment.get('course_run_key')
            self.context.data['default_enterprise_enrollment_realizations'].append({
                'course_key': course_run_key,
                'enrollment_status': 'enrolled',
                'subscription_license_uuid': license_uuids_by_course_run_key.get(course_run_key),
            })

    def _map_courses_to_single_license(self, enrollment_intentions):
        """
        Legacy (pre-ENT-11672) single-license course mapping.

        Used when ENABLE_MULTI_LICENSE_ENTITLEMENTS_BFF waffle flag is OFF to preserve
        the original behavior: every enrollable course is mapped to the one
        current activated license, if that license's catalog covers the course.

        Args:
            enrollment_intentions: List of enrollment intention dicts, each containing
                ``course_run_key`` and ``applicable_enterprise_catalog_uuids``.

        Returns:
            dict: Mapping of course_run_key → license UUID (may be empty).
        """
        current_license = self.current_activated_license
        if not current_license:
            return {}

        subscription_catalog = (
            current_license.get('subscription_plan', {}).get('enterprise_catalog_uuid')
        )
        mappings = {}
        for intention in enrollment_intentions:
            applicable_catalogs = intention.get('applicable_enterprise_catalog_uuids', [])
            if subscription_catalog in applicable_catalogs:
                mappings[intention['course_run_key']] = current_license['uuid']
        return mappings

    def _request_default_enrollment_realizations(self, license_uuids_by_course_run_key):
        """
        Sends the request to bulk enroll into default enrollment intentions via the LMS
        API client.
        """
        bulk_enrollment_payload = []
        for course_run_key, license_uuid in license_uuids_by_course_run_key.items():
            bulk_enrollment_payload.append({
                'user_id': self.context.lms_user_id,
                'course_run_key': course_run_key,
                'license_uuid': license_uuid,
                'is_default_auto_enrollment': True,
            })

        try:
            response_payload = self.lms_api_client.bulk_enroll_enterprise_learners(
                self.context.enterprise_customer_uuid,
                bulk_enrollment_payload,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception('Error realizing default enterprise enrollment intentions')
            self.add_error(
                user_message='There was an exception realizing default enrollments',
                developer_message=f'Default realization enrollment exception: {exc}',
            )
            response_payload = {}

        return response_payload

    def invalidate_default_enrollment_intentions_cache(self):
        invalidate_default_enterprise_enrollment_intentions_learner_status_cache(
            enterprise_customer_uuid=self.context.enterprise_customer_uuid,
            lms_user_id=self.context.lms_user_id,
        )

    def invalidate_enrollments_cache(self):
        invalidate_enterprise_course_enrollments_cache(
            enterprise_customer_uuid=self.context.enterprise_customer_uuid,
            lms_user_id=self.context.lms_user_id,
        )


class DashboardHandler(LearnerDashboardDataMixin, BaseLearnerPortalHandler):
    """
    A handler class for processing the learner dashboard route.

    The `DashboardHandler` extends `BaseLearnerPortalHandler` to handle the loading and processing
    of data specific to the learner dashboard.
    """

    def load_and_process(self):
        """
        Loads and processes data for the learner dashboard route.

        This method overrides the `load_and_process` method in `BaseLearnerPortalHandler`.
        """
        super().load_and_process()

        try:
            # Load data specific to the dashboard route
            self.load_enterprise_course_enrollments()
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Error loading and/or processing dashboard data for user %s and enterprise customer %s",
                self.context.lms_user_id,
                self.context.enterprise_customer_uuid,
            )
            self.add_error(
                user_message="Could not load and/or processing the learner dashboard.",
                developer_message=f"Failed to load and/or processing the learner dashboard data: {e}",
            )


class SearchHandler(BaseLearnerPortalHandler):
    """
    A handler class for processing the learner search route.

    Extends `BaseLearnerPortalHandler` to handle the loading and processing
    of data specific to the learner search.
    """


class AcademyHandler(BaseLearnerPortalHandler):
    """
    A handler class for processing the learner academy detail route.

    Extends `BaseLearnerPortalHandler` to handle the loading and processing
    of data specific to the learner academy detail route.
    """


class SkillsQuizHandler(BaseLearnerPortalHandler):
    """
    A handler class for processing the learner skills quiz route.

    Extends `BaseLearnerPortalHandler` to handle the loading and processing
    of data specific to the learner skills quiz route.
    """
