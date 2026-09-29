"""
Tests for loading a queue file, and for the admin page that uploads one.

The properties that matter: a file with one bad record loads none of it; a preview reports
exactly what a real load would do and writes nothing; retiring items keeps the votes already
cast on them and says how many it affected; and the page is behind the same permission as
adding an item by hand.
"""
import json
from unittest import mock

import ddt
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_review.models import PathwayReviewItem, ReviewPool, Verdict
from enterprise_access.apps.pathway_review.queue_loading import QueueError, load_queue_file, parse_queue, validate
from enterprise_access.apps.pathway_review.tests.factories import (
    PathwayReviewItemFactory,
    PathwayReviewVoteFactory,
    ladder_payload
)

UPLOAD_URL = 'admin:pathway_review_pathwayreviewitem_upload'


def record(item_id='Q001', **overrides):
    """One queue record, in the shape a pathway run writes."""
    base = {
        'id': item_id, 'family_key': 'data analyst', 'pathway': 'Data Analyst · Full ladder',
        'careers_covered': 18, 'mix': '2/2/1', 'pool': ReviewPool.REACH, 'stratum': 'ladder',
        'weight': 1.0, 'family_description': 'Analyses data.',
        'careers': [{'name': 'Data Analyst', 'desc': 'Analyses data.'}],
        'supply': {'Introductory': 12, 'Intermediate': 8, 'Advanced': 2},
        'courses': [
            {'step': 1, 'level': 'Introductory', 'key': 'X+1', 'title': 'First', 'provider': 'P', 'url': 'u'},
            {'step': 2, 'level': 'Intermediate', 'key': 'X+2', 'title': 'Second', 'provider': 'P', 'url': 'u'},
        ],
        'alternates': {'Introductory': [{'key': 'X+9', 'title': 'Other', 'provider': 'P', 'url': 'u'}]},
    }
    base.update(overrides)
    return base


def queue_json(*records, descriptions=None):
    return json.dumps({
        'ladders': list(records) or [record()],
        'course_descriptions': descriptions if descriptions is not None else {'X+1': 'About the first.'},
    })


@ddt.ddt
class ParseAndValidateTests(TestCase):
    """ A queue file is read and checked before anything is written. """

    @ddt.data(
        (b'\xff\xfe not utf 8', 'not UTF-8'),
        ('{oops', 'not valid JSON'),
        ('[]', 'JSON object'),
        ('{}', 'No "ladders"'),
        ('{"ladders": []}', 'No "ladders"'),
    )
    @ddt.unpack
    def test_unreadable_files_are_refused_by_name(self, raw, message):
        with self.assertRaises(QueueError) as ctx:
            parse_queue(raw)
        self.assertIn(message, str(ctx.exception))

    def test_a_sound_file_parses_to_its_records_and_descriptions(self):
        ladders, descriptions = parse_queue(queue_json())

        self.assertEqual(len(ladders), 1)
        self.assertEqual(descriptions, {'X+1': 'About the first.'})

    def test_descriptions_are_optional(self):
        self.assertEqual(parse_queue(json.dumps({'ladders': [record()]}))[1], {})

    @ddt.data(
        ({'id': ''}, 'missing id'),
        ({'courses': []}, 'has no courses'),
        ({'pool': 'nonsense'}, 'expected one of'),
        ({'id': 'x' * 40}, 'longer than the column'),
    )
    @ddt.unpack
    def test_a_record_that_cannot_be_rendered_is_named(self, overrides, message):
        problems = validate([record(**overrides)])

        self.assertTrue(problems)
        self.assertIn(message, problems[0])

    def test_a_course_missing_a_field_names_its_position(self):
        broken = record()
        broken['courses'][1] = {'step': 2, 'level': 'Intermediate', 'key': 'X+2'}

        self.assertIn('course 2 is missing title', validate([broken])[0])

    def test_a_record_that_is_not_an_object_is_named(self):
        self.assertEqual(validate(['not a record']), ['Record 1 is not an object.'])

    def test_a_course_that_is_not_an_object_is_named(self):
        broken = record()
        broken['courses'][1] = 'not a course'

        self.assertIn('course 2 is not an object', validate([broken])[0])

    def test_a_repeated_id_is_caught(self):
        self.assertIn('repeats the id of record 1', validate([record(), record()])[0])

    def test_a_sound_file_has_no_problems(self):
        self.assertEqual(validate([record(), record('Q002')]), [])


class LoadQueueTests(TestCase):
    """ Loading is all or nothing, can be rehearsed, and says what it retires. """

    def test_records_become_items_with_a_reviewer_visible_payload(self):
        report = load_queue_file(queue_json())

        item = PathwayReviewItem.objects.get(item_id='Q001')
        self.assertEqual((report['created'], report['updated']), (1, 0))
        self.assertEqual(item.payload['courses'][0]['desc'], 'About the first.')
        self.assertEqual(item.payload['alt']['Introductory'][0]['key'], 'X+9')
        self.assertEqual(item.payload['alt']['Advanced'], [])
        self.assertNotIn('pool', item.payload)
        self.assertNotIn('control_key', item.payload)

    def test_loading_the_same_ids_again_updates_them(self):
        load_queue_file(queue_json())
        report = load_queue_file(queue_json(record(pathway='Renamed')))

        self.assertEqual((report['created'], report['updated']), (0, 1))
        self.assertEqual(PathwayReviewItem.objects.get(item_id='Q001').pathway, 'Renamed')

    def test_one_bad_record_loads_none_of_the_file(self):
        with self.assertRaises(QueueError) as ctx:
            load_queue_file(queue_json(record(), record('Q002', courses=[])))

        self.assertEqual(ctx.exception.problems, ['Record 2 (Q002) has no courses.'])
        self.assertFalse(PathwayReviewItem.objects.exists())

    def test_a_preview_reports_what_it_would_do_and_writes_nothing(self):
        report = load_queue_file(queue_json(), dry_run=True)

        self.assertEqual((report['created'], report['dry_run']), (1, True))
        self.assertFalse(PathwayReviewItem.objects.exists())

    def test_items_left_out_stay_until_they_are_retired(self):
        PathwayReviewItemFactory(item_id='OLD1')

        left = load_queue_file(queue_json())
        self.assertEqual((left['would_deactivate'], left['deactivated']), (1, 0))
        self.assertTrue(PathwayReviewItem.objects.get(item_id='OLD1').is_active)

        retired = load_queue_file(queue_json(), deactivate_missing=True)
        self.assertEqual(retired['deactivated'], 1)
        self.assertFalse(PathwayReviewItem.objects.get(item_id='OLD1').is_active)

    def test_retiring_an_item_keeps_its_votes_and_counts_them(self):
        old = PathwayReviewItemFactory(item_id='OLD1', payload=ladder_payload())
        PathwayReviewVoteFactory(item=old, reviewer=UserFactory(), verdict=Verdict.GOOD)

        report = load_queue_file(queue_json(), deactivate_missing=True)

        self.assertEqual(report['deactivated_with_votes'], 1)
        self.assertEqual(old.votes.count(), 1)

    def test_a_preview_of_a_retiring_load_writes_nothing(self):
        PathwayReviewItemFactory(item_id='OLD1')

        load_queue_file(queue_json(), deactivate_missing=True, dry_run=True)

        self.assertTrue(PathwayReviewItem.objects.get(item_id='OLD1').is_active)


class UploadPageTests(TestCase):
    """ The upload page is behind the permission that governs adding an item. """

    def setUp(self):
        super().setUp()
        self.url = reverse(UPLOAD_URL)
        self.staff = UserFactory(is_staff=True)
        self.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='pathway_review', content_type__model='pathwayreviewitem',
        ))
        self.client.force_login(self.staff)

    def upload(self, raw=None, **fields):
        """POST a queue file to the upload page, following the redirect a real load makes."""
        data = {'queue_file': SimpleUploadedFile('queue.json', (raw or queue_json()).encode('utf-8'),
                                                 content_type='application/json')}
        data.update(fields)
        return self.client.post(self.url, data, follow=True)

    def test_a_signed_out_visitor_is_sent_to_log_in(self):
        self.client.logout()

        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_staff_without_the_permission_are_refused(self):
        self.client.force_login(UserFactory(is_staff=True))

        self.assertIn(self.client.get(self.url).status_code, (302, 403))

    def test_the_form_is_offered(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Queue file')

    def test_the_item_list_links_to_it(self):
        response = self.client.get(reverse('admin:pathway_review_pathwayreviewitem_changelist'))

        self.assertContains(response, 'Upload a queue file')

    def test_the_form_offers_preview_already_ticked(self):
        """What a person sees first is a rehearsal, so loading for real is a deliberate act."""
        response = self.client.get(self.url)

        self.assertContains(response, 'name="dry_run"')
        self.assertContains(response, 'checked')

    def test_a_preview_writes_nothing(self):
        """A rehearsal reports what would change and leaves the queue as it was."""
        response = self.upload(dry_run='on')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'nothing was loaded')
        self.assertFalse(PathwayReviewItem.objects.exists())

    def test_clearing_the_preview_loads_the_file(self):
        response = self.upload(dry_run='')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(PathwayReviewItem.objects.filter(item_id='Q001').exists())
        self.assertContains(response, 'are now in the queue')

    def test_a_bad_file_is_refused_with_every_reason(self):
        response = self.upload(json.dumps({'ladders': [record(), record('Q002', pool='nope')]}), dry_run='')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'problem(s) in that queue file')
        self.assertContains(response, 'expected one of')
        self.assertFalse(PathwayReviewItem.objects.exists())

    def test_unreadable_json_is_refused(self):
        response = self.upload('{not json', dry_run='')

        self.assertContains(response, 'not valid JSON')
        self.assertFalse(PathwayReviewItem.objects.exists())

    def test_a_file_too_large_to_be_a_queue_is_refused_before_it_is_read(self):
        with mock.patch('enterprise_access.apps.pathway_review.forms.MAX_UPLOAD_BYTES', 8):
            response = self.upload(dry_run='')

        self.assertContains(response, 'the limit is')
        self.assertFalse(PathwayReviewItem.objects.exists())

    def test_retiring_items_with_votes_warns(self):
        old = PathwayReviewItemFactory(item_id='OLD1', payload=ladder_payload())
        PathwayReviewVoteFactory(item=old, reviewer=UserFactory(), verdict=Verdict.GOOD)

        response = self.upload(dry_run='', deactivate_missing='on')

        self.assertContains(response, 'already carried votes')
        self.assertEqual(old.votes.count(), 1)
