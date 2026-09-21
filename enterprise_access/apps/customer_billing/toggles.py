"""Feature toggles for the customer_billing app."""

from edx_toggles.toggles import WaffleFlag

from enterprise_access.apps.customer_billing.constants import BYPASS_SALESFORCE_PROVISIONING_FLAG

CUSTOMER_BILLING_LOG_PREFIX = '[customer_billing] '


# .. toggle_name: customer_billing.bypass_salesforce_for_provisioning
# .. toggle_implementation: WaffleFlag
# .. toggle_default: False
# .. toggle_description: When enabled (and settings.ALLOW_SALESFORCE_BYPASS is True), the
#     invoice.paid Stripe webhook handler skips waiting for Salesforce and directly triggers
#     ProvisionNewCustomerWorkflow. Intended for end-to-end testing in staging.
# .. toggle_use_cases: temporary
# .. toggle_creation_date: 2026-07-21
# .. toggle_target_removal_date: 2026-10-21
BYPASS_SALESFORCE_FOR_PROVISIONING = WaffleFlag(
    BYPASS_SALESFORCE_PROVISIONING_FLAG,
    __name__,
    CUSTOMER_BILLING_LOG_PREFIX,
)


def bypass_salesforce_for_provisioning_enabled():
    """Return whether the invoice.paid handler should bypass Salesforce and provision directly."""
    return BYPASS_SALESFORCE_FOR_PROVISIONING.is_enabled()
