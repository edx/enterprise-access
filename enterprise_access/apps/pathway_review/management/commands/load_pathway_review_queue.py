"""
Load the human-review queue produced by the offline pathway measurement scripts.

Takes the ladder items from ``r1_review_queue.json`` and upserts one
:class:`PathwayReviewItem` per pathway, splitting each record in two: the ``payload`` a
reviewer's browser may see, and the blinding fields (``pool``, ``control_key``) it may not.
Program matches in the same file are ignored -- they are reviewed separately.

Each item's payload is self-contained, descriptions included, so the bench serves one
pathway per request instead of shipping the whole queue to the browser.

    ./manage.py load_pathway_review_queue --path /path/to/r1_review_queue.json
"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from enterprise_access.apps.pathway_review.models import PathwayReviewItem, ReviewPool

LEVELS = ('Introductory', 'Intermediate', 'Advanced')


def build_payload(record, course_descriptions):
    """Assemble the reviewer-visible half of one queue record."""
    def course(entry):
        return {
            'step': entry['step'], 'level': entry['level'], 'key': entry['key'],
            'title': entry['title'], 'provider': entry['provider'], 'url': entry['url'],
            'desc': course_descriptions.get(entry['key'], ''),
        }

    def alternate(entry):
        return {
            'key': entry['key'], 'title': entry['title'], 'provider': entry['provider'],
            'url': entry['url'], 'desc': course_descriptions.get(entry['key'], ''),
        }

    return {
        'pathway': record['pathway'],
        'careers': record['careers_covered'],
        'mix': record['mix'],
        'family_description': record.get('family_description', ''),
        'careers_list': [
            {'name': c['name'], 'desc': c.get('desc', '')} for c in record.get('careers', [])
        ],
        'supply': record.get('supply', {}),
        'courses': [course(c) for c in record['courses']],
        'alt': {lv: [alternate(c) for c in record.get('alternates', {}).get(lv, [])] for lv in LEVELS},
    }


class Command(BaseCommand):
    """ Management command to load or refresh the pathway review queue. """

    help = 'Load the pathway review queue from an r1_review_queue.json produced offline.'

    def add_arguments(self, parser):
        parser.add_argument('--path', required=True, help='Path to r1_review_queue.json')
        parser.add_argument(
            '--deactivate-missing', action='store_true',
            help='Mark items absent from this file inactive rather than leaving them in the queue.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            with open(options['path'], encoding='utf-8') as file_handle:
                data = json.load(file_handle)
        except (OSError, ValueError) as exc:
            raise CommandError(f'Could not read queue file: {exc}') from exc

        ladders = data.get('ladders')
        if not ladders:
            raise CommandError('No "ladders" in the queue file; nothing to load.')
        course_descriptions = data.get('course_descriptions') or {}

        seen, created_count, updated_count = [], 0, 0
        for record in ladders:
            _, created = PathwayReviewItem.objects.update_or_create(
                item_id=record['id'],
                defaults={
                    'family_key': record['family_key'],
                    'pathway': record['pathway'],
                    'careers_covered': record['careers_covered'],
                    'mix': record['mix'],
                    'pool': record['pool'],
                    'stratum': record.get('stratum', ''),
                    'weight': record.get('weight') or 1.0,
                    'tier': 0 if record['pool'] in (ReviewPool.REACH, ReviewPool.CONTROL) else 1,
                    'is_active': True,
                    'payload': build_payload(record, course_descriptions),
                    'control_key': record.get('control_meta') or {},
                },
            )
            seen.append(record['id'])
            created_count += int(created)
            updated_count += int(not created)

        deactivated = 0
        if options['deactivate_missing']:
            deactivated = PathwayReviewItem.objects.exclude(item_id__in=seen).update(is_active=False)

        controls = PathwayReviewItem.objects.filter(pool=ReviewPool.CONTROL).count()
        self.stdout.write(
            f'{created_count} created, {updated_count} updated, {deactivated} deactivated. '
            f'{controls} seeded controls in the queue.'
        )
