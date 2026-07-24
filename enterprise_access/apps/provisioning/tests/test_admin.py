"""
Unit tests for the provisioning module.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import ddt
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.messages import ERROR, SUCCESS
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.provisioning import admin, models


class AdminTriggerProvisioningWorkflowAdminTests(TestCase):
    """
    Unit tests for provisioning via a django admin form.
    """
    def setUp(self):
        self.request_factory = RequestFactory()
        self.admin_user = UserFactory(is_staff=True, is_superuser=True)
        self.site = AdminSite()
        self.model_admin = admin.AdminTriggerProvisioningSubscriptionTrialWorkflowAdmin(
            models.TriggerProvisionSubscriptionTrialCustomerWorkflow,
            self.site,
        )

    def _mock_session_messages(self, request):
        """
        Helper to setup some stub message storage on the test client session.
        """
        # pylint: disable=literal-used-as-attribute
        setattr(request, 'session', self.client.session)
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)
        return messages

    def _get_messages_from_request(self, request):
        """
        Helper to extract (level, message) tuples from the _messages storage attached to a Django request.
        Returns a list of (level, message) tuples.
        """
        # pylint: disable=protected-access
        if not hasattr(request, '_messages') or request._messages is None:
            return []
        return [(msg.level, msg.message) for msg in request._messages]

    @patch("enterprise_access.apps.provisioning.models.ProvisionNewCustomerWorkflow.generate_input_dict")
    @patch("enterprise_access.apps.provisioning.models.ProvisionNewCustomerWorkflow.objects.create")
    def test_add_view_success(self, mock_create, mock_generate_input_dict):
        """
        Test that add_view creates a workflow and redirects to the change page on success.
        """
        # Set up mocks
        mock_workflow_instance = MagicMock(
            succeeded_at=timezone.now(),
            failed_at=None,
            uuid="abc-123",
            pk=42,
        )
        mock_create.return_value = mock_workflow_instance
        mock_generate_input_dict.return_value = {"mock": "input"}

        post_data = {
            "customer_name": "Acme Inc.",
            "customer_slug": "acme-inc",
            "customer_country": "US",
            "admin_email_1": "admin1@example.com",
            "admin_email_2": "",
            "admin_email_3": "",
            "admin_email_4": "",
            "admin_email_5": "",
            "catalog_title": "Acme Catalog",
            "catalog_query_id": 2,
            "agreement_default_catalog_uuid": "",
            "plan_title": "Acme Plan",
            "plan_salesforce_opportunity_line_item": "OPP12345",
            "plan_start_date": "2025-01-01 00:00:00",
            "plan_expiration_date": "2026-01-01 00:00:00",
            "plan_product_id": 1,
            "plan_desired_num_licenses": 50,
            "plan_enterprise_catalog_uuid": "",
        }
        request = self.request_factory.post(
            '/admin/provisioning/admintriggerprovisionnewcustomerworkflow/add/',
            data=post_data,
        )
        request.user = self.admin_user

        self._mock_session_messages(request)

        response = self.model_admin.add_view(request)

        assert response.status_code == 302
        assert reverse(
            "admin:provisioning_provisionnewcustomerworkflow_change",
            args=[mock_workflow_instance.pk]
        ) in response.url
        mock_workflow_instance.execute.assert_called_once_with()

        messages = self._get_messages_from_request(request)
        assert (SUCCESS, "Successfully triggered and completed workflow: abc-123") == messages[0]

    @patch("enterprise_access.apps.provisioning.models.ProvisionNewCustomerWorkflow.generate_input_dict")
    @patch("enterprise_access.apps.provisioning.models.ProvisionNewCustomerWorkflow.objects.create")
    def test_add_view_workflow_failure(self, mock_create, mock_generate_input_dict):
        """
        Test that add_view shows error when workflow fails.
        """
        mock_workflow_instance = MagicMock(
            failed_at=timezone.now(),
            succeeded_at=None,
            uuid="abc-666",
            pk=45,
            exception_message="Some failure",
        )
        mock_create.return_value = mock_workflow_instance
        mock_generate_input_dict.return_value = {"mock": "input"}

        post_data = {
            "customer_name": "Acme Inc.",
            "customer_slug": "acme-inc",
            "customer_country": "US",
            "admin_email_1": "admin1@example.com",
            "catalog_title": "Acme Catalog",
            "catalog_query_id": 2,
            "plan_title": "Acme Plan",
            "plan_salesforce_opportunity_line_item": "OPP12345",
            "plan_start_date": "2025-01-01 00:00:00",
            "plan_expiration_date": "2026-01-01 00:00:00",
            "plan_product_id": 1,
            "plan_desired_num_licenses": 50,
        }
        request = self.request_factory.post(
            '/admin/provisioning/admintriggerprovisionnewcustomerworkflow/add/',
            data=post_data,
        )
        request.user = self.admin_user

        self._mock_session_messages(request)

        response = self.model_admin.add_view(request)

        # Should redirect even on failure
        assert response.status_code == 302
        mock_workflow_instance.execute.assert_called_once_with()

        messages = self._get_messages_from_request(request)
        assert (ERROR, 'Workflow triggered but failed: abc-666. Error: Some failure') == messages[0]

    def test_add_view_invalid_form(self):
        """
        Test that add_view returns to form on invalid input (no customer_name).
        """
        post_data = {
            "customer_name": "",  # required, left blank
            "customer_slug": "acme-inc",
            "customer_country": "US",
            "admin_email_1": "admin1@example.com",
            "catalog_title": "Acme Catalog",
            "catalog_query_id": 2,
            "plan_title": "Acme Plan",
            "plan_salesforce_opportunity_line_item": "OPP12345",
            "plan_start_date": "2025-01-01 00:00:00",
            "plan_expiration_date": "2026-01-01 00:00:00",
            "plan_product_id": 1,
            "plan_desired_num_licenses": 50,
        }
        request = self.request_factory.post(
            '/admin/provisioning/admintriggerprovisionnewcustomerworkflow/add/',
            data=post_data,
        )
        request.user = self.admin_user
        self._mock_session_messages(request)

        # Should redirect to the same page (form with errors)
        response = self.model_admin.add_view(request)
        assert response.status_code == 302
        assert response.url.endswith('/admin/provisioning/admintriggerprovisionnewcustomerworkflow/add/')

    def test_add_view_get(self):
        """
        Test that add_view renders the form on GET.
        """
        request = self.request_factory.get('/admin/provisioning/admintriggerprovisionnewcustomerworkflow/add/')
        request.user = self.admin_user

        self._mock_session_messages(request)

        response = self.model_admin.add_view(request)

        assert response.status_code == 200
        assert b'Trigger Subscription Trial Provisioning Workflow' in response.content


# (admin_class, model_class, display_method_name, step_getter_name, reverse_target) for every
# "simple" *_link display method: no branching on the type of the returned step record.
SIMPLE_LINK_CASES = (
    {
        'admin_class': admin.ProvisionNewCustomerWorkflowAdmin,
        'model_class': models.ProvisionNewCustomerWorkflow,
        'display_method_name': 'create_customer_step_link',
        'step_getter_name': 'get_create_customer_step',
        'reverse_target': 'admin:provisioning_getcreatecustomerstep_change',
    },
    {
        'admin_class': admin.ProvisionNewCustomerWorkflowAdmin,
        'model_class': models.ProvisionNewCustomerWorkflow,
        'display_method_name': 'create_admin_users_step_link',
        'step_getter_name': 'get_create_enterprise_admin_users_step',
        'reverse_target': 'admin:provisioning_getcreateenterpriseadminusersstep_change',
    },
    {
        'admin_class': admin.ProvisionNewCustomerWorkflowAdmin,
        'model_class': models.ProvisionNewCustomerWorkflow,
        'display_method_name': 'create_catalog_step_link',
        'step_getter_name': 'get_create_catalog_step',
        'reverse_target': 'admin:provisioning_getcreatecatalogstep_change',
    },
    {
        'admin_class': admin.ProvisionNewCustomerWorkflowAdmin,
        'model_class': models.ProvisionNewCustomerWorkflow,
        'display_method_name': 'create_customer_agreement_step_link',
        'step_getter_name': 'get_create_customer_agreement_step',
        'reverse_target': 'admin:provisioning_getcreatecustomeragreementstep_change',
    },
    {
        'admin_class': admin.ProvisionNewCustomerWorkflowAdmin,
        'model_class': models.ProvisionNewCustomerWorkflow,
        'display_method_name': 'create_subscription_plan_step_link',
        'step_getter_name': 'get_create_trial_subscription_plan_step',
        'reverse_target': 'admin:provisioning_getcreatetrialsubscriptionplanstep_change',
    },
    {
        'admin_class': admin.GetCreateEnterpriseAdminUsersStepAdmin,
        'model_class': models.GetCreateEnterpriseAdminUsersStep,
        'display_method_name': 'preceding_step_link',
        'step_getter_name': 'get_preceding_step_record',
        'reverse_target': 'admin:provisioning_getcreatecustomerstep_change',
    },
    {
        'admin_class': admin.GetCreateCatalogStepAdmin,
        'model_class': models.GetCreateCatalogStep,
        'display_method_name': 'preceding_step_link',
        'step_getter_name': 'get_preceding_step_record',
        'reverse_target': 'admin:provisioning_getcreateenterpriseadminusersstep_change',
    },
    {
        'admin_class': admin.AssociateAcademyStepAdmin,
        'model_class': models.AssociateAcademyStep,
        'display_method_name': 'preceding_step_link',
        'step_getter_name': 'get_preceding_step_record',
        'reverse_target': 'admin:provisioning_getcreatecatalogstep_change',
    },
    {
        'admin_class': admin.GetCreateTrialSubscriptionPlanStepAdmin,
        'model_class': models.GetCreateTrialSubscriptionPlanStep,
        'display_method_name': 'preceding_step_link',
        'step_getter_name': 'get_preceding_step_record',
        'reverse_target': 'admin:provisioning_getcreatecustomeragreementstep_change',
    },
    {
        'admin_class': admin.ProvisionWorkflowStepAdminBase,
        'model_class': models.GetCreateCustomerStep,
        'display_method_name': 'workflow_record_link',
        'step_getter_name': 'get_workflow_record',
        'reverse_target': 'admin:provisioning_provisionnewcustomerworkflow_change',
    },
)


@ddt.ddt
class ProvisioningAdminLinkDisplayTests(TestCase):
    """
    Tests for the `format_html()`-based link display methods on the provisioning admin classes.

    These methods used to build their return value with `mark_safe('<a href="{}">{}</a>'.format(...))`,
    which pylint-django 2.8.0's `mark-safe-interpolation` (W5151) check flags because interpolated
    values aren't HTML-escaped before being marked safe. They were converted to
    `format_html('<a href="{}">{}</a>', ...)`, which escapes each argument. These tests would have
    caught a broken conversion (e.g. swapped argument order, or reintroducing string interpolation).
    """
    def setUp(self):
        self.site = AdminSite()

    @ddt.data(*SIMPLE_LINK_CASES)
    @ddt.unpack
    def test_returns_none_when_no_step_record(
        self, admin_class, model_class, display_method_name, step_getter_name, reverse_target,
    ):
        """No underlying step/workflow record yet -> the display method returns None."""
        del reverse_target  # unused in this test
        model_admin = admin_class(model_class, self.site)
        obj = MagicMock(**{f'{step_getter_name}.return_value': None})

        display_method = getattr(model_admin, display_method_name)
        self.assertIsNone(display_method(obj))

    @ddt.data(*SIMPLE_LINK_CASES)
    @ddt.unpack
    def test_renders_anchor_tag_linking_to_step_record(
        self, admin_class, model_class, display_method_name, step_getter_name, reverse_target,
    ):
        """A step/workflow record exists -> the display method renders a link to its admin page."""
        model_admin = admin_class(model_class, self.site)
        step_pk = uuid4()
        step_record = MagicMock(pk=step_pk)
        obj = MagicMock(**{f'{step_getter_name}.return_value': step_record})

        result = getattr(model_admin, display_method_name)(obj)

        expected_url = reverse(reverse_target, args=(step_pk,))
        self.assertEqual(result, f'<a href="{expected_url}">{step_pk}</a>')

    def test_format_html_escapes_malicious_interpolated_value(self):
        """
        `format_html()` HTML-escapes its arguments, unlike the `mark_safe(str.format(...))` pattern
        it replaced. Uses `enterprise_customer_admin_link`, which builds its URL directly from a
        stored uuid value (no `reverse()` involved), so any string can stand in for that value.
        """
        model_admin = admin.ProvisionNewCustomerWorkflowAdmin(models.ProvisionNewCustomerWorkflow, self.site)
        malicious_uuid = '"><script>alert(1)</script>'
        step_record = MagicMock(output_object=MagicMock(uuid=malicious_uuid))
        obj = MagicMock(get_create_customer_step=MagicMock(return_value=step_record))

        result = model_admin.enterprise_customer_admin_link(obj)

        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt;', result)

    def test_subscription_plan_link_renders_anchor_tag(self):
        """
        `subscription_plan_link` builds its URL directly from a stored uuid value (no `reverse()`
        involved), pointing at the License Manager admin, analogous to `enterprise_customer_admin_link`.
        """
        model_admin = admin.ProvisionNewCustomerWorkflowAdmin(models.ProvisionNewCustomerWorkflow, self.site)
        plan_uuid = uuid4()
        step_record = MagicMock(output_object=MagicMock(uuid=plan_uuid))
        obj = MagicMock(get_create_trial_subscription_plan_step=MagicMock(return_value=step_record))

        result = model_admin.subscription_plan_link(obj)

        expected_url = f'{settings.LICENSE_MANAGER_URL}/admin/subscriptions/subscriptionplan/{plan_uuid}/change/'
        self.assertEqual(result, f'<a href="{expected_url}">{expected_url}</a>')

    def test_subscription_plan_link_escapes_malicious_interpolated_value(self):
        """
        Mirrors `test_format_html_escapes_malicious_interpolated_value`: `format_html()` HTML-escapes
        its arguments, unlike the `mark_safe(str.format(...))` pattern it replaced.
        """
        model_admin = admin.ProvisionNewCustomerWorkflowAdmin(models.ProvisionNewCustomerWorkflow, self.site)
        malicious_uuid = '"><script>alert(1)</script>'
        step_record = MagicMock(output_object=MagicMock(uuid=malicious_uuid))
        obj = MagicMock(get_create_trial_subscription_plan_step=MagicMock(return_value=step_record))

        result = model_admin.subscription_plan_link(obj)

        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt;', result)


class GetCreateCustomerAgreementStepAdminPrecedingStepLinkTests(TestCase):
    """
    Tests for `GetCreateCustomerAgreementStepAdmin.preceding_step_link`, which (unlike the other
    `preceding_step_link` implementations) branches on the type of the preceding step record.
    """
    def setUp(self):
        self.site = AdminSite()
        self.model_admin = admin.GetCreateCustomerAgreementStepAdmin(models.GetCreateCustomerAgreementStep, self.site)

    def test_returns_none_when_no_preceding_step_record(self):
        obj = MagicMock(get_preceding_step_record=MagicMock(return_value=None))
        self.assertIsNone(self.model_admin.preceding_step_link(obj))

    def test_links_to_associate_academy_step_by_default(self):
        step_record = models.AssociateAcademyStep(uuid=uuid4())
        obj = MagicMock(get_preceding_step_record=MagicMock(return_value=step_record))

        result = self.model_admin.preceding_step_link(obj)

        expected_url = reverse("admin:provisioning_associateacademystep_change", args=(step_record.pk,))
        self.assertEqual(result, f'<a href="{expected_url}">{step_record.pk}</a>')

    def test_links_to_catalog_step_when_preceding_step_is_a_catalog_step(self):
        step_record = models.GetCreateCatalogStep(uuid=uuid4())
        obj = MagicMock(get_preceding_step_record=MagicMock(return_value=step_record))

        result = self.model_admin.preceding_step_link(obj)

        expected_url = reverse("admin:provisioning_getcreatecatalogstep_change", args=(step_record.pk,))
        self.assertEqual(result, f'<a href="{expected_url}">{step_record.pk}</a>')
