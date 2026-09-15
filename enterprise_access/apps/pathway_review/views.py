"""
Views for the pathway review bench.

Plain Django views with session auth rather than DRF viewsets: this is an internal HTML
surface for named staff reviewers, not a customer-facing API, so it needs neither the
enterprise-scoped role machinery nor a published schema. The bench lives entirely in this
app and adds one route to the root urlconf.

Every entry point answers 404 rather than 403 when the gate fails. The instruction is that
the bench is only visible to people who may use it, and a 403 still tells you it is there.
The one exception is the page itself when nobody is signed in: a reviewer following a link
while logged out should reach SSO, not a dead end, so that case redirects to login.
"""

import functools
import json

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from enterprise_access.apps.pathway_review import selectors
from enterprise_access.apps.pathway_review.models import (
    VERDICTS_REQUIRING_NOTES,
    PathwayReviewerProfile,
    PathwayReviewItem,
    PathwayReviewVote,
    Verdict
)
from enterprise_access.apps.pathway_review.permissions import can_review_pathways

MAX_NOTES = 1200
MAX_GOAL = 2000


def review_access_required(view):
    """Hide the whole surface unless the flag is on and the user may review."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_review_pathways(request.user):
            raise Http404
        return view(request, *args, **kwargs)
    return wrapper


def serialize_item(item):
    """
    The reviewer-visible half of an item.

    Only ``payload`` is spread here. ``pool`` and ``control_key`` stay on the server, so a
    reviewer cannot tell a seeded control from a real pathway by reading the response.
    """
    return dict(item.payload, id=item.item_id, mix=item.mix)


@login_required
@review_access_required
def bench(request):
    """The review bench page itself."""
    return render(request, 'pathway_review/bench.html', {
        'reviewer_name': request.user.get_full_name() or request.user.username,
    })


@require_GET
@review_access_required
def next_item(request):
    """The next pathway for this reviewer, with their progress."""
    item = selectors.next_item_for(request.user)
    return JsonResponse({
        'item': serialize_item(item) if item else None,
        'progress': selectors.progress_for(request.user),
    })


@require_POST
@review_access_required
def submit_vote(request):
    """Record one judgement. Re-rating an item the reviewer already rated is refused."""
    try:
        body = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': 'Malformed request body.'}, status=400)

    item = PathwayReviewItem.objects.filter(item_id=body.get('item'), is_active=True).first()
    if not item:
        return JsonResponse({'error': 'That pathway is no longer in the queue.'}, status=404)

    verdict = body.get('verdict')
    if verdict not in Verdict.values:
        return JsonResponse({'error': 'Pick a verdict.'}, status=400)

    notes = (body.get('notes') or '').strip()[:MAX_NOTES]
    if verdict in VERDICTS_REQUIRING_NOTES and not notes:
        return JsonResponse(
            {'error': 'Say what was wrong, or how you would fix it.'}, status=400,
        )

    if PathwayReviewVote.objects.filter(item=item, reviewer=request.user).exists():
        return JsonResponse({'error': 'You have already rated this pathway.'}, status=409)

    PathwayReviewVote.objects.create(
        item=item,
        reviewer=request.user,
        verdict=verdict,
        dropped_steps=body.get('drops') or [],
        replacements=body.get('swaps') or {},
        reasons=body.get('reasons') or [],
        notes=notes,
        seconds=max(0, int(body.get('seconds') or 0)),
    )
    return JsonResponse({'progress': selectors.progress_for(request.user)})


@require_POST
@review_access_required
def set_goal(request):
    """Set this reviewer's own goal."""
    try:
        goal = int(json.loads(request.body or '{}').get('goal'))
    except (ValueError, TypeError):
        return JsonResponse({'error': 'A goal has to be a number.'}, status=400)

    goal = max(1, min(MAX_GOAL, goal))
    PathwayReviewerProfile.objects.update_or_create(
        user=request.user, defaults={'goal': goal},
    )
    return JsonResponse({'goal': goal})


@require_GET
@review_access_required
def leaderboard(request):
    """Who has reviewed the most, and which families they covered."""
    return JsonResponse({
        'rows': selectors.leaderboard_rows(),
        'you': request.user.id,
    })
