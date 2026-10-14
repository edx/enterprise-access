"""
Tests for the ``content_metadata_api.py`` module of the content_assignments app.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import ddt
from django.test import TestCase
from edx_django_utils.cache import TieredCache
from requests.exceptions import HTTPError

from ..content_metadata_api import (
    get_card_image_url,
    get_catalog_agnostic_content_metadata_for_assignment,
    get_course_partners,
    get_human_readable_date,
    parse_datetime_string
)


@ddt.ddt
class TestContentMetadataApi(TestCase):
    """
    Tests functions of the ``content_assignment_api.py`` file.
    """

    @ddt.data(
        (
            {'card_image_url': 'my-card-image', 'image_url': 'my-image-url'},
            'my-card-image',
        ),
        (
            {'card_image_url': 'my-card-image', 'image_url': None},
            'my-card-image',
        ),
        (
            {'card_image_url': 'my-card-image'},
            'my-card-image',
        ),
        (
            {'card_image_url': None, 'image_url': 'my-image-url'},
            'my-image-url',
        ),
        (
            {},
            None,
        )
    )
    @ddt.unpack
    def test_get_card_image_url(self, content_metadata, expected_output):
        self.assertEqual(expected_output, get_card_image_url(content_metadata))

    @ddt.data(
        ('2023-01-01T00:00:00.000000Z', 'Jan 01, 2023'),
        ('2023-01-01T00:00:00Z', 'Jan 01, 2023'),
        ('2023-01-01 00:00:00.000000Z', 'Jan 01, 2023'),
        ('2023-01-01 00:00:00Z', 'Jan 01, 2023'),
    )
    @ddt.unpack
    def test_get_human_readable_date(self, datetime_string, expected_output):
        self.assertEqual(expected_output, get_human_readable_date(datetime_string))

    def test_get_human_readable_date_exception(self):
        with self.assertRaisesRegex(ValueError, 'does not match format'):
            get_human_readable_date('2023-01-01')

    @ddt.data(
        (
            {'owners': [
                {'name': 'bob', 'id': 1}, {'name': 'sam', 'id': 2},
                {'name': 'dave', 'id': 3}, {'name': 'jill', 'id': 4}
            ]},
            'bob, sam, dave, and jill',
        ),
        (
            {'owners': [{'name': 'bob', 'id': 1}, {'name': 'sam', 'id': 2}]},
            'bob and sam',
        ),
        (
            {'owners': [{'name': 'bob', 'id': 1}]},
            'bob',
        ),
    )
    @ddt.unpack
    def test_get_course_partners(self, content_metadata, expected_output):
        self.assertEqual(expected_output, get_course_partners(content_metadata))

    def test_get_course_partners_exception(self):
        with self.assertRaisesRegex(Exception, 'must have a partner'):
            get_course_partners({'foo': 'bar'})


@ddt.ddt
class TestGetCatalogAgnosticContentMetadata(TestCase):
    """
    Tests for ``get_catalog_agnostic_content_metadata_for_assignment``.
    """
    GET_METADATA = 'enterprise_access.apps.content_assignments.content_metadata_api.get_and_cache_content_metadata'
    RUN_KEY = 'course-v1:edX+Test+1'
    COURSE_KEY = 'edX+Test'

    def setUp(self):
        super().setUp()
        TieredCache.dangerous_clear_all_tiers()

    def test_misses_are_remembered(self):
        assignment = SimpleNamespace(content_key=self.RUN_KEY, parent_content_key=self.COURSE_KEY)
        with mock.patch(self.GET_METADATA, return_value=None) as mock_get:
            first = get_catalog_agnostic_content_metadata_for_assignment(assignment)
            second = get_catalog_agnostic_content_metadata_for_assignment(assignment)

        assert first == second == {}
        assert mock_get.call_count == 2  # one per key on the first lookup, none on the second

    def test_hits_are_not_treated_as_misses(self):
        assignment = SimpleNamespace(content_key=self.RUN_KEY, parent_content_key=self.COURSE_KEY)
        with mock.patch(self.GET_METADATA, side_effect=[None, {'key': self.COURSE_KEY}, {'key': self.COURSE_KEY}]):
            assert get_catalog_agnostic_content_metadata_for_assignment(assignment) == {'key': self.COURSE_KEY}
            # RUN_KEY is now a remembered miss, so only the parent key is looked up again
            assert get_catalog_agnostic_content_metadata_for_assignment(assignment) == {'key': self.COURSE_KEY}

    def test_stops_at_first_match(self):
        assignment = SimpleNamespace(content_key=self.RUN_KEY, parent_content_key=self.COURSE_KEY)
        with mock.patch(self.GET_METADATA, return_value={'key': self.COURSE_KEY}) as mock_get:
            result = get_catalog_agnostic_content_metadata_for_assignment(assignment)

        assert result == {'key': self.COURSE_KEY}
        mock_get.assert_called_once_with(self.RUN_KEY, coerce_to_parent_course=True)

    def test_falls_back_to_parent_key_when_content_key_misses(self):
        assignment = SimpleNamespace(content_key=self.RUN_KEY, parent_content_key=self.COURSE_KEY)
        with mock.patch(self.GET_METADATA, side_effect=[None, {'key': self.COURSE_KEY}]) as mock_get:
            result = get_catalog_agnostic_content_metadata_for_assignment(assignment)

        assert result == {'key': self.COURSE_KEY}
        mock_get.assert_has_calls([
            mock.call(self.RUN_KEY, coerce_to_parent_course=True),
            mock.call(self.COURSE_KEY, coerce_to_parent_course=True),
        ])

    def test_dedupes_identical_keys(self):
        assignment = SimpleNamespace(content_key=self.COURSE_KEY, parent_content_key=self.COURSE_KEY)
        with mock.patch(self.GET_METADATA, return_value=None) as mock_get:
            result = get_catalog_agnostic_content_metadata_for_assignment(assignment)

        assert result == {}
        mock_get.assert_called_once_with(self.COURSE_KEY, coerce_to_parent_course=True)

    def test_404_is_a_miss(self):
        assignment = SimpleNamespace(content_key=self.RUN_KEY, parent_content_key=self.COURSE_KEY)
        error = HTTPError(response=mock.Mock(status_code=404))
        with mock.patch(self.GET_METADATA, side_effect=error) as mock_get:
            result = get_catalog_agnostic_content_metadata_for_assignment(assignment)

        assert result == {}
        assert mock_get.call_count == 2

    def test_other_request_errors_propagate_and_are_not_remembered(self):
        assignment = SimpleNamespace(content_key=self.RUN_KEY, parent_content_key=self.COURSE_KEY)
        error = HTTPError(response=mock.Mock(status_code=500))
        with mock.patch(self.GET_METADATA, side_effect=error):
            with self.assertRaises(HTTPError):
                get_catalog_agnostic_content_metadata_for_assignment(assignment)

        with mock.patch(self.GET_METADATA, return_value={'key': self.COURSE_KEY}) as mock_get:
            assert get_catalog_agnostic_content_metadata_for_assignment(assignment) == {'key': self.COURSE_KEY}
        mock_get.assert_called_once_with(self.RUN_KEY, coerce_to_parent_course=True)


@ddt.ddt
class TestParseDatetimeString(TestCase):
    """
    Tests for ``parse_datetime_string``.
    """

    @ddt.data(
        # (datetime_string, set_to_utc, expected naive wall-clock value, expected utcoffset)
        # The explicit ``DATE_INPUT_PATTERNS`` (always UTC, ``Z`` suffix).
        ('2026-08-31T07:25:38Z', False, datetime(2026, 8, 31, 7, 25, 38), None),
        ('2026-08-31T07:25:38Z', True, datetime(2026, 8, 31, 7, 25, 38), timedelta(0)),
        ('2026-08-31 07:25:38.845255Z', True, datetime(2026, 8, 31, 7, 25, 38, 845255), timedelta(0)),
        # ISO 8601 fallback: fractional seconds and a UTC offset.
        ('2026-08-31T07:25:38.845255+00:00', True, datetime(2026, 8, 31, 7, 25, 38, 845255), timedelta(0)),
        # ISO 8601 fallback: a non-UTC offset is converted to UTC, not relabelled.
        ('2026-08-31T09:25:38+02:00', True, datetime(2026, 8, 31, 7, 25, 38), timedelta(0)),
        # ISO 8601 fallback: a value with no offset is taken to be UTC.
        ('2026-08-31T07:25:38.845255', True, datetime(2026, 8, 31, 7, 25, 38, 845255), timedelta(0)),
        # ISO 8601 fallback without set_to_utc keeps the value's own offset.
        ('2026-08-31T09:25:38+02:00', False, datetime(2026, 8, 31, 9, 25, 38), timedelta(hours=2)),
    )
    @ddt.unpack
    def test_parses_supported_formats(self, datetime_string, set_to_utc, expected_wall_clock, expected_utcoffset):
        result = parse_datetime_string(datetime_string, set_to_utc=set_to_utc)

        assert result.replace(tzinfo=None) == expected_wall_clock
        assert result.utcoffset() == expected_utcoffset

    @ddt.data(None, '')
    def test_empty_values_return_none(self, datetime_string):
        assert parse_datetime_string(datetime_string, set_to_utc=True) is None

    @ddt.data(
        '2023-01-01',  # a bare date is not tried as ISO 8601, so it isn't read as midnight
        'not a date',  # tried as ISO 8601 (it has a space) and fails there too
    )
    def test_unparseable_value_raises_the_date_input_pattern_error(self, datetime_string):
        with self.assertRaisesRegex(ValueError, 'does not match format'):
            parse_datetime_string(datetime_string, set_to_utc=True)
