"""Feature toggles for enterprise-access."""

from edx_toggles.toggles import WaffleFlag, WaffleSwitch

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


# A ``WaffleSwitch`` rather than a ``WaffleFlag``, deliberately. A switch is a single
# global boolean an administrator sets in Django admin; it takes no request, so it cannot
# be turned on per-user, by percentage, or by a ``?flag=1`` query string. Two consequences
# we want:
#
# * Nobody can enable an unreleased, paid pipeline for themselves by crafting a URL. Waffle
#   only ever honours query-string overrides for *flags*, and never for switches.
# * It is readable where there is no request at all -- the evaluation harness and the
#   management commands -- so one toggle governs the request path and offline runs alike.
#   ``WaffleFlag.is_enabled()`` off-request cannot express that.

# .. toggle_name: enterprise_access.learner_pathways_server_pipeline
# .. toggle_implementation: WaffleSwitch
# .. toggle_default: False
# .. toggle_description: Enables the server-side learner pathways pipeline
#     (apps/pathways). When disabled, the careers and pathway endpoints return
#     HTTP 404, indistinguishable from endpoints that do not exist. No other code
#     path is affected, so rollback is turning this off rather than reverting
#     behaviour. Off by default because the pipeline issues paid model calls.
# .. toggle_use_cases: open_edx
# .. toggle_creation_date: 2026-09-10
LEARNER_PATHWAYS_SERVER_PIPELINE = WaffleSwitch(
    f'{ENTERPRISE_ACCESS_NAMESPACE}.learner_pathways_server_pipeline',
    __name__,
)

# A kill switch, and named as one: ``False`` leaves re-ranking **on**. The polarity is
# deliberate and is the opposite of the switch above.
#
# An enable-style switch defaulting to off would mean that turning the pipeline on gives
# you pathways assembled with no model input at all -- courses in retrieval order, no
# error, and a perfectly well-formed five-course response. That is precisely the silent
# degradation this pipeline has already been bitten by once (see ADR 0037 and the
# ``ordered_keys`` defect), and it should not be reachable by forgetting to set a second
# switch.
#
# There is no cost exposure in defaulting it on: with the pipeline switch off, no endpoint
# runs, and the only other caller is the evaluation harness, which bounds its own spend
# with ``max_calls`` and ``--dry-run``.
#
# .. toggle_name: enterprise_access.learner_pathways_disable_candidate_rerank
# .. toggle_implementation: WaffleSwitch
# .. toggle_default: False
# .. toggle_description: Kill switch for the model-backed candidate re-ranking step of
#     the learner pathways pipeline. Default False, meaning re-ranking runs. Turn it ON
#     to stop the pipeline issuing paid model calls while leaving pathways working:
#     re-ranking is the only step that calls an external model, and skipping it is a
#     supported degradation rather than a failure, because deterministic assembly still
#     produces a valid five-course pathway from the unordered candidate set. Use it as a
#     cost or latency control, or if a model provider is failing.
# .. toggle_use_cases: open_edx
# .. toggle_creation_date: 2026-09-10
LEARNER_PATHWAYS_DISABLE_CANDIDATE_RERANK = WaffleSwitch(
    f'{ENTERPRISE_ACCESS_NAMESPACE}.learner_pathways_disable_candidate_rerank',
    __name__,
)


def learner_pathways_server_pipeline_enabled():
    """Return whether the server-side learner pathways pipeline is enabled."""
    return LEARNER_PATHWAYS_SERVER_PIPELINE.is_enabled()


def learner_pathways_candidate_rerank_enabled():
    """
    Return whether model-backed candidate re-ranking is enabled.

    Reads the kill switch and inverts it, so callers ask the positive question and only
    this module has to know about the polarity.
    """
    return not LEARNER_PATHWAYS_DISABLE_CANDIDATE_RERANK.is_enabled()
