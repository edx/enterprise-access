"""Feature toggles for enterprise-access."""

from edx_toggles.toggles import WaffleFlag

ENTERPRISE_ACCESS_NAMESPACE = 'enterprise_access'
ENTERPRISE_ACCESS_LOG_PREFIX = '[enterprise_access] '


# .. toggle_name: enterprise_access.enable_multi_license_entitlements_bff
# .. toggle_implementation: WaffleFlag
# .. toggle_default: False
# .. toggle_description: Enables multi-license entitlements behavior in the BFF.
#     When enabled, learner dashboard responses include the v2 multi-license
#     schema and license-to-catalog indexing, and legacy subscription selection
#     follows the ENT-11672 first-activated rule.
# .. toggle_use_cases: open_edx
# .. toggle_creation_date: 2026-04-03
ENABLE_MULTI_LICENSE_ENTITLEMENTS_BFF = WaffleFlag(
    f'{ENTERPRISE_ACCESS_NAMESPACE}.enable_multi_license_entitlements_bff',
    __name__,
    ENTERPRISE_ACCESS_LOG_PREFIX,
)


def enable_multi_license_entitlements_bff():
    """Return whether multi-license BFF behavior is enabled."""
    return ENABLE_MULTI_LICENSE_ENTITLEMENTS_BFF.is_enabled()
