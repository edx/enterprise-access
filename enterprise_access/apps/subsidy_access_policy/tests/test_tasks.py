"""
Tests for subsidy_access_policy tasks.
"""
from unittest import mock
from uuid import uuid4

import ddt
from django.test import TestCase
from edx_django_utils.cache import TieredCache
from edx_django_utils.cache.utils import DEFAULT_REQUEST_CACHE

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.cache_utils import request_cache

from ..api import force_redeem_with_geag_metadata
from ..exceptions import SubsidyAccessPolicyLockAttemptFailed, SubsidyAPIHTTPError
from ..models import REQUEST_CACHE_NAMESPACE, ForcedPolicyRedemption
from ..tasks import force_redeem_task
from .factories import ForcedPolicyRedemptionFactory, PerLearnerSpendCapLearnerCreditAccessPolicyFactory


@ddt.ddt
@mock.patch.object(ForcedPolicyRedemption, 'force_redeem', autospec=True)
class ForceRedeemTaskTests(TestCase):
    """
    Tests for ``force_redeem_task`` and ``force_redeem_with_geag_metadata``.
    """
    def setUp(self):
        super().setUp()
        self.forced_redemption = ForcedPolicyRedemptionFactory(
            subsidy_access_policy=PerLearnerSpendCapLearnerCreditAccessPolicyFactory(),
            content_price_cents=100,
        )

    def test_task_clears_request_caches(self, mock_force_redeem):
        request_cache(namespace=REQUEST_CACHE_NAMESPACE).set('stale-aggregates', 'value')
        TieredCache.set_all_tiers('stale-learner-record', None)

        force_redeem_task(
            forced_redemption_uuid=str(self.forced_redemption.uuid),
            geag_metadata={},
            actor_lms_user_id=1,
            suppress_assignment_email=False,
        )

        cached = request_cache(namespace=REQUEST_CACHE_NAMESPACE).get_cached_response('stale-aggregates')
        self.assertFalse(cached.is_found)
        self.assertFalse(DEFAULT_REQUEST_CACHE.get_cached_response('stale-learner-record').is_found)  # TieredCache
        mock_force_redeem.assert_called_once()

    def test_task_never_raises(self, mock_force_redeem):
        mock_force_redeem.side_effect = Exception('boom')
        actor = UserFactory(lms_user_id=1)

        force_redeem_task(
            forced_redemption_uuid=str(self.forced_redemption.uuid),
            geag_metadata={},
            actor_lms_user_id=1,
            suppress_assignment_email=False,
        )
        force_redeem_task(
            forced_redemption_uuid=str(uuid4()),  # missing record
            geag_metadata={},
            actor_lms_user_id=1,
            suppress_assignment_email=False,
        )

        self.forced_redemption.refresh_from_db()
        self.assertIn('boom', self.forced_redemption.traceback)
        # The history row for recording the failure is attributed to the admin who queued the batch.
        self.assertEqual(self.forced_redemption.history.first().history_user, actor)

    def test_helper_records_failure_and_reraises(self, mock_force_redeem):
        mock_force_redeem.side_effect = Exception('cannot redeem')

        with self.assertRaisesRegex(Exception, 'cannot redeem'):
            force_redeem_with_geag_metadata(
                forced_redemption=self.forced_redemption,
                geag_metadata={},
                actor_lms_user_id=1,
            )

        self.forced_redemption.refresh_from_db()
        self.assertIsNotNone(self.forced_redemption.errored_at)
        self.assertIn('cannot redeem', self.forced_redemption.traceback)

    @ddt.data(SubsidyAPIHTTPError, SubsidyAccessPolicyLockAttemptFailed)
    def test_helper_keeps_failure_recorded_by_force_redeem(self, exception_class, mock_force_redeem):
        mock_force_redeem.side_effect = exception_class('recorded by force_redeem')

        with self.assertRaises(exception_class):
            force_redeem_with_geag_metadata(
                forced_redemption=self.forced_redemption,
                geag_metadata={},
                actor_lms_user_id=1,
            )

        self.forced_redemption.refresh_from_db()
        self.assertIsNone(self.forced_redemption.errored_at)  # left to force_redeem(), which is mocked here
