"""
Tests for the ``content_metadata_api.py`` module of the content_assignments app.
"""
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
    get_human_readable_date
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

    @ddt.data(
        HTTPError(response=mock.Mock(status_code=404)),
        HTTPError(response=mock.Mock(status_code=500)),
    )
    def test_request_errors_are_swallowed(self, error):
        assignment = SimpleNamespace(content_key=self.RUN_KEY, parent_content_key=self.COURSE_KEY)
        with mock.patch(self.GET_METADATA, side_effect=error) as mock_get:
            result = get_catalog_agnostic_content_metadata_for_assignment(assignment)

        assert result == {}
        assert mock_get.call_count == 2
