"""
Tests for the multi-license features introduced in ENT-11672.

Covers:
  - SubscriptionLicenseProcessor._select_best_license
  - SubscriptionLicenseProcessor._build_catalog_index
  - BaseLearnerPortalHandler._map_courses_to_licenses  (flag ON path)
  - BaseLearnerPortalHandler._map_courses_to_single_license (flag OFF path)
  - BaseLearnerPortalHandler.transform_subscriptions_result (flag ON/OFF)
    - Waffle flag behavior — enterprise_access.enable_multi_license_entitlements_bff
"""
from unittest import mock

from django.test import RequestFactory, TestCase, override_settings

from enterprise_access.apps.bffs.context import HandlerContext
from enterprise_access.apps.bffs.handlers import BaseLearnerPortalHandler, SubscriptionLicenseProcessor
from enterprise_access.apps.bffs.tests.utils import TestHandlerContextMixin


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_license(
    uuid,
    activation_date,
    expiration_date,
    catalog_uuid,
    status='activated',
    is_current=True,
):
    """Build a minimal license dict mirroring the License Manager API shape."""
    return {
        'uuid': uuid,
        'status': status,
        'activation_date': activation_date,
        'subscription_plan': {
            'uuid': f'plan-{uuid}',
            'enterprise_catalog_uuid': catalog_uuid,
            'is_current': is_current,
            'expiration_date': expiration_date,
        },
    }


# ---------------------------------------------------------------------------
# SubscriptionLicenseProcessor._select_best_license
# ---------------------------------------------------------------------------

class TestSelectBestLicense(TestCase):
    """Unit tests for _select_best_license."""

    def setUp(self):
        self.processor = SubscriptionLicenseProcessor()

    def test_single_license_returned_directly(self):
        """With one candidate, the same object is returned without sorting."""
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        result = self.processor._select_best_license([lic])
        self.assertIs(result, lic)

    def test_earliest_activation_wins(self):
        """
        ENT-11672 business rule: the first-activated license is preferred.
        Both licenses cover the same catalog & expiration.
        """
        early = _make_license('lic-early', '2023-01-01', '2026-12-31', 'cat-a')
        late  = _make_license('lic-late',  '2024-06-01', '2026-12-31', 'cat-a')

        result = self.processor._select_best_license([late, early])
        self.assertEqual(result['uuid'], 'lic-early')

    def test_latest_expiration_breaks_activation_tie(self):
        """
        When activation_date is identical, the license with the latest expiration wins.
        """
        shorter = _make_license('lic-short', '2024-01-01', '2025-06-30', 'cat-a')
        longer  = _make_license('lic-long',  '2024-01-01', '2026-12-31', 'cat-a')

        result = self.processor._select_best_license([shorter, longer])
        self.assertEqual(result['uuid'], 'lic-long')

    def test_uuid_descending_breaks_all_ties(self):
        """
        When activation_date and expiration_date are identical, UUID DESC is the
        final deterministic fallback.
        """
        lic_a = _make_license('zzz-license', '2024-01-01', '2026-12-31', 'cat-a')
        lic_b = _make_license('aaa-license', '2024-01-01', '2026-12-31', 'cat-a')

        result = self.processor._select_best_license([lic_a, lic_b])
        self.assertEqual(result['uuid'], 'zzz-license')

    def test_result_is_deterministic_regardless_of_input_order(self):
        """
        The same license wins whether the input list is [a, b] or [b, a].
        """
        lic_a = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        lic_b = _make_license('lic-b', '2024-06-01', '2026-12-31', 'cat-a')

        result1 = self.processor._select_best_license([lic_a, lic_b])
        result2 = self.processor._select_best_license([lic_b, lic_a])
        self.assertEqual(result1['uuid'], result2['uuid'])

    def test_none_activation_date_treated_as_far_future(self):
        """
        A license with activation_date=None sorts after a real date
        (treated as '9999-12-31'), so the real date wins (earlier).
        """
        real  = _make_license('lic-real', '2023-01-01', '2026-12-31', 'cat-a')
        unset = _make_license('lic-unset', None, '2026-12-31', 'cat-a')

        result = self.processor._select_best_license([unset, real])
        self.assertEqual(result['uuid'], 'lic-real')

    def test_three_candidates_correct_winner(self):
        """Full three-way race: earliest activation wins."""
        a = _make_license('lic-a', '2023-01-01', '2026-01-01', 'cat-a')
        b = _make_license('lic-b', '2024-01-01', '2027-01-01', 'cat-a')
        c = _make_license('lic-c', '2022-06-01', '2025-01-01', 'cat-a')  # earliest

        result = self.processor._select_best_license([a, b, c])
        self.assertEqual(result['uuid'], 'lic-c')


# ---------------------------------------------------------------------------
# SubscriptionLicenseProcessor._build_catalog_index
# ---------------------------------------------------------------------------

class TestBuildCatalogIndex(TestCase):
    """Unit tests for _build_catalog_index."""

    def setUp(self):
        self.processor = SubscriptionLicenseProcessor()

    def test_empty_list_returns_empty_dict(self):
        self.assertEqual(self.processor._build_catalog_index([]), {})

    def test_groups_licenses_by_catalog(self):
        lic1 = _make_license('lic-1', '2024-01-01', '2026-12-31', 'cat-a')
        lic2 = _make_license('lic-2', '2024-01-01', '2026-12-31', 'cat-a')
        lic3 = _make_license('lic-3', '2024-01-01', '2026-12-31', 'cat-b')

        index = self.processor._build_catalog_index([lic1, lic2, lic3])

        self.assertEqual(len(index), 2)
        self.assertCountEqual([l['uuid'] for l in index['cat-a']], ['lic-1', 'lic-2'])
        self.assertCountEqual([l['uuid'] for l in index['cat-b']], ['lic-3'])

    def test_license_without_catalog_uuid_is_excluded(self):
        lic = _make_license('lic-1', '2024-01-01', '2026-12-31', 'cat-a')
        lic['subscription_plan'].pop('enterprise_catalog_uuid', None)

        index = self.processor._build_catalog_index([lic])
        self.assertEqual(index, {})

    def test_license_with_none_catalog_uuid_is_excluded(self):
        lic = _make_license('lic-1', '2024-01-01', '2026-12-31', 'cat-a')
        lic['subscription_plan']['enterprise_catalog_uuid'] = None

        index = self.processor._build_catalog_index([lic])
        self.assertEqual(index, {})

    def test_preserves_license_object_identity(self):
        """The index contains the exact same dicts (not copies)."""
        lic = _make_license('lic-1', '2024-01-01', '2026-12-31', 'cat-a')
        index = self.processor._build_catalog_index([lic])
        self.assertIs(index['cat-a'][0], lic)


# ---------------------------------------------------------------------------
# BaseLearnerPortalHandler._map_courses_to_licenses  (multi-license path)
# ---------------------------------------------------------------------------

class TestMapCoursesToLicenses(TestHandlerContextMixin):
    """
    Integration-style tests for _map_courses_to_licenses.
    Uses the real handler so the full SubscriptionLicenseProcessor mixin is exercised.
    """

    def _make_handler(self, activated_licenses):
        """Create a handler with pre-loaded activated licenses."""
        context = HandlerContext(self.request)
        handler = BaseLearnerPortalHandler(context)
        # Bypass loading real data; inject licenses directly
        context.data['enterprise_customer_user_subsidies'] = {
            'subscriptions': {
                'subscription_licenses_by_status': {
                    'activated': activated_licenses,
                }
            }
        }
        return handler

    def test_each_course_mapped_to_its_catalog_license(self):
        """Three courses, three distinct catalogs → each maps to its own license."""
        lic_a = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        lic_b = _make_license('lic-b', '2024-01-01', '2026-12-31', 'cat-b')
        lic_c = _make_license('lic-c', '2024-01-01', '2026-12-31', 'cat-c')
        handler = self._make_handler([lic_a, lic_b, lic_c])

        intentions = [
            {'course_run_key': 'course-A', 'applicable_enterprise_catalog_uuids': ['cat-a']},
            {'course_run_key': 'course-B', 'applicable_enterprise_catalog_uuids': ['cat-b']},
            {'course_run_key': 'course-C', 'applicable_enterprise_catalog_uuids': ['cat-c']},
        ]
        mappings = handler._map_courses_to_licenses(intentions)

        self.assertEqual(mappings, {
            'course-A': 'lic-a',
            'course-B': 'lic-b',
            'course-C': 'lic-c',
        })

    def test_course_in_multiple_catalogs_uses_earliest_activated_license(self):
        """
        Course is available in both cat-a and cat-b.
        lic-b was activated earlier → wins over lic-a.
        """
        lic_a = _make_license('lic-a', '2024-06-01', '2026-12-31', 'cat-a')  # later activation
        lic_b = _make_license('lic-b', '2023-01-01', '2026-12-31', 'cat-b')  # earlier activation
        handler = self._make_handler([lic_a, lic_b])

        intentions = [
            {'course_run_key': 'course-X', 'applicable_enterprise_catalog_uuids': ['cat-a', 'cat-b']},
        ]
        mappings = handler._map_courses_to_licenses(intentions)

        self.assertEqual(mappings['course-X'], 'lic-b')

    def test_course_with_no_matching_catalog_is_omitted(self):
        """A course whose catalog has no license should be absent from the mapping."""
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler([lic])

        intentions = [
            {'course_run_key': 'course-Z', 'applicable_enterprise_catalog_uuids': ['cat-unknown']},
        ]
        mappings = handler._map_courses_to_licenses(intentions)

        self.assertEqual(mappings, {})

    def test_no_activated_licenses_returns_empty(self):
        """Handler with zero activated licenses returns empty mapping."""
        handler = self._make_handler([])

        intentions = [
            {'course_run_key': 'course-A', 'applicable_enterprise_catalog_uuids': ['cat-a']},
        ]
        mappings = handler._map_courses_to_licenses(intentions)

        self.assertEqual(mappings, {})

    def test_empty_intentions_returns_empty(self):
        """Empty enrollment intentions list → empty mapping."""
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler([lic])

        mappings = handler._map_courses_to_licenses([])
        self.assertEqual(mappings, {})

    def test_duplicate_licenses_from_multiple_catalogs_deduped(self):
        """
        If the same license appears in two catalogs and a course is in both,
        that license should still only be counted once in the selection.
        """
        lic = _make_license('lic-shared', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler([lic])

        # Manually add a second catalog to the index that maps to the same license
        # by using a course that lists two catalogs — only cat-a has a license
        intentions = [
            {'course_run_key': 'course-X', 'applicable_enterprise_catalog_uuids': ['cat-a', 'cat-a']},
        ]
        mappings = handler._map_courses_to_licenses(intentions)
        self.assertEqual(mappings, {'course-X': 'lic-shared'})

    def test_overlapping_catalogs_picks_latest_expiration_when_activation_ties(self):
        """
        Two licenses in the same catalog, same activation date.
        The one with later expiration is the secondary tie-breaker.
        """
        lic_short = _make_license('lic-short', '2024-01-01', '2025-06-30', 'cat-a')
        lic_long  = _make_license('lic-long',  '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler([lic_short, lic_long])

        intentions = [
            {'course_run_key': 'course-X', 'applicable_enterprise_catalog_uuids': ['cat-a']},
        ]
        mappings = handler._map_courses_to_licenses(intentions)
        self.assertEqual(mappings['course-X'], 'lic-long')


# ---------------------------------------------------------------------------
# BaseLearnerPortalHandler._map_courses_to_single_license  (legacy path)
# ---------------------------------------------------------------------------

class TestMapCoursesToSingleLicense(TestHandlerContextMixin):
    """Tests for the legacy (flag OFF) single-license course mapping."""

    def _make_handler_with_license(self, license_dict):
        context = HandlerContext(self.request)
        handler = BaseLearnerPortalHandler(context)
        context.data['enterprise_customer_user_subsidies'] = {
            'subscriptions': {
                'subscription_licenses_by_status': {
                    'activated': [license_dict] if license_dict else [],
                }
            }
        }
        return handler

    def test_course_in_license_catalog_is_mapped(self):
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler_with_license(lic)

        intentions = [
            {'course_run_key': 'course-A', 'applicable_enterprise_catalog_uuids': ['cat-a']},
        ]
        mappings = handler._map_courses_to_single_license(intentions)
        self.assertEqual(mappings, {'course-A': 'lic-a'})

    def test_course_not_in_license_catalog_is_omitted(self):
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler_with_license(lic)

        intentions = [
            {'course_run_key': 'course-B', 'applicable_enterprise_catalog_uuids': ['cat-b']},
        ]
        mappings = handler._map_courses_to_single_license(intentions)
        self.assertEqual(mappings, {})

    def test_no_activated_license_returns_empty(self):
        handler = self._make_handler_with_license(None)

        intentions = [
            {'course_run_key': 'course-A', 'applicable_enterprise_catalog_uuids': ['cat-a']},
        ]
        mappings = handler._map_courses_to_single_license(intentions)
        self.assertEqual(mappings, {})


# ---------------------------------------------------------------------------
# transform_subscriptions_result — waffle flag ON vs OFF
# ---------------------------------------------------------------------------

class TestTransformSubscriptionsResult(TestHandlerContextMixin):
    """
    Tests for BaseLearnerPortalHandler.transform_subscriptions_result,
    focusing on the waffle-flag-gated fields:
      - licenses_by_catalog (populated only when flag is ON)
      - license_schema_version ('v2' when ON, 'v1' when OFF)
    """

    def _make_handler(self, licenses):
        context = HandlerContext(self.request)
        context.data = {
            'enterprise_customer_user_subsidies': {
                'subscriptions': {
                    'subscription_licenses_by_status': {
                        'activated': licenses,
                    },
                },
            },
        }
        return BaseLearnerPortalHandler(context)

    def _subscriptions_result(self, licenses):
        return {'results': licenses, 'customer_agreement': None}

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=False)
    def test_flag_off_returns_v1_schema_and_empty_catalog_index(self, _mock_flag):
        """With flag OFF: license_schema_version='v1', licenses_by_catalog={}, but flat list matches main."""
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler([lic])

        result = handler.transform_subscriptions_result(self._subscriptions_result([lic]))

        self.assertEqual(result['license_schema_version'], 'v1')
        self.assertEqual(result['licenses_by_catalog'], {})
        # v1 preserves main behavior: flat list still exposes all licenses returned by the API
        self.assertEqual(len(result['subscription_licenses']), 1)
        self.assertEqual(result['subscription_licenses'][0]['uuid'], 'lic-a')

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=False)
    def test_flag_off_multiple_licenses_preserves_main_flat_list(self, _mock_flag):
        """
        v1 (flag OFF): preserve legacy single-license behavior by exposing only the
        selected singular license in subscription_licenses.
        """
        lic_a = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        lic_b = _make_license('lic-b', '2024-03-01', '2025-06-30', 'cat-b')
        lic_c = _make_license('lic-c', '2024-06-01', '2027-01-01', 'cat-c')
        handler = self._make_handler([lic_a, lic_b, lic_c])

        result = handler.transform_subscriptions_result(
            self._subscriptions_result([lic_a, lic_b, lic_c])
        )

        self.assertEqual(len(result['subscription_licenses']), 3)
        self.assertCountEqual([license['uuid'] for license in result['subscription_licenses']], ['lic-a', 'lic-b', 'lic-c'])
        self.assertEqual(len(result['subscription_licenses_by_status'].get('activated', [])), 3)
        self.assertEqual(result['license_schema_version'], 'v1')
        self.assertEqual(result['licenses_by_catalog'], {})
        self.assertEqual(result['subscription_license']['uuid'], 'lic-a')



class TestScopeSecuredAlgoliaByFlag(TestHandlerContextMixin):
    """Tests that Algolia scoping follows legacy single-license vs multi-license flag behavior."""

    def _subscriptions_result(self, licenses):
        return {'results': licenses, 'customer_agreement': None}

    def _make_handler(self, licenses, selected_license=None):
        context = HandlerContext(self.request)
        handler = BaseLearnerPortalHandler(context)
        context.data['enterprise_customer_user_subsidies'] = {
            'subscriptions': {
                'subscription_licenses_by_status': {
                    'activated': licenses,
                },
                'subscription_license': selected_license,
            }
        }
        handler.context.refresh_secured_algolia_api_keys = mock.Mock()
        return handler

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=False)
    def test_flag_off_scopes_to_single_selected_license_catalog(self, _mock_flag):
        lic_a = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        lic_b = _make_license('lic-b', '2024-02-01', '2026-12-31', 'cat-b')
        handler = self._make_handler([lic_a, lic_b], selected_license=lic_a)

        handler.scope_secured_algolia_api_keys_to_activated_licenses()

        handler.context.refresh_secured_algolia_api_keys.assert_called_once_with(catalog_uuids={'cat-a'})

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=True)
    def test_flag_on_scopes_to_all_activated_license_catalogs(self, _mock_flag):
        lic_a = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        lic_b = _make_license('lic-b', '2024-02-01', '2026-12-31', 'cat-b')
        handler = self._make_handler([lic_a, lic_b], selected_license=lic_a)

        handler.scope_secured_algolia_api_keys_to_activated_licenses()

        handler.context.refresh_secured_algolia_api_keys.assert_called_once_with(catalog_uuids={'cat-a', 'cat-b'})

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=True)
    def test_flag_on_returns_v2_schema_and_populated_catalog_index(self, _mock_flag):
        """With flag ON: license_schema_version='v2', licenses_by_catalog is built."""
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler([lic])

        result = handler.transform_subscriptions_result(self._subscriptions_result([lic]))

        self.assertEqual(result['license_schema_version'], 'v2')
        self.assertIn('cat-a', result['licenses_by_catalog'])
        self.assertEqual(len(result['licenses_by_catalog']['cat-a']), 1)
        self.assertEqual(result['licenses_by_catalog']['cat-a'][0]['uuid'], 'lic-a')

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=True)
    def test_flag_on_indexes_only_activated_licenses(self, _mock_flag):
        """licenses_by_catalog only includes ACTIVATED licenses, not assigned/revoked."""
        act = _make_license('lic-act',  '2024-01-01', '2026-12-31', 'cat-a', status='activated')
        asgn = _make_license('lic-asgn', '2024-01-01', '2026-12-31', 'cat-b', status='assigned')
        revk = _make_license('lic-rev',  '2024-01-01', '2026-12-31', 'cat-c', status='revoked')
        handler = self._make_handler([act, asgn, revk])

        result = handler.transform_subscriptions_result(
            self._subscriptions_result([act, asgn, revk])
        )

        self.assertIn('cat-a', result['licenses_by_catalog'])
        self.assertNotIn('cat-b', result['licenses_by_catalog'])
        self.assertNotIn('cat-c', result['licenses_by_catalog'])

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=False)
    def test_legacy_fields_always_present_regardless_of_flag(self, _mock_flag):
        """subscription_license and subscription_plan are present in both flag states."""
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler([lic])

        result = handler.transform_subscriptions_result(self._subscriptions_result([lic]))

        self.assertIsNotNone(result['subscription_license'])
        self.assertEqual(result['subscription_license']['uuid'], 'lic-a')
        self.assertIsNotNone(result['subscription_plan'])

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=True)
    def test_no_licenses_returns_empty_catalog_index(self, _mock_flag):
        """Zero licenses: licenses_by_catalog stays empty dict even with flag ON."""
        handler = self._make_handler([])

        result = handler.transform_subscriptions_result(self._subscriptions_result([]))

        self.assertEqual(result['licenses_by_catalog'], {})
        self.assertEqual(result['license_schema_version'], 'v2')
        self.assertIsNone(result['subscription_license'])

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=True)
    def test_three_licenses_three_catalogs_indexed_correctly(self, _mock_flag):
        """Multi-license (Knotion) scenario: 3 licenses → 3 catalog buckets."""
        lics = [
            _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a'),
            _make_license('lic-b', '2024-01-01', '2026-12-31', 'cat-b'),
            _make_license('lic-c', '2024-01-01', '2026-12-31', 'cat-c'),
        ]
        handler = self._make_handler(lics)

        result = handler.transform_subscriptions_result(self._subscriptions_result(lics))

        self.assertEqual(len(result['licenses_by_catalog']), 3)
        for catalog_key in ('cat-a', 'cat-b', 'cat-c'):
            self.assertIn(catalog_key, result['licenses_by_catalog'])
            self.assertEqual(len(result['licenses_by_catalog'][catalog_key]), 1)


# ---------------------------------------------------------------------------
# Waffle flag end-to-end routing in enroll_in_redeemable
# ---------------------------------------------------------------------------

class TestWaffleFlagRouting(TestHandlerContextMixin):
    """
    Verifies that enroll_in_redeemable_default_enterprise_enrollment_intentions
    calls the correct course-mapping method based on the waffle flag state.
    """

    def _make_handler_with_intention(self, activated_license):
        context = HandlerContext(self.request)
        handler = BaseLearnerPortalHandler(context)
        context.data['enterprise_customer_user_subsidies'] = {
            'subscriptions': {
                'subscription_licenses_by_status': {
                    'activated': [activated_license],
                }
            }
        }
        context.data['default_enterprise_enrollment_intentions'] = {
            'enrollment_statuses': {
                'needs_enrollment': {
                    'enrollable': [
                        {
                            'course_run_key': 'course-run-1',
                            'applicable_enterprise_catalog_uuids': ['cat-a'],
                        },
                    ],
                    'not_enrollable': [],
                },
                'already_enrolled': [],
            },
        }
        return handler

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=True)
    @mock.patch.object(BaseLearnerPortalHandler, '_map_courses_to_licenses', return_value={})
    @mock.patch.object(BaseLearnerPortalHandler, '_map_courses_to_single_license', return_value={})
    def test_flag_on_calls_multi_license_mapping(
        self, mock_single, mock_multi, _mock_flag
    ):
        """When flag is ON, _map_courses_to_licenses is called."""
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler_with_intention(lic)

        handler.enroll_in_redeemable_default_enterprise_enrollment_intentions()

        mock_multi.assert_called_once()
        mock_single.assert_not_called()

    @mock.patch('enterprise_access.apps.bffs.handlers.enable_multi_license_entitlements_bff', return_value=False)
    @mock.patch.object(BaseLearnerPortalHandler, '_map_courses_to_licenses', return_value={})
    @mock.patch.object(BaseLearnerPortalHandler, '_map_courses_to_single_license', return_value={})
    def test_flag_off_calls_single_license_mapping(
        self, mock_single, mock_multi, _mock_flag
    ):
        """When flag is OFF, fallback to _map_courses_to_single_license (legacy)."""
        lic = _make_license('lic-a', '2024-01-01', '2026-12-31', 'cat-a')
        handler = self._make_handler_with_intention(lic)

        handler.enroll_in_redeemable_default_enterprise_enrollment_intentions()

        mock_single.assert_called_once()
        mock_multi.assert_not_called()
