"""
Factoryboy factories for the pathway review bench.
"""
import factory

from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathway_review.models import (
    PathwayReviewerProfile,
    PathwayReviewItem,
    PathwayReviewVote,
    ReviewPool,
    Verdict
)


def ladder_payload():
    """
    Build a reviewer-visible payload: a five-rung ladder and five alternates per level.

    Rungs 1-2 are introductory, 3-4 intermediate and 5 advanced; the alternates for a level are
    keyed ``Alt+<level initial><n>``, so ``Alt+I1`` is an intermediate alternate.
    """
    levels = ['Introductory', 'Introductory', 'Intermediate', 'Intermediate', 'Advanced']
    courses = [
        {'step': step, 'level': level, 'key': f'Ladder+{step}', 'title': f'Rung {step}'}
        for step, level in enumerate(levels, 1)
    ]
    initials = {'Introductory': 'B', 'Intermediate': 'I', 'Advanced': 'A'}
    alt = {
        level: [{'key': f'Alt+{initial}{n}', 'title': f'{level} alternate {n}'} for n in range(1, 6)]
        for level, initial in initials.items()
    }
    return {'pathway': 'Example', 'courses': courses, 'alt': alt}


class PathwayReviewItemFactory(factory.django.DjangoModelFactory):
    """ Test factory for the `PathwayReviewItem` model. """

    class Meta:
        model = PathwayReviewItem

    item_id = factory.Sequence(lambda n: f'L{n:04d}')
    family_key = factory.Faker('job')
    pathway = factory.Faker('job')
    careers_covered = 12
    mix = '2/2/1'
    pool = ReviewPool.REACH
    stratum = '2/2/1'
    weight = 1.0
    tier = 0
    payload = factory.LazyFunction(lambda: {'pathway': 'Example', 'courses': []})
    control_key = factory.LazyFunction(dict)


class PathwayReviewVoteFactory(factory.django.DjangoModelFactory):
    """ Test factory for the `PathwayReviewVote` model. """

    class Meta:
        model = PathwayReviewVote

    item = factory.SubFactory(PathwayReviewItemFactory)
    reviewer = factory.SubFactory(UserFactory)
    verdict = Verdict.GOOD
    dropped_steps = factory.LazyFunction(list)
    replacements = factory.LazyFunction(dict)
    reasons = factory.LazyFunction(list)
    notes = ''
    seconds = 60


class PathwayReviewerProfileFactory(factory.django.DjangoModelFactory):
    """ Test factory for the `PathwayReviewerProfile` model. """

    class Meta:
        model = PathwayReviewerProfile

    user = factory.SubFactory(UserFactory)
    goal = 20
