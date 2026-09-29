"""
Tests for the ``load_pathway_review_queue`` management command.
"""
import json
import tempfile
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_review.models import PathwayReviewItem, ReviewPool, Verdict
from enterprise_access.apps.pathway_review.tests.factories import (
    PathwayReviewItemFactory,
    PathwayReviewVoteFactory,
    ladder_payload
)


def queue_file(payload):
    """Write a queue document to a temp file and return its path."""
    handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
    json.dump(payload, handle)
    handle.close()
    return handle.name


def ladder(item_id, pool=ReviewPool.REACH, **extra):
    """A minimal queue record in the shape the offline scripts emit."""
    record = {
        'id': item_id, 'family_key': 'project manager', 'pathway': 'Project Manager',
        'careers_covered': 128, 'mix': '2/2/1', 'pool': pool, 'stratum': '2/2/1', 'weight': 1.0,
        'supply': {'Introductory': 12, 'Intermediate': 12, 'Advanced': 10},
        'careers': [{'name': 'IT Project Manager', 'desc': 'Runs technology projects.'}],
        'family_description': 'Leads a team to achieve project goals.',
        'courses': [{
            'step': 1, 'level': 'Introductory', 'key': 'AdelaideX+Project101x',
            'title': 'Introduction to Project Management', 'provider': 'Adelaide',
            'url': 'https://www.edx.org/learn/x',
        }],
        'alternates': {'Introductory': [{
            'key': 'RITx+PM9001x', 'title': 'Project Management Life Cycle',
            'provider': 'RIT', 'url': 'https://www.edx.org/learn/y',
        }], 'Intermediate': [], 'Advanced': []},
    }
    record.update(extra)
    return record


class LoadPathwayReviewQueueTests(TestCase):
    """ The loader splits each record into what reviewers may see and what they may not. """

    descriptions = {
        'AdelaideX+Project101x': 'A full description of the intro course.',
        'RITx+PM9001x': 'A full description of the alternate.',
    }

    def test_loads_ladders_and_ignores_programs(self):
        path = queue_file({
            'ladders': [ladder('L0001'), ladder('L0002')],
            'programs': [{'id': 'P0001', 'family': 'Registered Nurse'}],
            'course_descriptions': self.descriptions,
        })
        call_command('load_pathway_review_queue', path=path)

        self.assertEqual(PathwayReviewItem.objects.count(), 2)
        self.assertFalse(PathwayReviewItem.objects.filter(item_id__startswith='P').exists())

    def test_payload_carries_descriptions_and_no_blinding_fields(self):
        path = queue_file({
            'ladders': [ladder('L0003', pool=ReviewPool.CONTROL,
                               control_meta={'planted_steps': [2, 5]})],
            'course_descriptions': self.descriptions,
        })
        call_command('load_pathway_review_queue', path=path)
        item = PathwayReviewItem.objects.get(item_id='L0003')

        self.assertEqual(item.control_key, {'planted_steps': [2, 5]})
        self.assertTrue(item.is_control)
        self.assertEqual(
            item.payload['courses'][0]['desc'], 'A full description of the intro course.',
        )
        self.assertEqual(
            item.payload['alt']['Introductory'][0]['desc'], 'A full description of the alternate.',
        )
        serialized = json.dumps(item.payload)
        for leaked in ('pool', 'control', 'planted_steps', 'weight', 'stratum'):
            self.assertNotIn(leaked, serialized)

    def test_control_items_share_the_reach_tier(self):
        """Tier drives queue order, so it must not separate controls from real items."""
        path = queue_file({'ladders': [
            ladder('L0004', pool=ReviewPool.REACH),
            ladder('L0005', pool=ReviewPool.CONTROL),
            ladder('L0006', pool=ReviewPool.TAIL),
        ]})
        call_command('load_pathway_review_queue', path=path)

        tiers = dict(PathwayReviewItem.objects.values_list('item_id', 'tier'))
        self.assertEqual(tiers['L0004'], tiers['L0005'])
        self.assertNotEqual(tiers['L0004'], tiers['L0006'])

    def test_reloading_updates_rather_than_duplicates(self):
        path = queue_file({'ladders': [ladder('L0007')]})
        call_command('load_pathway_review_queue', path=path)
        updated = queue_file({'ladders': [ladder('L0007', pathway='Programme Manager')]})
        call_command('load_pathway_review_queue', path=updated)

        self.assertEqual(PathwayReviewItem.objects.count(), 1)
        self.assertEqual(PathwayReviewItem.objects.get().pathway, 'Programme Manager')

    def test_deactivate_missing(self):
        call_command('load_pathway_review_queue', path=queue_file({'ladders': [ladder('L0008')]}))
        call_command(
            'load_pathway_review_queue',
            path=queue_file({'ladders': [ladder('L0009')]}),
            deactivate_missing=True,
        )
        self.assertFalse(PathwayReviewItem.objects.get(item_id='L0008').is_active)
        self.assertTrue(PathwayReviewItem.objects.get(item_id='L0009').is_active)

    def test_missing_ladders_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command('load_pathway_review_queue', path=queue_file({'programs': []}))


class LoadCommandReportingTests(TestCase):
    """ What the command says, and what it refuses, when a file or the queue is not simple. """

    def run_command(self, **options):
        """Call the command, returning what it wrote to stdout and stderr."""
        out, err = StringIO(), StringIO()
        call_command('load_pathway_review_queue', stdout=out, stderr=err, **options)
        return out.getvalue(), err.getvalue()

    def test_a_path_that_cannot_be_read_is_an_error(self):
        with self.assertRaisesRegex(CommandError, 'Could not read queue file'):
            call_command('load_pathway_review_queue', path='/nonexistent/queue.json')

    def test_every_problem_is_listed_and_the_rest_are_counted(self):
        """A generator writing a bad file needs the reasons, not just the refusal."""
        broken = [ladder(f'L{n:04d}', courses=[]) for n in range(1, 26)]
        err = StringIO()

        with self.assertRaisesRegex(CommandError, '25 problem'):
            call_command('load_pathway_review_queue', path=queue_file({'ladders': broken}), stderr=err)

        written = err.getvalue()
        self.assertIn('L0001) has no courses', written)
        self.assertIn('... and 5 more', written)
        self.assertFalse(PathwayReviewItem.objects.exists())

    def test_items_left_out_are_reported_as_still_active(self):
        PathwayReviewItemFactory(item_id='OLD1')

        out, _ = self.run_command(path=queue_file({'ladders': [ladder('L0100')]}))

        self.assertIn('1 item(s) are not in this file and were left active', out)
        self.assertTrue(PathwayReviewItem.objects.get(item_id='OLD1').is_active)

    def test_retiring_items_that_carry_votes_is_called_out(self):
        old = PathwayReviewItemFactory(item_id='OLD1', payload=ladder_payload())
        PathwayReviewVoteFactory(item=old, reviewer=UserFactory(), verdict=Verdict.GOOD)

        out, _ = self.run_command(
            path=queue_file({'ladders': [ladder('L0101')]}), deactivate_missing=True,
        )

        self.assertIn('1 deactivated item(s) already carried votes', out)
        self.assertEqual(old.votes.count(), 1)

    def test_a_dry_run_reports_what_it_would_load_and_writes_nothing(self):
        out, _ = self.run_command(path=queue_file({'ladders': [ladder('L0102')]}), dry_run=True)

        self.assertIn('Would load: 1 created', out)
        self.assertFalse(PathwayReviewItem.objects.exists())
