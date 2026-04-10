"""
Supporting models for subsidy_access_policy.

These models are separated from the main models.py to reduce file size
while maintaining backwards compatibility through re-exports in models.py.
"""
import logging
from uuid import uuid4

from django.db import models
from django_extensions.db.models import TimeStampedModel
from simple_history.models import HistoricalRecords

from enterprise_access.apps.content_assignments import api as assignments_api
from enterprise_access.utils import format_traceback, localized_utcnow

from .constants import FORCE_ENROLLMENT_KEYWORD, AccessMethods
from .content_metadata_api import get_and_cache_content_metadata
from .exceptions import SubsidyAccessPolicyLockAttemptFailed, SubsidyAPIHTTPError

logger = logging.getLogger(__name__)


class PolicyGroupAssociation(TimeStampedModel):
    """
    This model ties together a policy (SubsidyAccessPolicy) and a group (EnterpriseGroup in edx-enterprise).

    .. no_pii: This model has no PII
    """

    class Meta:
        unique_together = [
            ('subsidy_access_policy', 'enterprise_group_uuid'),
        ]

    subsidy_access_policy = models.ForeignKey(
        'subsidy_access_policy.SubsidyAccessPolicy',
        related_name="groups",
        on_delete=models.CASCADE,
        null=False,
        help_text="The SubsidyAccessPolicy that this group is tied to.",
    )

    enterprise_group_uuid = models.UUIDField(
        editable=True,
        unique=False,
        null=True,
        blank=True,
        help_text='The uuid that uniquely identifies the associated group.',
    )


class ForcedPolicyRedemption(TimeStampedModel):
    """
    There is frequently a need to force through a redemption
    (and related enrollment/fulfillment) of a particular learner,
    covered by a particular subsidy access policy, into some specific course run.
    This needs exists for reasons related to upstream business constraints,
    notably in cases where a course is included in a policy's catalog,
    but the desired course run is not discoverable due to the
    current state of its metadata. This model supports executing such a redemption.

    .. no_pii: This model has no PII
    """
    uuid = models.UUIDField(
        primary_key=True,
        default=uuid4,
        editable=False,
        unique=True,
        help_text='The uuid that uniquely identifies this policy record.',
    )
    subsidy_access_policy = models.ForeignKey(
        'subsidy_access_policy.SubsidyAccessPolicy',
        related_name="forced_redemptions",
        on_delete=models.SET_NULL,
        null=True,
        help_text="The SubsidyAccessPolicy that this forced redemption relates to.",
    )
    lms_user_id = models.IntegerField(
        null=False,
        blank=False,
        db_index=True,
        help_text=(
            "The id of the Open edX LMS user record that identifies the learner.",
        ),
    )
    course_run_key = models.CharField(
        max_length=255,
        blank=False,
        null=False,
        db_index=True,
        help_text=(
            "The course run key to enroll the learner into.",
        ),
    )
    content_price_cents = models.BigIntegerField(
        null=False,
        blank=False,
        help_text="Cost of the content in USD Cents, should be >= 0.",
    )
    wait_to_redeem = models.BooleanField(
        default=False,
        help_text="If selected, will not force redemption when the record is saved via Django admin.",
    )
    redeemed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The time the forced redemption succeeded.",
    )
    errored_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The time the forced redemption failed.",
    )
    traceback = models.TextField(
        blank=True,
        null=True,
        editable=False,
        help_text="Any traceback we recorded when an error was encountered.",
    )
    transaction_uuid = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        db_index=True,
        help_text=(
            "The transaction uuid caused by successful redemption.",
        ),
    )
    history = HistoricalRecords()

    @property
    def policy_uuid(self):
        """
        Convenience property used by this model's Admin class.
        """
        return self.subsidy_access_policy.uuid

    def __str__(self):
        return (
            f'<{self.__class__.__name__} policy_uuid={self.subsidy_access_policy.uuid}, '
            f'transaction_uuid={self.transaction_uuid}, '
            f'lms_user_id={self.lms_user_id}, course_run_key={self.course_run_key}>'
        )

    def create_assignment(self):
        """
        For assignment-based policies, an allocated ``LearnerContentAssignment`` must exist
        before redemption can occur.
        """
        assignment_configuration = self.subsidy_access_policy.assignment_configuration
        # Ensure that the requested content key is available for the related customer.
        _ = get_and_cache_content_metadata(
            assignment_configuration.enterprise_customer_uuid,
            self.course_run_key,
        )

        client = self.subsidy_access_policy.lms_api_client
        ecu_record = client.get_enterprise_user(
            self.subsidy_access_policy.enterprise_customer_uuid,
            self.lms_user_id,
        )
        if not ecu_record:
            raise Exception(f'No ECU record could be found for lms_user_id {self.lms_user_id}')

        user_email = ecu_record.get('user', {}).get('email')
        if not user_email:
            raise Exception(f'No email could be found for lms_user_id {self.lms_user_id}')

        return assignments_api.allocate_assignments(
            assignment_configuration,
            [user_email],
            self.course_run_key,
            self.content_price_cents,
            known_lms_user_ids=[self.lms_user_id],
        )

    def force_redeem(self, extra_metadata=None):
        """
        Forces redemption for the requested course run key in the associated policy.
        """
        if self.redeemed_at and self.transaction_uuid:
            # Just return if we've already got a successful redemption.
            return

        if self.subsidy_access_policy.access_method == AccessMethods.ASSIGNED:
            self.create_assignment()

        try:
            with self.subsidy_access_policy.lock():
                can_redeem, reason, existing_transactions = self.subsidy_access_policy.can_redeem(
                    self.lms_user_id, self.course_run_key, skip_enrollment_deadline_check=True,
                )
                extra_metadata = extra_metadata or {}
                if can_redeem:
                    result = self.subsidy_access_policy.redeem(
                        self.lms_user_id,
                        self.course_run_key,
                        existing_transactions,
                        metadata={
                            FORCE_ENROLLMENT_KEYWORD: True,
                            **extra_metadata,
                        },
                    )
                    self.transaction_uuid = result['uuid']
                    self.redeemed_at = result['modified']
                    self.save()
                else:
                    raise Exception(f'Failed forced redemption: {reason}')
        except SubsidyAccessPolicyLockAttemptFailed as exc:
            logger.exception(exc)
            self.errored_at = localized_utcnow()
            self.traceback = format_traceback(exc)
            self.save()
            raise
        except SubsidyAPIHTTPError as exc:
            error_payload = exc.error_payload()
            self.errored_at = localized_utcnow()
            self.traceback = format_traceback(exc) + f'\nResponse payload:\n{error_payload}'
            self.save()
            logger.exception(f'{exc} when creating transaction in subsidy API: {error_payload}')
            raise
