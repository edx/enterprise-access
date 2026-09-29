"""
Models for the pathway review bench.

The bench collects human judgements on assembled career pathways so that approved ones can
be used as worked examples. Following the eval-harness pattern, it owns no pipeline logic:
ladders are assembled elsewhere and loaded as fixtures by ``load_pathway_review_queue``.
Nothing here retrieves, ranks or assembles.

The split between :attr:`PathwayReviewItem.payload` and the blinding fields is load bearing.
``payload`` is the only column ever serialized to a reviewer's browser. ``pool`` says whether
an item is a planted control and ``control_key`` holds which of its rungs were corrupted --
a reviewer who can see either is no longer blind, so the boundary lives in the database
rather than in a build step that strips fields on the way out.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django_extensions.db.models import TimeStampedModel


class ReviewPool(models.TextChoices):
    """Which sampling pool an item was drawn from. Never served to reviewers."""

    REACH = 'reach', 'Reach census'
    TAIL = 'tail', 'Stratified tail sample'
    CONTROL = 'control', 'Seeded control'


class Verdict(models.TextChoices):
    """A reviewer's overall judgement of a pathway."""

    GOOD = 'good', 'Good'
    NEEDS_WORK = 'needs_work', 'Needs work'
    BAD = 'bad', 'Bad'
    SKIP = 'skip', 'Skipped - could not judge'


#: Verdicts that carry no diagnosis and so require the reviewer to explain themselves.
VERDICTS_REQUIRING_NOTES = frozenset({Verdict.NEEDS_WORK, Verdict.BAD})

#: A dropped rung where nothing the search found would do: the catalog lacks the content.
#: The same sentinel the first client sent and stored, so older votes need no rewrite.
NOTHING_WORKS = '__none__'
#: What the first client sent, and stored, for a dropped rung the reviewer left unanswered.
#: It is read as no pick at all, never as :data:`NOTHING_WORKS`, and is no longer stored.
LEGACY_NO_PICK = ''
#: How many replacements a reviewer may mark "also fine" beside their best pick. A replacement
#: is a ranked answer -- one course the reviewer would have put there, and a few they would
#: accept -- and marking everything plausible would dilute it. Suggestions carry no such cap:
#: see :attr:`PathwayReviewVote.suggestions`.
MAX_ALSO_FINE = 3


def normalize_replacement(value):
    """
    One stored replacement in the current shape, whichever shape it was saved in.

    Returns :data:`NOTHING_WORKS` for "nothing here would work", ``{'best': key, 'also': [keys]}``
    for a pick, or ``None`` for no pick. A bare course key is how votes were stored before
    reviewers could mark more than one replacement; it reads as a best pick with nothing else
    marked. :data:`LEGACY_NO_PICK`, and any value that is none of these, reads as no pick.
    """
    if value == NOTHING_WORKS:
        return NOTHING_WORKS
    if value == LEGACY_NO_PICK:
        return None
    if isinstance(value, str):
        return {'best': value, 'also': []}
    if isinstance(value, dict) and isinstance(value.get('best'), str) and value['best']:
        also = value.get('also')
        also = [key for key in also if isinstance(key, str) and key] if isinstance(also, list) else []
        return {'best': value['best'], 'also': also}
    return None


def normalize_replacements(value):
    """
    Return a vote's ``replacements`` in the current shape, for legacy and current votes alike.

    Read the field through this rather than directly: votes saved before the best-plus-also-fine
    picker hold a bare course key per step, and ``''`` for a step left unanswered. A step with
    no pick is absent from the result.
    """
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for step, replacement in value.items():
        replacement = normalize_replacement(replacement)
        if replacement is not None:
            normalized[str(step)] = replacement
    return normalized


class PathwayReviewItem(TimeStampedModel):
    """
    One assembled pathway placed in front of reviewers.

    .. no_pii:
    """

    item_id = models.CharField(max_length=16, unique=True, db_index=True)
    family_key = models.CharField(max_length=255)
    pathway = models.CharField(max_length=255)
    careers_covered = models.PositiveIntegerField(default=0)
    mix = models.CharField(max_length=16, blank=True)

    pool = models.CharField(max_length=16, choices=ReviewPool.choices)
    stratum = models.CharField(max_length=16, blank=True)
    #: Undoes the tail sample's disproportionate allocation. 1.0 for a census item.
    weight = models.FloatField(default=1.0)
    #: Queue ordering only. Deliberately does not distinguish reach from control.
    tier = models.PositiveSmallIntegerField(default=0)

    is_active = models.BooleanField(default=True)

    #: Everything the browser may see: courses, per-rung alternates, descriptions.
    payload = models.JSONField(default=dict)
    #: Which rungs were corrupted on a seeded control. Never serialized to a reviewer.
    control_key = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['tier', '-careers_covered', 'item_id']
        indexes = [models.Index(fields=['is_active', 'tier'])]

    @property
    def is_control(self):
        """A planted item, scored against reviewers rather than counted as gold."""
        return self.pool == ReviewPool.CONTROL

    def __str__(self):
        return f'PathwayReviewItem({self.item_id}: {self.pathway})'


class PathwayReviewVote(TimeStampedModel):
    """
    One reviewer's judgement of one item. At most one per reviewer per item.

    .. pii: Associates a review and its free-text notes with the reviewing user.
    .. pii_types: username
    .. pii_retirement: local_api
    """

    item = models.ForeignKey(PathwayReviewItem, on_delete=models.CASCADE, related_name='votes')
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='pathway_reviews',
    )

    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    #: Rung numbers (1-5) the reviewer dropped.
    dropped_steps = models.JSONField(default=list, blank=True)
    #: Dropped step number (a string) -> what should have been in that rung. Either
    #: ``{'best': key, 'also': [up to three keys]}``, the reviewer's first choice plus others that
    #: would also do, or ``'__none__'`` where nothing in the rung would do. A pick means the
    #: ranker missed better content; ``'__none__'`` means the catalog lacks it. A dropped step
    #: with no answer is absent. Older votes hold a bare course key in place of the dict, and
    #: ``''`` for a step left unanswered; read the field through :attr:`replacement_picks`, which
    #: returns the current shape for both and leaves the unanswered steps out.
    replacements = models.JSONField(default=dict, blank=True)
    #: Kept step number (a string) -> other courses from the same rung that the reviewer says
    #: would also work there. The kept course stays: a suggestion widens the set of acceptable
    #: answers rather than correcting the pathway, so it is kept apart from :attr:`replacements`,
    #: which only dropped steps have. Uncapped, unlike the also-fine picks on a replacement:
    #: nothing here is ranked, so a reviewer who finds six courses that would serve is telling
    #: us more than one who finds three. The rung's own list of alternates is the only bound.
    #: Empty on votes cast before the bench offered it.
    suggestions = models.JSONField(default=dict, blank=True)
    reasons = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)
    seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created']
        constraints = [
            models.UniqueConstraint(fields=['item', 'reviewer'], name='one_vote_per_reviewer_per_item'),
        ]

    @property
    def replacement_picks(self):
        """:attr:`replacements` in the current shape, however this vote was stored."""
        return normalize_replacements(self.replacements)

    def clean(self):
        """A verdict that is not positive owes an explanation, or it cannot be acted on."""
        if self.verdict in VERDICTS_REQUIRING_NOTES and not (self.notes or '').strip():
            raise ValidationError({'notes': 'Say what was wrong, or how you would fix it.'})

    def save(self, *args, **kwargs):
        self.full_clean(exclude=['item', 'reviewer'])
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'PathwayReviewVote({self.item.item_id} by {self.reviewer_id}: {self.verdict})'


class PathwayReviewerProfile(TimeStampedModel):
    """
    A reviewer's self-set goal, which drives their progress meter and the leaderboard.

    .. pii: Associates a review goal with the user who set it.
    .. pii_types: username
    .. pii_retirement: local_api
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='pathway_review_profile',
    )
    goal = models.PositiveIntegerField(default=20)

    def __str__(self):
        return f'PathwayReviewerProfile({self.user_id}: goal {self.goal})'
