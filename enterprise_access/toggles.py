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


# .. toggle_name: enterprise_access.pathway_review_bench
# .. toggle_implementation: WaffleFlag
# .. toggle_default: False
# .. toggle_description: Enables the internal pathway review bench, where staff reviewers
#     judge assembled career pathways so approved ones can be used as worked examples.
#     Off by default: the bench is an internal data-collection tool, not a learner surface,
#     and it is gated by the pathway_review.add_pathwayreviewvote permission in addition to
#     this flag.
# .. toggle_use_cases: opt_in
# .. toggle_creation_date: 2026-09-15
PATHWAY_REVIEW_BENCH = WaffleFlag(
    f'{ENTERPRISE_ACCESS_NAMESPACE}.pathway_review_bench',
    __name__,
    ENTERPRISE_ACCESS_LOG_PREFIX,
)


def pathway_review_bench_enabled():
    """Return whether the pathway review bench is enabled."""
    return PATHWAY_REVIEW_BENCH.is_enabled()
