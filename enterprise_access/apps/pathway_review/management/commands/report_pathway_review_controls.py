"""
Report how each reviewer fared on the seeded controls.

A leaderboard rewards volume, and volume is exactly what the planted items keep honest. A
reviewer who approved the controls was not reading, and on consensus data that failure is
invisible -- their ratings look like agreement. This is how you see it.

"Caught" means the reviewer did not wave the item through: any verdict other than ``good``,
or a drop landing on one of the rungs that was actually corrupted.

    ./manage.py report_pathway_review_controls
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from enterprise_access.apps.pathway_review.models import PathwayReviewItem, ReviewPool
from enterprise_access.apps.pathway_review.selectors import control_performance


class Command(BaseCommand):
    """ Management command reporting reviewer performance against the seeded controls. """

    help = 'Report how each reviewer fared on the seeded control items.'

    def handle(self, *args, **options):
        planted = PathwayReviewItem.objects.filter(pool=ReviewPool.CONTROL, is_active=True).count()
        if not planted:
            self.stdout.write('No seeded controls in the queue; nothing to report.')
            return

        stats = control_performance()
        if not stats:
            self.stdout.write(f'{planted} controls in the queue, none rated yet.')
            return

        names = dict(get_user_model().objects.filter(id__in=stats).values_list('id', 'username'))
        self.stdout.write(f'{planted} seeded controls in the queue.\n')
        self.stdout.write(f'{"reviewer":28} {"seen":>5} {"caught":>7} {"passed":>7}')
        for reviewer_id, row in sorted(stats.items(), key=lambda kv: -kv[1]['seen']):
            passed = row['seen'] - row['caught']
            flag = '   <-- passed controls' if passed else ''
            self.stdout.write(
                f'{names.get(reviewer_id, reviewer_id):28} {row["seen"]:>5} '
                f'{row["caught"]:>7} {passed:>7}{flag}'
            )
        self.stdout.write(
            '\nA reviewer who passed controls should have their other ratings treated with '
            'suspicion, not merely discounted: consensus gold data cannot detect them.'
        )
