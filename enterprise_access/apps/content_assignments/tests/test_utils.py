"""
Tests for Enterprise Access content_assignments utils.
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import ddt
from django.test import TestCase
from pytz import UTC

from enterprise_access.apps.api_client.tests.test_constants import DATE_FORMAT_ISO_8601
from enterprise_access.apps.content_assignments.constants import (
    BRAZE_TIMESTAMP_FORMAT,
    AssignmentAutomaticExpiredReason
)
from enterprise_access.apps.content_assignments.content_metadata_api import get_normalized_metadata_for_assignment
from enterprise_access.apps.content_assignments.tests.factories import LearnerContentAssignmentFactory
from enterprise_access.apps.content_assignments.utils import (
    get_self_paced_normalized_start_date,
    has_time_to_complete,
    is_within_minimum_start_date_threshold
)
from enterprise_access.utils import (
    _curr_date,
    _days_from_now,
    _get_latest_course_run_end_date,
    get_automatic_expiration_date_and_reason,
    get_course_run_metadata_for_assignment
)

UTILS_MODULE = 'enterprise_access.utils'
CONTENT_METADATA_API_MODULE = 'enterprise_access.apps.content_assignments.content_metadata_api'
RUN_1 = 'course-v1:edX+Test+1'
RUN_2 = 'course-v1:edX+Test+2'

mock_course_run_1 = {
    'start_date': _days_from_now(-370, DATE_FORMAT_ISO_8601),
    'end_date': _days_from_now(350, DATE_FORMAT_ISO_8601),
    'enroll_by_date': _days_from_now(-363, DATE_FORMAT_ISO_8601),
    'enroll_start_date': _days_from_now(-380, DATE_FORMAT_ISO_8601),
    'content_price': 100,
}

mock_course_run_2 = {
    'start_date': _days_from_now(-70, DATE_FORMAT_ISO_8601),
    'end_date': _days_from_now(50, DATE_FORMAT_ISO_8601),
    'enroll_by_date': _days_from_now(-63, DATE_FORMAT_ISO_8601),
    'enroll_start_date': _days_from_now(-80, DATE_FORMAT_ISO_8601),
    'content_price': 100,
}

mock_advertised_course_run = {
    'start_date': _days_from_now(-10, DATE_FORMAT_ISO_8601),
    'end_date': _days_from_now(10, DATE_FORMAT_ISO_8601),
    'enroll_by_date': _days_from_now(-3, DATE_FORMAT_ISO_8601),
    'enroll_start_date': _days_from_now(-20, DATE_FORMAT_ISO_8601),
    'content_price': 100,
}

assignment_uuid = uuid.uuid4()
advertised_course_run_uuid = uuid.uuid4()


@ddt.ddt
class UtilsTests(TestCase):
    """
    Tests related to utility functions for content assignments
    """
    def setUp(self):
        super().setUp()
        self.mock_course_key = "edX+DemoX"
        self.mock_course_run_key_1 = "course-v1:edX+DemoX+1T360"
        self.mock_course_run_key_2 = "course-v1:edX+DemoX+1T60"
        self.mock_advertised_course_run_key = 'course-v1:edX+DemoX+1T0'
        self.mock_course_run_1 = mock_course_run_1
        self.mock_course_run_2 = mock_course_run_2
        self.mock_advertised_course_run = mock_advertised_course_run
        self.mock_content_metadata = {
            'normalized_metadata': self.mock_advertised_course_run,
            'normalized_metadata_by_run': {
                "course-v1:edX+DemoX+1T360": self.mock_course_run_1,
                "course-v1:edX+DemoX+1T60": self.mock_course_run_2,
                'course-v1:edX+DemoX+1T0': self.mock_advertised_course_run
            }
        }

    @ddt.data(
        # Start after is before the curr_date - START_DATE_DEFAULT_TO_TODAY_THRESHOLD_DAYS
        {
            "start_date": _days_from_now(5),
            "curr_date": _curr_date(),
            "expected_output": False
        },
        # Start after is before the curr_date - START_DATE_DEFAULT_TO_TODAY_THRESHOLD_DAYS

        {
            "start_date": _days_from_now(-5),
            "curr_date": _curr_date(),
            "expected_output": False
        },
        # Start after is before the curr_date - START_DATE_DEFAULT_TO_TODAY_THRESHOLD_DAYS
        {
            "start_date": _days_from_now(15),
            "curr_date": _curr_date(),
            "expected_output": False
        },
        # Start date is before the curr_date - START_DATE_DEFAULT_TO_TODAY_THRESHOLD_DAYS
        {
            "start_date": _days_from_now(-15),
            "curr_date": _curr_date(),
            "expected_output": True
        },
        # Start after is before the curr_date - START_DATE_DEFAULT_TO_TODAY_THRESHOLD_DAYS
        {
            "start_date": _curr_date(),
            "curr_date": _curr_date(),
            "expected_output": False
        }
    )
    @ddt.unpack
    def test_is_within_minimum_start_date_threshold(self, start_date, curr_date, expected_output):
        assert is_within_minimum_start_date_threshold(curr_date, start_date) == expected_output

    @ddt.data(
        # endDate is the exact day as weeks to complete offset
        {
            "end_date": _days_from_now(49),
            "curr_date": _curr_date(),
            "weeks_to_complete": 7,
            "expected_output": True
        },
        # weeks to complete is within endDate
        {
            "end_date": _days_from_now(49),
            "curr_date": _curr_date(),
            "weeks_to_complete": 4,
            "expected_output": True
        },
        # weeks to complete is beyond end date
        {
            "end_date": _days_from_now(49),
            "curr_date": _curr_date(),
            "weeks_to_complete": 8,
            "expected_output": False
        },
        # end date is current date
        {
            "end_date": _curr_date(),
            "curr_date": _curr_date(),
            "weeks_to_complete": 1,
            "expected_output": False
        },
    )
    @ddt.unpack
    def test_has_time_to_complete(self, end_date, curr_date, weeks_to_complete, expected_output):
        assert has_time_to_complete(curr_date, end_date, weeks_to_complete) == expected_output

    @ddt.data(
        {
            "start_date": None,
            "end_date": _days_from_now(10, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": "self_paced",
                "weeks_to_complete": 8,
            },
        },
        {
            "start_date": _days_from_now(5, DATE_FORMAT_ISO_8601),
            "end_date": None,
            "course_metadata": {
                "pacing_type": "self_paced",
                "weeks_to_complete": 8,
            },
        },
        {
            "start_date": _days_from_now(5, DATE_FORMAT_ISO_8601),
            "end_date": _days_from_now(10, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": None,
                "weeks_to_complete": 8,
            },
        },
        {
            "start_date": _days_from_now(5, DATE_FORMAT_ISO_8601),
            "end_date": _days_from_now(10, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": "self_paced",
                "weeks_to_complete": None,
            },
        },
        {
            "start_date": None,
            "end_date": None,
            "course_metadata": {
                "pacing_type": None,
                "weeks_to_complete": None,
            },
        },
    )
    @ddt.unpack
    def test_get_self_paced_normalized_start_date_empty_data(self, start_date, end_date, course_metadata):
        assert get_self_paced_normalized_start_date(start_date, end_date, course_metadata) == \
               _curr_date(BRAZE_TIMESTAMP_FORMAT)

    @ddt.data(
        # Self-paced, and start date is more than START_DATE_DEFAULT_TO_TODAY_THRESHOLD_DAYS days before today.
        # Adjust the start date to become today.
        {
            "start_date": _days_from_now(-15, DATE_FORMAT_ISO_8601),
            "end_date": _days_from_now(10, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": "self_paced",
                "weeks_to_complete": 300,
            },
            "expected_normalized_start_date_is_now": True,
        },
        # Self-paced, and there's enough time to complete.
        # Adjust the start date to become today.
        {
            "start_date": _days_from_now(-5, DATE_FORMAT_ISO_8601),
            "end_date": _days_from_now(28, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": "self_paced",
                "weeks_to_complete": 3,
            },
            "expected_normalized_start_date_is_now": True,
        },

        ###
        # All subsequent test cases should result in NOT adjusting the start date.
        ###

        # Course starts more than START_DATE_DEFAULT_TO_TODAY_THRESHOLD_DAYS days before today,
        # BUT the course is instructor paced.
        {
            "start_date": _days_from_now(-15, DATE_FORMAT_ISO_8601),
            "end_date": _days_from_now(28, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": "instructor_paced",
                "weeks_to_complete": 300,
            },
        },
        # Course starts in the past, there's enough time to complete,
        # BUT the course is instructor paced.
        {
            "start_date": _days_from_now(-5, DATE_FORMAT_ISO_8601),
            "end_date": _days_from_now(28, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": "instructor_paced",
                "weeks_to_complete": 3,
            },
        },
        # Course is Self-paced, BUT there's no time to complete and it started
        # within START_DATE_DEFAULT_TO_TODAY_THRESHOLD_DAYS days ago.
        {
            "start_date": _days_from_now(-5, DATE_FORMAT_ISO_8601),
            "end_date": _days_from_now(10, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": "self_paced",
                "weeks_to_complete": 300,
            },
        },
        # NEW test case for fixing ENT-10263.
        # Course is Self-paced, there's time to complete, BUT start date is in the future.
        {
            "start_date": _days_from_now(5, DATE_FORMAT_ISO_8601),
            "end_date": _days_from_now(28, DATE_FORMAT_ISO_8601),
            "course_metadata": {
                "pacing_type": "self_paced",
                "weeks_to_complete": 3,
            },
        },
    )
    @ddt.unpack
    def test_get_self_paced_normalized_start_date_self_paced(
        self,
        start_date,
        end_date,
        course_metadata,
        expected_normalized_start_date_is_now=False,
    ):
        expected_normalized_start_date = (
            _curr_date(BRAZE_TIMESTAMP_FORMAT)
            if expected_normalized_start_date_is_now
            else start_date
        )
        assert get_self_paced_normalized_start_date(start_date, end_date, course_metadata) == \
            expected_normalized_start_date

    @ddt.data(
        # Expects course run associated with configured content_key for run-based assignment
        {
            'assignment': {
                'is_assigned_course_run': True,
                'preferred_course_run_key': 'course-v1:edX+DemoX+1T60',
                'content_key': 'course-v1:edX+DemoX+1T60',
            },
            'expected_output': mock_course_run_2,
        },
        # Expects the preferred content_key
        {
            'assignment': {
                'is_assigned_course_run': False,
                'preferred_course_run_key': 'course-v1:edX+DemoX+1T360',
                'content_key': 'edX+DemoX',
            },
            'expected_output': mock_course_run_1,
        },
        # Expects the top level course key
        {
            'assignment': {
                'is_assigned_course_run': False,
                'preferred_course_run_key': None,
                'content_key': 'edX+DemoX',
            },
            'expected_output': mock_advertised_course_run,
        },
    )
    @ddt.unpack
    def test_get_normalized_metadata_for_assignment(self, assignment, expected_output):
        assignment_obj = LearnerContentAssignmentFactory(**assignment)
        normalized_metadata = get_normalized_metadata_for_assignment(
            assignment=assignment_obj,
            content_metadata=self.mock_content_metadata
        )
        self.assertEqual(normalized_metadata, expected_output)

    @ddt.data(
        # Test case for preferred course run that exists
        {
            'assignment': {
                'preferred_course_run_key': 'course-v1:edX+DemoX+1T60',
                'uuid': assignment_uuid
            },
            'content_metadata': {
                'course_runs': [
                    {'key': 'course-v1:edX+DemoX+1T60', 'data': 'test1'},
                    {'key': 'course-v1:edX+DemoX+1T360', 'data': 'test2'}
                ],
                'advertised_course_run_uuid': advertised_course_run_uuid,
                'key': 'test-content-key'
            },
            'expected_output': {'key': 'course-v1:edX+DemoX+1T60', 'data': 'test1'}
        },
        # Test case for preferred course run that doesn't exist - should fall back to advertised run
        {
            'assignment': {
                'preferred_course_run_key': 'non-existent-key',
                'uuid': assignment_uuid
            },
            'content_metadata': {
                'course_runs': [
                    {'key': 'course-v1:edX+DemoX+1T60', 'uuid': advertised_course_run_uuid, 'data': 'test1'},
                    {'key': 'course-v1:edX+DemoX+1T360', 'data': 'test2'}
                ],
                'advertised_course_run_uuid': advertised_course_run_uuid,
                'key': 'test-content-key'
            },
            'expected_output': {'key': 'course-v1:edX+DemoX+1T60', 'uuid': advertised_course_run_uuid, 'data': 'test1'}
        },
        # Test case for no preferred course run - should return advertised run
        {
            'assignment': {
                'preferred_course_run_key': None,
                'uuid': assignment_uuid
            },
            'content_metadata': {
                'course_runs': [
                    {'key': 'course-v1:edX+DemoX+1T60', 'uuid': advertised_course_run_uuid, 'data': 'test1'},
                    {'key': 'course-v1:edX+DemoX+1T360', 'data': 'test2'}
                ],
                'advertised_course_run_uuid': advertised_course_run_uuid,
                'key': 'test-content-key'
            },
            'expected_output': {'key': 'course-v1:edX+DemoX+1T60', 'uuid': advertised_course_run_uuid, 'data': 'test1'}
        }
    )
    @ddt.unpack
    def test_get_course_run_metadata_for_assignment(self, assignment, content_metadata, expected_output):
        """
        Test get_course_run_metadata_for_assignment returns the correct course run metadata
        based on assignment and content metadata.
        """
        assignment_obj = LearnerContentAssignmentFactory(**assignment)
        course_run_metadata = get_course_run_metadata_for_assignment(
            assignment=assignment_obj,
            content_metadata=content_metadata
        )
        self.assertEqual(course_run_metadata, expected_output)


def _iso(days_from_now):
    return (datetime.now(UTC) + timedelta(days=days_from_now)).strftime('%Y-%m-%dT%H:%M:%SZ')


@ddt.ddt
class TestGetLatestCourseRunEndDate(TestCase):
    """
    Tests for ``_get_latest_course_run_end_date``.
    """

    @ddt.data(
        {},
        None,
        {'key': RUN_1, 'normalized_metadata_by_run': {RUN_1: {'end_date': 'not-a-real-date'}}},
        {'key': RUN_1, 'normalized_metadata_by_run': {RUN_1: {'end_date': None}}},
        # one run without an end date (e.g. self-paced) blocks the reason, even if other runs ended
        {'normalized_metadata_by_run': {RUN_1: {'end_date': _iso(-30)}, RUN_2: {'end_date': None}}},
    )
    def test_returns_none(self, content_metadata):
        assert _get_latest_course_run_end_date(content_metadata) is None

    def test_returns_latest_end_date_when_all_runs_ended(self):
        content_metadata = {
            'normalized_metadata_by_run': {
                RUN_1: {'end_date': '2020-01-01T00:00:00Z'},
                RUN_2: {'end_date': '2020-06-01T00:00:00Z'},
            },
        }
        assert _get_latest_course_run_end_date(content_metadata) == datetime(2020, 6, 1, tzinfo=UTC)

    def test_returns_future_end_date_when_a_run_has_not_ended(self):
        """Callers decide whether the date has passed, so a future date is returned as-is."""
        content_metadata = {
            'normalized_metadata_by_run': {
                RUN_1: {'end_date': '2020-01-01T00:00:00Z'},
                RUN_2: {'end_date': '2099-01-01T00:00:00Z'},
            },
        }
        assert _get_latest_course_run_end_date(content_metadata) == datetime(2099, 1, 1, tzinfo=UTC)


@ddt.ddt
class TestCatalogAgnosticFallback(TestCase):
    """
    ``use_catalog_agnostic_fallback`` gates the synchronous catalog-agnostic lookup, and the fallback
    metadata may only inform the course-run-ended date, never the enrollment deadline.
    """

    @staticmethod
    def _fake_assignment():
        policy = SimpleNamespace(catalog_uuid='catalog-uuid')
        return SimpleNamespace(
            uuid='assignment-uuid',
            content_key=RUN_1,
            assignment_configuration=SimpleNamespace(subsidy_access_policy=policy),
            get_allocation_timeout_expiration=lambda: datetime.now(UTC) + timedelta(days=90),
        )

    def _get_expiration(self, fallback_metadata, use_fallback=True):
        """
        Run get_automatic_expiration_date_and_reason for content missing from the policy catalog.
        Returns (result, fallback mock).
        """
        fallback_path = f'{CONTENT_METADATA_API_MODULE}.get_catalog_agnostic_content_metadata_for_assignment'
        with mock.patch(f'{UTILS_MODULE}._get_subsidy_expiration', return_value=None), \
             mock.patch(f'{CONTENT_METADATA_API_MODULE}.get_content_metadata_for_assignments', return_value={}), \
             mock.patch(fallback_path, return_value=fallback_metadata) as mock_fallback:
            result = get_automatic_expiration_date_and_reason(
                self._fake_assignment(),
                use_catalog_agnostic_fallback=use_fallback,
            )
        return result, mock_fallback

    @ddt.data(
        (False, False, AssignmentAutomaticExpiredReason.NINETY_DAYS_PASSED),
        (True, True, AssignmentAutomaticExpiredReason.COURSE_RUN_ENDED),
    )
    @ddt.unpack
    def test_fallback_only_used_when_flag_set(self, use_fallback, expect_fallback_called, expected_reason):
        ended_metadata = {
            'normalized_metadata': {'enroll_by_date': None},
            'normalized_metadata_by_run': {RUN_1: {'end_date': _iso(-1)}},
        }
        result, mock_fallback = self._get_expiration(ended_metadata, use_fallback=use_fallback)

        assert mock_fallback.called is expect_fallback_called
        assert result['reason'] == expected_reason

    def test_fallback_metadata_enroll_by_date_is_ignored(self):
        """
        A past ``enroll_by_date`` in the fallback metadata must not win over the course run end date,
        otherwise the assignment would expire as ENROLLMENT_DATE_PASSED and never have its PII cleared.
        """
        ended_metadata = {
            'normalized_metadata': {'enroll_by_date': _iso(-5)},
            'normalized_metadata_by_run': {RUN_1: {'enroll_by_date': _iso(-5), 'end_date': _iso(-1)}},
        }
        result, _ = self._get_expiration(ended_metadata)

        assert result['reason'] == AssignmentAutomaticExpiredReason.COURSE_RUN_ENDED
