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
