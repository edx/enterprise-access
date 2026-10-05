"""
Offline evaluation and replay for the pathway shape review: votes in, measures out.

A reviewer went through the judge's shape picks on the Pathway Review Bench and, for each
item, gave a verdict, dropped courses, and either chose a replacement from the dropped
course's rung of the candidate window or said nothing there would work. This module turns
those votes into a fixture a change can be scored against, and replays selection on the
windows the collection stored, so a change to selection is measured without re-retrieving
or re-ranking anything:

``build_fixture``
    Offline. Swap pairs (dropped course -> chosen replacement, with both courses' ranks in
    the window), "nothing would work" slots, the courses kept on items rated good, the
    judge-versus-reviewer confusion, and one theme per edit, coded once from the notes by
    the keyword table below.

``replay_career``
    Re-runs the app's own ``pathway_variants.build_variants`` and ``judging.judge_pathway``
    on one career's stored window, and returns a run in ``CareerRun.to_dict()`` shape, so
    ``shape_review.select_shapes`` reads it exactly as it reads a collection.

``score_swaps`` and ``judge_prefers_replacement``
    Whether a replayed pick takes the reviewer's replacements and loses what he dropped;
    and whether the judge, shown the original pathway and the swapped one, ranks the swap
    higher.

Held-out votes
--------------
Nothing here knows which vote file it was given, and it must stay that way: the same code
builds the development fixture and, later, the held-out one. Tune the theme table or any
rule against the development votes only.

Calls
-----
``build_fixture`` and ``score_swaps`` are offline. ``replay_career`` issues the model arms'
calls and one judgement per distinct course list per rubric; ``judge_prefers_replacement``
issues at most two judgements per swap pair, checked against ``max_calls`` before each pair.
Both count what they issue.
"""
import inspect
import json
import logging
import re
import statistics
from dataclasses import asdict, is_dataclass

from enterprise_access.apps.pathway_eval import shape_review, variant_collection
from enterprise_access.apps.pathways import judging, pathway_variants
from enterprise_access.apps.pathways.judging import JUDGE_RUBRICS, RUBRIC_V1
from enterprise_access.apps.pathways.pathway_variants import (
    MIN_PATHWAY_SIZE,
    STRATEGY_MODEL_PICK,
    STRATEGY_RANKED_CUT,
    STRATEGY_SHAPE_CUT
)

logger = logging.getLogger(__name__)

SKIP = 'skip'
HUMAN_GOOD = 'good'
HUMAN_VERDICTS = ('good', 'needs_work', 'bad')
JUDGE_VERDICTS = ('good', 'weak', 'bad')
JUDGE_VERDICT_SCORE = {'good': 3, 'weak': 2, 'bad': 1}

# The three things a dropped step can say, read from what the bench wrote for it:
#
# * a pick -- a key (the legacy bench), or ``{'best': key, 'also': [keys]}`` (the current one);
# * ``"__none__"`` -- "Nothing here would work": the reviewer looked at the rung's alternatives
#   and none would do. This, and only this, is a content gap;
# * ``""``, or no entry at all -- the course was dropped and no replacement chosen. Not a gap:
#   the reviewer may simply not have looked.
OUTCOME_PICK = 'pick'
OUTCOME_NOTHING_WORKS = 'nothing_works'
OUTCOME_NO_PICK = 'no_pick'
SWAP_OUTCOMES = (OUTCOME_PICK, OUTCOME_NOTHING_WORKS, OUTCOME_NO_PICK)
NOTHING_WOULD_WORK = '__none__'

# Bench item ids: ``S<career rank>-<tier number>-<tier word>``, such as ``S01-3-ladder``.
ITEM_ID_PATTERN = re.compile(r'^S(\d+)-(\d+)-([a-z]+)$')
TIER_BY_ITEM_SUFFIX = {'intro': 'intro', 'inter': 'intermediate', 'ladder': 'ladder', 'other': 'other'}
# The queue's pathway title: ``Project Manager · Full ladder``.
PATHWAY_TITLE_SEPARATOR = ' · '

RUBRICS = JUDGE_RUBRICS

# Arms that issue no model call. Every other strategy is charged one call per variant.
FREE_STRATEGIES = (STRATEGY_RANKED_CUT, STRATEGY_SHAPE_CUT)
# Arms that take a size. A replay passes no sizes (it replays shapes), so they build nothing.
SIZE_ONLY_STRATEGIES = (STRATEGY_RANKED_CUT, STRATEGY_MODEL_PICK)

REPLAY_TRACE_PREFIX = 'replay'
REPLAY_JUDGE_TRACE_PREFIX = 'replay-judge'
SWAP_JUDGE_TRACE_PREFIX = 'swap-judge'


class ReplayContractError(RuntimeError):
    """The app function a replay calls does not (yet) take an argument the replay needs."""


# ---------------------------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------------------------

THEME_REGIONAL = 'regional'
THEME_FLAGSHIP = 'flagship_coherence'
THEME_PROMOTE_AI = 'promote_ai'
THEME_TOO_SPECIFIC = 'too_specific'
THEME_REDUNDANT = 'redundant'
THEME_ROLE_FIT = 'role_fit'
THEME_LEVEL_HONESTY = 'level_honesty'
THEME_OTHER = 'other'

# The keyword table. Written 2026-09-29 from the development votes only (S01-S08 and S09-1..3),
# BEFORE any prompt or selection work on these themes, and deliberately frozen: a table revised
# after seeing how a prompt scores would stop measuring that prompt. Change it only as a new
# fixture version, and never while looking at held-out votes.
#
# Patterns are case-insensitive regular expressions over the whole note. A vote carries every
# theme whose patterns match. Order is priority: when an edit has to be given one primary
# theme, the earliest wins (see ``swap_theme``).
THEME_KEYWORDS = (
    (THEME_REGIONAL, (
        r'\bregional', r'\blocali[sz]', r'english[- ]language', r'english market', r'\bus results\b',
        r'\bnon[- ]english\b', r'country[- ]specific',
    )),
    (THEME_FLAGSHIP, (
        r'\bcs ?50\b', r'\bflagship', r'always favou?r', r'\bwe push\b', r'best\b.{0,40}\bcourse we have',
        r'favou?r\b.{0,40}\bfor higher levels',
    )),
    (THEME_PROMOTE_AI, (
        r'\bai\b', r'artificial intelligence', r'\bgen ?ai\b', r'generative', r'\bllms?\b',
    )),
    (THEME_TOO_SPECIFIC, (
        r'too specific', r'overly\b.{0,20}\bspecific', r'tech[- ]specific', r'\bspecific for\b', r'too narrow',
        r'\bniche\b', r'less focused', r'\b(broad|general) (career |job )?family\b', r'family level',
    )),
    (THEME_REDUNDANT, (
        r'\bto+ similar\b', r'\bvery similar\b', r'repetitive', r'redundan', r'\boverlap',
        r'close in (their )?coverage', r'\bduplicat', r'same (content|material|ground)',
    )),
    (THEME_ROLE_FIT, (
        r'better (pick|choice|fit) for (a|an|the)\b', r'\bfor a manager\b', r'\bmanager should\b', r'\bimplies\b',
        r'\bstake ?holders?\b', r'\bshareholders?\b', r'\bthink like\b', r'\bpersuasion\b', r'\bcommunicat',
        r'\bcoaching\b',
    )),
    (THEME_LEVEL_HONESTY, (
        r'actual introductory', r'not (really |truly )?introductory', r'wrong (difficulty|level)',
        r'too (advanced|hard|basic|easy|challenging|difficult)', r'\bhard ?core\b', r'very challenging',
        r'/challenging',
    )),
)
THEME_PRIORITY = tuple(theme for theme, _ in THEME_KEYWORDS) + (THEME_OTHER,)
_THEME_PATTERNS = tuple(
    (theme, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
    for theme, patterns in THEME_KEYWORDS
)

# The bench's reason chips, used only when the note codes to nothing. "Too generic" and
# "Catalog has nothing better" are left unmapped: the one dev vote that ticked "too generic"
# wrote "too specific" in its note, so the chip's meaning is not settled.
REASON_THEMES = {
    'wrong_level': THEME_LEVEL_HONESTY,
    'bad_order': THEME_LEVEL_HONESTY,
    'wrong_job': THEME_ROLE_FIT,
}

# Hand-coded exceptions, applied after the keyword rules: an item id recodes every edit on that
# vote, an ``(item id, step)`` recodes one swap. Empty: the rules code every development vote
# the way a reader of its note would.
THEME_OVERRIDES: dict = {}

# A replacement "is an AI course" by its title. Used to tell which of a vote's swaps a
# "promote AI" note was about when the vote made more than one.
AI_TITLE_PATTERN = re.compile(
    r'\bAI\b|artificial intelligence|generative|\bgen ?ai\b|\bLLMs?\b|machine learning|chatgpt|copilot',
    re.IGNORECASE,
)

_SENTENCE_BREAK = re.compile(r'\n+|(?<=[.!?])\s+')
# A dropped course is "mentioned" in a sentence by its key, its full title, or its provider.
# Short strings are ignored: a two-letter provider would match half the notes.
_MIN_MENTION_CHARS = 4


def _by_priority(themes) -> list[str]:
    """De-duplicate themes into ``THEME_PRIORITY`` order."""
    present = set(themes)
    return [theme for theme in THEME_PRIORITY if theme in present]


def code_themes(notes: str, reasons=()) -> list[str]:
    """
    Every theme a note's text matches, in priority order.

    Falls back to the bench's reason chips (``REASON_THEMES``) only when the note matches
    nothing. Returns ``[]`` for a note that says nothing codeable; the caller decides whether
    that is ``other`` (an edit with an unexplained note) or nothing at all (a silent vote).
    """
    text = ' '.join((notes or '').split())
    themes = [theme for theme, patterns in _THEME_PATTERNS if any(pattern.search(text) for pattern in patterns)]
    if not themes:
        themes = [REASON_THEMES[reason] for reason in reasons or () if reason in REASON_THEMES]
    return _by_priority(themes)


def vote_themes(vote: dict, overrides=None) -> tuple[list[str], str]:
    """
    A vote's themes and where they came from: ``override``, ``keywords``, ``other`` or ``none``.

    A vote with edits or a note that codes to nothing is ``other``; a silent vote with no
    edits has no theme.
    """
    overrides = THEME_OVERRIDES if overrides is None else overrides
    if vote['item_id'] in overrides:
        return [overrides[vote['item_id']]], 'override'
    themes = code_themes(vote.get('notes', ''), vote.get('reasons'))
    if themes:
        return themes, 'keywords'
    if (vote.get('notes') or '').strip() or vote.get('drops') or vote.get('swaps'):
        return [THEME_OTHER], 'other'
    return [], 'none'


def _mentions(sentence: str, course: dict) -> bool:
    """Whether a sentence names the course by key, full title or provider."""
    lowered = sentence.lower()
    for text in (course.get('key'), course.get('title'), course.get('provider')):
        text = (text or '').strip().lower()
        if len(text) >= _MIN_MENTION_CHARS and text in lowered:
            return True
    return False


def swap_theme(vote: dict, step: int, dropped: dict, replacement_title: str, *,
               themes: list[str], overrides=None) -> tuple[str, str]:
    """
    The one primary theme of one swap on a vote, and how it was chosen.

    1. An ``(item id, step)`` or item-id override.
    2. The themes of the note's sentences that name the dropped course, if any code.
    3. ``promote_ai`` when the note has it and the replacement's title reads as an AI course.
    4. The vote's themes, less ``promote_ai`` when the replacement is not an AI course and
       another theme remains -- a vote that swapped in one AI course and fixed something
       else in the same breath is about two things.

    The earliest theme in ``THEME_PRIORITY`` wins within a step.
    """
    overrides = THEME_OVERRIDES if overrides is None else overrides
    if (vote['item_id'], step) in overrides:
        return overrides[(vote['item_id'], step)], 'override'
    if vote['item_id'] in overrides:
        return overrides[vote['item_id']], 'override'

    sentences = [sentence for sentence in _SENTENCE_BREAK.split(vote.get('notes') or '') if sentence.strip()]
    mentioned = _by_priority(
        theme for sentence in sentences if _mentions(sentence, dropped) for theme in code_themes(sentence)
    )
    if mentioned:
        return mentioned[0], 'sentence'

    is_ai = bool(AI_TITLE_PATTERN.search(replacement_title or ''))
    if THEME_PROMOTE_AI in themes and is_ai:
        return THEME_PROMOTE_AI, 'ai_replacement'
    remaining = [theme for theme in themes if theme != THEME_PROMOTE_AI or is_ai]
    candidates = remaining or themes
    return (candidates[0], 'vote') if candidates else (THEME_OTHER, 'other')


# ---------------------------------------------------------------------------------------------
# Votes
# ---------------------------------------------------------------------------------------------

def _swap(outcome, best=None, also=()) -> dict:
    return {'outcome': outcome, 'best': best, 'also': list(also), 'none': outcome == OUTCOME_NOTHING_WORKS}


def normalise_swap(value) -> dict:
    """
    One dropped step as ``{'outcome', 'best', 'also', 'none'}``.

    ``outcome`` is ``pick`` (``best`` and/or ``also`` hold keys), ``nothing_works`` (the
    ``"__none__"`` marker; ``none`` is true for this alone) or ``no_pick`` (``""``, or nothing
    usable). Reads the legacy string and the ``{'best': key, 'also': [keys]}`` shape alike, and
    is idempotent, so an already-normalised swap passes through unchanged.
    """
    if isinstance(value, dict):
        best, also = value.get('best'), value.get('also') or []
        marked_none = value.get('outcome') == OUTCOME_NOTHING_WORKS or value.get('none') is True
    else:
        best, also, marked_none = value, [], False
    best = best.strip() if isinstance(best, str) else ''
    if best == NOTHING_WOULD_WORK:
        best, marked_none = '', True
    others = []
    for key in also if isinstance(also, (list, tuple)) else [also]:
        key = key.strip() if isinstance(key, str) else ''
        if key and key != NOTHING_WOULD_WORK and key != best and key not in others:
            others.append(key)
    if best or others:
        return _swap(OUTCOME_PICK, best or None, others)
    return _swap(OUTCOME_NOTHING_WORKS if marked_none else OUTCOME_NO_PICK)


def normalise_vote(raw: dict) -> dict:
    """
    One bench vote with steps as ints, drops covering every swapped step, and swaps normalised.

    Idempotent. A step that is swapped is dropped by definition, whether or not the export
    listed it in ``drops``; a dropped step with no swap entry is a ``no_pick``.
    """
    swaps = {}
    for step, value in (raw.get('swaps') or {}).items():
        try:
            swaps[int(step)] = normalise_swap(value)
        except (TypeError, ValueError):
            logger.warning('Vote %s: ignoring swap on unreadable step %r.', raw.get('item_id'), step)
    drops = set()
    for step in raw.get('drops') or []:
        try:
            drops.add(int(step))
        except (TypeError, ValueError):
            logger.warning('Vote %s: ignoring unreadable drop %r.', raw.get('item_id'), step)
    for step in drops - set(swaps):
        swaps[step] = _swap(OUTCOME_NO_PICK)
    return {
        'item_id': str(raw.get('item_id') or ''),
        'verdict': str(raw.get('verdict') or ''),
        'drops': sorted(drops | set(swaps)),
        'swaps': dict(sorted(swaps.items())),
        'reasons': list(raw.get('reasons') or []),
        'notes': str(raw.get('notes') or ''),
        'seconds': raw.get('seconds'),
        'created': str(raw.get('created') or ''),
    }


def load_votes(path) -> list[dict]:
    """Read a bench vote export (``{'votes': [...]}`` or a bare list) into normalised votes."""
    with open(path, encoding='utf-8') as handle:
        data = json.load(handle)
    raw_votes = data.get('votes') if isinstance(data, dict) else data
    return [normalise_vote(vote) for vote in raw_votes or [] if isinstance(vote, dict)]


def _latest_vote_per_item(votes) -> tuple[list[dict], int]:
    """One vote per item, the latest by ``created``; and how many earlier ones were set aside."""
    latest: dict = {}
    for vote in votes:
        current = latest.get(vote['item_id'])
        if current is None or vote['created'] >= current['created']:
            latest[vote['item_id']] = vote
    return [latest[item_id] for item_id in sorted(latest)], len(votes) - len(latest)


# ---------------------------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------------------------

def career_from_pathway_title(title: str) -> str:
    """``'Project Manager · Full ladder'`` -> ``'Project Manager'``."""
    return (title or '').split(PATHWAY_TITLE_SEPARATOR)[0].strip()


def _item_career(item_id, item, judged_item, career_names) -> tuple[str, str]:
    """The item's career, cross-checked across every source that names it; ``(career, problem)``."""
    names = [
        (judged_item or {}).get('career') or '',
        item.get('career') or career_from_pathway_title(item.get('pathway', '')),
    ]
    match = ITEM_ID_PATTERN.match(item_id)
    if career_names and match and 1 <= int(match.group(1)) <= len(career_names):
        names.append(career_names[int(match.group(1)) - 1])
    distinct = {name.strip().lower(): name.strip() for name in names if name and name.strip()}
    if len(distinct) > 1:
        return '', f'{item_id}: sources disagree on its career ({sorted(distinct.values())})'
    if not distinct:
        return '', f'{item_id}: no career found for it'
    return next(iter(distinct.values())), ''


def _item_tier(item_id, item, judged_item) -> str:
    """The shape tier: the judge key's, else the item id's suffix, else the queue's stratum."""
    tier = (judged_item or {}).get('tier')
    if tier:
        return tier
    match = ITEM_ID_PATTERN.match(item_id)
    if match and match.group(3) in TIER_BY_ITEM_SUFFIX:
        return TIER_BY_ITEM_SUFFIX[match.group(3)]
    return item.get('stratum') or ''


def _runs_by_career(checkpoint_runs) -> dict:
    """Runs keyed by lower-cased career and requested name. Accepts dicts or ``CareerRun`` objects."""
    runs = checkpoint_runs.values() if isinstance(checkpoint_runs, dict) else checkpoint_runs or []
    by_name = {}
    for run in runs:
        run = run.to_dict() if hasattr(run, 'to_dict') else run
        for name in (run.get('requested_name'), run.get('career_name')):
            if name:
                by_name.setdefault(name.strip().lower(), run)
    return by_name


class _Window:
    """A career's ordered candidate window, with each course's rank overall and on its rung."""

    def __init__(self, candidates):
        self.candidates = list(candidates or [])
        self.by_key, self.window_rank, self.rung_rank, self.rung_size = {}, {}, {}, {}
        for index, candidate in enumerate(self.candidates):
            key = candidate.get('key')
            if not key or key in self.by_key:
                continue
            level = candidate.get('level_type') or ''
            self.by_key[key] = candidate
            self.window_rank[key] = index
            self.rung_rank[key] = self.rung_size.get(level, 0)
            self.rung_size[level] = self.rung_size.get(level, 0) + 1

    def title(self, key, fallback=''):
        return (self.by_key.get(key) or {}).get('title') or fallback

    def level(self, key, fallback=''):
        return (self.by_key.get(key) or {}).get('level_type') or fallback


def _alternate_titles(item) -> dict:
    """Key -> title for the queue item's alternates, a fallback when a key is not in the window."""
    return {
        alternate.get('key'): alternate.get('title', '')
        for alternates in (item.get('alternates') or {}).values()
        for alternate in alternates or []
        if isinstance(alternate, dict) and alternate.get('key')
    }


def _item_record(item_id, item, career, tier, vote, judged_item) -> dict:
    courses = sorted(item.get('courses') or [], key=lambda course: course.get('step', 0))
    return {
        'item_id': item_id,
        'career': career,
        'tier': tier,
        'level_mix': item.get('mix', ''),
        'course_keys': [course.get('key', '') for course in courses],
        'courses': [
            {key: course.get(key, '') for key in ('step', 'key', 'title', 'level', 'provider')}
            for course in courses
        ],
        'human_verdict': vote['verdict'],
        'judge_verdict': (judged_item or {}).get('verdict', ''),
    }


def build_fixture(*, votes, queue, judge_key, checkpoint_runs, career_names=None, theme_overrides=None) -> dict:
    """
    The review fixture: every measure later scoring needs, derived once and offline.

    Args:
        votes: Bench votes (raw or from ``load_votes``). The latest vote per item counts.
        queue: The bench queue (``queue.json``): items, their courses by step, alternates.
        judge_key: ``judge_key.json``: the judge's verdict, tier and on-topic flags per item.
        checkpoint_runs: The collection's runs (dicts or ``CareerRun``), with candidate windows.
        career_names: The careers file in rank order, so ``S01`` is its first name. Optional;
            when given it must agree with the judge key and the queue.
        theme_overrides: Replaces ``THEME_OVERRIDES``, for tests.

    Returns a JSON-safe dict in a stable order. Ranks are 0-based: ``window_rank`` is the
    position in the career's re-rank order, ``rung_rank`` the position among candidates on the
    same level. Skipped votes contribute no edits; a career whose every vote was a skip is
    listed in ``excluded_careers`` and left out of every measure.

    Each dropped step is one of three outcomes (see ``normalise_swap``). Picks become
    ``swap_pairs``; "nothing would work" becomes ``nothing_would_work``, the content gaps; a drop
    with no pick goes to ``dropped_without_pick`` and is never read as a gap. That list alone
    also carries unscored votes' drops, flagged ``scored: false``, since it feeds no measure.

    Raises:
        ValueError: A voted item missing from the queue, a step missing from its item, or an
            item whose sources disagree on its career.
    """
    overrides = THEME_OVERRIDES if theme_overrides is None else theme_overrides
    items = {item.get('id'): item for item in (queue or {}).get('ladders') or [] if isinstance(item, dict)}
    judged = (judge_key or {}).get('items') or {}
    runs = _runs_by_career(checkpoint_runs)
    latest, superseded = _latest_vote_per_item([normalise_vote(vote) for vote in votes or []])

    problems, contexts = [], []
    for vote in latest:
        item = items.get(vote['item_id'])
        if item is None:
            problems.append(f'{vote["item_id"]}: not in the queue')
            continue
        career, problem = _item_career(vote['item_id'], item, judged.get(vote['item_id']), career_names)
        if problem:
            problems.append(problem)
            continue
        contexts.append((vote, item, career))
    if problems:
        raise ValueError('; '.join(problems))

    by_career: dict = {}
    for vote, _, career in contexts:
        by_career.setdefault(career, []).append(vote['verdict'])
    excluded = sorted(career for career, verdicts in by_career.items() if all(v == SKIP for v in verdicts))

    fixture = {
        'items': {}, 'votes': [], 'swap_pairs': [], 'nothing_would_work': [], 'dropped_without_pick': [],
        'kept_good': [], 'excluded_careers': excluded,
    }
    for vote, item, career in contexts:
        _add_vote(fixture, vote, item, career, judged.get(vote['item_id']),
                  _Window((runs.get(career.lower()) or {}).get('candidates')), overrides, problems)
    if problems:
        raise ValueError('; '.join(problems))

    fixture['swap_pairs'].sort(key=lambda pair: (pair['item_id'], pair['step'], not pair['is_best'],
                                                 pair['replacement_key']))
    fixture['confusion'] = confusion(fixture['votes'], excluded)
    fixture['themes'] = _theme_summary(fixture, overrides)
    fixture['rank_stats'] = rank_stats(fixture['swap_pairs'])
    scored = [vote for vote in fixture['votes'] if vote['scored']]
    fixture['summary'] = {
        'votes': len(fixture['votes']),
        'superseded_votes': superseded,
        'scored_votes': len(scored),
        'skipped_votes': sum(1 for vote in fixture['votes'] if vote['verdict'] == SKIP),
        'careers': len(by_career),
        'swap_pairs': len(fixture['swap_pairs']),
        'best_swap_pairs': sum(1 for pair in fixture['swap_pairs'] if pair['is_best']),
        'nothing_would_work': len(fixture['nothing_would_work']),
        'dropped_without_pick': len(fixture['dropped_without_pick']),
        'dropped_without_pick_scored': sum(1 for entry in fixture['dropped_without_pick'] if entry['scored']),
        'kept_good_items': len(fixture['kept_good']),
        'kept_good_courses': sum(len(entry['kept_keys']) for entry in fixture['kept_good']),
    }
    return fixture


def _add_vote(fixture, vote, item, career, judged_item, window, overrides, problems) -> None:
    """Record one vote, its item, and its edits on ``fixture``."""
    item_id = vote['item_id']
    tier = _item_tier(item_id, item, judged_item)
    record = _item_record(item_id, item, career, tier, vote, judged_item)
    fixture['items'][item_id] = record
    themes, source = vote_themes(vote, overrides)
    scored = vote['verdict'] != SKIP and career not in fixture['excluded_careers']
    fixture['votes'].append({
        'item_id': item_id, 'career': career, 'tier': tier, 'verdict': vote['verdict'],
        'judge_verdict': record['judge_verdict'], 'scored': scored, 'themes': themes, 'theme_source': source,
        'reasons': vote['reasons'], 'drops': vote['drops'], 'notes': vote['notes'],
        'edits': [{'step': step, **swap} for step, swap in vote['swaps'].items()],
    })

    courses = {course['step']: course for course in record['courses']}
    on_topic = (judged_item or {}).get('on_topic') or {}
    alternates = _alternate_titles(item)
    base = {'item_id': item_id, 'career': career, 'tier': tier}
    for step in vote['drops']:
        dropped = courses.get(step)
        if dropped is None:
            problems.append(f'{item_id}: step {step} was dropped but the item has no such step')
            continue
        swap = vote['swaps'][step]
        slot = {**base, 'step': step, 'level': dropped['level'], 'dropped_key': dropped['key']}
        if swap['outcome'] == OUTCOME_NO_PICK:
            fixture['dropped_without_pick'].append({**slot, 'scored': scored})
        elif not scored:
            continue
        elif swap['outcome'] == OUTCOME_NOTHING_WORKS:
            fixture['nothing_would_work'].append({**slot, 'vote_themes': themes})
        else:
            choices = ([(swap['best'], True)] if swap['best'] else []) + [(key, False) for key in swap['also']]
            for key, is_best in choices:
                title = window.title(key, alternates.get(key, ''))
                theme, theme_source = swap_theme(vote, step, dropped, title, themes=themes, overrides=overrides)
                fixture['swap_pairs'].append({
                    **slot,
                    'dropped_title': dropped['title'],
                    'dropped_judged_on_topic': on_topic.get(dropped['key']),
                    'replacement_key': key,
                    'replacement_title': title,
                    'replacement_level': window.level(key),
                    'is_best': is_best,
                    'dropped_window_rank': window.window_rank.get(dropped['key']),
                    'replacement_window_rank': window.window_rank.get(key),
                    'dropped_rung_rank': window.rung_rank.get(dropped['key']),
                    'replacement_rung_rank': window.rung_rank.get(key),
                    'window_size': len(window.candidates),
                    'rung_size': window.rung_size.get(dropped['level'], 0),
                    'theme': theme,
                    'theme_source': theme_source,
                })
    if scored and vote['verdict'] == HUMAN_GOOD:
        kept = [course['key'] for step, course in sorted(courses.items()) if step not in vote['drops']]
        fixture['kept_good'].append({**base, 'kept_keys': kept,
                                     'dropped_keys': [courses[s]['key'] for s in vote['drops'] if s in courses]})


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def confusion(votes, excluded_careers=()) -> dict:
    """
    The judge's verdict against the reviewer's, per item.

    Skips are counted apart, by the judge's verdict, and never enter the matrix or the
    precision. ``judge_good_precision`` is the share of items the judge rated good that the
    reviewer also rated good.
    """
    matrix = {verdict: {human: 0 for human in HUMAN_VERDICTS} for verdict in JUDGE_VERDICTS}
    skips = {verdict: 0 for verdict in JUDGE_VERDICTS}
    for vote in votes:
        judge = vote.get('judge_verdict') or 'none'
        if vote['verdict'] == SKIP:
            skips[judge] = skips.get(judge, 0) + 1
            continue
        if vote['career'] in excluded_careers:
            continue
        row = matrix.setdefault(judge, {human: 0 for human in HUMAN_VERDICTS})
        row[vote['verdict']] = row.get(vote['verdict'], 0) + 1
    judge_good = sum(matrix['good'].values())
    both_good = matrix['good'].get(HUMAN_GOOD, 0)
    scored = sum(sum(row.values()) for row in matrix.values())
    human_good = sum(row.get(HUMAN_GOOD, 0) for row in matrix.values())
    return {
        'matrix': matrix,
        'skips': skips,
        'scored': scored,
        'judge_good_precision': {'value': _rate(both_good, judge_good), 'numerator': both_good,
                                 'denominator': judge_good},
        'human_good_rate': {'value': _rate(human_good, scored), 'numerator': human_good, 'denominator': scored},
    }


def _theme_summary(fixture, overrides) -> dict:
    """The keyword table as applied, and how many scored votes and best swaps carry each theme."""
    vote_counts, swap_counts = {}, {}
    for vote in fixture['votes']:
        if vote['scored']:
            for theme in vote['themes']:
                vote_counts[theme] = vote_counts.get(theme, 0) + 1
    for pair in fixture['swap_pairs']:
        if pair['is_best']:
            swap_counts[pair['theme']] = swap_counts.get(pair['theme'], 0) + 1
    return {
        'keywords': {theme: list(patterns) for theme, patterns in THEME_KEYWORDS},
        'priority': list(THEME_PRIORITY),
        'reason_themes': dict(REASON_THEMES),
        'overrides': {
            (key if isinstance(key, str) else f'{key[0]}#{key[1]}'): theme for key, theme in overrides.items()
        },
        'vote_counts': {theme: vote_counts[theme] for theme in THEME_PRIORITY if theme in vote_counts},
        'swap_counts': {theme: swap_counts[theme] for theme in THEME_PRIORITY if theme in swap_counts},
    }


def _spread(values) -> dict:
    values = [value for value in values if value is not None]
    if not values:
        return {'n': 0, 'median': None, 'mean': None, 'min': None, 'max': None}
    return {'n': len(values), 'median': statistics.median(values), 'mean': round(statistics.mean(values), 2),
            'min': min(values), 'max': max(values)}


def rank_stats(swap_pairs) -> dict:
    """
    Where the reviewer's best replacements sat in the window, against what they replaced.

    ``replacement_below_dropped_on_rung`` counts pairs where the re-rank placed the reviewer's
    choice lower on the rung than the course he removed -- the ranker's miss the swap reveals.
    ``rung_rank_delta`` is replacement minus dropped: positive means he reached further down.
    """
    best = [pair for pair in swap_pairs if pair['is_best']]
    ranked = [pair for pair in best
              if pair['dropped_rung_rank'] is not None and pair['replacement_rung_rank'] is not None]
    unique = {(pair['career'], pair['dropped_key'], pair['replacement_key']) for pair in best}
    return {
        'pairs': len(best),
        'unique_pairs': len(unique),
        'replacement_not_in_window': sum(1 for pair in best if pair['replacement_window_rank'] is None),
        'replacement_level_differs': sum(
            1 for pair in best if pair['replacement_level'] and pair['replacement_level'] != pair['level']
        ),
        'replacement_below_dropped_on_rung': sum(
            1 for pair in ranked if pair['replacement_rung_rank'] > pair['dropped_rung_rank']
        ),
        'dropped_rung_rank': _spread(pair['dropped_rung_rank'] for pair in best),
        'replacement_rung_rank': _spread(pair['replacement_rung_rank'] for pair in best),
        'rung_rank_delta': _spread(pair['replacement_rung_rank'] - pair['dropped_rung_rank'] for pair in ranked),
        'dropped_window_rank': _spread(pair['dropped_window_rank'] for pair in best),
        'replacement_window_rank': _spread(pair['replacement_window_rank'] for pair in best),
    }


def career_context_from_queue(queue) -> dict:
    """
    What the queue says about each career, from its first item.

    Returns ``{career: {'career_description', 'family_titles', 'family_size'}}``. The family
    size is the queue's ``careers_covered`` where it has one, else the number of titles.
    """
    context = {}
    for item in (queue or {}).get('ladders') or []:
        career = item.get('career') or career_from_pathway_title(item.get('pathway', ''))
        if not career or career in context:
            continue
        titles = [entry.get('name', '') for entry in item.get('careers') or [] if isinstance(entry, dict)]
        titles = [title for title in titles if title]
        context[career] = {
            'career_description': item.get('family_description') or '',
            'family_titles': titles,
            'family_size': int(item.get('careers_covered') or len(titles)),
        }
    return context


# ---------------------------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------------------------

def _unset(name, value) -> bool:
    """Whether an optional argument carries nothing a caller would miss if it were not passed."""
    return not value or (name == 'rubric' and value == RUBRIC_V1)


def contract_kwargs(func, values: dict) -> dict:
    """
    The optional arguments ``func`` accepts, from ``values``.

    Arguments the function does not take are left out only when they are unset (``_unset``);
    a set one it cannot take raises, so a replay never silently runs without the policy, the
    rubric or the career context it was asked for.

    Raises:
        ReplayContractError: ``func`` lacks a parameter that ``values`` sets.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return dict(values)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return dict(values)
    missing = sorted(name for name, value in values.items() if name not in params and not _unset(name, value))
    if missing:
        raise ReplayContractError(
            f'{getattr(func, "__qualname__", func)} does not accept {missing}; the replay needs the app '
            'change that adds them.'
        )
    return {name: value for name, value in values.items() if name in params}


def judgement_field(rubric: str) -> str:
    """Where a variant carries a rubric's judgement, as ``shape_review`` reads it."""
    return shape_review.JUDGEMENT_FIELDS.get(rubric) or f'judgement_{rubric}'


def normalise_rubrics(rubrics) -> list[str]:
    """
    The app's rubric normalisation: canonical order, v1 alone when none were given.

    Raises:
        ValueError: A rubric not in ``judging.JUDGE_RUBRICS``.
    """
    return judging.normalise_rubrics(rubrics)


def _json_safe(value):
    """Plain JSON types from whatever a variant carries: dataclasses, tuples, sets, ``to_dict``."""
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if hasattr(value, 'to_dict') and callable(value.to_dict):
        return _json_safe(value.to_dict())
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_json_safe(item) for item in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def variant_record(variant) -> dict:
    """
    A ``pathway_variants.Variant`` as ``PathwayAssemblyWorkflow.variants()`` exports one.

    Completeness, the realised mix and the gates are the variant's own properties, never
    recomputed here. ``seats`` is whatever the variant carries under that name (an editorial
    arm records which seat each course fills), ``[]`` when it carries none.
    """
    trace = variant.trace or {}
    seats = getattr(variant, 'seats', None)
    return {
        'label': variant.label,
        'strategy': variant.strategy,
        'requested_size': variant.requested_size,
        'shape': pathway_variants.shape_name(variant.shape) if variant.shape is not None else '',
        'courses': [
            {'key': course.key, 'title': course.title, 'level_type': course.level_type, 'partner': course.partner}
            for course in variant.courses
        ],
        'complete': variant.is_complete,
        'level_mix': dict(variant.realised_level_mix),
        # As ``BuildVariantsStep.to_output``: a variant that failed outright has nothing to gate.
        'violations': list(variant.violations()) if variant.courses else [],
        'seats': _json_safe(seats) if seats is not None else [],
        'repair': _json_safe({k: v for k, v in (getattr(variant, 'repair', None) or {}).items() if k != 'trace'}),
        'dropped': dict(variant.dropped),
        'fabricated_keys': list(variant.fabricated_keys),
        'error': variant.error,
        'backend': trace.get('backend', '') or '',
        'model': trace.get('model', '') or '',
        'input_tokens': trace.get('input_tokens'),
        'output_tokens': trace.get('output_tokens'),
        'elapsed_ms': trace.get('elapsed_ms', 0) or 0,
    }


def judgement_record(result: dict, *, label: str, rubric: str) -> dict:
    """``judge_pathway``'s result as a ``PathwayJudgement`` exports it, plus ``rubric``."""
    trace = result.get('trace') or {}
    record = {key: _json_safe(value) for key, value in result.items() if key != 'trace'}
    record.update({
        'label': label, 'rubric': rubric, 'same_as': '',
        'backend': trace.get('backend', '') or '', 'model': trace.get('model', '') or '',
        'input_tokens': trace.get('input_tokens'), 'output_tokens': trace.get('output_tokens'),
        'elapsed_ms': trace.get('elapsed_ms', 0) or 0,
    })
    return record


def _arm_called_model(variant) -> bool:
    """Whether a variant's arm asked a model: a response came back, or the request itself failed."""
    if variant.strategy in FREE_STRATEGIES:
        return False
    return bool(variant.trace) or 'failed' in (variant.error or '')


def judge_one(*, career_name, career_skills, courses, trace_id, backend, rubric, context) -> dict:
    """``judging.judge_pathway`` with the rubric and career context, where the app takes them."""
    optional = contract_kwargs(judging.judge_pathway, {'rubric': rubric, **context})
    return judging.judge_pathway(
        career_name=career_name, career_skills=list(career_skills or []), courses=courses,
        trace_id=trace_id, backend=backend, **optional,
    )


def _course_details(keys, candidates_by_key, fallback: dict | None = None) -> list[dict]:
    """The full candidate dict per key, as the judge step passes them; a minimal dict if absent."""
    fallback = fallback or {}
    return [candidates_by_key.get(key) or fallback.get(key) or {'key': key} for key in keys]


def replay_career(run: dict, *, strategies, shapes, career_skills, career_description: str = '',
                  family_titles=(), family_size: int = 0, policy=None, rubrics=(RUBRIC_V1,),
                  variant_backend=None, judge_backend=None, single_ecosystem: bool | None = None) -> dict:
    """
    Re-run selection and judging on one career's stored window, without retrieval or re-rank.

    Calls the app's ``pathway_variants.build_variants`` on ``run['candidates']`` (the ordered
    window the collection recorded) with no sizes, then ``judging.judge_pathway`` once per
    rubric for every variant of two or more courses, reusing a verdict for an identical
    course list as ``JudgePathwaysStep`` does.

    Args:
        run: A collection run (``CareerRun.to_dict()``) recorded with its candidates.
        strategies, shapes: The arms and shapes to build.
        career_skills: The career's skills, looked up by the caller (this stays offline).
        career_description, family_titles, family_size: Career context for the arms and the
            judge, passed where the app takes them.
        policy: An editorial policy for the arms that read one.
        single_ecosystem: Passed to ``build_variants`` as given: ``True`` or ``False`` decides,
            and ``None`` leaves it to the app's default (on, unless its kill switch is on).
        rubrics: ``v1`` and/or ``v2``; each lands in ``judgement_field(rubric)``.

    Returns:
        A run in ``CareerRun.to_dict()`` shape -- ``workflow_uuid`` ``replay:<career>``, no
        delivered pathway -- plus ``replay``: what was run and the model calls it issued.

    Raises:
        ReplayContractError: The app does not yet take an argument this replay sets.
        ValueError: An unknown strategy, shape or rubric.
    """
    career = run.get('career_name') or run.get('requested_name') or ''
    candidates = list(run.get('candidates') or [])
    rubrics = normalise_rubrics(rubrics)
    context = {
        'career_description': career_description or '',
        'family_titles': list(family_titles or []),
        'family_size': int(family_size or 0),
    }
    build_optional = contract_kwargs(pathway_variants.build_variants,
                                     {'policy': policy, 'single_ecosystem': single_ecosystem, **context})
    for rubric in rubrics:
        # Checked before the arms run, so a judge that cannot take the rubric costs no arm calls.
        contract_kwargs(judging.judge_pathway, {'rubric': rubric, **context})
    variants = pathway_variants.build_variants(
        career_name=career, career_skills=list(career_skills or []), ordered_candidates=candidates,
        sizes=[], strategies=list(strategies), shapes=list(shapes),
        trace_prefix=f'{REPLAY_TRACE_PREFIX}:{career}', backend=variant_backend, **build_optional,
    )

    details = {candidate.get('key'): candidate for candidate in candidates if candidate.get('key')}
    judged = {rubric: {} for rubric in rubrics}
    judge_calls, records = 0, []
    for variant in variants:
        record = variant_record(variant)
        for rubric in RUBRICS:
            record[judgement_field(rubric)] = None
        if len(variant.courses) >= MIN_PATHWAY_SIZE:
            keys = tuple(course.key for course in variant.courses)
            fallback = {
                course.key: {'key': course.key, 'title': course.title, 'level_type': course.level_type}
                for course in variant.courses
            }
            for rubric in rubrics:
                earlier = judged[rubric].get(keys)
                if earlier is not None:
                    record[judgement_field(rubric)] = {**earlier, 'label': variant.label, 'same_as': earlier['label']}
                    continue
                result = judge_one(
                    career_name=career, career_skills=career_skills,
                    courses=_course_details(keys, details, fallback),
                    trace_id=f'{REPLAY_JUDGE_TRACE_PREFIX}:{career}:{rubric}:{variant.label}',
                    backend=judge_backend, rubric=rubric, context=context,
                )
                judge_calls += 1
                judgement = judgement_record(result, label=variant.label, rubric=rubric)
                record[judgement_field(rubric)] = judgement
                if not judgement.get('error'):
                    # Only a real verdict is worth reusing; a failed call is retried for the next list.
                    judged[rubric][keys] = judgement
        records.append(record)

    arm_calls = sum(1 for variant in variants if _arm_called_model(variant)) + sum(
        1 for variant in variants if (getattr(variant, 'repair', None) or {}).get('attempted'))
    # Built through ``CareerRun`` so the export keeps the collection's shape as that grows.
    replayed = variant_collection.CareerRun(
        requested_name=run.get('requested_name') or career,
        career_name=career,
        external_id=run.get('external_id', ''),
        skill_count=len(career_skills or []),
        workflow_uuid=f'{REPLAY_TRACE_PREFIX}:{career}',
        variants=records,
        candidates=candidates,
        career_description=context['career_description'],
        career_skills=list(career_skills or []),
    ).to_dict()
    return {
        **replayed,
        'replay': {
            'source_workflow_uuid': str(run.get('workflow_uuid') or ''),
            'strategies': list(strategies),
            'shapes': list(shapes),
            'rubrics': rubrics,
            'policy': policy is not None,
            'career_skills': list(career_skills or []),
            **context,
            'model_calls': {'variant_arms': arm_calls, 'judge': judge_calls, 'total': arm_calls + judge_calls},
        },
    }


def replay_call_bound(*, strategies, shapes, rubrics) -> int:
    """
    Upper bound on the paid calls ``replay_career`` can issue for one career.

    The app's own ``pathway_variants.estimated_model_calls`` with no sizes (a replay builds
    shapes), less the one judgement per rubric it charges for the delivered pathway, which a
    replay has none of.
    """
    rubrics = normalise_rubrics(rubrics)
    calls = pathway_variants.estimated_model_calls(
        sizes=[], strategies=list(strategies), judge_enabled=True, shapes=list(shapes), judge_rubrics=rubrics,
    )
    return calls - len(rubrics)


def append_replay(path, run: dict) -> None:
    """Append one replayed run to a JSON-lines checkpoint and flush it."""
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(run, sort_keys=True) + '\n')
        handle.flush()


def load_replays(path) -> list[dict]:
    """
    The replayed runs in a checkpoint, one per career (later lines win), errors left out.

    Kept as raw dicts rather than ``CareerRun``: a replay has no delivered pathway, which
    ``CareerRun.is_final`` would read as unfinished, and carries ``replay`` metadata.
    """
    runs: dict = {}
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            run = json.loads(line)
        except ValueError:
            logger.warning('Skipping unreadable replay line %d in %s.', number, path)
            continue
        if isinstance(run, dict) and not run.get('error'):
            runs[run.get('requested_name') or run.get('career_name') or ''] = run
    return list(runs.values())


# ---------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------

def select_for_rubric(run: dict, rubric: str) -> dict:
    """
    ``shape_review.select_shapes`` under a rubric.

    Raises:
        ReplayContractError: ``select_shapes`` takes no rubric yet and a non-v1 one was asked for.
    """
    optional = contract_kwargs(shape_review.select_shapes, {'rubric': rubric})
    return shape_review.select_shapes(run, **optional)


def _picks_by_career(replays, rubric) -> dict:
    """``{career (lower): {tier: pick or None}}`` for every replayed run."""
    picks = {}
    for run in replays:
        if run.get('error'):
            continue
        selection = select_for_rubric(run, rubric)
        tiers = {tier['tier']: tier['pick'] for tier in selection['tiers']}
        for name in (run.get('career_name'), run.get('requested_name')):
            if name:
                picks.setdefault(name.strip().lower(), tiers)
    return picks


def _pick_keys(picks, career, tier):
    """``(status, keys)``: ``not_replayed``, ``no_pick`` or ``picked`` with the pick's course keys."""
    tiers = picks.get(career.lower())
    if tiers is None:
        return 'not_replayed', set()
    pick = tiers.get(tier)
    if not pick:
        return 'no_pick', set()
    return 'picked', {course.get('key') for course in pick.get('courses') or []}


def _slot_aggregate(rows) -> dict:
    """Counts and rates over swap slots; rates are over the slots whose tier has a pick."""
    picked = [row for row in rows if row['status'] == 'picked']
    hit_best = sum(1 for row in picked if row['hit_best'])
    hit_any = sum(1 for row in picked if row['hit_any'])
    retained = sum(1 for row in picked if row['dropped_retained'])
    return {
        'slots': len(rows), 'with_pick': len(picked),
        'no_pick': sum(1 for row in rows if row['status'] == 'no_pick'),
        'not_replayed': sum(1 for row in rows if row['status'] == 'not_replayed'),
        'hit_best': hit_best, 'hit_any': hit_any, 'dropped_retained': retained,
        'hit_best_rate': _rate(hit_best, len(picked)), 'hit_any_rate': _rate(hit_any, len(picked)),
        'dropped_retained_rate': _rate(retained, len(picked)),
    }


def score_swaps(fixture: dict, replays, *, rubric: str = RUBRIC_V1) -> dict:
    """
    How a replay's picks answer the reviewer's edits.

    Per swap slot (one dropped step on one item), against the replayed pick for that item's
    career and tier: whether the pick holds the reviewer's best replacement (``hit_best``),
    any replacement he accepted (``hit_any``), and whether it still holds the dropped course.
    Rates are over slots whose tier produced a pick; ``no_pick`` and ``not_replayed`` are
    counted apart. Also: how many of the courses kept on items rated good the pick retains,
    and whether a course dropped as "nothing would work", or dropped with no pick, is still
    picked (scored votes only).
    """
    picks = _picks_by_career(replays, rubric)
    slots: dict = {}
    for pair in fixture.get('swap_pairs') or []:
        slots.setdefault((pair['item_id'], pair['step']), []).append(pair)

    rows = []
    for (item_id, step), pairs in sorted(slots.items()):
        best = next((pair for pair in pairs if pair['is_best']), pairs[0])
        acceptable = [pair['replacement_key'] for pair in pairs]
        status, keys = _pick_keys(picks, best['career'], best['tier'])
        rows.append({
            'item_id': item_id, 'step': step, 'career': best['career'], 'tier': best['tier'],
            'theme': best['theme'], 'dropped_key': best['dropped_key'],
            'best_key': best['replacement_key'] if best['is_best'] else None, 'acceptable_keys': acceptable,
            'status': status,
            'hit_best': best['is_best'] and best['replacement_key'] in keys,
            'hit_any': any(key in keys for key in acceptable),
            'dropped_retained': best['dropped_key'] in keys,
        })

    by_theme = {}
    for theme in THEME_PRIORITY:
        themed = [row for row in rows if row['theme'] == theme]
        if themed:
            by_theme[theme] = _slot_aggregate(themed)

    kept_rows = []
    for entry in fixture.get('kept_good') or []:
        status, keys = _pick_keys(picks, entry['career'], entry['tier'])
        kept_rows.append({**entry, 'status': status, 'retained_keys': [k for k in entry['kept_keys'] if k in keys]})
    kept_picked = [row for row in kept_rows if row['status'] == 'picked']
    kept_total = sum(len(row['kept_keys']) for row in kept_picked)
    kept_found = sum(len(row['retained_keys']) for row in kept_picked)

    none_rows = []
    for entry in fixture.get('nothing_would_work') or []:
        status, keys = _pick_keys(picks, entry['career'], entry['tier'])
        none_rows.append({**entry, 'status': status, 'dropped_retained': entry['dropped_key'] in keys})
    none_picked = [row for row in none_rows if row['status'] == 'picked']

    unpicked_rows = []
    for entry in fixture.get('dropped_without_pick') or []:
        if entry.get('scored'):
            status, keys = _pick_keys(picks, entry['career'], entry['tier'])
            unpicked_rows.append({**entry, 'status': status, 'dropped_retained': entry['dropped_key'] in keys})
    unpicked_picked = [row for row in unpicked_rows if row['status'] == 'picked']

    replayed = sorted({run.get('career_name') or run.get('requested_name', '') for run in replays})
    excluded = set(fixture.get('excluded_careers') or [])
    fixture_careers = sorted({item['career'] for item in (fixture.get('items') or {}).values()} - excluded)
    return {
        'rubric': rubric,
        'careers_replayed': replayed,
        'careers_missing': [career for career in fixture_careers if career.lower() not in picks],
        'overall': _slot_aggregate(rows),
        'by_theme': by_theme,
        'slots': rows,
        'kept_good': {
            'items': len(kept_rows), 'items_with_pick': len(kept_picked),
            'courses': kept_total, 'retained': kept_found, 'retention_rate': _rate(kept_found, kept_total),
            'items_fully_retained': sum(1 for row in kept_picked if len(row['retained_keys']) == len(row['kept_keys'])),
            'rows': kept_rows,
        },
        'nothing_would_work': {
            'slots': len(none_rows), 'with_pick': len(none_picked),
            'dropped_retained': sum(1 for row in none_picked if row['dropped_retained']),
            'dropped_retained_rate': _rate(sum(1 for row in none_picked if row['dropped_retained']), len(none_picked)),
            'rows': none_rows,
        },
        'dropped_without_pick': {
            'slots': len(unpicked_rows), 'with_pick': len(unpicked_picked),
            'dropped_retained': sum(1 for row in unpicked_picked if row['dropped_retained']),
            'dropped_retained_rate': _rate(sum(1 for row in unpicked_picked if row['dropped_retained']),
                                           len(unpicked_picked)),
            'rows': unpicked_rows,
        },
    }


def n_flagged(judgement: dict | None) -> int:
    """
    Courses the judge flagged, counted as ``shape_review`` counts them (``judging.is_flagged``).

    A v1 judgement flags nothing, so this is 0 and the tie-break it feeds is inert.
    """
    flags = (judgement or {}).get('flags') or {}
    if not isinstance(flags, dict):
        return 0
    return sum(1 for course_flags in flags.values() if judging.is_flagged(course_flags))


def judgement_rank(judgement: dict | None) -> tuple:
    """Higher is better: verdict, then share of courses on topic, then fewer flagged courses."""
    judgement = judgement or {}
    n_courses = int(judgement.get('n_courses') or 0)
    share = (int(judgement.get('n_on_topic') or 0) / n_courses) if n_courses else 0.0
    return JUDGE_VERDICT_SCORE.get(judgement.get('verdict'), 0), round(share, 6), -n_flagged(judgement)


def _swap_pathways(fixture, pair) -> tuple[tuple, tuple]:
    """The item's pathway as reviewed, and the same pathway with this one swap made."""
    original = tuple(fixture['items'][pair['item_id']]['course_keys'])
    swapped = tuple(pair['replacement_key'] if key == pair['dropped_key'] else key for key in original)
    return original, swapped


def judge_swap_call_bound(fixture: dict) -> int:
    """The most judgements ``judge_prefers_replacement`` can issue: one per distinct pathway."""
    pathways = set()
    for pair in fixture.get('swap_pairs') or []:
        for keys in _swap_pathways(fixture, pair):
            pathways.add((pair['career'], keys))
    return len(pathways)


def career_inputs_from_replays(replays) -> dict:
    """``{career: {career_skills, candidates, career_description, family_titles, family_size}}``."""
    inputs = {}
    for run in replays:
        meta = run.get('replay') or {}
        career = run.get('career_name') or run.get('requested_name') or ''
        inputs[career] = {
            'career_skills': list(run.get('career_skills') or meta.get('career_skills') or []),
            'candidates': list(run.get('candidates') or []),
            'career_description': meta.get('career_description') or run.get('career_description', ''),
            'family_titles': list(meta.get('family_titles') or []),
            'family_size': int(meta.get('family_size') or 0),
        }
    return inputs


def _judged_summary(judgement):
    return {key: judgement.get(key) for key in ('verdict', 'n_on_topic', 'n_courses', 'reason', 'error')} | {
        'n_flagged': n_flagged(judgement)}


def _preference(original, swapped) -> str:
    """``replacement``, ``original``, ``tie``, or ``error`` when either side has no verdict."""
    if original.get('error') or swapped.get('error') or not original.get('verdict') or not swapped.get('verdict'):
        return 'error'
    first, second = judgement_rank(swapped), judgement_rank(original)
    if first == second:
        return 'tie'
    return 'replacement' if first > second else 'original'


def judge_prefers_replacement(fixture: dict, *, careers: dict, rubric: str = RUBRIC_V1, judge_backend=None,
                              max_calls: int | None = None) -> dict:
    """
    Does the judge agree with the reviewer's swaps?

    For each swap pair, judges the item's pathway as reviewed and the same pathway with that
    one course swapped, under ``rubric``, and reports which ranks higher by
    ``judgement_rank``. Both sides are judged fresh, in the same conditions, rather than
    comparing against the stored verdict. A pathway shared by several pairs is judged once.

    Args:
        careers: ``career_inputs_from_replays`` output: skills, window and context per career.
        max_calls: Checked before each pair against the calls it still needs, so the budget
            is never overshot and a pair is never half-judged.

    Returns ``pairs`` (one row each), ``overall`` and ``by_theme`` counts of which side the
    judge preferred, and ``calls_issued``.
    """
    lowered = {name.lower(): value for name, value in (careers or {}).items()}
    cache: dict = {}
    calls, exhausted, rows = 0, False, []
    for pair in fixture.get('swap_pairs') or []:
        row = {key: pair[key] for key in ('item_id', 'step', 'career', 'tier', 'theme', 'dropped_key',
                                          'replacement_key', 'is_best')}
        inputs = lowered.get(pair['career'].lower())
        original, swapped = _swap_pathways(fixture, pair)
        if not inputs or not inputs.get('career_skills'):
            rows.append({**row, 'prefers': 'no_career_inputs'})
            continue
        if pair['replacement_key'] in original:
            rows.append({**row, 'prefers': 'replacement_already_in_pathway'})
            continue
        needed = [keys for keys in dict.fromkeys((original, swapped)) if (pair['career'], keys) not in cache]
        if max_calls is not None and calls + len(needed) > max_calls:
            exhausted = True
            rows.append({**row, 'prefers': 'not_judged_budget'})
            continue
        details = {c.get('key'): c for c in inputs.get('candidates') or [] if c.get('key')}
        fallback = {course['key']: {'key': course['key'], 'title': course['title'], 'level_type': course['level']}
                    for course in fixture['items'][pair['item_id']]['courses']}
        fallback[pair['replacement_key']] = {'key': pair['replacement_key'], 'title': pair['replacement_title'],
                                             'level_type': pair['replacement_level'] or pair['level']}
        context = {key: inputs.get(key) for key in ('career_description', 'family_titles', 'family_size')}
        for keys in needed:
            label = 'original' if keys == original else f'swap:{pair["step"]}:{pair["replacement_key"]}'
            result = judge_one(
                career_name=pair['career'], career_skills=inputs['career_skills'],
                courses=_course_details(keys, details, fallback),
                trace_id=f'{SWAP_JUDGE_TRACE_PREFIX}:{pair["item_id"]}:{rubric}:{label}',
                backend=judge_backend, rubric=rubric, context=context,
            )
            calls += 1
            cache[(pair['career'], keys)] = judgement_record(result, label=label, rubric=rubric)
        before, after = cache[(pair['career'], original)], cache[(pair['career'], swapped)]
        rows.append({**row, 'prefers': _preference(before, after),
                     'original': _judged_summary(before), 'swapped': _judged_summary(after)})

    def tally(selected):
        counts = {}
        for row in selected:
            counts[row['prefers']] = counts.get(row['prefers'], 0) + 1
        decided = sum(counts.get(key, 0) for key in ('replacement', 'original', 'tie'))
        counts['agreement_rate'] = _rate(counts.get('replacement', 0), decided)
        return counts

    return {
        'rubric': rubric,
        'calls_issued': calls,
        'call_bound': judge_swap_call_bound(fixture),
        'budget_exhausted': exhausted,
        'overall': tally(rows),
        'by_theme': {theme: tally([row for row in rows if row['theme'] == theme])
                     for theme in THEME_PRIORITY if any(row['theme'] == theme for row in rows)},
        'pairs': rows,
    }
