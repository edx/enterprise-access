"""
Tests for standalone helper functions in ``enterprise_access/utils.py`` that back the
COURSE_RUN_ENDED assignment expiration reason.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase, mock

import ddt
from pytz import UTC
from requests.exceptions import HTTPError

from enterprise_access.apps.content_assignments.constants import AssignmentAutomaticExpiredReason
from enterprise_access.utils import (
    _get_catalog_agnostic_content_metadata_for_assignment,
    _get_course_run_ended_date,
    get_automatic_expiration_date_and_reason
)

CONTENT_METADATA_API_MODULE = 'enterprise_access.apps.content_metadata.api'
UTILS_MODULE = 'enterprise_access.utils'
CONTENT_ASSIGNMENTS_METADATA_MODULE = 'enterprise_access.apps.content_assignments.content_metadata_api'


def _iso(days_from_now):
    return (datetime.now(UTC) + timedelta(days=days_from_now)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _fake_assignment():
    policy = SimpleNamespace(catalog_uuid='catalog-uuid')
    return SimpleNamespace(
        uuid='assignment-uuid',
        content_key='course-v1:edX+Test+1',
        assignment_configuration=SimpleNamespace(subsidy_access_policy=policy),
        get_allocation_timeout_expiration=lambda: datetime.now(UTC) + timedelta(days=90),
    )


def test_get_course_run_ended_date_returns_none_for_empty_content_metadata():
    assert _get_course_run_ended_date({}) is None
    assert _get_course_run_ended_date(None) is None


def test_get_course_run_ended_date_returns_none_for_malformed_end_date():
    """A run end date that fails to parse should short-circuit the whole lookup."""
    content_metadata = {
        'key': 'course-v1:edX+Test+1',
        'normalized_metadata_by_run': {
            'course-v1:edX+Test+1': {'end_date': 'not-a-real-date'},
        },
    }
    assert _get_course_run_ended_date(content_metadata) is None


def test_get_course_run_ended_date_returns_none_for_missing_end_date():
    """A self-paced run with no end date should block the reason for the whole course."""
    content_metadata = {
        'key': 'course-v1:edX+Test+1',
        'normalized_metadata_by_run': {
            'course-v1:edX+Test+1': {'end_date': None},
        },
    }
    assert _get_course_run_ended_date(content_metadata) is None


def test_get_catalog_agnostic_content_metadata_stops_at_first_match():
    """Only ``content_key`` is looked up when ``parent_content_key`` is absent."""
    assignment = SimpleNamespace(content_key='course-v1:edX+Test+1', parent_content_key=None)
    with mock.patch(f'{CONTENT_METADATA_API_MODULE}.get_and_cache_content_metadata') as mock_get_metadata:
        mock_get_metadata.return_value = {'key': 'course-v1:edX+Test+1'}
        result = _get_catalog_agnostic_content_metadata_for_assignment(assignment)

    assert result == {'key': 'course-v1:edX+Test+1'}
    mock_get_metadata.assert_called_once_with('course-v1:edX+Test+1', coerce_to_parent_course=True)


def test_get_catalog_agnostic_content_metadata_falls_back_to_parent_only():
    """Only ``parent_content_key`` is looked up when ``content_key`` is absent."""
    assignment = SimpleNamespace(content_key=None, parent_content_key='edX+Test')
    with mock.patch(f'{CONTENT_METADATA_API_MODULE}.get_and_cache_content_metadata') as mock_get_metadata:
        mock_get_metadata.return_value = {'key': 'edX+Test'}
        result = _get_catalog_agnostic_content_metadata_for_assignment(assignment)

    assert result == {'key': 'edX+Test'}
    mock_get_metadata.assert_called_once_with('edX+Test', coerce_to_parent_course=True)


def test_get_catalog_agnostic_content_metadata_dedupes_identical_keys():
    """``parent_content_key`` equal to ``content_key`` should not be queried twice."""
    assignment = SimpleNamespace(content_key='edX+Test', parent_content_key='edX+Test')
    with mock.patch(f'{CONTENT_METADATA_API_MODULE}.get_and_cache_content_metadata') as mock_get_metadata:
        mock_get_metadata.return_value = None
        result = _get_catalog_agnostic_content_metadata_for_assignment(assignment)

    assert result == {}
    # One identifier, tried with coerce_to_parent_course=True then False: 2 calls, not 4.
    assert mock_get_metadata.call_count == 2


def test_get_catalog_agnostic_content_metadata_retries_after_http_error():
    """A 404/5xx on the ``coerce_to_parent_course=True`` attempt should not stop the ``False`` retry."""
    assignment = SimpleNamespace(content_key='course-v1:edX+Test+1', parent_content_key=None)
    with mock.patch(f'{CONTENT_METADATA_API_MODULE}.get_and_cache_content_metadata') as mock_get_metadata:
        mock_get_metadata.side_effect = [
            HTTPError(response=mock.Mock(status_code=404)),
            {'key': 'course-v1:edX+Test+1'},
        ]
        result = _get_catalog_agnostic_content_metadata_for_assignment(assignment)

    assert result == {'key': 'course-v1:edX+Test+1'}
    mock_get_metadata.assert_has_calls([
        mock.call('course-v1:edX+Test+1', coerce_to_parent_course=True),
        mock.call('course-v1:edX+Test+1', coerce_to_parent_course=False),
    ])


def test_get_catalog_agnostic_content_metadata_returns_empty_dict_when_both_attempts_error():
    """An HTTPError on every attempt should not crash; it should just return no metadata."""
    assignment = SimpleNamespace(content_key='course-v1:edX+Test+1', parent_content_key=None)
    with mock.patch(f'{CONTENT_METADATA_API_MODULE}.get_and_cache_content_metadata') as mock_get_metadata:
        mock_get_metadata.side_effect = HTTPError(response=mock.Mock(status_code=404))
        result = _get_catalog_agnostic_content_metadata_for_assignment(assignment)

    assert result == {}
    assert mock_get_metadata.call_count == 2


def test_get_course_run_ended_date_returns_latest_end_date_when_all_runs_ended():
    content_metadata = {
        'normalized_metadata_by_run': {
            'course-v1:edX+Test+1': {'end_date': _iso(-30)},
            'course-v1:edX+Test+2': {'end_date': _iso(-2)},
        },
    }
    result = _get_course_run_ended_date(content_metadata)
    assert abs(result - (datetime.now(UTC) - timedelta(days=2))) < timedelta(minutes=1)


def test_get_course_run_ended_date_returns_none_when_any_run_still_active():
    content_metadata = {
        'normalized_metadata_by_run': {
            'course-v1:edX+Test+1': {'end_date': _iso(-30)},
            'course-v1:edX+Test+2': {'end_date': _iso(30)},
        },
    }
    assert _get_course_run_ended_date(content_metadata) is None


def test_get_course_run_ended_date_falls_back_to_course_runs():
    """Course-level metadata without ``normalized_metadata_by_run`` uses the raw ``course_runs`` list."""
    content_metadata = {
        'course_runs': [
            {'key': 'course-v1:edX+Test+1', 'end': _iso(-10)},
            {'key': 'course-v1:edX+Test+2', 'end': _iso(-5)},
        ],
    }
    assert _get_course_run_ended_date(content_metadata) is not None


@ddt.ddt
class TestCatalogAgnosticFallbackFlag(TestCase):
    """
    ``use_catalog_agnostic_fallback`` must gate the synchronous catalog-agnostic lookup.
    """

    @ddt.data(
        (False, False),
        (True, True),
    )
    @ddt.unpack
    def test_fallback_only_used_when_flag_set(self, use_fallback, expect_fallback_called):
        ended_metadata = {
            'normalized_metadata': {'enroll_by_date': None},
            'normalized_metadata_by_run': {'course-v1:edX+Test+1': {'end_date': _iso(-1)}},
        }
        with mock.patch(f'{UTILS_MODULE}._get_subsidy_expiration', return_value=None), \
             mock.patch(f'{UTILS_MODULE}._get_enrollment_deadline_date', return_value=None), \
             mock.patch(f'{CONTENT_ASSIGNMENTS_METADATA_MODULE}.get_content_metadata_for_assignments',
                        return_value={}), \
             mock.patch(f'{UTILS_MODULE}._get_catalog_agnostic_content_metadata_for_assignment',
                        return_value=ended_metadata) as mock_fallback:
            result = get_automatic_expiration_date_and_reason(
                _fake_assignment(),
                use_catalog_agnostic_fallback=use_fallback,
            )

        assert mock_fallback.called is expect_fallback_called
        if expect_fallback_called:
            # course no longer in the policy catalog, no enrollment deadline: still expires
            assert result['reason'] == AssignmentAutomaticExpiredReason.COURSE_RUN_ENDED
        else:
            assert result['reason'] == AssignmentAutomaticExpiredReason.NINETY_DAYS_PASSED
