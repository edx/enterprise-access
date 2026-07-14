"""
Tests for customer_billing tasks.
"""
import json
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import mock
from uuid import uuid4

import stripe
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import BRAZE_TIMESTAMP_FORMAT
from enterprise_access.apps.customer_billing.models import (
    CheckoutIntent,
    SspProduct,
    StripeEventData,
    StripeEventSummary
)
from enterprise_access.apps.customer_billing.tasks import (
    _build_common_trigger_properties,
    _send_cancelation_campaign,
    get_enterprise_admins,
    prepare_admin_braze_recipients,
    send_billing_error_email_task,
    send_enterprise_provision_signup_confirmation_email,
    send_finalized_cancelation_email_task,
    send_paid_cancellation_email_task,
    send_payment_receipt_email,
    send_reinstatement_email_task,
    send_trial_cancellation_email_task,
    send_trial_end_and_subscription_started_email_task,
    send_trial_ending_reminder_email_task
)
from enterprise_access.apps.customer_billing.tests.utils import AttrDict
from enterprise_access.utils import format_datetime_obj


class TestBuildCommonTriggerProperties(TestCase):
    """Tests for the _build_common_trigger_properties helper."""

    def test_provided_academy_name_is_used_without_db_lookup(self):
        """When academy_name is in extra kwargs, it is used directly."""
        ssp_product = SspProduct.objects.create(
            slug='essentials-ai',
            stripe_price_lookup_key='essentials-ai-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=uuid4(),
            is_active=True,
        )
        result = _build_common_trigger_properties(
            ssp_product=ssp_product,
            organization_name='Acme',
            academy_name='Provided Academy',
        )
        self.assertEqual(result['academy_name'], 'Provided Academy')

    def test_academy_name_resolved_from_db_when_not_provided(self):
        """When academy_name is absent, the SspProduct academy_title is used."""
        ssp_product = SspProduct.objects.create(
            slug='essentials-ai',
            stripe_price_lookup_key='essentials-ai-key',
            catalog_query_uuid=uuid4(),
            academy_uuid=uuid4(),
            is_active=True,
        )
        with mock.patch.object(SspProduct, 'academy_title', new_callable=mock.PropertyMock, return_value='DB Academy'):
            result = _build_common_trigger_properties(
                ssp_product=ssp_product,
                organization_name='Acme',
            )
        self.assertEqual(result['academy_name'], 'DB Academy')

    def test_none_values_are_filtered_from_extra_kwargs(self):
        """Extra kwargs with None values should be omitted from trigger properties."""
        result = _build_common_trigger_properties(
            organization_name='Acme',
            optional_value=None,
            included_value='included',
        )

        self.assertNotIn('optional_value', result)
        self.assertEqual(result['included_value'], 'included')


class TestBillingTaskHelpers(TestCase):
    """Tests for shared helper functions in customer billing tasks."""

    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    def test_get_enterprise_admins_raises_when_empty_and_required(self, mock_lms_client):
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {'admin_users': []}

        with self.assertRaisesRegex(Exception, 'No admin users'):
            get_enterprise_admins('test-enterprise', raise_if_empty=True)

    def test_prepare_admin_braze_recipients_raises_when_all_recipients_fail(self):
        braze_client = mock.Mock()
        braze_client.create_braze_recipient.side_effect = RuntimeError('braze fail')

        with self.assertRaisesRegex(Exception, 'No Braze recipients created'):
            prepare_admin_braze_recipients(
                braze_client,
                [{'email': 'admin@example.com', 'lms_user_id': 1}],
                'test-enterprise',
                raise_if_empty=True,
            )


class TestSendTrialCancellationEmailTask(TestCase):
    """Tests for send_trial_cancellation_email_task."""

    def setUp(self):
        """Set up test data."""
        self.user = UserFactory()
        self.checkout_intent = CheckoutIntent.create_intent(
            user=self.user,
            slug="test-enterprise",
            name="Test Enterprise",
            quantity=10,
        )
        self.checkout_intent.stripe_customer_id = "cus_test_123"
        self.checkout_intent.save()

        self.trial_end_datetime = datetime(2021, 1, 1)
        self.trial_end_timestamp = int(self.trial_end_datetime.timestamp())

        self.cancel_at_datetime = datetime(2021, 4, 1)
        self.cancel_at_timestamp = int(self.cancel_at_datetime.timestamp())

    @mock.patch(
        "enterprise_access.apps.customer_billing.tasks.BrazeApiClient"
    )
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_trial_cancellation_email_success(
        self, mock_lms_client, mock_braze_client
    ):
        """Test successful trial cancellation email send."""
        # Mock LMS response with admin users
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [
                {"email": "admin1@example.com", "lms_user_id": 123},
                {"email": "admin2@example.com", "lms_user_id": 456},
            ]
        }

        # Mock Braze client
        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.side_effect = [
            {"external_user_id": "123"},
            {"external_user_id": "456"},
        ]

        # Run the task
        send_trial_cancellation_email_task(
            checkout_intent_id=str(self.checkout_intent.id),
            cancel_at_timestamp=self.cancel_at_timestamp,
        )

        # Verify Braze campaign was sent
        mock_braze_instance.send_campaign_message.assert_called_once()
        call_args = mock_braze_instance.send_campaign_message.call_args

        # Check campaign ID
        self.assertEqual(
            call_args[0][0],
            settings.BRAZE_TRIAL_CANCELLATION_CAMPAIGN
        )

        # Check recipients
        recipients = call_args[1]["recipients"]
        self.assertEqual(len(recipients), 2)

        # Check trigger properties
        trigger_props = call_args[1]["trigger_properties"]
        self.assertIn("trial_end_date", trigger_props)
        self.assertIn("period_end_date", trigger_props)
        self.assertIn("restart_subscription_url", trigger_props)

    @mock.patch(
        "enterprise_access.apps.customer_billing.tasks.BrazeApiClient"
    )
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_trial_cancellation_email_braze_exception(
        self, mock_lms_client, mock_braze_client
    ):
        """Test that Braze API exception is raised and logged."""
        # Mock LMS response with admin users
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [
                {"email": "admin1@example.com", "lms_user_id": 123},
            ]
        }

        # Mock Braze client to raise exception when sending campaign
        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.return_value = {
            "external_user_id": "123"
        }
        mock_braze_instance.send_campaign_message.side_effect = Exception(
            "Braze API error"
        )

        # Run the task and expect exception to be raised
        with self.assertRaises(Exception) as context:
            send_trial_cancellation_email_task(
                checkout_intent_id=self.checkout_intent.id,
                cancel_at_timestamp=self.cancel_at_timestamp,
            )

        # Verify the exception message
        self.assertIn("Braze API error", str(context.exception))

    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_cancellation_includes_product_info_when_ssp_product_set(
        self, mock_lms_client, mock_braze_client
    ):
        """When checkout_intent has an ssp_product, product_slug and academy_name are in trigger props."""
        ssp_product = SspProduct.objects.create(
            slug='essentials-ai-2025',
            stripe_price_lookup_key='essentials_ai_2025_key',
            catalog_query_uuid=uuid4(),
            academy_uuid=uuid4(),
            is_active=True,
        )
        self.checkout_intent.ssp_product = ssp_product
        self.checkout_intent.save()

        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': [{'email': 'admin@example.com', 'lms_user_id': 1}]
        }
        mock_braze_client.return_value.create_braze_recipient.return_value = {'external_user_id': '1'}

        with mock.patch.object(SspProduct, 'academy_title', new_callable=mock.PropertyMock, return_value='AI Academy'):
            send_trial_cancellation_email_task(
                checkout_intent_id=self.checkout_intent.id,
                cancel_at_timestamp=self.cancel_at_timestamp,
            )

        call_kwargs = mock_braze_client.return_value.send_campaign_message.call_args[1]
        trigger_props = call_kwargs['trigger_properties']
        self.assertEqual(trigger_props.get('product_slug'), 'essentials-ai-2025')
        self.assertEqual(trigger_props.get('academy_name'), 'AI Academy')

    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_cancellation_without_academy_uuid_still_sends_without_academy_name(
        self,
        mock_lms_client,
        mock_braze_client,
    ):
        """Non-academy products should still send cancellation emails without academy metadata."""
        ssp_product = SspProduct.objects.create(
            slug='teams-cancellation-test',
            stripe_price_lookup_key='teams_cancellation_test_key',
            catalog_query_uuid=uuid4(),
            is_active=True,
        )
        self.checkout_intent.ssp_product = ssp_product
        self.checkout_intent.save()

        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': [{'email': 'admin@example.com', 'lms_user_id': 1}]
        }
        mock_braze_client.return_value.create_braze_recipient.return_value = {'external_user_id': '1'}

        send_trial_cancellation_email_task(
            checkout_intent_id=self.checkout_intent.id,
            cancel_at_timestamp=self.cancel_at_timestamp,
        )

        mock_braze_client.return_value.send_campaign_message.assert_called_once()
        trigger_props = mock_braze_client.return_value.send_campaign_message.call_args[1]['trigger_properties']
        self.assertEqual(trigger_props['product_slug'], 'teams-cancellation-test')
        self.assertEqual(trigger_props['product_type'], 'teams')
        self.assertNotIn('academy_name', trigger_props)

    @mock.patch('enterprise_access.apps.customer_billing.tasks._send_cancelation_campaign')
    @mock.patch('enterprise_access.apps.customer_billing.tasks._get_checkout_intent_with_product')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_campaign_id')
    def test_trial_cancellation_fetches_checkout_intent_once_and_passes_object(
        self,
        _mock_get_campaign_id,
        mock_get_checkout_intent,
        mock_send_cancelation_campaign,
    ):
        mock_checkout_intent = mock.Mock()
        mock_checkout_intent.ssp_product = mock.Mock()
        mock_get_checkout_intent.return_value = mock_checkout_intent

        send_trial_cancellation_email_task(
            checkout_intent_id=self.checkout_intent.id,
            cancel_at_timestamp=self.cancel_at_timestamp,
        )

        mock_get_checkout_intent.assert_called_once_with(self.checkout_intent.id)
        call_args = mock_send_cancelation_campaign.call_args[0]
        self.assertIs(call_args[0], mock_checkout_intent)


class TestSendBillingErrorEmailTask(TestCase):
    """Tests for send_billing_error_email_task."""

    def setUp(self):
        self.user = UserFactory()
        self.checkout_intent = CheckoutIntent.create_intent(
            user=self.user,
            slug="test-enterprise",
            name="Test Enterprise",
            quantity=10,
        )
        self.checkout_intent.stripe_customer_id = "cus_billing_err"
        self.checkout_intent.save()

    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_enterprise_admins')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.prepare_admin_braze_recipients')
    def test_send_billing_error_email_no_recipients_returns(self, mock_prepare, mock_get_admins, mock_braze):
        """If no recipients are prepared, task should return early without sending."""
        mock_get_admins.return_value = [{'email': 'admin@test.com'}]
        mock_prepare.return_value = []

        # Should return None / no exception
        send_billing_error_email_task(self.checkout_intent.id)

        mock_braze.return_value.send_campaign_message.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks._get_checkout_intent_with_product')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_enterprise_admins')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.prepare_admin_braze_recipients')
    def test_send_billing_error_uses_checkout_helper_and_non_raising_admin_lookup(
        self,
        mock_prepare,
        mock_get_admins,
        mock_get_checkout_intent,
        mock_braze,
    ):
        """Billing error task should fetch checkout intent via helper and use non-raising admin lookup."""
        mock_checkout_intent = mock.Mock()
        mock_checkout_intent.enterprise_slug = 'test-enterprise'
        mock_checkout_intent.enterprise_name = 'Test Enterprise'
        mock_checkout_intent.ssp_product = self.checkout_intent.ssp_product
        mock_get_checkout_intent.return_value = mock_checkout_intent

        mock_get_admins.return_value = [{'email': 'admin@test.com', 'lms_user_id': 1}]
        mock_prepare.return_value = []

        send_billing_error_email_task(self.checkout_intent.id)

        mock_get_checkout_intent.assert_called_once_with(self.checkout_intent.id)
        mock_get_admins.assert_called_once_with('test-enterprise', raise_if_empty=False)
        mock_braze.return_value.send_campaign_message.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_campaign_id')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_enterprise_admins')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.prepare_admin_braze_recipients')
    @override_settings(STRIPE_CUSTOMER_PORTAL_URL='https://customer.portal')
    def test_send_billing_error_email_sends_campaign(
        self,
        mock_prepare,
        mock_get_admins,
        mock_get_campaign,
        mock_braze,
    ):
        """When recipients exist, should call Braze with campaign from get_campaign_id and
        trigger properties.
        """
        mock_get_admins.return_value = [{'email': 'admin@test.com', 'lms_user_id': 1}]
        mock_recipient = {'external_id': 'braze_1'}
        mock_prepare.return_value = [mock_recipient]
        mock_get_campaign.return_value = 'campaign-uuid-123'

        send_billing_error_email_task(self.checkout_intent.id)

        mock_braze.return_value.send_campaign_message.assert_called_once()
        args, kwargs = mock_braze.return_value.send_campaign_message.call_args
        # campaign id is first positional arg
        self.assertEqual(args[0], 'campaign-uuid-123')
        # recipients passed through
        self.assertEqual(kwargs['recipients'], [mock_recipient])
        # trigger_properties should include enterprise_admin_portal_url and customer_portal_url
        tp = kwargs['trigger_properties']
        self.assertIn('enterprise_admin_portal_url', tp)
        self.assertIn('restart_subscription_url', tp)
        self.assertIn('customer_portal_url', tp)


class TestSendPaidCancellationEmailTask(TestCase):
    """Tests for send_paid_cancellation_email_task."""

    def setUp(self):
        """Set up test data."""
        self.user = UserFactory()
        self.checkout_intent = CheckoutIntent.create_intent(
            user=self.user,
            slug="test-enterprise",
            name="Test Enterprise",
            quantity=10,
        )
        self.checkout_intent.stripe_customer_id = "cus_test_123"
        self.checkout_intent.save()

        self.cancel_at_datetime = datetime(2025, 6, 1)
        self.cancel_at_timestamp = int(self.cancel_at_datetime.timestamp())

    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_paid_cancellation_email_success(self, mock_lms_client, mock_braze_client):
        """Test successful paid cancellation email send."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [
                {"email": "admin1@example.com", "lms_user_id": 123},
                {"email": "admin2@example.com", "lms_user_id": 456},
            ]
        }

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.side_effect = [
            {"external_user_id": "123"},
            {"external_user_id": "456"},
        ]

        send_paid_cancellation_email_task(
            checkout_intent_id=self.checkout_intent.id,
            cancel_at_timestamp=self.cancel_at_timestamp,
        )

        mock_braze_instance.send_campaign_message.assert_called_once()
        call_args = mock_braze_instance.send_campaign_message.call_args

        # Correct campaign
        self.assertEqual(call_args[0][0], settings.BRAZE_PAID_CANCELLATION_CAMPAIGN)

        # Two recipients
        self.assertEqual(len(call_args[1]["recipients"]), 2)

        # Correct trigger properties
        trigger_props = call_args[1]["trigger_properties"]
        self.assertIn("trial_end_date", trigger_props)
        self.assertIn("period_end_date", trigger_props)
        self.assertIn("restart_subscription_url", trigger_props)

    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_paid_cancellation_email_braze_exception(self, mock_lms_client, mock_braze_client):
        """Test that a Braze API exception propagates."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin1@example.com", "lms_user_id": 123}]
        }

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.return_value = {"external_user_id": "123"}
        mock_braze_instance.send_campaign_message.side_effect = Exception("Braze API error")

        with self.assertRaises(Exception) as context:
            send_paid_cancellation_email_task(
                checkout_intent_id=self.checkout_intent.id,
                cancel_at_timestamp=self.cancel_at_timestamp,
            )

        self.assertIn("Braze API error", str(context.exception))

    @mock.patch('enterprise_access.apps.customer_billing.tasks._send_cancelation_campaign')
    @mock.patch('enterprise_access.apps.customer_billing.tasks._get_checkout_intent_with_product')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_campaign_id')
    def test_paid_cancellation_fetches_checkout_intent_once_and_passes_object(
        self,
        _mock_get_campaign_id,
        mock_get_checkout_intent,
        mock_send_cancelation_campaign,
    ):
        mock_checkout_intent = mock.Mock()
        mock_checkout_intent.ssp_product = mock.Mock()
        mock_get_checkout_intent.return_value = mock_checkout_intent

        send_paid_cancellation_email_task(
            checkout_intent_id=self.checkout_intent.id,
            cancel_at_timestamp=self.cancel_at_timestamp,
        )

        mock_get_checkout_intent.assert_called_once_with(self.checkout_intent.id)
        call_args = mock_send_cancelation_campaign.call_args[0]
        self.assertIs(call_args[0], mock_checkout_intent)


class TestSendFinalizedCancelationEmailTask(TestCase):
    """Tests for send_finalized_cancelation_email_task."""

    def setUp(self):
        """Set up test data."""
        self.user = UserFactory()
        self.checkout_intent = CheckoutIntent.create_intent(
            user=self.user,
            slug="test-enterprise",
            name="Test Enterprise",
            quantity=10,
        )
        self.checkout_intent.stripe_customer_id = "cus_test_123"
        self.checkout_intent.save()

        self.trial_end_datetime = datetime(2021, 1, 1)
        self.trial_end_timestamp = int(self.trial_end_datetime.timestamp())

    @mock.patch(
        "enterprise_access.apps.customer_billing.tasks.BrazeApiClient"
    )
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_finalized_cancelation_email_success(
        self, mock_lms_client, mock_braze_client
    ):
        """Test successful finalized cancellation email send."""
        # Mock LMS response with admin users
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [
                {"email": "admin1@example.com", "lms_user_id": 123},
                {"email": "admin2@example.com", "lms_user_id": 456},
            ]
        }

        # Mock Braze client
        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.side_effect = [
            {"external_user_id": "123"},
            {"external_user_id": "456"},
        ]

        # Run the task
        send_finalized_cancelation_email_task(
            checkout_intent_id=str(self.checkout_intent.id),
            ended_at_timestamp=self.trial_end_timestamp,
        )

        # Verify Braze campaign was sent
        mock_braze_instance.send_campaign_message.assert_called_once()
        call_args = mock_braze_instance.send_campaign_message.call_args

        # Check campaign ID - should use the finalization campaign
        self.assertEqual(
            call_args[0][0], settings.BRAZE_SSP_CANCELATION_FINALIZATION_CAMPAIGN
        )

        # Check recipients
        recipients = call_args[1]["recipients"]
        self.assertEqual(len(recipients), 2)

        # Check trigger properties
        trigger_props = call_args[1]["trigger_properties"]
        self.assertIn("trial_end_date", trigger_props)
        self.assertIn("period_end_date", trigger_props)
        self.assertIn("restart_subscription_url", trigger_props)

    @mock.patch(
        "enterprise_access.apps.customer_billing.tasks.BrazeApiClient"
    )
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_finalized_cancelation_email_braze_exception(
        self, mock_lms_client, mock_braze_client
    ):
        """Test that Braze API exception is raised and logged."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [
                {"email": "admin1@example.com", "lms_user_id": 123},
            ]
        }

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.return_value = {
            "external_user_id": "123"
        }
        mock_braze_instance.send_campaign_message.side_effect = Exception(
            "Braze API error"
        )

        with self.assertRaises(Exception) as context:
            send_finalized_cancelation_email_task(
                checkout_intent_id=self.checkout_intent.id,
                ended_at_timestamp=self.trial_end_timestamp,
            )

        # Verify the exception message
        self.assertIn("Braze API error", str(context.exception))


class TestSendCancelationCampaignHelper(TestCase):
    """Tests for the _send_cancelation_campaign helper."""

    @mock.patch('enterprise_access.apps.customer_billing.tasks.send_campaign_message')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.prepare_admin_braze_recipients')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_enterprise_admins')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    def test_academy_title_evaluated_once(
        self,
        _mock_braze_client,
        mock_get_admins,
        mock_prepare_recipients,
        mock_send_campaign,
    ):
        mock_get_admins.return_value = [{'email': 'admin@example.com', 'lms_user_id': 1}]
        mock_prepare_recipients.return_value = [{'external_user_id': '1'}]

        ssp_product = mock.Mock()
        ssp_product.slug = 'essentials-once'
        ssp_product.academy_uuid = uuid4()
        academy_title_property = mock.PropertyMock(return_value='Academy Once')
        type(ssp_product).academy_title = academy_title_property

        checkout_intent = mock.Mock()
        checkout_intent.id = 42
        checkout_intent.enterprise_slug = 'test-enterprise'
        checkout_intent.enterprise_name = 'Test Enterprise'
        checkout_intent.ssp_product = ssp_product

        _send_cancelation_campaign(
            checkout_intent,
            ending_timestamp=int(datetime(2025, 6, 1).timestamp()),
            campaign_identifier='campaign-id',
            email_description='test cancellation email',
        )

        self.assertEqual(academy_title_property.call_count, 1)
        trigger_properties = mock_send_campaign.call_args.kwargs['trigger_properties']
        self.assertEqual(trigger_properties.get('academy_name'), 'Academy Once')
        self.assertIn('restart_subscription_url', trigger_properties)


class TestSendReinstatementEmailTask(TestCase):
    """Tests for send_reinstatement_email_task."""

    def setUp(self):
        """Set up test data."""
        self.user = UserFactory()
        self.checkout_intent = CheckoutIntent.create_intent(
            user=self.user,
            slug="test-enterprise",
            name="Test Enterprise",
            quantity=10,
        )
        self.checkout_intent.stripe_customer_id = "cus_test_123"
        self.checkout_intent.save()

    @mock.patch(
        "enterprise_access.apps.customer_billing.tasks.BrazeApiClient"
    )
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_reinstatement_email_success(
        self, mock_lms_client, mock_braze_client
    ):
        """Test successful reinstatement email send."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [
                {"email": "admin1@example.com", "lms_user_id": 123},
                {"email": "admin2@example.com", "lms_user_id": 456},
            ]
        }

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.side_effect = [
            {"external_user_id": "123"},
            {"external_user_id": "456"},
        ]

        send_reinstatement_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_braze_instance.send_campaign_message.assert_called_once()
        call_args = mock_braze_instance.send_campaign_message.call_args
        self.assertEqual(
            call_args[0][0], settings.BRAZE_SSP_SUBSCRIPTION_REINSTATED_CAMPAIGN
        )

        recipients = call_args[1]["recipients"]
        self.assertEqual(len(recipients), 2)

        trigger_props = call_args[1]["trigger_properties"]
        self.assertIn("enterprise_admin_portal_url", trigger_props)
        self.assertEqual(
            trigger_props["enterprise_admin_portal_url"],
            f'{settings.ENTERPRISE_ADMIN_PORTAL_URL}/test-enterprise'
        )

    @mock.patch(
        "enterprise_access.apps.customer_billing.tasks.BrazeApiClient"
    )
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_reinstatement_email_braze_exception(
        self, mock_lms_client, mock_braze_client
    ):
        """Test that Braze API exception is raised and logged."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [
                {"email": "admin1@example.com", "lms_user_id": 123},
            ]
        }

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.return_value = {
            "external_user_id": "123"
        }
        mock_braze_instance.send_campaign_message.side_effect = Exception(
            "Braze API error"
        )

        with self.assertRaises(Exception) as context:
            send_reinstatement_email_task(
                checkout_intent_id=self.checkout_intent.id,
            )

        self.assertIn("Braze API error", str(context.exception))

    @mock.patch(
        "enterprise_access.apps.customer_billing.tasks.BrazeApiClient"
    )
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_reinstatement_email_no_admin_users(
        self, mock_lms_client, mock_braze_client
    ):
        """Test that exception is raised when no admin users are found."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": []
        }

        with self.assertRaisesRegex(Exception, 'No admin users'):
            send_reinstatement_email_task(
                checkout_intent_id=self.checkout_intent.id,
            )

        mock_braze_client.return_value.send_campaign_message.assert_not_called()


class TestSendEnterpriseProvisionSignupConfirmationEmail(TestCase):
    """
    Tests for send_enterprise_provision_signup_confirmation_email task.
    """
    def setUp(self):
        super().setUp()
        self.trial_start = timezone.make_aware(datetime(2025, 1, 1))
        self.trial_end = timezone.make_aware(datetime(2026, 1, 1))
        self.test_data = {
            'subscription_start_date': self.trial_start,
            'subscription_end_date': self.trial_end,
            'number_of_licenses': 100,
            'activation_link': f"{settings.LMS_URL}/activate/some-activation-key",
            'organization_name': 'Test Corp',
            'enterprise_slug': 'test-corp',
        }
        self.mock_subscription = {
            'trial_start': int(self.trial_start.timestamp()),
            'trial_end': int(self.trial_end.timestamp()),
            'plan': {
                'amount': 10000  # $100.00 in cents
            }
        }
        self.mock_admin_users = [
            {
                'email': 'admin1@test.com',
                'lms_user_id': 1,
            },
            {
                'email': 'admin2@test.com',
                'lms_user_id': 2,
            }
        ]
        self.expected_braze_properties = {
            'subscription_start_date': 'Jan 01, 2025',
            'subscription_end_date': 'Jan 01, 2026',
            'number_of_licenses': 100,
            'organization': 'Test Corp',
            'activation_link': f"{settings.LMS_URL}/activate/some-activation-key",
            'enterprise_admin_portal_url': f'{settings.ENTERPRISE_ADMIN_PORTAL_URL}/test-corp/admin/subscriptions',
            'trial_start_datetime': format_datetime_obj(self.trial_start, output_pattern=BRAZE_TIMESTAMP_FORMAT),
            'trial_end_datetime': format_datetime_obj(self.trial_end, output_pattern=BRAZE_TIMESTAMP_FORMAT),
            'plan_amount': 100.00,
            'total_amount': 100.00 * 100,
        }

    @mock.patch('enterprise_access.apps.customer_billing.tasks.validate_trial_subscription')
    def test_no_valid_trial_subscription(self, mock_validate_trial):
        """
        Test that task exits early when no valid trial subscription exists.
        """
        mock_validate_trial.return_value = (False, None)
        send_enterprise_provision_signup_confirmation_email(**self.test_data)
        mock_validate_trial.assert_called_once_with(self.test_data['enterprise_slug'])

    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.validate_trial_subscription')
    def test_no_admin_users(self, mock_validate_trial, mock_lms_client, mock_braze_client):
        """
        Test that task exits when no admin users are found.
        """
        mock_validate_trial.return_value = (True, self.mock_subscription)
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': []
        }

        with self.assertRaisesRegex(Exception, 'No admin users'):
            send_enterprise_provision_signup_confirmation_email(**self.test_data)

        mock_validate_trial.assert_called_once_with(self.test_data['enterprise_slug'])
        mock_lms_client.return_value.get_enterprise_customer_data.assert_called_once_with(
            enterprise_customer_slug=self.test_data['enterprise_slug']
        )
        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.validate_trial_subscription')
    def test_successful_email_send(self, mock_validate_trial, mock_lms_client, mock_braze_client):
        """
        Test successful email sending to multiple admin users.
        """
        mock_validate_trial.return_value = (True, self.mock_subscription)
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': self.mock_admin_users
        }

        mock_braze = mock_braze_client.return_value
        braze_recipients = []
        actual_calls = []

        def create_recipient_side_effect(user_email, lms_user_id):
            actual_calls.append(mock.call(user_email=user_email, lms_user_id=lms_user_id))
            recipient = {'external_id': f'braze_{lms_user_id}'}
            braze_recipients.append(recipient)
            return recipient
        mock_braze.create_braze_recipient.side_effect = create_recipient_side_effect
        send_enterprise_provision_signup_confirmation_email(**self.test_data)
        expected_calls = [
            mock.call(user_email=admin['email'], lms_user_id=admin.get('lms_user_id'))
            for admin in self.mock_admin_users
        ]
        mock_validate_trial.assert_called_once_with(self.test_data['enterprise_slug'])
        mock_lms_client.return_value.get_enterprise_customer_data.assert_called_once_with(
            enterprise_customer_slug=self.test_data['enterprise_slug']
        )
        mock_braze.create_braze_recipient.assert_has_calls(expected_calls, any_order=True)
        self.assertEqual(mock_braze.create_braze_recipient.call_count, len(self.mock_admin_users))
        mock_braze.send_campaign_message.assert_called_once_with(
            settings.BRAZE_ENTERPRISE_PROVISION_SIGNUP_CONFIRMATION_CAMPAIGN,
            recipients=braze_recipients,
            trigger_properties=self.expected_braze_properties,
        )

    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.validate_trial_subscription')
    def test_braze_campaign_send_failure(self, mock_validate_trial, mock_lms_client, mock_braze_client):
        """
        Test that Braze campaign sending failures raise exceptions.
        """
        mock_validate_trial.return_value = (True, self.mock_subscription)
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': self.mock_admin_users
        }
        mock_braze = mock_braze_client.return_value
        mock_braze.create_braze_recipient.side_effect = [
            {'external_id': 'braze1'},
            {'external_id': 'braze2'},
        ]
        mock_braze.send_campaign_message.side_effect = Exception("Braze Campaign Error")
        with self.assertRaises(Exception) as context:
            send_enterprise_provision_signup_confirmation_email(**self.test_data)

        self.assertEqual(str(context.exception), "Braze Campaign Error")

    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.validate_trial_subscription')
    def test_uses_checkout_intent_ssp_product_when_present(
        self, mock_validate_trial, mock_lms_client, mock_braze_client
    ):
        """The task resolves the latest CheckoutIntent once and uses its SspProduct."""
        mock_validate_trial.return_value = (True, self.mock_subscription)
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': self.mock_admin_users
        }
        mock_braze = mock_braze_client.return_value
        mock_braze.create_braze_recipient.side_effect = [
            {'external_id': 'braze1'}, {'external_id': 'braze2'}
        ]

        ssp_product = SspProduct.objects.create(
            slug='essentials-signup-lookup',
            stripe_price_lookup_key='essentials_signup_lookup_key',
            catalog_query_uuid=uuid4(),
            is_active=True,
        )
        user = UserFactory()
        intent = CheckoutIntent.create_intent(user=user, slug='test-corp', name='Test Corp', quantity=1)
        intent.ssp_product = ssp_product
        intent.save()

        send_enterprise_provision_signup_confirmation_email(**self.test_data)

        mock_braze.send_campaign_message.assert_called_once()


class TestSendPaymentReceiptEmail(TestCase):
    """
    Tests for send_payment_receipt_email task.
    """
    def setUp(self):
        super().setUp()
        self.user = UserFactory(email='hello@world.com')
        self.checkout_intent = CheckoutIntent.create_intent(
            user=self.user,
            slug="test-enterprise",
            name="Test Enterprise",
            quantity=5,
        )
        self.checkout_intent.stripe_customer_id = "cus_test_123"
        self.checkout_intent.save()

        self.invoice_id = 'in_1SNvVOQ60jNALKNUMk8TZucs'
        self.payment_intent_id = 'pi_test_payment_intent_123'
        self.payment_method_id = 'pm_test_payment_method_456'
        self.mock_invoice_data = {
            'id': self.invoice_id,
            'created': 1761829387,
            'payment_intent': self.payment_intent_id,  # This is a string ID, not an object
        }
        self.mock_payment_intent = AttrDict.wrap({
            'id': self.payment_intent_id,
            'payment_method': self.payment_method_id,
        })
        self.mock_payment_method = AttrDict.wrap({
            'id': self.payment_method_id,
            'card': {
                'brand': 'visa',
                'last4': '4242'
            },
            'billing_details': {
                'name': 'Test User',
                'address': {
                    'line1': '123 Test St',
                    'line2': 'Suite 100',
                    'city': 'Test City',
                    'state': 'TS',
                    'postal_code': '12345',
                    'country': 'US'
                }
            }
        })
        self.mock_admin_users = [
            {
                'email': 'admin1@test.com',
                'lms_user_id': 1,
            },
            {
                'email': 'admin2@test.com',
                'lms_user_id': 2,
            }
        ]
        self.enterprise_customer_name = 'Test Enterprise'
        self.enterprise_slug = 'test-enterprise'

        # Create StripeEventData and StripeEventSummary for the invoice
        self.stripe_event_data = StripeEventData.objects.create(
            event_id="evt_test_payment_receipt",
            event_type="invoice.paid",
            checkout_intent=self.checkout_intent,
            data={'created': 1700000000},
        )
        # The post_save signal tries to auto-create StripeEventSummary but fails silently
        # when the event data lacks a Stripe object payload. Create it directly here.
        self.invoice_summary, _ = StripeEventSummary.objects.get_or_create(
            stripe_event_data=self.stripe_event_data,
            defaults={
                'event_id': self.stripe_event_data.event_id,
                'event_type': self.stripe_event_data.event_type,
                'stripe_event_created_at': datetime.fromtimestamp(1700000000, tz=dt_timezone.utc),
                'checkout_intent': self.checkout_intent,
                'stripe_invoice_id': self.invoice_id,
                'invoice_amount_paid': 198000,  # $1,980.00 (5 licenses * $396.00)
                'invoice_unit_amount': 39600,   # $396.00 per license
                'invoice_quantity': 5,
                'invoice_currency': 'usd',
            },
        )

    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_method')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_intent')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.format_datetime_obj')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    def test_successful_payment_receipt_email(
        self, mock_lms_client, mock_braze_client, mock_format_datetime,
        mock_get_payment_intent, mock_get_payment_method
    ):
        """
        Test successful payment receipt email sending.
        """
        # Mock the date formatting function
        mock_format_datetime.return_value = '03 November 2025'

        # Mock Stripe API calls
        mock_get_payment_intent.return_value = self.mock_payment_intent
        mock_get_payment_method.return_value = self.mock_payment_method

        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': self.mock_admin_users
        }

        mock_braze = mock_braze_client.return_value
        braze_recipients = []
        actual_calls = []

        def create_recipient_side_effect(user_email, lms_user_id):
            actual_calls.append(mock.call(user_email=user_email, lms_user_id=lms_user_id))
            recipient = {'external_id': f'braze_{lms_user_id}'}
            braze_recipients.append(recipient)
            return recipient
        mock_braze.create_braze_recipient.side_effect = create_recipient_side_effect

        # Call the task
        send_payment_receipt_email(
            invoice_id=self.invoice_id,
            invoice_data=self.mock_invoice_data,
            enterprise_customer_name=self.enterprise_customer_name,
            enterprise_slug=self.enterprise_slug,
        )

        # Verify Stripe API calls
        mock_get_payment_intent.assert_called_once_with(self.payment_intent_id)
        mock_get_payment_method.assert_called_once_with(self.payment_method_id)

        # Verify LMS API was called to get admin users
        mock_lms_client.return_value.get_enterprise_customer_data.assert_called_once_with(
            enterprise_customer_slug=self.enterprise_slug
        )

        # Verify Braze recipients were created for each admin
        expected_recipient_calls = [
            mock.call(user_email=admin['email'], lms_user_id=admin.get('lms_user_id'))
            for admin in self.mock_admin_users
        ]
        mock_braze.create_braze_recipient.assert_has_calls(expected_recipient_calls, any_order=True)

        # Verify the campaign was sent with correct properties
        # Note: total_paid_amount comes from invoice_summary.invoice_amount_paid (198000 cents = $1980.00)
        # price_per_license comes from invoice_summary.invoice_unit_amount (39600 cents = $396.00)
        expected_properties = {
            'product_slug': settings.SSP_DEFAULT_PRODUCT_SLUG,
            'product_key': settings.SSP_DEFAULT_PRODUCT_SLUG,
            'total_paid_amount': 1980.0,  # $396.00 * 5 licenses = $1,980.00
            'date_paid': '03 November 2025',  # Based on mock timestamp
            'payment_method': 'visa - 4242',
            'license_count': 5,
            'price_per_license': 396.0,
            'customer_name': 'Test User',
            'organization': 'Test Enterprise',
            'billing_address': '123 Test St\nSuite 100\nTest City, TS 12345\nUS',
            'enterprise_admin_portal_url': f'{settings.ENTERPRISE_ADMIN_PORTAL_URL}/test-enterprise',
            'receipt_number': 'in_1SNvVOQ60jNALKNUMk8TZucs',
        }

        mock_braze.send_campaign_message.assert_called_once_with(
            settings.BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN,
            recipients=braze_recipients,
            trigger_properties=expected_properties,
        )
        # ensure the actual properties are JSON-serializable
        actual_trigger_properties = mock_braze.send_campaign_message.call_args_list[0][1]['trigger_properties']
        json.dumps(actual_trigger_properties)

    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_method')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_intent')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    def test_payment_receipt_no_admin_users(
        self, mock_lms_client, mock_braze_client, mock_get_payment_intent, mock_get_payment_method
    ):
        """
        Test that exception is not raised when no admin users are found, and instead the email is
        sent to the email address of the CheckoutIntent user.
        """
        # Mock Stripe API calls
        mock_get_payment_intent.return_value = self.mock_payment_intent
        mock_get_payment_method.return_value = self.mock_payment_method

        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': []
        }
        mock_braze = mock_braze_client.return_value

        send_payment_receipt_email(
            invoice_id=self.invoice_id,
            invoice_data=self.mock_invoice_data,
            enterprise_customer_name=self.enterprise_customer_name,
            enterprise_slug=self.enterprise_slug,
        )

        mock_braze.create_braze_recipient.assert_called_once_with(
            user_email=self.checkout_intent.user.email,
            lms_user_id=self.checkout_intent.user.lms_user_id,
        )
        mock_lms_client.return_value.get_enterprise_customer_data.assert_called_once()
        mock_braze_client.return_value.send_campaign_message.assert_called_once_with(
            settings.BRAZE_ENTERPRISE_PROVISION_PAYMENT_RECEIPT_CAMPAIGN,
            recipients=[mock_braze.create_braze_recipient.return_value],
            trigger_properties=mock.ANY,
        )

    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_method')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_intent')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    def test_payment_receipt_braze_recipient_error(
        self, mock_lms_client, mock_braze_client, mock_get_payment_intent, mock_get_payment_method
    ):
        """
        Test handling of Braze recipient creation errors.
        """
        # Mock Stripe API calls
        mock_get_payment_intent.return_value = self.mock_payment_intent
        mock_get_payment_method.return_value = self.mock_payment_method

        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': self.mock_admin_users
        }

        # Make first recipient creation fail, second one succeed
        mock_braze = mock_braze_client.return_value
        mock_braze.create_braze_recipient.side_effect = [
            Exception("Failed to create recipient"),
            {'external_id': 'braze_2'}
        ]

        send_payment_receipt_email(
            invoice_id=self.invoice_id,
            invoice_data=self.mock_invoice_data,
            enterprise_customer_name=self.enterprise_customer_name,
            enterprise_slug=self.enterprise_slug,
        )

        # Verify campaign was still sent for the successful recipient
        mock_braze.send_campaign_message.assert_called_once()
        actual_recipients = mock_braze.send_campaign_message.call_args[1]['recipients']
        self.assertEqual(len(actual_recipients), 1)
        self.assertEqual(actual_recipients[0]['external_id'], 'braze_2')

    @mock.patch('enterprise_access.apps.customer_billing.tasks.prepare_admin_braze_recipients')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    def test_no_braze_recipients_returns_early(
        self, mock_lms_client, mock_braze_client, mock_prepare
    ):
        """When all recipient creations fail, prepare returns [] and task exits before sending."""
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': self.mock_admin_users
        }
        mock_prepare.return_value = []

        send_payment_receipt_email(
            invoice_id=self.invoice_id,
            invoice_data=self.mock_invoice_data,
            enterprise_customer_name=self.enterprise_customer_name,
            enterprise_slug=self.enterprise_slug,
        )

        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_method')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_intent')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    def test_payment_receipt_no_invoice_summary(
        self, mock_lms_client, mock_braze_client, mock_get_payment_intent, mock_get_payment_method
    ):
        """
        Test that email is not sent when no invoice summary is found.
        """
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': self.mock_admin_users
        }

        # Use a different invoice ID that doesn't have a summary
        send_payment_receipt_email(
            invoice_id='in_nonexistent_invoice',
            invoice_data=self.mock_invoice_data,
            enterprise_customer_name=self.enterprise_customer_name,
            enterprise_slug=self.enterprise_slug,
        )

        # Verify Braze campaign was not sent
        mock_braze_client.return_value.send_campaign_message.assert_not_called()
        # Verify Stripe API was not called since we exit early
        mock_get_payment_intent.assert_not_called()
        mock_get_payment_method.assert_not_called()

    @mock.patch('enterprise_access.apps.customer_billing.tasks.get_stripe_payment_intent')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.BrazeApiClient')
    @mock.patch('enterprise_access.apps.customer_billing.tasks.LmsApiClient')
    def test_payment_receipt_stripe_api_error(
        self, mock_lms_client, mock_braze_client, mock_get_payment_intent
    ):
        """
        Test that Stripe API errors are handled gracefully and email is sent with default values.
        """
        # Mock Stripe API to raise an error
        mock_get_payment_intent.side_effect = stripe.StripeError("Stripe API error")

        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': self.mock_admin_users
        }

        mock_braze = mock_braze_client.return_value
        mock_braze.create_braze_recipient.side_effect = [
            {'external_id': 'braze_1'},
            {'external_id': 'braze_2'}
        ]

        send_payment_receipt_email(
            invoice_id=self.invoice_id,
            invoice_data=self.mock_invoice_data,
            enterprise_customer_name=self.enterprise_customer_name,
            enterprise_slug=self.enterprise_slug,
        )

        # Verify the campaign was still sent with default payment method values
        mock_braze.send_campaign_message.assert_called_once()
        call_args = mock_braze.send_campaign_message.call_args
        trigger_props = call_args[1]['trigger_properties']

        # Payment method should fall back to default values
        self.assertEqual(trigger_props['payment_method'], 'Card - ****')
        self.assertEqual(trigger_props['customer_name'], '')
        self.assertEqual(trigger_props['billing_address'], '')

        # Other properties should still be populated from invoice summary
        self.assertEqual(trigger_props['total_paid_amount'], 1980.0)
        self.assertEqual(trigger_props['license_count'], 5)


class TestSendTrialEndingReminderEmailTask(TestCase):
    """Tests for send_trial_ending_reminder_email_task."""

    def setUp(self):
        """Set up test data."""
        self.user = UserFactory()
        self.checkout_intent = CheckoutIntent.create_intent(
            user=self.user,
            slug="test-enterprise",
            name="Test Enterprise",
            quantity=10,
        )
        self.checkout_intent.stripe_customer_id = "cus_test_123"
        self.checkout_intent.save()

        self.mock_subscription = mock.Mock(
            id="sub_test_123",
            default_payment_method="pm_test_456",
            latest_invoice="in_test_789",
        )
        self.mock_subscription.__getitem__ = mock.Mock(return_value=mock.Mock(
            data=[
                mock.Mock(
                    current_period_end=int(datetime(2022, 1, 1).timestamp()),
                    quantity=10,
                )
            ]
        ))

    @mock.patch("enterprise_access.apps.customer_billing.tasks.stripe.PaymentMethod.retrieve")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_send_trial_ending_reminder_email_success(
        self, mock_lms_client, mock_braze_client, mock_get_subscription, mock_payment_method
    ):
        """Test successful trial ending reminder email send."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [
                {"email": "admin1@example.com", "lms_user_id": 123},
                {"email": "admin2@example.com", "lms_user_id": 456},
            ]
        }

        mock_get_subscription.return_value = self.mock_subscription

        mock_payment_method.return_value = mock.Mock(
            type="card",
            card=mock.Mock(brand="visa", last4="4242"),
        )

        stripe_event_data = StripeEventData.objects.create(
            event_id="evt_test_123",
            event_type="customer.subscription.created",
            checkout_intent=self.checkout_intent,
            data={'created': 1700000000},
        )
        StripeEventSummary.objects.get_or_create(
            stripe_event_data=stripe_event_data,
            defaults={
                'event_id': stripe_event_data.event_id,
                'event_type': stripe_event_data.event_type,
                'stripe_event_created_at': datetime.fromtimestamp(1700000000, tz=dt_timezone.utc),
                'checkout_intent': self.checkout_intent,
                'stripe_subscription_id': "sub_test_123",
                'upcoming_invoice_amount_due': 633600,
            },
        )

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.side_effect = [
            {"external_user_id": "123"},
            {"external_user_id": "456"},
        ]

        send_trial_ending_reminder_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_braze_instance.send_campaign_message.assert_called_once()
        call_args = mock_braze_instance.send_campaign_message.call_args

        self.assertEqual(
            call_args[0][0], settings.BRAZE_ENTERPRISE_PROVISION_TRIAL_ENDING_SOON_CAMPAIGN
        )

        recipients = call_args[1]["recipients"]
        self.assertEqual(len(recipients), 2)

        trigger_props = call_args[1]["trigger_properties"]
        self.assertIn("renewal_datetime", trigger_props)
        self.assertEqual(
            trigger_props["renewal_datetime"],
            format_datetime_obj(datetime(2022, 1, 1), output_pattern=BRAZE_TIMESTAMP_FORMAT),
        )
        self.assertIn("subscription_management_url", trigger_props)
        self.assertEqual(trigger_props["license_count"], 10)
        self.assertEqual(trigger_props["payment_method"], "Visa ending in 4242")
        self.assertEqual(trigger_props["total_paid_amount"], "$6,336.00 USD")

    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_checkout_intent_not_found(self, mock_lms_client, mock_get_subscription):
        """Test handling of non-existent checkout intent."""
        send_trial_ending_reminder_email_task(checkout_intent_id=99999)

        mock_lms_client.return_value.get_enterprise_customer_data.assert_not_called()
        mock_get_subscription.assert_not_called()

    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_no_admin_users_found(
        self, mock_lms_client, mock_braze_client, mock_get_subscription
    ):
        """Test when no admin users are found."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": []
        }

        with self.assertRaisesRegex(Exception, 'No admin users'):
            send_trial_ending_reminder_email_task(
                checkout_intent_id=self.checkout_intent.id,
            )

        mock_get_subscription.assert_not_called()
        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.prepare_admin_braze_recipients")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_no_braze_recipients_returns_early(
        self, mock_lms_client, mock_braze_client, mock_prepare, mock_get_subscription
    ):
        """When prepare_admin_braze_recipients returns [], task exits before hitting Stripe."""
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            'admin_users': [{'email': 'admin@example.com', 'lms_user_id': 1}]
        }
        mock_prepare.return_value = []

        send_trial_ending_reminder_email_task(checkout_intent_id=self.checkout_intent.id)

        mock_get_subscription.assert_not_called()
        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_no_stripe_customer_id(
        self, mock_lms_client, mock_braze_client, mock_get_subscription
    ):
        """Test when checkout intent has no Stripe customer ID."""
        self.checkout_intent.stripe_customer_id = None
        self.checkout_intent.save()

        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin@example.com", "lms_user_id": 123}]
        }

        send_trial_ending_reminder_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_get_subscription.assert_not_called()
        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_no_trialing_subscription_found(
        self, mock_lms_client, mock_braze_client, mock_get_subscription
    ):
        """Test when no trialing subscription is found."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin@example.com", "lms_user_id": 123}]
        }

        mock_get_subscription.return_value = None

        send_trial_ending_reminder_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_subscription_has_no_items(
        self, mock_lms_client, mock_braze_client, mock_get_subscription
    ):
        """Test when subscription has no items."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin@example.com", "lms_user_id": 123}]
        }

        mock_subscription = mock.Mock(id="sub_test_123")
        mock_subscription.__getitem__ = mock.Mock(return_value=mock.Mock(data=[]))
        mock_get_subscription.return_value = mock_subscription

        send_trial_ending_reminder_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch("enterprise_access.apps.customer_billing.tasks.stripe.PaymentMethod.retrieve")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_no_payment_method(
        self, mock_lms_client, mock_braze_client, mock_get_subscription, mock_payment_method
    ):
        """Test when subscription has no payment method."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin@example.com", "lms_user_id": 123}]
        }

        mock_subscription = mock.Mock(
            id="sub_test_123",
            default_payment_method=None,
            latest_invoice=None,
        )
        mock_subscription.__getitem__ = mock.Mock(return_value=mock.Mock(
            data=[
                mock.Mock(
                    current_period_end=1640995200,
                    quantity=10,
                )
            ]
        ))
        mock_get_subscription.return_value = mock_subscription

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.return_value = {
            "external_user_id": "123"
        }

        send_trial_ending_reminder_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_payment_method.assert_not_called()
        mock_braze_instance.send_campaign_message.assert_called_once()
        call_args = mock_braze_instance.send_campaign_message.call_args
        trigger_props = call_args[1]["trigger_properties"]
        self.assertEqual(trigger_props["payment_method"], "")
        self.assertEqual(trigger_props["total_paid_amount"], "$0.00 USD")

    @mock.patch("enterprise_access.apps.customer_billing.tasks.stripe.PaymentMethod.retrieve")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_stripe_error_during_subscription_retrieval(
        self, mock_lms_client, mock_braze_client, mock_get_subscription, _mock_payment_method
    ):
        """Test handling of Stripe API errors."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin@example.com", "lms_user_id": 123}]
        }

        mock_get_subscription.side_effect = stripe.StripeError("API error")

        send_trial_ending_reminder_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch("enterprise_access.apps.customer_billing.tasks.stripe.PaymentMethod.retrieve")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_generic_exception_during_subscription_retrieval(
        self, mock_lms_client, mock_braze_client, mock_get_subscription, mock_payment_method
    ):
        """Non-Stripe exceptions during subscription detail assembly are swallowed."""
        mock_lms_client.return_value.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin@example.com", "lms_user_id": 123}]
        }
        mock_get_subscription.return_value = self.mock_subscription
        mock_payment_method.side_effect = RuntimeError('unexpected failure')

        send_trial_ending_reminder_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_braze_client.return_value.send_campaign_message.assert_not_called()

    @mock.patch("enterprise_access.apps.customer_billing.tasks.stripe.PaymentMethod.retrieve")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_braze_exception(
        self, mock_lms_client, mock_braze_client, mock_get_subscription, mock_payment_method
    ):
        """Test that Braze API exception is raised and logged."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin@example.com", "lms_user_id": 123}]
        }

        mock_get_subscription.return_value = self.mock_subscription

        mock_payment_method.return_value = mock.Mock(
            type="card",
            card=mock.Mock(brand="mastercard", last4="5555"),
        )

        stripe_event_data = StripeEventData.objects.create(
            event_id="evt_test_456",
            event_type="invoice.paid",
            checkout_intent=self.checkout_intent,
            data={'created': 1700000000},
        )
        StripeEventSummary.objects.get_or_create(
            stripe_event_data=stripe_event_data,
            defaults={
                'event_id': stripe_event_data.event_id,
                'event_type': stripe_event_data.event_type,
                'stripe_event_created_at': datetime.fromtimestamp(1700000000, tz=dt_timezone.utc),
                'checkout_intent': self.checkout_intent,
                'stripe_invoice_id': "in_test_789",
                'invoice_amount_paid': 100000,
            },
        )

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.return_value = {
            "external_user_id": "123"
        }
        mock_braze_instance.send_campaign_message.side_effect = Exception(
            "Braze API error"
        )

        with self.assertRaises(Exception) as context:
            send_trial_ending_reminder_email_task(
                checkout_intent_id=self.checkout_intent.id,
            )

        self.assertIn("Braze API error", str(context.exception))

    @mock.patch("enterprise_access.apps.customer_billing.tasks.stripe.PaymentMethod.retrieve")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_trialing_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_no_invoice_summary_found(
        self, mock_lms_client, mock_braze_client, mock_get_subscription, mock_payment_method
    ):
        """Test when no invoice summary is found in database."""
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            "admin_users": [{"email": "admin@example.com", "lms_user_id": 123}]
        }

        mock_get_subscription.return_value = self.mock_subscription

        mock_payment_method.return_value = mock.Mock(
            type="card",
            card=mock.Mock(brand="amex", last4="0005"),
        )

        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.return_value = {
            "external_user_id": "123"
        }

        send_trial_ending_reminder_email_task(
            checkout_intent_id=self.checkout_intent.id,
        )

        mock_braze_instance.send_campaign_message.assert_called_once()
        call_args = mock_braze_instance.send_campaign_message.call_args
        trigger_props = call_args[1]["trigger_properties"]
        self.assertEqual(trigger_props["total_paid_amount"], "$0.00 USD")


class TestSendTrialEndAndSubscriptionStartedEmailTask(TestCase):
    """
    Tests for send_trial_end_and_subscription_started_email_task.
    """

    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.CheckoutIntent")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_success_sends_to_all_admins(
        self,
        mock_lms_client,
        mock_braze_client,
        mock_checkout_intent,
        mock_get_stripe_subscription
    ):
        subscription = AttrDict.wrap({
            'id': 'sub_123',
            'quantity': 5,
            'plan': {'amount': 10000},
            'items': {
                "data": [
                    {
                        'current_period_start': 1762273481,  # 05 Dec 2025
                        'current_period_end': 1793809481,  # 05 Dec 2026
                    }
                ]
            },
            'latest_invoice': {'hosted_invoice_url': 'https://invoice.url'},
        })
        checkout_intent_obj = mock.Mock()
        checkout_intent_obj.enterprise_name = 'Test Org'
        checkout_intent_obj.enterprise_slug = 'test-org'
        checkout_intent_obj.ssp_product = mock.Mock(
            slug='teams-yearly',
            academy_uuid=None,
            academy_title=None,
        )
        mock_checkout_intent.objects.select_related.return_value.get.return_value = checkout_intent_obj
        mock_get_stripe_subscription.return_value = subscription
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {
            'admin_users': [
                {'email': 'admin1@test.com'},
                {'email': 'admin2@test.com'},
            ]
        }
        mock_braze_instance = mock_braze_client.return_value
        mock_braze_instance.create_braze_recipient.side_effect = [
            {'external_user_id': '1'},
            {'external_user_id': '2'},
        ]
        send_trial_end_and_subscription_started_email_task('sub_123', 1)
        assert mock_braze_instance.send_campaign_message.called
        args, kwargs = mock_braze_instance.send_campaign_message.call_args
        assert args[0] == settings.BRAZE_ENTERPRISE_PROVISION_TRIAL_END_SUBSCRIPTION_STARTED_CAMPAIGN
        assert len(kwargs['recipients']) == 2
        props = kwargs['trigger_properties']
        assert props['total_license'] == 5
        assert props['billing_amount'] == '100'
        assert 'subscription_start_period' in props
        assert 'subscription_end_period' in props
        assert 'next_payment_date' in props
        assert props['organization'] == 'Test Org'
        assert props['invoice_url'] == 'https://invoice.url'

    @mock.patch("enterprise_access.apps.customer_billing.tasks.get_stripe_subscription")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.CheckoutIntent")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.BrazeApiClient")
    @mock.patch("enterprise_access.apps.customer_billing.tasks.LmsApiClient")
    def test_no_admins_logs_and_returns(
        self,
        mock_lms_client,
        mock_braze_client,
        mock_checkout_intent,
        mock_get_stripe_subscription
    ):
        subscription = AttrDict.wrap({
            'id': 'sub_123',
            'quantity': 5,
            'plan': {'amount': 10000},
            'items': {
                "data": [
                    {
                        'current_period_start': 1762273481,  # 05 Dec 2025
                        'current_period_end': 1793809481,  # 05 Dec 2026
                    }
                ]
            },
            'latest_invoice': {'hosted_invoice_url': 'https://invoice.url'},
        })
        checkout_intent_obj = mock.Mock()
        checkout_intent_obj.enterprise_name = 'Test Org'
        checkout_intent_obj.enterprise_slug = 'test-org'
        mock_checkout_intent.objects.get.return_value = checkout_intent_obj
        mock_get_stripe_subscription.return_value = subscription
        mock_lms_instance = mock_lms_client.return_value
        mock_lms_instance.get_enterprise_customer_data.return_value = {'admin_users': []}

        with self.assertRaisesRegex(Exception, 'No admin users'):
            send_trial_end_and_subscription_started_email_task('sub_123', 1)

        assert not mock_braze_client.return_value.send_campaign_message.called
