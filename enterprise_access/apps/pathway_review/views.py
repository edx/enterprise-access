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
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from enterprise_access.apps.pathway_review import selectors
from enterprise_access.apps.pathway_review.models import (
    LEGACY_NO_PICK,
    MAX_ALSO_FINE,
    NOTHING_WORKS,
    VERDICTS_REQUIRING_NOTES,
    PathwayReviewerProfile,
    PathwayReviewItem,
    PathwayReviewVote,
    Verdict,
    normalize_replacement
)
from enterprise_access.apps.pathway_review.permissions import can_review_pathways

MAX_NOTES = 1200
MAX_GOAL = 2000
MAX_SECONDS = 60 * 60 * 6


def as_list(value):
    """Coerce client input to a list; anything else is dropped rather than stored."""
    return value if isinstance(value, list) else []


def as_dict(value):
    """Coerce client input to a dict; anything else is dropped rather than stored."""
    return value if isinstance(value, dict) else {}


class InvalidSwap(ValueError):
    """A replacement pick the reviewer could not have made from the rung they were shown."""


def parse_swap(value):
    """
    One step's replacement in the stored shape, from either the current or the legacy client.

    Both clients send ``'__none__'`` for "nothing here would work". The current client sends
    ``{'best': key, 'also': [keys]}`` for a pick and nothing for a step left unanswered. The first
    client sent a bare key for a pick and ``''`` for an unanswered step; a tab open across a
    deploy still does, so both are accepted. Returns ``None`` for no pick, which is not stored.
    """
    if isinstance(value, str):
        return normalize_replacement(value)
    if not isinstance(value, dict):
        raise InvalidSwap('A replacement is one best course plus any that would also be fine.')

    best, also = value.get('best') or '', value.get('also') or []
    if not isinstance(best, str) or not isinstance(also, list) or not all(
        isinstance(key, str) and key for key in also
    ):
        raise InvalidSwap('A replacement is one best course plus any that would also be fine.')
    if not best:
        raise InvalidSwap(
            'Pick a best replacement before marking others as also fine.' if also
            else 'A replacement needs a best course.'
        )
    if len(also) > MAX_ALSO_FINE:
        raise InvalidSwap(f'Mark at most {MAX_ALSO_FINE} courses as also fine.')
    if len(set(also)) != len(also):
        raise InvalidSwap('A course is marked also fine more than once.')
    if best in also:
        raise InvalidSwap('The best replacement cannot also be marked also fine.')
    return {'best': best, 'also': list(also)}


def clean_swaps(item, drops, swaps):
    """
    Check the reviewer's replacements against what each dropped rung actually offered.

    Every key has to be one of the alternates the bench showed for that step's level: a
    replacement the reviewer could not have seen is a client bug, not a judgement, and stored
    it would read as "the ranker missed this". Raises :class:`InvalidSwap` naming the problem.
    """
    payload = item.payload or {}
    levels = {str(course.get('step')): course.get('level') for course in payload.get('courses') or []}
    offered = payload.get('alt') or {}
    dropped = {str(step) for step in drops}

    replacements = {}
    for step, value in swaps.items():
        step = str(step)
        if value == LEGACY_NO_PICK:
            continue  # the first client's "no pick": nothing to check, and nothing to store
        if step not in levels:
            raise InvalidSwap(f'This pathway has no step {step}.')
        if step not in dropped:
            raise InvalidSwap(f'Step {step} was kept, so it takes no replacement.')
        replacement = parse_swap(value)
        if replacement != NOTHING_WORKS:
            level = levels[step]
            allowed = {alt.get('key') for alt in offered.get(level) or []}
            for key in [replacement['best']] + replacement['also']:
                if key not in allowed:
                    raise InvalidSwap(
                        f'{key} is not one of the {str(level).lower()} courses offered for step {step}.'
                    )
        replacements[step] = replacement
    return replacements


def clean_suggestions(item, drops, suggest):
    """
    Check the reviewer's suggestions for rungs they kept against what each rung offered.

    A suggestion says "this course is fine, and these would also work here", so it belongs to a
    kept step; a dropped step's alternatives are its replacement picks. Every key has to be one
    of the alternates shown for that step's level, for the same reason as :func:`clean_swaps`.
    That list is also the only bound on how many a step may carry: nothing here is ranked, so
    there is no best to dilute and a reviewer may mark every alternate that would serve. An
    empty list is no suggestion and is not stored. Raises :class:`InvalidSwap`.
    """
    payload = item.payload or {}
    levels = {str(course.get('step')): course.get('level') for course in payload.get('courses') or []}
    offered = payload.get('alt') or {}
    dropped = {str(step) for step in drops}

    suggestions = {}
    for step, keys in suggest.items():
        step = str(step)
        if step not in levels:
            raise InvalidSwap(f'This pathway has no step {step}.')
        if not isinstance(keys, list) or not all(isinstance(key, str) and key for key in keys):
            raise InvalidSwap('Suggestions are a list of course keys.')
        if not keys:
            continue
        if step in dropped:
            raise InvalidSwap(f'Step {step} was dropped, so its alternatives are replacements, not suggestions.')
        if len(set(keys)) != len(keys):
            raise InvalidSwap('A course is suggested more than once.')
        level = levels[step]
        allowed = {alt.get('key') for alt in offered.get(level) or []}
        for key in keys:
            if key not in allowed:
                raise InvalidSwap(f'{key} is not one of the {str(level).lower()} courses offered for step {step}.')
        suggestions[step] = list(keys)
    return suggestions


def review_access_required(view):
    """Hide the whole surface unless the flag is on and the user may review."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_review_pathways(request.user):
            raise Http404
        return view(request, *args, **kwargs)
    return wrapper


def serialize_item(item, user=None):
    """
    The reviewer-visible half of an item.

    Only ``payload`` is spread here. ``pool`` and ``control_key`` stay on the server, so a
    reviewer cannot tell a seeded control from a real pathway by reading the response.

    ``carried`` is what this reviewer has already called acceptable on each of this item's
    levels, so a career shown in four shapes does not ask for the same answer four times. It
    says nothing about any other reviewer.
    """
    serialized = dict(item.payload, id=item.item_id, mix=item.mix)
    serialized['carried'] = selectors.carried_acceptable(user, item) if user else {}
    return serialized


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
        'item': serialize_item(item, request.user) if item else None,
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

    try:
        seconds = max(0, min(MAX_SECONDS, int(body.get('seconds') or 0)))
    except (TypeError, ValueError):
        seconds = 0

    drops = as_list(body.get('drops'))
    try:
        replacements = clean_swaps(item, drops, as_dict(body.get('swaps')))
        suggestions = clean_suggestions(item, drops, as_dict(body.get('suggest')))
    except InvalidSwap as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    if PathwayReviewVote.objects.filter(item=item, reviewer=request.user).exists():
        return JsonResponse({'error': 'You have already rated this pathway.'}, status=409)

    try:
        # The check above is the fast path; the unique constraint is the authority. A second
        # tab or a double-click can slip between the two, and an IntegrityError raised outside
        # its own atomic block would poison the surrounding transaction.
        with transaction.atomic():
            PathwayReviewVote.objects.create(
                item=item,
                reviewer=request.user,
                verdict=verdict,
                dropped_steps=drops,
                replacements=replacements,
                suggestions=suggestions,
                reasons=as_list(body.get('reasons')),
                notes=notes,
                seconds=seconds,
            )
    except IntegrityError:
        return JsonResponse({'error': 'You have already rated this pathway.'}, status=409)
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
