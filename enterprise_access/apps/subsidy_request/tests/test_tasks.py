"""
Tests for learner credit decline notification task.
"""
from unittest import mock
from uuid import uuid4

from django.apps import apps
from django.conf import settings
from django.test import TestCase

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.subsidy_request.tasks import send_learner_credit_bnr_decline_notification_task


class TestSendLearnerCreditBNRDeclineNotificationTask(TestCase):
    """Tests for send_learner_credit_bnr_decline_notification_task."""

    def setUp(self):
        """Set up test data."""
        self.user = UserFactory()
        subsidy_model = apps.get_model('subsidy_request.LearnerCreditRequest')
        self.enterprise_customer_uuid = uuid4()
        self.learner_credit_request = subsidy_model.objects.create(
            user=self.user,
            enterprise_customer_uuid=self.enterprise_customer_uuid,
            course_title="Test Course",
            course_partners=[{"uuid": str(uuid4()), "name": "Test Partner"}],
            decline_reason="Budget constraints",
        )

    @mock.patch("enterprise_access.apps.subsidy_request.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.subsidy_request.tasks.LmsApiClient")
    def test_send_decline_notification_success(self, mock_lms_client, mock_braze_client):
        """Test successful decline notification email send via Braze."""
        # Mock LMS client to return enterprise data
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "name": "Test Enterprise",
            "slug": "test-enterprise",
            "admin_users": [
                {"email": "admin1@example.com"},
                {"email": "admin2@example.com"},
            ],
        }

        # Mock Braze client recipient creation
        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_recipient.return_value = {"external_user_id": "999"}
        mock_braze_instance.generate_mailto_link.return_value = "mailto:admin1@example.com,admin2@example.com"

        # Call the task
        send_learner_credit_bnr_decline_notification_task(self.learner_credit_request.uuid)

        # Assert Braze send_campaign_message was called with correct arguments
        mock_braze_instance.send_campaign_message.assert_called_once()
        call_args = mock_braze_instance.send_campaign_message.call_args

        # Campaign UUID should match settings
        self.assertEqual(call_args[0][0], settings.BRAZE_LEARNER_CREDIT_BNR_DECLINE_NOTIFICATION_CAMPAIGN)

        recipients = call_args[1]["recipients"]
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0]["external_user_id"], "999")

        trigger_props = call_args[1]["trigger_properties"]
        self.assertEqual(trigger_props["organization"], "Test Enterprise")
        self.assertEqual(trigger_props["course_title"], "Test Course")
        self.assertEqual(trigger_props["decline_reason"], "Budget constraints")
        self.assertIn("enterprise_dashboard_url", trigger_props)
        self.assertIn("contact_admin_link", trigger_props)
