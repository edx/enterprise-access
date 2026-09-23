"""
Tests for the ``load_pathway_review_queue`` management command.
"""
import json
import tempfile

from django.core.management import CommandError, call_command
from django.test import TestCase

from enterprise_access.apps.pathway_review.models import PathwayReviewItem, ReviewPool


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
