"""
Tests for BFF handlers
"""
from unittest import mock

from rest_framework import status

from enterprise_access.apps.bffs.context import HandlerContext
from enterprise_access.apps.bffs.handlers import BaseHandler, BaseLearnerPortalHandler, DashboardHandler
from enterprise_access.apps.bffs.tests.utils import TestHandlerContextMixin
from enterprise_access.apps.api_client.constants import LicenseStatuses


class TestBaseHandler(TestHandlerContextMixin):
    """
    Test BaseHandler
    """

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_base_handler_load_and_process_not_implemented(self, mock_get_enterprise_customers_for_user):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        context = HandlerContext(self.request)
        base_handler = BaseHandler(context)
        with self.assertRaises(NotImplementedError):
            base_handler.load_and_process()

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_base_handler_add_error(self, mock_get_enterprise_customers_for_user):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        context = HandlerContext(self.request)
        base_handler = BaseHandler(context)
        # Define kwargs for add_error
        arguments = {
            **self.mock_error,
            "status_code": status.HTTP_400_BAD_REQUEST
        }
        base_handler.add_error(**arguments)
        self.assertEqual(self.mock_error, base_handler.context.errors[-1])
        self.assertEqual(status.HTTP_400_BAD_REQUEST, base_handler.context.status_code)

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_base_handler_add_warning(self, mock_get_enterprise_customers_for_user):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        context = HandlerContext(self.request)
        base_handler = BaseHandler(context)
        base_handler.add_warning(**self.mock_warning)
        self.assertEqual(self.mock_warning, base_handler.context.warnings[0])


class TestSelectBestLicense(TestHandlerContextMixin):
    """
    Unit tests for SubscriptionLicenseProcessor._select_best_license.

    Business rule (ENT-11672): When a learner has multiple licenses that all cover
    a given course, the license they FIRST ACTIVATED wins. Expiration date is used
    only as a secondary tie-breaker.

    Tie-breaker precedence:
        1. Earliest activation_date (ASC)  — primary: first-activated wins
        2. Latest expiration_date (DESC)   — secondary: longer access window
        3. UUID (DESC)                     — deterministic stable fallback
    """

    def _make_license(self, uuid, activation_date, expiration_date, catalog_uuid='cat-a'):
        return {
            'uuid': uuid,
            'status': 'activated',
            'activation_date': activation_date,
            'subscription_plan': {
                'enterprise_catalog_uuid': catalog_uuid,
                'is_current': True,
                'expiration_date': expiration_date,
            },
        }

    def _processor(self):
        return BaseLearnerPortalHandler.__new__(BaseLearnerPortalHandler)

    def test_single_license_returned_directly(self):
        """Single candidate is always returned without sorting."""
        lic = self._make_license('lic-a', '2024-06-01', '2026-01-01')
        result = self._processor()._select_best_license([lic])
        self.assertEqual(result['uuid'], 'lic-a')

    def test_earliest_activation_wins_regardless_of_expiration(self):
        """
        ENT-11672 primary rule: the license activated FIRST wins,
        even when the other license has a later expiration date.
        """
        lic_first  = self._make_license('lic-first',  '2023-01-15', '2025-06-30')  # activated earlier, expires sooner
        lic_second = self._make_license('lic-second', '2024-03-01', '2026-12-31')  # activated later,  expires later

        result = self._processor()._select_best_license([lic_first, lic_second])
        self.assertEqual(result['uuid'], 'lic-first',
                         "Earliest activation_date must win regardless of expiration_date")

    def test_latest_expiration_wins_when_activation_dates_equal(self):
        """
        Secondary rule: when activation_date is identical,
        the license with the latest expiration_date is preferred.
        """
        shared_activation = '2024-03-01'
        lic_short = self._make_license('lic-short', shared_activation, '2025-06-30')
        lic_long  = self._make_license('lic-long',  shared_activation, '2026-12-31')

        result = self._processor()._select_best_license([lic_short, lic_long])
        self.assertEqual(result['uuid'], 'lic-long',
                         "When activation_date ties, latest expiration_date must win")

    def test_uuid_desc_is_stable_fallback(self):
        """
        When both activation_date and expiration_date are identical,
        UUID descending order is the deterministic fallback.
        """
        shared_activation = '2024-03-01'
        shared_expiration = '2026-12-31'
        lic_a = self._make_license('aaa-111', shared_activation, shared_expiration)
        lic_b = self._make_license('zzz-999', shared_activation, shared_expiration)
        # 'zzz-999' > 'aaa-111' lexicographically, so UUID DESC → zzz-999 wins
        result = self._processor()._select_best_license([lic_a, lic_b])
        self.assertEqual(result['uuid'], 'zzz-999')

    def test_result_is_stable_regardless_of_input_order(self):
        """Same winner regardless of which order licenses are passed in."""
        lic_first  = self._make_license('lic-first',  '2023-01-15', '2025-06-30')
        lic_second = self._make_license('lic-second', '2024-03-01', '2026-12-31')

        proc = self._processor()
        r1 = proc._select_best_license([lic_first, lic_second])
        r2 = proc._select_best_license([lic_second, lic_first])
        self.assertEqual(r1['uuid'], r2['uuid'])

    def test_three_licenses_picks_first_activated(self):
        """With three candidates, the one with the earliest activation_date wins."""
        lic_a = self._make_license('lic-a', '2024-05-01', '2026-12-31')
        lic_b = self._make_license('lic-b', '2022-11-01', '2025-06-30')  # earliest activation
        lic_c = self._make_license('lic-c', '2023-08-15', '2027-01-01')

        result = self._processor()._select_best_license([lic_a, lic_b, lic_c])
        self.assertEqual(result['uuid'], 'lic-b',
                         "License activated in 2022 must beat those activated in 2023 and 2024")

    def test_missing_activation_date_sorts_last(self):
        """
        A license with no activation_date should lose to one that has a date,
        because None/empty is replaced by '9999-12-31' (sorts last ascending).
        """
        lic_with_date    = self._make_license('lic-dated',   '2024-01-01', '2026-12-31')
        lic_without_date = self._make_license('lic-no-date', None,         '2026-12-31')

        result = self._processor()._select_best_license([lic_with_date, lic_without_date])
        self.assertEqual(result['uuid'], 'lic-dated',
                         "License with an actual activation_date must beat one with no date")


class TestMapCoursesToLicenses(TestHandlerContextMixin):
    """
    Tests for BaseLearnerPortalHandler._map_courses_to_licenses.
    Verifies per-course license matching using the real _select_best_license implementation.
    """

    def _make_handler(self):
        class _DummyHandler(BaseLearnerPortalHandler):
            pass
        context = HandlerContext(self.request)
        return _DummyHandler(context)

    def _make_license(self, uuid, activation_date, expiration_date, catalog_uuid):
        return {
            'uuid': uuid,
            'status': 'activated',
            'activation_date': activation_date,
            'subscription_plan': {
                'enterprise_catalog_uuid': catalog_uuid,
                'is_current': True,
                'expiration_date': expiration_date,
            },
        }

    def test_each_course_mapped_to_its_catalog_license(self):
        """Three courses in three separate catalogs each map to the correct license."""
        licenses = [
            self._make_license('lic-1', '2024-01-01', '2026-01-01', 'catalog-a'),
            self._make_license('lic-2', '2024-01-01', '2026-01-01', 'catalog-b'),
            self._make_license('lic-3', '2024-01-01', '2026-01-01', 'catalog-c'),
        ]
        intentions = [
            {'course_run_key': 'course-a', 'applicable_enterprise_catalog_uuids': ['catalog-a']},
            {'course_run_key': 'course-b', 'applicable_enterprise_catalog_uuids': ['catalog-b']},
            {'course_run_key': 'course-c', 'applicable_enterprise_catalog_uuids': ['catalog-c']},
        ]
        handler = self._make_handler()
        with mock.patch.object(
            type(handler), 'current_activated_licenses',
            new_callable=mock.PropertyMock, return_value=licenses,
        ):
            mappings = handler._map_courses_to_licenses(intentions)
        self.assertEqual(mappings['course-a'], 'lic-1')
        self.assertEqual(mappings['course-b'], 'lic-2')
        self.assertEqual(mappings['course-c'], 'lic-3')

    def test_overlapping_catalogs_picks_earliest_activation(self):
        """
        ENT-11672: when a course is in two catalogs covered by different licenses,
        the license activated FIRST wins (not the one with the latest expiration).
        """
        licenses = [
            # license-1: activated later, expires later
            self._make_license('license-1', '2024-01-01', '2027-01-01', 'catalog-1'),
            # license-2: activated earlier, expires sooner — should WIN
            self._make_license('license-2', '2023-01-01', '2026-01-01', 'catalog-2'),
        ]
        intentions = [
            {
                'course_run_key': 'course-overlap',
                'applicable_enterprise_catalog_uuids': ['catalog-1', 'catalog-2'],
            },
        ]
        handler = self._make_handler()
        with mock.patch.object(
            type(handler), 'current_activated_licenses',
            new_callable=mock.PropertyMock, return_value=licenses,
        ):
            mappings = handler._map_courses_to_licenses(intentions)
        self.assertEqual(mappings['course-overlap'], 'license-2',
                         "license-2 activated in 2023 must beat license-1 activated in 2024")

    def test_course_with_no_matching_license_is_omitted(self):
        """A course whose catalog has no active license must not appear in the result."""
        licenses = [self._make_license('lic-1', '2024-01-01', '2026-01-01', 'catalog-a')]
        intentions = [
            {'course_run_key': 'course-x', 'applicable_enterprise_catalog_uuids': ['catalog-z']},
        ]
        handler = self._make_handler()
        with mock.patch.object(
            type(handler), 'current_activated_licenses',
            new_callable=mock.PropertyMock, return_value=licenses,
        ):
            mappings = handler._map_courses_to_licenses(intentions)
        self.assertNotIn('course-x', mappings)

    def test_no_activated_licenses_returns_empty(self):
        """When the learner has no activated licenses, the mapping is empty."""
        intentions = [
            {'course_run_key': 'course-x', 'applicable_enterprise_catalog_uuids': ['catalog-a']},
        ]
        handler = self._make_handler()
        with mock.patch.object(
            type(handler), 'current_activated_licenses',
            new_callable=mock.PropertyMock, return_value=[],
        ):
            mappings = handler._map_courses_to_licenses(intentions)
        self.assertEqual(mappings, {})

    def test_single_catalog_course_maps_directly(self):
        """A course in a single catalog picks that catalog's license with no tie-breaking needed."""
        licenses = [self._make_license('lic-only', '2024-01-01', '2026-01-01', 'catalog-a')]
        intentions = [
            {'course_run_key': 'course-a', 'applicable_enterprise_catalog_uuids': ['catalog-a']},
        ]
        handler = self._make_handler()
        with mock.patch.object(
            type(handler), 'current_activated_licenses',
            new_callable=mock.PropertyMock, return_value=licenses,
        ):
            mappings = handler._map_courses_to_licenses(intentions)
        self.assertEqual(mappings['course-a'], 'lic-only')


class TestScopedAlgoliaRefresh(TestHandlerContextMixin):
    """Tests for scoping the secured Algolia key to the learner's activated catalogs."""

    def _make_handler(self):
        handler = BaseLearnerPortalHandler.__new__(BaseLearnerPortalHandler)
        class SimpleContext:
            def __init__(self):
                self.data = {}
                self.refresh_secured_algolia_api_keys = mock.Mock()
        handler.context = SimpleContext()
        return handler

    def _make_license(self, uuid, catalog_uuid):
        return {
            'uuid': uuid,
            'status': LicenseStatuses.ACTIVATED,
            'activation_date': '2024-01-01',
            'subscription_plan': {
                'enterprise_catalog_uuid': catalog_uuid,
                'is_current': True,
                'expiration_date': '2026-01-01',
            },
        }

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=True)
    def test_scope_secured_algolia_api_keys_to_activated_licenses(self, mock_toggle):
        handler = self._make_handler()
        licenses = [
            self._make_license('lic-1', 'catalog-a'),
            self._make_license('lic-2', 'catalog-b'),
        ]
        
        # Manually attach properties to avoid PropertyMock complexity if any.
        # However, it should work fine as a PropertyMock.
        with mock.patch.object(type(handler), 'current_activated_licenses', new_callable=mock.PropertyMock, return_value=licenses):
            handler.scope_secured_algolia_api_keys_to_activated_licenses()

        handler.context.refresh_secured_algolia_api_keys.assert_called_once()
        called_args = handler.context.refresh_secured_algolia_api_keys.call_args[1]['catalog_uuids']
        self.assertSetEqual(set(called_args), {'catalog-a', 'catalog-b'}, f"catalog_uuids was: {called_args}")


class TestBaseLearnerPortalHandler(TestHandlerContextMixin):
    """
    Test BaseLearnerPortalHandler
    """

    def setUp(self):
        super().setUp()
        self.expected_enterprise_customer = {
            **self.mock_enterprise_customer,
            'disable_search': False,
            'show_integration_warning': True,
        }
        self.expected_enterprise_customer_2 = {
            **self.mock_enterprise_customer_2,
            'disable_search': False,
            'show_integration_warning': False,
        }
        self.mock_subscription_licenses_data = {
            'customer_agreement': None,
            'results': [],
        }
        self.mock_default_enterprise_enrollment_intentions_learner_status_data = {
            "lms_user_id": self.mock_user.id,
            "user_email": self.mock_user.email,
            "enterprise_customer_uuid": self.mock_enterprise_customer_uuid,
            "enrollment_statuses": {
                "needs_enrollment": {
                    "enrollable": [],
                    "not_enrollable": [],
                },
                'already_enrolled': [],
            },
            "metadata": {
                "total_default_enterprise_enrollment_intentions": 0,
                "total_needs_enrollment": {
                    "enrollable": 0,
                    "not_enrollable": 0
                },
                "total_already_enrolled": 0
            }
        }

    def get_expected_enterprise_customer(self, enterprise_customer_user):
        enterprise_customer = enterprise_customer_user.get('enterprise_customer')
        return (
            self.expected_enterprise_customer
            if enterprise_customer.get('uuid') == self.mock_enterprise_customer_uuid
            else self.expected_enterprise_customer_2
        )

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @mock.patch(
        'enterprise_access.apps.api_client.license_manager_client.LicenseManagerUserApiClient'
        '.get_subscription_licenses_for_learner'
    )
    @mock.patch(
        'enterprise_access.apps.api_client.lms_client.LmsUserApiClient'
        '.get_default_enterprise_enrollment_intentions_learner_status'
    )
    @mock.patch(
        'enterprise_access.apps.api_client.enterprise_catalog_client'
        '.EnterpriseCatalogUserV1ApiClient.get_secured_algolia_api_key'
    )
    def test_load_and_process(
        self,
        mock_get_secured_algolia_api_key_for_user,
        mock_get_default_enrollment_intentions_learner_status,
        mock_get_subscription_licenses_for_learner,
        mock_get_enterprise_customers_for_user,
    ):
        """
        Test load_and_process method
        """
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        mock_get_subscription_licenses_for_learner.return_value = self.mock_subscription_licenses_data
        mock_get_secured_algolia_api_key_for_user.return_value = self.mock_secured_algolia_api_key_response
        mock_get_default_enrollment_intentions_learner_status.return_value =\
            self.mock_default_enterprise_enrollment_intentions_learner_status_data

        context = HandlerContext(self.request)
        handler = BaseLearnerPortalHandler(context)

        handler.load_and_process()

        # Enterprise Customer related assertions
        actual_enterprise_customer = handler.context.data.get('enterprise_customer')
        actual_active_enterprise_customer = handler.context.data.get('active_enterprise_customer')
        actual_linked_ecus = handler.context.data.get('all_linked_enterprise_customer_users')
        expected_linked_ecus = [
            {
                **enterprise_customer_user,
                'enterprise_customer': self.get_expected_enterprise_customer(enterprise_customer_user),
            }
            for enterprise_customer_user in self.mock_enterprise_learner_response_data['results']
        ]
        actual_staff_enterprise_customer = handler.context.data.get('staff_enterprise_customer')
        expected_staff_enterprise_customer = None
        self.assertEqual(actual_enterprise_customer, self.expected_enterprise_customer)
        self.assertEqual(actual_active_enterprise_customer, self.expected_enterprise_customer)
        self.assertEqual(actual_linked_ecus, expected_linked_ecus)
        self.assertEqual(actual_staff_enterprise_customer, expected_staff_enterprise_customer)

        # Base subscriptions related assertions
        actual_subscriptions = handler.context.data['enterprise_customer_user_subsidies']['subscriptions']
        expected_subscriptions = {
            'customer_agreement': None,
            'subscription_licenses': [],
            'subscription_licenses_by_status': {},
            'licenses_by_catalog': {},
            'license_schema_version': 'v1',  # waffle flag is OFF in tests
            'subscription_license': None,
            'subscription_plan': None,
            'show_expiration_notifications': False,
        }
        self.assertEqual(actual_subscriptions, expected_subscriptions)

        # Default enterprise enrollment intentions related assertions
        actual_default_enterprise_enrollment_intentions = (
            handler.context.data.get('default_enterprise_enrollment_intentions')
        )
        expected_default_enterprise_enrollment_intentions = (
            self.mock_default_enterprise_enrollment_intentions_learner_status_data
        )
        self.assertEqual(
            actual_default_enterprise_enrollment_intentions,
            expected_default_enterprise_enrollment_intentions
        )

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    def test_load_and_process_without_learner_portal_enabled(self, mock_get_enterprise_customers_for_user):
        """
        Test load_and_process method without learner portal enabled. No enterprise
        customer metadata should be returned.
        """
        mock_customer_without_learner_portal = {
            **self.mock_enterprise_customer,
            'enable_learner_portal': False,
        }
        mock_get_enterprise_customers_for_user.return_value = {
            **self.mock_enterprise_learner_response_data,
            'results': [
                {
                    'active': True,
                    'enterprise_customer': mock_customer_without_learner_portal,
                },
                {
                    'active': False,
                    'enterprise_customer': self.mock_enterprise_customer_2,
                },
            ],
        }
        context = HandlerContext(self.request)
        handler = BaseLearnerPortalHandler(context)

        handler.load_and_process()

        actual_enterprise_customer = handler.context.data.get('enterprise_customer')
        actual_active_enterprise_customer = handler.context.data.get('active_enterprise_customer')
        actual_linked_ecus = handler.context.data.get('all_linked_enterprise_customer_users')

        # Assert enterprise_customer and active_enterprise_customer are None
        self.assertEqual(actual_enterprise_customer, None)
        self.assertEqual(actual_active_enterprise_customer, None)

        # Assert only the enterprise customer with learner portal enabled is returned
        self.assertEqual(len(actual_linked_ecus), 1)
        self.assertEqual(actual_linked_ecus[0]['enterprise_customer'], self.expected_enterprise_customer_2)

        # Assert warnings added for enterprise customers without learner portal enabled
        self.assertEqual(len(handler.context.warnings), 2)
        expected_warning_user_message = 'Learner portal not enabled for enterprise customer'

        def _expected_warning_developer_message(customer_record_key):
            return (
                f"[{customer_record_key}] Learner portal not enabled for enterprise customer "
                f"{mock_customer_without_learner_portal.get('uuid')} for request user {self.mock_user.lms_user_id}"
            )

        self.assertEqual(handler.context.warnings[0]['user_message'], expected_warning_user_message)
        self.assertEqual(
            handler.context.warnings[0]['developer_message'],
            _expected_warning_developer_message(customer_record_key='enterprise_customer')
        )
        self.assertEqual(handler.context.warnings[1]['user_message'], expected_warning_user_message)
        self.assertEqual(
            handler.context.warnings[1]['developer_message'],
            _expected_warning_developer_message(customer_record_key='active_enterprise_customer')
        )

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsApiClient.get_enterprise_customer_data')
    def test_load_and_process_staff_enterprise_customer(
        self,
        mock_get_enterprise_customer_data,
        mock_get_enterprise_customers_for_user,
    ):
        mock_get_enterprise_customers_for_user.return_value = {
            **self.mock_enterprise_learner_response_data,
            'results': [],
        }
        mock_get_enterprise_customer_data.return_value = self.mock_enterprise_customer
        request = self.request
        request.user = self.mock_staff_user
        context = HandlerContext(request)
        handler = BaseLearnerPortalHandler(context)

        handler.load_and_process()

        actual_enterprise_customer = handler.context.data.get('enterprise_customer')
        expected_enterprise_customer = self.expected_enterprise_customer
        self.assertEqual(actual_enterprise_customer, expected_enterprise_customer)
        actual_staff_enterprise_customer = handler.context.data.get('staff_enterprise_customer')
        expected_staff_enterprise_customer = self.expected_enterprise_customer
        self.assertEqual(actual_staff_enterprise_customer, expected_staff_enterprise_customer)

    @mock.patch(
        'enterprise_access.apps.api_client.enterprise_catalog_client'
        '.EnterpriseCatalogUserV1ApiClient.get_secured_algolia_api_key'
    )
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsApiClient.bulk_enroll_enterprise_learners')
    def test_request_default_enrollment_realizations(
        self,
        mock_bulk_enroll,
        mock_get_customers,
        mock_get_secured_algolia_api_key_for_user,
    ):
        mock_get_customers.return_value = self.mock_enterprise_learner_response_data
        mock_get_secured_algolia_api_key_for_user.return_value = self.mock_secured_algolia_api_key_response
        license_uuids_by_course_run_key = {
            'course-run-1': 'license-1',
            'course-run-2': 'license-2',
        }
        context = HandlerContext(self.request)
        handler = BaseLearnerPortalHandler(context)

        response = handler._request_default_enrollment_realizations(license_uuids_by_course_run_key)

        self.assertEqual(response, mock_bulk_enroll.return_value)
        actual_customer_uuid_arg, actual_payload_arg = mock_bulk_enroll.call_args_list[0][0]
        self.assertEqual(actual_customer_uuid_arg, context.enterprise_customer_uuid)
        expected_payload = [
            {'user_id': context.lms_user_id, 'course_run_key': 'course-run-1',
             'license_uuid': 'license-1', 'is_default_auto_enrollment': True},
            {'user_id': context.lms_user_id, 'course_run_key': 'course-run-2',
             'license_uuid': 'license-2', 'is_default_auto_enrollment': True},
        ]
        self.assertCountEqual(expected_payload, actual_payload_arg)
        self.assertEqual(context.errors, [])

    @mock.patch(
        'enterprise_access.apps.api_client.enterprise_catalog_client'
        '.EnterpriseCatalogUserV1ApiClient.get_secured_algolia_api_key'
    )
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsApiClient.bulk_enroll_enterprise_learners')
    def test_request_default_enrollment_realizations_exception(
        self,
        mock_bulk_enroll,
        mock_get_customers,
        mock_get_secured_algolia_api_key_for_user
    ):
        mock_get_customers.return_value = self.mock_enterprise_learner_response_data
        mock_get_secured_algolia_api_key_for_user.return_value = self.mock_secured_algolia_api_key_response
        license_uuids_by_course_run_key = {
            'course-run-1': 'license-1',
            'course-run-2': 'license-2',
        }
        context = HandlerContext(self.request)
        handler = BaseLearnerPortalHandler(context)
        mock_bulk_enroll.side_effect = Exception('foobar')

        response = handler._request_default_enrollment_realizations(license_uuids_by_course_run_key)

        self.assertEqual(response, {})
        self.assertEqual(
            context.errors,
            [{
                'developer_message': 'Default realization enrollment exception: foobar',
                'user_message': 'There was an exception realizing default enrollments',
            }],
        )

    @mock.patch(
        'enterprise_access.apps.api_client.enterprise_catalog_client'
        '.EnterpriseCatalogUserV1ApiClient.get_secured_algolia_api_key'
    )
    @mock.patch(
        'enterprise_access.apps.api_client.lms_client.LmsUserApiClient'
        '.get_enterprise_customers_for_user')
    @mock.patch(
        'enterprise_access.apps.api_client.lms_client.LmsApiClient'
        '.bulk_enroll_enterprise_learners')
    @mock.patch(
        'enterprise_access.apps.api_client.lms_client.LmsUserApiClient'
        '.get_default_enterprise_enrollment_intentions_learner_status'
    )
    def test_realize_default_enrollments(
        self, mock_get_intentions, mock_bulk_enroll, mock_get_customers, mock_get_secured_algolia_api_key_for_user
    ):
        mock_get_customers.return_value = self.mock_enterprise_learner_response_data
        mock_get_secured_algolia_api_key_for_user.return_value = self.mock_secured_algolia_api_key_response
        mock_get_intentions.return_value = {
            "lms_user_id": self.mock_user.id,
            "user_email": self.mock_user.email,
            "enterprise_customer_uuid": self.mock_enterprise_customer_uuid,
            "enrollment_statuses": {
                "needs_enrollment": {
                    "enrollable": [
                        {
                            'applicable_enterprise_catalog_uuids': ['catalog-55', 'catalog-1'],
                            'course_run_key': 'course-run-1',
                        },
                        {
                            'applicable_enterprise_catalog_uuids': ['catalog-88', 'catalog-1'],
                            'course_run_key': 'course-run-2',
                        },
                    ],
                    "not_enrollable": [],
                },
                'already_enrolled': [],
            },
        }
        mock_bulk_enroll.return_value = {
            'successes': [
                {'course_run_key': 'course-run-1'},
            ],
            'failures': [
                {'course_run_key': 'course-run-2'},
            ],
        }

        context = HandlerContext(self.request)
        context.data['enterprise_customer_user_subsidies'] = {
            'subscriptions': {
                'subscription_licenses_by_status': {
                    LicenseStatuses.ACTIVATED: [{
                        'uuid': 'license-1',
                        'subscription_plan': {
                            'is_current': True,
                            'uuid': 'subscription-plan-1',
                            'enterprise_catalog_uuid': 'catalog-1',
                        },
                    }]
                }
            }
        }
        handler = BaseLearnerPortalHandler(context)

        handler.load_default_enterprise_enrollment_intentions()
        handler.enroll_in_redeemable_default_enterprise_enrollment_intentions()

        actual_customer_uuid_arg, actual_payload_arg = mock_bulk_enroll.call_args_list[0][0]
        self.assertEqual(actual_customer_uuid_arg, context.enterprise_customer_uuid)
        expected_payload = [
            {'user_id': context.lms_user_id, 'course_run_key': 'course-run-1',
             'license_uuid': 'license-1', 'is_default_auto_enrollment': True},
            {'user_id': context.lms_user_id, 'course_run_key': 'course-run-2',
             'license_uuid': 'license-1', 'is_default_auto_enrollment': True},
        ]
        self.assertCountEqual(expected_payload, actual_payload_arg)
        self.assertEqual(
            handler.context.data['default_enterprise_enrollment_realizations'],
            [{
                'course_key': 'course-run-1',
                'enrollment_status': 'enrolled',
                'subscription_license_uuid': 'license-1',
            }],
        )
        self.assertEqual(
            handler.context.errors,
            [{
                'developer_message': (
                    'Default realization enrollment failures: [{"course_run_key": "course-run-2"}]'
                ),
                'user_message': 'There were failures realizing default enrollments',
            }],
        )

        # a simple validation here that a second consecutive call to
        # load the default intentions means the handler doesn't read from the cache,
        # because the first request included enrollable intentions.
        # We make this assertion using the returned value from the mock call
        # to fetch default intention status. In a production-like setting, this
        # second call should contain data indicating that default enrollment
        # intentions were actually realized.
        handler.load_default_enterprise_enrollment_intentions()
        self.assertEqual(
            handler.context.data['default_enterprise_enrollment_intentions'],
            mock_get_intentions.return_value,
        )


class TestDashboardHandler(TestHandlerContextMixin):
    """
    Test DashboardHandler
    """

    def setUp(self):
        super().setUp()

        self.mock_original_enterprise_course_enrollment = {
            "course_run_id": "course-v1:BabsonX+MIS01x+1T2019",
            "course_run_status": "in_progress",
            "created": "2023-09-29T14:24:45.409031+00:00",
            "start_date": "2019-03-19T10:00:00Z",
            "end_date": "2024-12-31T04:30:00Z",
            "display_name": "AI for Leaders",
            "due_dates": [],
            "pacing": "self",
            "org_name": "BabsonX",
            "is_revoked": False,
            "is_enrollment_active": True,
            "mode": "verified",
            "course_run_url": "https://learning.edx.org/course/course-v1:BabsonX+MIS01x+1T2019/home",
            "certificate_download_url": None,
            "resume_course_run_url": None,
            "course_key": "BabsonX+MIS01x",
            "course_type": "verified-audit",
            "product_source": "edx",
            "enroll_by": "2024-12-21T23:59:59Z",
            "emails_enabled": False,
            "micromasters_title": None,
        }
        self.mock_transformed_enterprise_course_enrollment = {
            "course_run_id": self.mock_original_enterprise_course_enrollment['course_run_id'],
            "course_run_status": self.mock_original_enterprise_course_enrollment['course_run_status'],
            "created": self.mock_original_enterprise_course_enrollment['created'],
            "start_date": self.mock_original_enterprise_course_enrollment['start_date'],
            "end_date": self.mock_original_enterprise_course_enrollment['end_date'],
            "title": self.mock_original_enterprise_course_enrollment['display_name'],
            "notifications": self.mock_original_enterprise_course_enrollment['due_dates'],
            "pacing": self.mock_original_enterprise_course_enrollment['pacing'],
            "org_name": self.mock_original_enterprise_course_enrollment['org_name'],
            "is_revoked": self.mock_original_enterprise_course_enrollment['is_revoked'],
            "is_enrollment_active": self.mock_original_enterprise_course_enrollment['is_enrollment_active'],
            "mode": self.mock_original_enterprise_course_enrollment['mode'],
            "link_to_course": self.mock_original_enterprise_course_enrollment['course_run_url'],
            "link_to_certificate": None,
            "resume_course_run_url": None,
            "course_key": self.mock_original_enterprise_course_enrollment['course_key'],
            "course_type": self.mock_original_enterprise_course_enrollment['course_type'],
            "product_source": self.mock_original_enterprise_course_enrollment['product_source'],
            "enroll_by": self.mock_original_enterprise_course_enrollment['enroll_by'],
            "can_unenroll": True,
            "has_emails_enabled": self.mock_original_enterprise_course_enrollment['emails_enabled'],
            "micromasters_title": self.mock_original_enterprise_course_enrollment['micromasters_title'],
        }
        self.mock_original_enterprise_course_enrollments = [self.mock_original_enterprise_course_enrollment]
        self.mock_transformed_enterprise_course_enrollments = [self.mock_transformed_enterprise_course_enrollment]

    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_customers_for_user')
    @mock.patch('enterprise_access.apps.api_client.lms_client.LmsUserApiClient.get_enterprise_course_enrollments')
    def test_load_and_process(
        self,
        mock_get_enterprise_course_enrollments,
        mock_get_enterprise_customers_for_user,
    ):
        mock_get_enterprise_customers_for_user.return_value = self.mock_enterprise_learner_response_data
        mock_get_enterprise_course_enrollments.return_value = self.mock_original_enterprise_course_enrollments

        context = HandlerContext(self.request)
        dashboard_handler = DashboardHandler(context)

        dashboard_handler.load_and_process()

        self.assertEqual(
            dashboard_handler.context.data.get('enterprise_course_enrollments'),
            self.mock_transformed_enterprise_course_enrollments,
        )
