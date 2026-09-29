"""
Queries behind the pathway review bench.

Queue order is decided here rather than in the browser. The client used to need every vote
in order to balance its own queue, which meant shipping one reviewer's judgements to the
next -- the opposite of the independence the two-rater design depends on. The browser is now
told only which item to rate next.
"""

from django.contrib.auth import get_user_model
from django.db.models import Count

from enterprise_access.apps.pathway_review.models import (
    NOTHING_WORKS,
    PathwayReviewerProfile,
    PathwayReviewItem,
    PathwayReviewVote,
    normalize_replacements
)

DEFAULT_GOAL = 20


def next_item_for(user):
    """
    The item this reviewer should judge next, or ``None`` when they have seen them all.

    Least-reviewed first, so coverage evens out however many reviewers turn up; then by tier,
    which puts the highest-traffic pathways first without revealing which items are controls;
    then by reach, so a thin session still spends itself on what learners actually hit.
    """
    return (
        PathwayReviewItem.objects
        .filter(is_active=True)
        .exclude(votes__reviewer=user)
        .annotate(vote_count=Count('votes'))
        .order_by('vote_count', 'tier', '-careers_covered', 'item_id')
        .first()
    )


def goal_for(user):
    """This reviewer's self-set goal, defaulting until they choose one."""
    profile = PathwayReviewerProfile.objects.filter(user=user).first()
    return profile.goal if profile else DEFAULT_GOAL


def progress_for(user):
    """How far this reviewer has got, for the counter and the celebration."""
    reviewed = PathwayReviewVote.objects.filter(reviewer=user).count()
    profile = PathwayReviewerProfile.objects.filter(user=user).first()
    return {
        'reviewed': reviewed,
        'goal': profile.goal if profile else DEFAULT_GOAL,
        'goal_set': profile is not None,
        'remaining': PathwayReviewItem.objects.filter(is_active=True).exclude(votes__reviewer=user).count(),
    }


def leaderboard_rows():
    """
    Who has reviewed the most, and which families each of them covered.

    Verdicts are deliberately absent: the families someone reviewed are harmless, but showing
    how they judged one would let the next reviewer anchor on it.
    """
    goals = dict(PathwayReviewerProfile.objects.values_list('user_id', 'goal'))
    reviewed = {}
    for user_id, pathway in PathwayReviewVote.objects.values_list('reviewer_id', 'item__pathway'):
        reviewed.setdefault(user_id, []).append(pathway)

    users = (
        get_user_model().objects
        .filter(pathway_reviews__isnull=False)
        .annotate(total=Count('pathway_reviews', distinct=True))
        .order_by('-total', 'username')
        .distinct()
    )
    rows = []
    for user in users:
        rows.append({
            'user_id': user.id,
            'name': user.get_full_name() or user.username,
            'total': user.total,
            'goal': goals.get(user.id, DEFAULT_GOAL),
            'families': sorted(set(reviewed.get(user.id, []))),
        })
    return rows


def control_performance():
    """
    How each reviewer fared on the seeded controls.

    Not shown in the bench. It exists so that "who reviewed the most" can be read next to
    "who was actually reading", which is the whole point of planting the controls.
    """
    stats = {}
    votes = (
        PathwayReviewVote.objects
        .filter(item__pool='control')
        .select_related('item', 'reviewer')
    )
    for vote in votes:
        row = stats.setdefault(vote.reviewer_id, {'seen': 0, 'caught': 0})
        row['seen'] += 1
        planted = set(vote.item.control_key.get('planted_steps') or [])
        found = planted & set(vote.dropped_steps or [])
        if vote.verdict != 'good' or found:
            row['caught'] += 1
    return stats


def _levels_by_step(item):
    """Step number (as a string) to the level of that rung, from an item's payload."""
    return {str(course.get('step')): course.get('level') for course in (item.payload or {}).get('courses') or []}


def _acceptable_by_level(vote):
    """
    The courses one vote put forward as acceptable, grouped by the level they sit on.

    Both menus answer the same question -- what else would serve on this rung -- so both count:
    the suggestions on a course the reviewer kept, and the also-fine picks beside a replacement.
    The best pick does not: it says what should have been there instead, which is a judgement
    about that pathway rather than a property of the career. Nor does "nothing would work",
    which is the absence of an answer, not a set of courses.
    """
    levels = _levels_by_step(vote.item)
    found = {}
    for step, keys in (vote.suggestions or {}).items():
        level = levels.get(str(step))
        if level and isinstance(keys, list):
            found.setdefault(level, []).extend(key for key in keys if isinstance(key, str) and key)
    for step, replacement in normalize_replacements(vote.replacements).items():
        level = levels.get(str(step))
        if level and replacement != NOTHING_WORKS:
            found.setdefault(level, []).extend(replacement['also'])
    return {level: list(dict.fromkeys(keys)) for level, keys in found.items() if keys}


def carried_acceptable(user, item):
    """
    What this reviewer has already said would serve on each of this item's levels.

    One career is shown in several shapes, so the same introductory courses come round again and
    again; naming them once is the point of this. The answer is read from their own votes on the
    same family rather than kept in a table of its own, which means it needs no backfill and
    cannot drift from what they actually recorded.

    The most recent vote *that said something about a level* wins, so rating a pathway without
    opening either menu leaves an earlier answer standing. Unticking a carried course therefore
    carries forward too -- the vote that dropped it is the latest word on that level. The one
    case this cannot tell apart is unticking every course at a level, which reads as having said
    nothing and leaves the earlier answer in place.

    Only courses this item actually offers on that level come back, since the window behind two
    shapes of one career need not hold the same courses.
    """
    offered = {
        level: {alt.get('key') for alt in alternates or []}
        for level, alternates in ((item.payload or {}).get('alt') or {}).items()
    }
    if not offered:
        return {}

    votes = (
        PathwayReviewVote.objects
        .filter(reviewer=user, item__family_key=item.family_key)
        .exclude(item_id=item.id)
        .select_related('item')
        .order_by('created')
    )
    latest = {}
    for vote in votes:                      # oldest first, so the last word on a level wins
        for level, keys in _acceptable_by_level(vote).items():
            latest[level] = keys

    carried = {}
    for level, keys in latest.items():
        kept = [key for key in keys if key in offered.get(level, set())]
        if kept:
            carried[level] = kept
    return carried
