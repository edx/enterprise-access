"""
Tests for the provisioning.models module.
"""
from datetime import datetime, timezone
from unittest import mock
from unittest.mock import patch
from uuid import uuid4

import ddt
from django.conf import settings
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from enterprise_access.apps.api_client.license_manager_client import LicenseManagerApiClient
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.customer_billing.constants import CheckoutIntentState
from enterprise_access.apps.customer_billing.models import (
    CheckoutIntent,
    SelfServiceSubscriptionRenewal,
    SspProduct,
    StripeEventSummary
)
from enterprise_access.apps.customer_billing.tests.factories import (
    CheckoutIntentFactory,
    StripeEventDataFactory,
    StripeEventSummaryFactory
)
from enterprise_access.apps.provisioning import models as prov_models
from enterprise_access.apps.provisioning.models import (
    AssociateAcademyStep,
    GetCreateCatalogStep,
    GetCreateCatalogStepInput,
    GetCreateCustomerAgreementStep,
    GetCreateFirstPaidSubscriptionPlanStepInput,
    GetCreateSubscriptionPlanRenewalStep,
    GetCreateTrialSubscriptionPlanStepInput,
    NotificationStep,
    ProvisionNewCustomerWorkflow
)
from enterprise_access.apps.provisioning.tests.factories import ProvisionNewCustomerWorkflowFactory


class TestAssociateAcademyStep(TestCase):
    """
    Tests for the AssociateAcademyStep model.
    """

    def setUp(self):
        self.workflow = ProvisionNewCustomerWorkflowFactory()
        self.step = AssociateAcademyStep.objects.create(
            workflow_record_uuid=self.workflow.uuid,
            input_data={},
        )

    @mock.patch('enterprise_access.apps.provisioning.models.associate_academy_with_catalog')
    def test_process_input_skips_when_no_academy_uuid(self, mock_associate):
        accumulated_output = mock.Mock()
        accumulated_output.create_catalog_output = mock.Mock(uuid=uuid4())

        result = self.step.process_input(accumulated_output)

        self.assertIsNone(result.academy_uuid)
        self.assertEqual(result.enterprise_catalog_uuid, accumulated_output.create_catalog_output.uuid)
        mock_associate.assert_not_called()

    @mock.patch('enterprise_access.apps.provisioning.models.associate_academy_with_catalog')
    def test_process_input_associates_when_academy_uuid_present(self, mock_associate):
        academy_uuid = uuid4()
        catalog_uuid = uuid4()
        self.step.input_data = {'academy_uuid': str(academy_uuid)}

        accumulated_output = mock.Mock()
        accumulated_output.create_catalog_output = mock.Mock(uuid=catalog_uuid)

        result = self.step.process_input(accumulated_output)

        self.assertEqual(result.academy_uuid, academy_uuid)
        self.assertEqual(result.enterprise_catalog_uuid, catalog_uuid)
        mock_associate.assert_called_once_with(
            academy_uuid=str(academy_uuid),
            enterprise_catalog_uuid=str(catalog_uuid),
        )

    def test_get_workflow_record(self):
        self.assertEqual(self.step.get_workflow_record(), self.workflow)

    def test_get_preceding_step_record(self):
        catalog_step = GetCreateCatalogStep.objects.create(
            workflow_record_uuid=self.workflow.uuid,
            input_data={},
            output_data={},
        )
        self.step.preceding_step_uuid = catalog_step.uuid

        self.assertEqual(self.step.get_preceding_step_record(), catalog_step)


class TestProvisionNewCustomerWorkflow(TestCase):
    """
    Tests for workflow step ordering.
    """

    def test_step_order_includes_associate_academy_after_catalog(self):
        steps = list(ProvisionNewCustomerWorkflow.steps)

        self.assertLess(steps.index(GetCreateCatalogStep), steps.index(AssociateAcademyStep))
        self.assertLess(steps.index(AssociateAcademyStep), steps.index(GetCreateCustomerAgreementStep))

    def test_customer_agreement_preceding_step_resolves_to_associate_academy(self):
        workflow = ProvisionNewCustomerWorkflowFactory()
        associate_step = AssociateAcademyStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            input_data={},
            output_data={},
        )
        agreement_step = GetCreateCustomerAgreementStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            preceding_step_uuid=associate_step.uuid,
            input_data={},
            output_data={},
        )

        self.assertEqual(agreement_step.get_preceding_step_record(), associate_step)

    def test_customer_agreement_preceding_step_falls_back_to_catalog(self):
        workflow = ProvisionNewCustomerWorkflowFactory()
        catalog_step = GetCreateCatalogStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            input_data={},
            output_data={},
        )
        agreement_step = GetCreateCustomerAgreementStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            preceding_step_uuid=catalog_step.uuid,
            input_data={},
            output_data={},
        )

        self.assertEqual(agreement_step.get_preceding_step_record(), catalog_step)

    def test_workflow_get_associate_academy_step_and_output_dict(self):
        workflow = ProvisionNewCustomerWorkflowFactory.create_complete_workflow()
        associate_step = AssociateAcademyStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            input_data={},
            output_data={
                'academy_uuid': None,
                'enterprise_catalog_uuid': str(uuid4()),
            },
        )

        self.assertEqual(workflow.get_associate_academy_step(), associate_step)
        self.assertEqual(
            workflow.associate_academy_output_dict(),
            workflow.output_data['associate_academy_output'],
        )


class TestGetCreateSubscriptionPlanRenewalStep(TestCase):
    """
    Tests for the GetCreateSubscriptionPlanRenewalStep model and its renewal record creation.
    """

    def setUp(self):
        """Set up test data."""
        self.user = UserFactory()
        self.workflow = ProvisionNewCustomerWorkflowFactory()
        self.checkout_intent = CheckoutIntentFactory(user=self.user)

        # Link the checkout intent to the workflow
        self.checkout_intent.workflow = self.workflow
        self.checkout_intent.save()

        self.renewal_step = GetCreateSubscriptionPlanRenewalStep.objects.create(
            workflow_record_uuid=self.workflow.uuid,
            input_data={
                'title': 'Test Renewal Plan',
                'salesforce_opportunity_line_item': 'test-oli-456',
                'start_date': '2025-01-01T00:00:00Z',
                'expiration_date': '2026-01-01T00:00:00Z',
                'desired_num_licenses': 10,
            }
        )

    def tearDown(self):
        """Clean up test data."""
        SelfServiceSubscriptionRenewal.objects.all().delete()
        StripeEventSummary.objects.all().delete()
        CheckoutIntent.objects.all().delete()

    @mock.patch.object(LicenseManagerApiClient, 'create_subscription_plan_renewal')
    def test_process_input_creates_renewal_record(self, mock_create_renewal):
        """Test that processing input creates a SelfServiceSubscriptionRenewal record."""
        mock_renewal_response = {
            'id': 123,
            'title': 'Test Renewal Plan',
            'created': '2024-01-15T10:30:00Z',
            'start_date': '2025-01-01T00:00:00Z',
            'expiration_date': '2026-01-01T00:00:00Z',
            'salesforce_opportunity_id': 'test-oli-456',
            'prior_subscription_plan': str(uuid4()),
            'renewed_subscription_plan': str(uuid4()),
            'number_of_licenses': 10,
            'effective_date': '2025-01-01T00:00:00Z',
            'renewed_expiration_date': '2027-01-01T00:00:00Z',
        }
        mock_create_renewal.return_value = mock_renewal_response

        # Create an existing StripeEventSummary to provide stripe_subscription_id
        stripe_subscription_id = 'sub_test_12345'
        event_data = StripeEventDataFactory.create(checkout_intent=self.checkout_intent)
        summary = StripeEventSummaryFactory.create(
            stripe_event_data=event_data,
        )
        summary.stripe_subscription_id = stripe_subscription_id
        summary.save()

        # Create mock accumulated_output with the required structure
        mock_accumulated_output = mock.Mock()
        trial_expiration = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mock_accumulated_output.create_trial_subscription_plan_output = mock.Mock(
            uuid=mock_renewal_response['prior_subscription_plan'],
            expiration_date=trial_expiration,
        )
        mock_accumulated_output.create_first_paid_subscription_plan_output = mock.Mock(
            uuid=mock_renewal_response['renewed_subscription_plan'],
            expiration_date=datetime(2027, 1, 1),
        )

        # Process the input
        result = self.renewal_step.process_input(mock_accumulated_output)

        # Verify the license manager API was called
        mock_create_renewal.assert_called_once()

        # Verify the response matches the mock
        result_dict = result.to_dict()
        self.assertEqual(result_dict['id'], mock_renewal_response['id'])
        self.assertEqual(result_dict['prior_subscription_plan'], mock_renewal_response['prior_subscription_plan'])
        self.assertEqual(result_dict['renewed_subscription_plan'], mock_renewal_response['renewed_subscription_plan'])

        # Verify a SelfServiceSubscriptionRenewal record was created
        expected_renewal_id = mock_renewal_response['id']
        renewal_record = SelfServiceSubscriptionRenewal.objects.get(
            checkout_intent=self.checkout_intent,
            subscription_plan_renewal_id=expected_renewal_id
        )
        self.assertEqual(renewal_record.stripe_subscription_id, stripe_subscription_id)
        self.assertIsNone(renewal_record.processed_at)
        self.assertEqual(renewal_record.stripe_event_data, summary.stripe_event_data)
        self.assertEqual(renewal_record.effective_date, trial_expiration)

    @mock.patch.object(LicenseManagerApiClient, 'create_subscription_plan_renewal')
    def test_process_input_with_existing_renewal_record(self, mock_create_renewal):
        """Test that processing with existing renewal record is idempotent."""
        mock_renewal_response = {
            'id': 123,
            'title': 'Test Renewal Plan',
            'created': '2024-01-15T10:30:00Z',
            'salesforce_opportunity_id': 'test-oli-456',
            'prior_subscription_plan': str(uuid4()),
            'renewed_subscription_plan': str(uuid4()),
            'number_of_licenses': 10,
            'effective_date': '2025-01-01T00:00:00Z',
            'renewed_expiration_date': '2027-01-01T00:00:00Z',
        }
        mock_create_renewal.return_value = mock_renewal_response

        stripe_subscription_id = 'sub_test_12345'
        event_data = StripeEventDataFactory.create(checkout_intent=self.checkout_intent)
        summary = StripeEventSummaryFactory.create(
            stripe_event_data=event_data,
            stripe_subscription_id=stripe_subscription_id,
            subscription_status='trialing',
        )

        expected_renewal_id = 123
        existing_renewal = SelfServiceSubscriptionRenewal.objects.create(
            checkout_intent=self.checkout_intent,
            subscription_plan_renewal_id=expected_renewal_id,
            stripe_subscription_id=summary.stripe_subscription_id,
            stripe_event_data=summary.stripe_event_data,
        )

        mock_accumulated_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output.uuid = str(uuid4())
        mock_accumulated_output.create_trial_subscription_plan_output.expiration_date = datetime(2026, 1, 1)

        mock_accumulated_output.create_first_paid_subscription_plan_output = mock.Mock()
        mock_accumulated_output.create_first_paid_subscription_plan_output.uuid = str(uuid4())
        mock_accumulated_output.create_first_paid_subscription_plan_output.expiration_date = datetime(2027, 1, 1)

        # Process the input
        self.renewal_step.process_input(mock_accumulated_output)

        # Verify the license manager API was called
        mock_create_renewal.assert_called_once()

        # Verify only one renewal record exists (no duplicate created)
        renewal_records = SelfServiceSubscriptionRenewal.objects.filter(
            checkout_intent=self.checkout_intent,
            subscription_plan_renewal_id=expected_renewal_id
        )
        self.assertEqual(renewal_records.count(), 1)

        # Verify it's the same record
        renewal_record = renewal_records.first()
        self.assertEqual(renewal_record.id, existing_renewal.id)
        self.assertEqual(renewal_record.stripe_subscription_id, summary.stripe_subscription_id)

    @mock.patch.object(LicenseManagerApiClient, 'create_subscription_plan_renewal')
    def test_process_input_without_stripe_subscription_id(self, mock_create_renewal):
        """Test creating renewal record fails when no StripeEventSummary exists yet."""
        mock_renewal_response = {
            'id': 456,
            'title': 'Test Renewal Plan',
            'created': '2024-01-15T10:30:00Z',
            'salesforce_opportunity_id': None,
            'prior_subscription_plan': str(uuid4()),
            'renewed_subscription_plan': str(uuid4()),
            'number_of_licenses': 10,
            'effective_date': '2025-01-01T00:00:00Z',
            'renewed_expiration_date': '2027-01-01T00:00:00Z',
        }
        mock_create_renewal.return_value = mock_renewal_response

        # Don't create any StripeEventSummary records
        mock_accumulated_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output.uuid = str(uuid4())
        mock_accumulated_output.create_trial_subscription_plan_output.expiration_date = datetime(2026, 1, 1)

        mock_accumulated_output.create_first_paid_subscription_plan_output = mock.Mock()
        mock_accumulated_output.create_first_paid_subscription_plan_output.uuid = str(uuid4())
        mock_accumulated_output.create_first_paid_subscription_plan_output.expiration_date = datetime(2027, 1, 1)

        with self.assertRaises(self.renewal_step.exception_class):
            self.renewal_step.process_input(mock_accumulated_output)

        # Verify the license manager API was called
        mock_create_renewal.assert_called_once()

        # But no renewal record is written in this exceptional case
        self.assertEqual(SelfServiceSubscriptionRenewal.objects.all().count(), 0)

    @mock.patch.object(LicenseManagerApiClient, 'create_subscription_plan_renewal')
    def test_process_input_license_manager_error(self, mock_create_renewal):
        """Test error handling when license manager API fails."""
        # Mock license manager API failure
        mock_create_renewal.side_effect = Exception("License Manager API error")

        mock_accumulated_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output.uuid = str(uuid4())
        mock_accumulated_output.create_trial_subscription_plan_output.expiration_date = datetime(2026, 1, 1)

        mock_accumulated_output.create_first_paid_subscription_plan_output = mock.Mock()
        mock_accumulated_output.create_first_paid_subscription_plan_output.uuid = str(uuid4())
        mock_accumulated_output.create_first_paid_subscription_plan_output.expiration_date = datetime(2027, 1, 1)

        self.checkout_intent.state = CheckoutIntentState.PAID
        self.checkout_intent.save()
        # Process the input and expect the exception to propagate
        with self.assertRaises(Exception) as context:
            self.renewal_step.process_input(mock_accumulated_output)

        self.assertIn("Failed to get/create subscription plan renewal", str(context.exception))

        # Verify no SelfServiceSubscriptionRenewal record was created on failure
        renewal_records = SelfServiceSubscriptionRenewal.objects.filter(
            checkout_intent=self.checkout_intent
        )
        self.assertEqual(renewal_records.count(), 0)

    @mock.patch.object(LicenseManagerApiClient, 'create_subscription_plan_renewal')
    def test_process_input_gets_latest_stripe_subscription_id(self, mock_create_renewal):
        """Test that the latest StripeEventSummary stripe_subscription_id is used."""
        mock_renewal_response = {
            'id': 789,
            'title': 'Test Renewal Plan',
            'created': '2024-01-15T10:30:00Z',
            'salesforce_opportunity_id': None,
            'prior_subscription_plan': str(uuid4()),
            'renewed_subscription_plan': str(uuid4()),
            'number_of_licenses': 10,
            'effective_date': '2025-01-01T00:00:00Z',
            'renewed_expiration_date': '2027-01-01T00:00:00Z',
        }
        mock_create_renewal.return_value = mock_renewal_response

        # Create multiple StripeEventSummary records with different timestamps
        older_event = StripeEventDataFactory(checkout_intent=self.checkout_intent)
        older_summary = older_event.summary
        older_summary.stripe_subscription_id = 'sub_older_123'
        older_summary.stripe_event_created_at = older_summary.stripe_event_created_at.replace(hour=1)
        older_summary.save()

        latest_event = StripeEventDataFactory(checkout_intent=self.checkout_intent)
        latest_summary = latest_event.summary
        latest_summary.stripe_subscription_id = 'sub_latest_456'
        latest_summary.stripe_event_created_at = latest_summary.stripe_event_created_at.replace(hour=2)
        latest_summary.save()

        mock_accumulated_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output.uuid = str(uuid4())
        mock_accumulated_output.create_trial_subscription_plan_output.expiration_date = datetime(2026, 1, 1)

        mock_accumulated_output.create_first_paid_subscription_plan_output = mock.Mock()
        mock_accumulated_output.create_first_paid_subscription_plan_output.uuid = str(uuid4())
        mock_accumulated_output.create_first_paid_subscription_plan_output.expiration_date = datetime(2027, 1, 1)

        # Process the input
        self.renewal_step.process_input(mock_accumulated_output)

        # Verify the renewal record uses the latest stripe_subscription_id
        expected_renewal_id = 789
        renewal_record = SelfServiceSubscriptionRenewal.objects.get(
            checkout_intent=self.checkout_intent,
            subscription_plan_renewal_id=expected_renewal_id
        )
        self.assertEqual(renewal_record.stripe_subscription_id, 'sub_latest_456')


class TestNotificationStep(TestCase):
    """
    Tests for NotificationStep activation link behavior.
    """

    @mock.patch('enterprise_access.apps.provisioning.models.send_enterprise_provision_signup_confirmation_email')
    @mock.patch('enterprise_access.apps.provisioning.models.LmsApiClient.get_lms_user_activation_link')
    def test_process_input_includes_activation_link_when_username_or_email_present(
            self,
            mock_get_activation_link,
            mock_send_signup_email,
    ):
        """
        Verify NotificationStep attempts to fetch an activation link and passes it
        through to the signup confirmation email task when username/email is present.
        """
        expected_activation_link = 'http://edx-platform.example.com/activate/abc123'
        mock_get_activation_link.return_value = expected_activation_link

        # Create a real step record (matches other tests' pattern of using .objects.create)
        step = NotificationStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data={},
        )

        step.fulfill_checkout_intent = mock.Mock()

        checkout_intent = mock.Mock()
        checkout_intent.user.username = 'fake-username'
        step.get_linked_checkout_intent = mock.Mock(return_value=checkout_intent)

        workflow = mock.Mock()
        workflow.input_object.create_trial_subscription_plan_input.desired_num_licenses = 10
        workflow.input_object.create_enterprise_admin_users_input.user_emails = ['test@example.com']
        step.get_workflow_record = mock.Mock(return_value=workflow)

        mock_accumulated_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output.start_date = datetime(2025, 1, 1)
        mock_accumulated_output.create_trial_subscription_plan_output.expiration_date = datetime(2025, 2, 1)
        mock_accumulated_output.create_customer_output.name = 'Test Customer'
        mock_accumulated_output.create_customer_output.slug = 'test-customer'

        # Execute
        step.process_input(mock_accumulated_output)

        # Activation link fetched with expected args
        mock_get_activation_link.assert_called_once_with(
            username='fake-username',
            user_email='test@example.com',
        )

        # Email task called with activation link in the right position
        mock_send_signup_email.delay.assert_called_once()
        delay_args, delay_kwargs = mock_send_signup_email.delay.call_args

        self.assertEqual(delay_args, ())
        self.assertEqual(
            delay_kwargs,
            {
                'subscription_start_date': datetime(2025, 1, 1),
                'subscription_end_date': datetime(2025, 2, 1),
                'number_of_licenses': 10,
                'activation_link': expected_activation_link,
                'organization_name': 'Test Customer',
                'enterprise_slug': 'test-customer',
            },
        )
        self.assertNotIn('ssp_product_slug', delay_kwargs)
        self.assertNotIn('academy_name', delay_kwargs)

    @mock.patch('enterprise_access.apps.provisioning.models.send_enterprise_provision_signup_confirmation_email')
    @mock.patch('enterprise_access.apps.provisioning.models.LmsApiClient.get_lms_user_activation_link')
    def test_process_input_skips_activation_link_lookup_when_username_and_email_absent(
            self,
            mock_get_activation_link,
            mock_send_signup_email,
    ):
        """NotificationStep should leave activation_link unset when no identity data exists."""
        step = NotificationStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data={},
        )

        step.fulfill_checkout_intent = mock.Mock()

        checkout_intent = mock.Mock()
        checkout_intent.user.username = None
        step.get_linked_checkout_intent = mock.Mock(return_value=checkout_intent)

        workflow = mock.Mock()
        workflow.input_object.create_trial_subscription_plan_input.desired_num_licenses = 10
        workflow.input_object.create_enterprise_admin_users_input.user_emails = []
        step.get_workflow_record = mock.Mock(return_value=workflow)

        mock_accumulated_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output.start_date = datetime(2025, 1, 1)
        mock_accumulated_output.create_trial_subscription_plan_output.expiration_date = datetime(2025, 2, 1)
        mock_accumulated_output.create_customer_output.name = 'Test Customer'
        mock_accumulated_output.create_customer_output.slug = 'test-customer'

        step.process_input(mock_accumulated_output)

        mock_get_activation_link.assert_not_called()
        _, delay_kwargs = mock_send_signup_email.delay.call_args
        self.assertIsNone(delay_kwargs['activation_link'])

    @mock.patch('enterprise_access.apps.provisioning.models.send_enterprise_provision_signup_confirmation_email')
    @mock.patch('enterprise_access.apps.provisioning.models.LmsApiClient.get_lms_user_activation_link')
    def test_process_input_does_not_forward_academy_name_when_present(
            self,
            mock_get_activation_link,
            mock_send_signup_email,
    ):
        """NotificationStep should no longer forward academy metadata to the task."""
        mock_get_activation_link.return_value = 'http://edx-platform.example.com/activate/abc123'

        step = NotificationStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data={},
        )

        step.fulfill_checkout_intent = mock.Mock()

        checkout_intent = mock.Mock()
        checkout_intent.user.username = 'fake-username'
        checkout_intent.ssp_product.slug = 'essentials-ai'
        checkout_intent.ssp_product.academy_title = 'AI Academy'
        step.get_linked_checkout_intent = mock.Mock(return_value=checkout_intent)

        workflow = mock.Mock()
        workflow.input_object.create_trial_subscription_plan_input.desired_num_licenses = 10
        workflow.input_object.create_enterprise_admin_users_input.user_emails = ['test@example.com']
        step.get_workflow_record = mock.Mock(return_value=workflow)

        mock_accumulated_output = mock.Mock()
        mock_accumulated_output.create_trial_subscription_plan_output.start_date = datetime(2025, 1, 1)
        mock_accumulated_output.create_trial_subscription_plan_output.expiration_date = datetime(2025, 2, 1)
        mock_accumulated_output.create_customer_output.name = 'Test Customer'
        mock_accumulated_output.create_customer_output.slug = 'test-customer'

        step.process_input(mock_accumulated_output)

        _, delay_kwargs = mock_send_signup_email.delay.call_args
        self.assertNotIn('academy_name', delay_kwargs)
        self.assertNotIn('ssp_product_slug', delay_kwargs)
        self.assertEqual(delay_kwargs['activation_link'], 'http://edx-platform.example.com/activate/abc123')

    def test_get_workflow_record_returns_matching_workflow(self):
        workflow = ProvisionNewCustomerWorkflowFactory()
        step = NotificationStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            input_data={},
        )

        self.assertEqual(step.get_workflow_record(), workflow)

    def test_get_preceding_step_record_returns_matching_renewal_step(self):
        workflow = ProvisionNewCustomerWorkflowFactory()
        renewal_step = GetCreateSubscriptionPlanRenewalStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            input_data={},
            output_data={},
        )
        step = NotificationStep.objects.create(
            workflow_record_uuid=workflow.uuid,
            preceding_step_uuid=renewal_step.uuid,
            input_data={},
        )

        self.assertEqual(step.get_preceding_step_record(), renewal_step)


class TestCheckoutIntentStepMixinUnit(TestCase):
    """Unit tests for CheckoutIntentStepMixin helper behavior."""

    @mock.patch('enterprise_access.apps.provisioning.models.CheckoutIntent')
    def test_get_fulfillable_checkout_intent_via_slug_not_found_raises(self, mock_checkout_intent):
        mock_checkout_intent.filter_by_name_and_slug.return_value.filter.return_value.first.return_value = None
        mock_checkout_intent.DoesNotExist = Exception

        class DummyStep(prov_models.CheckoutIntentStepMixin):
            """Dummy step exposing workflow input for slug lookup."""

            def get_workflow_record(self):
                # workflow.input_object.create_customer_input.slug is read
                wf = mock.Mock()
                wf.input_object = mock.Mock()
                wf.input_object.create_customer_input = mock.Mock(slug='acme')
                return wf

        step = DummyStep()
        with self.assertRaises(Exception):
            step.get_fulfillable_checkout_intent_via_slug()

    @mock.patch('enterprise_access.apps.provisioning.models.CheckoutIntent')
    def test_link_checkout_intent_sets_workflow_and_saves(self, mock_checkout_intent):
        mock_ci = mock.Mock()
        mock_checkout_intent.filter_by_name_and_slug.return_value.filter.return_value.first.return_value = mock_ci

        class DummyStep(prov_models.CheckoutIntentStepMixin):
            """Dummy step that provides a workflow record for linking."""

            def __init__(self):
                self._workflow = mock.Mock()

            def get_workflow_record(self):
                return self._workflow

        step = DummyStep()
        enterprise_uuid = uuid4()
        step.link_checkout_intent(enterprise_uuid)
        # Avoid accessing protected attribute directly; use public accessor
        self.assertEqual(mock_ci.workflow, step.get_workflow_record())
        self.assertEqual(mock_ci.enterprise_uuid, enterprise_uuid)
        mock_ci.save.assert_called_once_with(update_fields=['workflow', 'enterprise_uuid'])


class TestGetCreateCatalogStepCatalogQueryId(TestCase):
    """
    Tests for GetCreateCatalogStep._get_catalog_query_id branch coverage.
    """

    def setUp(self):
        self.workflow = ProvisionNewCustomerWorkflowFactory()

    def test_catalog_query_id_int_returns_int(self):
        """When catalog_query_id is an int like 42, return it directly."""
        step = GetCreateCatalogStep.objects.create(
            workflow_record_uuid=self.workflow.uuid,
            input_data={'catalog_query_id': 42},
        )
        # pylint: disable=protected-access
        result = step._get_catalog_query_id(workflow_input=None)
        self.assertEqual(result, 42)
        self.assertIsInstance(result, int)

    def test_catalog_query_id_none_falls_through(self):
        """When catalog_query_id is None, method falls through to product_id lookup."""
        step = GetCreateCatalogStep.objects.create(
            workflow_record_uuid=self.workflow.uuid,
            input_data={},
        )
        self.assertTrue(settings.PRODUCT_ID_TO_CATALOG_QUERY_ID_MAPPING)
        product_id, expected_catalog_query_id = next(iter(settings.PRODUCT_ID_TO_CATALOG_QUERY_ID_MAPPING.items()))
        workflow_input = mock.Mock()
        workflow_input.create_trial_subscription_plan_input = mock.Mock(product_id=product_id)
        # pylint: disable=protected-access
        result = step._get_catalog_query_id(workflow_input)
        self.assertEqual(result, expected_catalog_query_id)


@ddt.ddt
class TestGenerateInputDictSspResolution(TestCase):
    """
    Tests for ProvisionNewCustomerWorkflow.generate_input_dict _resolve_ssp branches.
    """

    MINIMAL_CUSTOMER = {'name': 'Test', 'slug': 'test-co', 'country': 'US'}
    MINIMAL_TRIAL = {
        'title': 'Trial', 'salesforce_opportunity_line_item': '00k1',
        'start_date': '2025-06-01T00:00:00Z', 'expiration_date': '2026-03-31T00:00:00Z',
        'desired_num_licenses': 5,
    }
    MINIMAL_PAID = {'title': 'Paid'}

    def _create_ssp(
        self,
        *,
        slug,
        trial_product_id,
        paid_product_id,
        catalog_query_uuid=None,
        academy_uuid=None,
    ):
        """Create a test SSP product with the supplied identifiers."""
        return SspProduct.objects.create(
            slug=slug,
            stripe_price_lookup_key=f'price_{slug}',
            catalog_query_uuid=catalog_query_uuid or uuid4(),
            academy_uuid=academy_uuid,
            license_manager_product_id_trial=trial_product_id,
            license_manager_product_id_paid=paid_product_id,
            is_active=True,
        )

    def _generate_input_dict(
        self,
        *,
        trial_subscription_plan_request_dict=None,
        first_paid_subscription_plan_request_dict=None,
        top_level_ssp_product_slug=None,
    ):
        """Build provisioning input with optional SSP overrides for a test case."""
        return ProvisionNewCustomerWorkflow.generate_input_dict(
            customer_request_dict=self.MINIMAL_CUSTOMER,
            admin_email_list=['a@b.com'],
            catalog_request_dict={},
            academy_request_dict={},
            customer_agreement_request_dict={},
            trial_subscription_plan_request_dict=trial_subscription_plan_request_dict,
            first_paid_subscription_plan_request_dict=first_paid_subscription_plan_request_dict,
            top_level_ssp_product_slug=top_level_ssp_product_slug,
        )

    @patch('enterprise_access.apps.provisioning.models.EnterpriseCatalogApiClient')
    def test_ssp_with_catalog_query_uuid_resolves_to_int_id(self, mock_catalog_cls):
        """catalog_query_uuid present → calls API and stores int id in catalog input."""
        mock_catalog_cls.return_value.get_catalog_query_id_from_uuid.return_value = 99

        ssp = self._create_ssp(
            slug='test-ssp-cq',
            trial_product_id=10,
            paid_product_id=20,
            academy_uuid=uuid4(),
        )

        result = self._generate_input_dict(
            trial_subscription_plan_request_dict={**self.MINIMAL_TRIAL},
            first_paid_subscription_plan_request_dict={**self.MINIMAL_PAID},
            top_level_ssp_product_slug=ssp.slug,
        )

        self.assertEqual(result[GetCreateCatalogStepInput.KEY]['catalog_query_id'], 99)
        self.assertIsInstance(result[GetCreateCatalogStepInput.KEY]['catalog_query_id'], int)
        mock_catalog_cls.return_value.get_catalog_query_id_from_uuid.assert_called()
        self.assertEqual(
            result['associate_academy_input']['academy_uuid'],
            str(ssp.academy_uuid),
        )

    def test_ssp_catalog_query_uuid_api_returns_none_raises_type_error(self):
        """
        Catalog query UUID present but API returns None → int(None) raises TypeError.
        The method should fail loudly instead of silently omitting catalog_query_id.
        """
        with patch('enterprise_access.apps.provisioning.models.EnterpriseCatalogApiClient') as mock_catalog_cls:
            mock_catalog_cls.return_value.get_catalog_query_id_from_uuid.return_value = None

            ssp = self._create_ssp(
                slug='test-ssp-no-cq',
                trial_product_id=10,
                paid_product_id=20,
                academy_uuid=None,
            )

            with self.assertRaises(TypeError):
                self._generate_input_dict(
                    trial_subscription_plan_request_dict={**self.MINIMAL_TRIAL},
                    first_paid_subscription_plan_request_dict={**self.MINIMAL_PAID},
                    top_level_ssp_product_slug=ssp.slug,
                )

    def test_ssp_catalog_query_uuid_api_returns_invalid_str_raises_value_error(self):
        """
        Catalog query UUID present but API returns a non-numeric string →
        int('not-a-number') raises ValueError. The method should fail loudly.
        """
        with patch('enterprise_access.apps.provisioning.models.EnterpriseCatalogApiClient') as mock_catalog_cls:
            mock_catalog_cls.return_value.get_catalog_query_id_from_uuid.return_value = 'not-a-number'

            ssp = self._create_ssp(
                slug='test-ssp-bad-str',
                trial_product_id=10,
                paid_product_id=20,
                academy_uuid=None,
            )

            with self.assertRaises(ValueError):
                self._generate_input_dict(
                    trial_subscription_plan_request_dict={**self.MINIMAL_TRIAL},
                    first_paid_subscription_plan_request_dict={**self.MINIMAL_PAID},
                    top_level_ssp_product_slug=ssp.slug,
                )

    @patch('enterprise_access.apps.provisioning.models.EnterpriseCatalogApiClient')
    def test_ssp_with_product_id_none(self, mock_catalog_cls):
        """license_manager_product_id_trial is None → product_id falls back to settings default."""
        mock_catalog_cls.return_value.get_catalog_query_id_from_uuid.return_value = 42

        ssp = self._create_ssp(
            slug='test-ssp-nopid',
            trial_product_id=None,
            paid_product_id=None,
            academy_uuid=None,
        )

        result = self._generate_input_dict(
            trial_subscription_plan_request_dict={**self.MINIMAL_TRIAL},
            first_paid_subscription_plan_request_dict={**self.MINIMAL_PAID},
            top_level_ssp_product_slug=ssp.slug,
        )

        trial_input = result['create_trial_subscription_plan_input']
        paid_input = result['create_first_paid_subscription_plan_input']
        self.assertEqual(trial_input['product_id'], settings.PROVISIONING_TRIAL_SUBSCRIPTION_PRODUCT_ID)
        self.assertEqual(paid_input['product_id'], settings.PROVISIONING_PAID_SUBSCRIPTION_PRODUCT_ID)

    @mock.patch('enterprise_access.apps.provisioning.models.SspProduct.objects.get')
    @mock.patch('enterprise_access.apps.provisioning.models.EnterpriseCatalogApiClient')
    def test_top_level_slug_reuses_single_ssp_resolution(self, mock_catalog_cls, mock_ssp_get):
        """A shared top-level SSP slug should resolve once for both plans."""
        ssp = self._create_ssp(
            slug='shared-ssp',
            trial_product_id=11,
            paid_product_id=22,
            academy_uuid=uuid4(),
        )
        mock_ssp_get.return_value = ssp
        mock_catalog_cls.return_value.get_catalog_query_id_from_uuid.return_value = 123

        result = self._generate_input_dict(
            trial_subscription_plan_request_dict={**self.MINIMAL_TRIAL},
            first_paid_subscription_plan_request_dict={**self.MINIMAL_PAID},
            top_level_ssp_product_slug=ssp.slug,
        )

        self.assertEqual(result[GetCreateCatalogStepInput.KEY]['catalog_query_id'], 123)
        self.assertEqual(result[GetCreateTrialSubscriptionPlanStepInput.KEY]['product_id'], 11)
        self.assertEqual(result[GetCreateFirstPaidSubscriptionPlanStepInput.KEY]['product_id'], 22)
        mock_ssp_get.assert_called_once_with(slug=ssp.slug, is_active=True)
        mock_catalog_cls.return_value.get_catalog_query_id_from_uuid.assert_called_once_with(ssp.catalog_query_uuid)

    def test_ssp_invalid_slug_raises_validation_error(self):
        """Unknown ssp_product_slug raises ValidationError."""
        with self.assertRaises(ValidationError):
            self._generate_input_dict(
                trial_subscription_plan_request_dict={**self.MINIMAL_TRIAL, 'ssp_product_slug': 'nonexistent-slug'},
                first_paid_subscription_plan_request_dict={**self.MINIMAL_PAID},
                top_level_ssp_product_slug=None,
            )

    @ddt.data(
        {
            'scenario': 'top-level slug only',
            'top_level_trial_product_id': 10,
            'top_level_paid_product_id': 20,
            'plan_level_trial_product_id': None,
            'plan_level_paid_product_id': None,
            'trial_subscription_plan_request_dict': {
                'title': 'Trial',
                'salesforce_opportunity_line_item': '00k1',
                'start_date': '2025-06-01T00:00:00Z',
                'expiration_date': '2026-03-31T00:00:00Z',
                'desired_num_licenses': 5,
            },
            'first_paid_subscription_plan_request_dict': {'title': 'Paid'},
            'top_level_ssp_product_slug': 'top-level-ssp',
            'expected_trial_product_id': 10,
            'expected_paid_product_id': 20,
        },
        {
            'scenario': 'top-level slug with none plan dicts',
            'top_level_trial_product_id': 100,
            'top_level_paid_product_id': 200,
            'plan_level_trial_product_id': None,
            'plan_level_paid_product_id': None,
            'trial_subscription_plan_request_dict': None,
            'first_paid_subscription_plan_request_dict': None,
            'top_level_ssp_product_slug': 'top-level-ssp',
            'expected_trial_product_id': 100,
            'expected_paid_product_id': 200,
        },
        {
            'scenario': 'per-plan slug overrides top-level',
            'top_level_trial_product_id': 10,
            'top_level_paid_product_id': 20,
            'plan_level_trial_product_id': 77,
            'plan_level_paid_product_id': 88,
            'trial_subscription_plan_request_dict': {
                'title': 'Trial',
                'salesforce_opportunity_line_item': '00k1',
                'start_date': '2025-06-01T00:00:00Z',
                'expiration_date': '2026-03-31T00:00:00Z',
                'desired_num_licenses': 5,
                'ssp_product_slug': 'plan-level-ssp',
            },
            'first_paid_subscription_plan_request_dict': {'title': 'Paid'},
            'top_level_ssp_product_slug': 'top-level-ssp',
            'expected_trial_product_id': 77,
            'expected_paid_product_id': 20,
        },
    )
    @ddt.unpack
    @mock.patch('enterprise_access.apps.provisioning.models.EnterpriseCatalogApiClient')
    def test_top_level_and_plan_level_slug_resolution(
        self,
        mock_catalog_cls,
        scenario,
        top_level_trial_product_id,
        top_level_paid_product_id,
        plan_level_trial_product_id,
        plan_level_paid_product_id,
        trial_subscription_plan_request_dict,
        first_paid_subscription_plan_request_dict,
        top_level_ssp_product_slug,
        expected_trial_product_id,
        expected_paid_product_id,
    ):
        """Top-level SSP slugs should seed missing plan data while preserving per-plan overrides."""
        mock_catalog_cls.return_value.get_catalog_query_id_from_uuid.return_value = 42

        self._create_ssp(
            slug='top-level-ssp',
            trial_product_id=top_level_trial_product_id,
            paid_product_id=top_level_paid_product_id,
            academy_uuid=None,
        )
        if plan_level_trial_product_id is not None:
            self._create_ssp(
                slug='plan-level-ssp',
                trial_product_id=plan_level_trial_product_id,
                paid_product_id=plan_level_paid_product_id,
                academy_uuid=None,
            )

        result = self._generate_input_dict(
            trial_subscription_plan_request_dict=trial_subscription_plan_request_dict,
            first_paid_subscription_plan_request_dict=first_paid_subscription_plan_request_dict,
            top_level_ssp_product_slug=top_level_ssp_product_slug,
        )

        self.assertEqual(result[GetCreateCatalogStepInput.KEY]['catalog_query_id'], 42, msg=scenario)
        self.assertEqual(
            result[GetCreateTrialSubscriptionPlanStepInput.KEY]['product_id'],
            expected_trial_product_id,
            msg=scenario,
        )
        self.assertEqual(
            result[GetCreateFirstPaidSubscriptionPlanStepInput.KEY]['product_id'],
            expected_paid_product_id,
            msg=scenario,
        )
