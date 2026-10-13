"""
Celery tasks for the subsidy_access_policy app.
"""
import logging

from celery import shared_task
from edx_django_utils.cache import RequestCache

from enterprise_access.apps.core.models import User
from enterprise_access.apps.subsidy_access_policy.api import force_redeem_with_geag_metadata
from enterprise_access.apps.subsidy_access_policy.models import ForcedPolicyRedemption

logger = logging.getLogger(__name__)


# Not LoggedTask/LoggedTaskWithRetry: LoggedTask logs full task args at INFO, and geag_metadata is learner PII.
@shared_task(ignore_result=True)
def force_redeem_task(forced_redemption_uuid, geag_metadata, actor_lms_user_id, suppress_assignment_email):
    """
    Force-redeems one ForcedPolicyRedemption created by the bulk admin view; runs as one link of a chain.
    ``geag_metadata`` is an ``api.GeagMetadata`` dict.

    Never raises, so one failed row doesn't stop the rest of the chain. Failures are recorded on the
    record (``errored_at``/``traceback``) and logged.
    """
    # Nothing clears request caches (including TieredCache's in-process tier) between tasks in a worker, as
    # RequestCacheMiddleware does between web requests. Spend checks must see earlier rows, and lookups that
    # failed for an earlier task (e.g. learner not linked to the enterprise) must be retried fresh.
    RequestCache.clear_all_namespaces()
    forced_redemption = None
    try:
        forced_redemption = ForcedPolicyRedemption.objects.get(uuid=forced_redemption_uuid)
        # There's no request in a worker, so tell simple_history who to attribute this task's history rows to.
        forced_redemption._history_user = User.objects.filter(  # pylint: disable=protected-access
            lms_user_id=actor_lms_user_id,
        ).first()
        force_redeem_with_geag_metadata(
            forced_redemption=forced_redemption,
            geag_metadata=geag_metadata,
            actor_lms_user_id=actor_lms_user_id,
            suppress_assignment_email=suppress_assignment_email,
        )
    except Exception:  # pylint: disable=broad-except
        logger.exception('[BULK FORCED REDEMPTION] force_redeem failed for %s', forced_redemption_uuid)

    log_args = (
        getattr(forced_redemption, 'batch_uuid', None),
        forced_redemption_uuid,
        getattr(forced_redemption, 'subsidy_access_policy_id', None),
        getattr(forced_redemption, 'lms_user_id', None),
        getattr(forced_redemption, 'course_run_key', None),
        getattr(forced_redemption, 'content_price_cents', None),
    )
    if forced_redemption and forced_redemption.transaction_uuid:
        logger.info(
            '[BULK FORCED REDEMPTION] batch=%s REDEEMED forced_redemption=%s policy=%s '
            'lms_user_id=%s course_run_key=%s content_price_cents=%s transaction_uuid=%s',
            *log_args,
            forced_redemption.transaction_uuid,
        )
    else:
        logger.error(
            '[BULK FORCED REDEMPTION] batch=%s NOT_REDEEMED forced_redemption=%s policy=%s '
            'lms_user_id=%s course_run_key=%s content_price_cents=%s',
            *log_args,
        )
