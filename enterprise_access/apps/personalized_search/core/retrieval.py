"""
Retrieval (tech spec "Retrieval", S-4): up to four Algolia sub-requests, a deterministic
prefilter to 20 candidates, and the follow-up attribute. Also hydrates ItemCards from hits.

Sub-requests go to the catalog in one round trip with a per-call read timeout, through the
``CatalogSearch`` port (``adapters/algolia.py`` in enterprise-access: ``multi_search_catalog_index``).
Queries and hits use Algolia's catalog-index vocabulary (filters, ``content_type``, ``aggregation_key``);
another catalog only has to accept and return that shape.
"""
import html
import logging
import math
import re
from dataclasses import dataclass

from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.errors import CatalogSearchError
from enterprise_access.apps.personalized_search.core.ports import CatalogSearch

logger = logging.getLogger(__name__)
EXEC_ED = 'Executive Education'
EXEC_ED_COURSE_TYPE = 'executive-education-2u'


@dataclass(frozen=True)
class CatalogScope:
    """
    How to scope catalog reads. Normal: the learner's secured key plus the enterprise filter.
    Dev only: the plain key plus an explicit catalog filter (``PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE``).
    ``secured_key`` is opaque here; only the catalog adapter reads it.

    The content language follows the learner's portal language, as Learner Pathways does: ``locale``
    picks the catalog records whose metadata (title, description) is in that language
    (``metadata_language``), and courses and programs are also limited to ones taught in it (``language``,
    the facet's display name, e.g. "Spanish").
    """

    filter: str
    secured_key: object | None = None
    allow_unscoped: bool = False
    label: str | None = None
    locale: str = 'en'
    language_name: str = 'English'

    @property
    def course_language_filter(self) -> str:
        return f'language:{_quote(self.language_name)} AND {self.metadata_language_filter}'

    @property
    def metadata_language_filter(self) -> str:
        return f'metadata_language:{_quote(self.locale)}'

    @property
    def program_language_filter(self) -> str:
        """Programs taught in the learner's language (a program lists every language it's offered in)."""
        return f'language:{_quote(self.language_name)} AND {self.metadata_language_filter}'


class RetrievalFailed(Exception):
    """Algolia couldn't be searched."""


def _quote(value: str) -> str:
    """An Algolia filter string literal: backslashes, then double quotes, escaped."""
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _strip_html(text: str | None) -> str:
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', text or ''))).strip()


def item_type_of(hit: dict) -> str:
    """course, executive_education or program, from content_type and learning_type."""
    if hit.get('content_type') == 'program':
        return 'program'
    if hit.get('learning_type') == EXEC_ED or hit.get('course_type') == EXEC_ED_COURSE_TYPE:
        return 'executive_education'
    return 'course'


def item_id_of(hit: dict) -> str | None:
    """Course key, or the program uuid from aggregation_key."""
    if hit.get('content_type') == 'program':
        aggregation_key = hit.get('aggregation_key') or ''
        return aggregation_key.split(':')[-1] or None
    return hit.get('key')


def _hours(value) -> int | None:
    """A whole number of hours, or None (bools, < 1, non-finite and non-numbers are dropped), like ``intent._weeks``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 1:
        return None
    return int(value)


def hydrate_card(hit: dict) -> dict:
    """ItemCard minus ``why``/``fit`` (spec "ItemCard" table)."""
    item_type = item_type_of(hit)
    item_id = item_id_of(hit)
    run = hit.get('advertised_course_run') or {}
    if item_type == 'program':
        org = (hit.get('authoring_organizations') or [{}])[0] or {}
        partner_name, partner_logo = org.get('key'), org.get('logo_image_url')
        image = hit.get('card_image_url') or hit.get('banner_image_url')
        link = {'kind': 'program', 'program_uuid': item_id}
        facts = {'level_type': hit.get('level_type'), 'program_type': hit.get('type'),
                 'course_count': len(hit.get('course_keys') or []) or None}
    else:
        partner = (hit.get('partners') or [{}])[0] or {}
        partner_name, partner_logo = partner.get('name'), partner.get('logo_image_url')
        image = hit.get('card_image_url') or hit.get('original_image_url')
        link = {'kind': 'course', 'course_key': item_id, 'course_type': hit.get('course_type')}
        facts = {'level_type': hit.get('level_type'), 'weeks_to_complete': run.get('weeks_to_complete'),
                 'pacing_type': run.get('pacing_type'),
                 # Hours per week, from the advertised run's effort range (optional; older clients ignore it).
                 'hours_per_week_min': _hours(run.get('min_effort')),
                 'hours_per_week_max': _hours(run.get('max_effort'))}
    return {
        'item_id': item_id, 'item_type': item_type, 'title': hit.get('title') or '',
        'image_url': image, 'partner_name': partner_name, 'partner_logo_url': partner_logo,
        'link': link, 'facts': facts,
    }


def compose_view(candidate: dict) -> dict:
    """The candidate as the compose prompt sees it (spec "LLM step contracts")."""
    hit, card = candidate['hit'], candidate['card']
    description = hit.get('subtitle') if card['item_type'] == 'program' else hit.get('short_description')
    view = {
        'item_id': card['item_id'], 'item_type': card['item_type'], 'title': card['title'],
        'partner': card['partner_name'], 'level': hit.get('level_type'),
        'pacing': card['facts'].get('pacing_type'), 'weeks': card['facts'].get('weeks_to_complete'),
        'hours_per_week': [card['facts'].get('hours_per_week_min'), card['facts'].get('hours_per_week_max')],
        'short_description': _strip_html(description)[:300], 'skills': (hit.get('skill_names') or [])[:6],
    }
    if card['item_type'] == 'program':
        # The credential ("Professional Certificate", "MicroMasters"): without it the model can't say a program
        # earns a certificate, and hedges when a learner asks for one.
        view['program_type'] = card['facts'].get('program_type')
    return view


def preferred_types(intent: dict) -> set[str]:
    """The item types the learner asked for, when narrower than all of them; empty means no preference."""
    types = set(intent.get('item_types') or ()) & set(constants.ITEM_TYPES)
    return types if types and types != set(constants.ITEM_TYPES) else set()


def programs_wanted(intent: dict) -> bool:
    """
    Related programs are always offered unless the learner opted out: they asked for courses (courses in
    item_types without programs) or said they don't want a predefined program, which the intent step
    records the same way.
    """
    types = set(intent.get('item_types') or constants.ITEM_TYPES)
    return 'program' in types or 'course' not in types


def _exec_ed_clauses() -> tuple[str, str]:
    """
    Filter clauses for Executive Education and for regular courses, matching ``item_type_of``: either
    field marks a course as Executive Education. Algolia can't negate a group, so the second is spelled out.
    """
    learning_type, course_type = f'learning_type:{_quote(EXEC_ED)}', f'course_type:{_quote(EXEC_ED_COURSE_TYPE)}'
    return f'({learning_type} OR {course_type})', f'NOT {learning_type} AND NOT {course_type}'


def _course_filter(scope: CatalogScope, types: set[str]) -> str:
    """Courses in the learner's language and scope, narrowed to regular or Executive Education when asked."""
    course_filter = f'content_type:course AND {scope.course_language_filter} AND ({scope.filter})'
    exec_ed, not_exec_ed = _exec_ed_clauses()
    if types & {'course', 'executive_education'} == {'executive_education'}:
        course_filter += f' AND {exec_ed}'
    elif 'executive_education' not in types:
        course_filter += f' AND {not_exec_ed}'
    return course_filter


def _hit_requests(intent: dict, scope: CatalogScope, excluded_ids: list[str], types: set[str],
                  shortlist: int = constants.MAX_CANDIDATES) -> list[dict]:
    """
    Strict (skills) and broadened requests that return hits, for the given types. A shortlist larger than
    the default asks for proportionally more hits, so the prefilter has enough to choose from.
    """
    scale = max(1.0, shortlist / constants.MAX_CANDIDATES)
    course_hits, program_hits = math.ceil(30 * scale), math.ceil(15 * scale)
    skills = intent.get('skills') or []
    skills_clause = ' OR '.join(f'skill_names:{_quote(s)}' for s in skills)
    broad_query = ' '.join(skills + (intent.get('roles') or [])) or intent.get('query') or ''
    requests = []
    if types & {'course', 'executive_education'}:
        course_filter = _course_filter(scope, types) + ''.join(f' AND NOT key:{_quote(i)}' for i in excluded_ids)
        if skills_clause:
            requests.append({'tier': 2, 'query': intent.get('query') or '',
                             'filters': f'{course_filter} AND ({skills_clause})', 'hitsPerPage': course_hits})
        requests.append({'tier': 1, 'query': broad_query, 'filters': course_filter, 'hitsPerPage': course_hits,
                         'removeWordsIfNoResults': 'allOptional'})
    if 'program' in types:
        program_filter = f'content_type:program AND {scope.program_language_filter} AND ({scope.filter})'
        program_filter += ''.join(f' AND NOT aggregation_key:{_quote("program:" + i)}' for i in excluded_ids)
        if skills_clause:
            requests.append({'tier': 2, 'query': intent.get('query') or '',
                             'filters': f'{program_filter} AND ({skills_clause})', 'hitsPerPage': program_hits})
        requests.append({'tier': 1, 'query': broad_query, 'filters': program_filter, 'hitsPerPage': program_hits,
                         'removeWordsIfNoResults': 'allOptional'})
    return requests


def _count_requests(intent: dict, scope: CatalogScope) -> list[dict]:
    """
    Count-only requests (no hits) for "See all (n)", per type, in the same round trip. The cards come
    from either the strict skills request or the broadened keyword one, so each type is counted both
    ways and the larger count wins (``search``): a keyword-only count could show "See all (1)" above
    three cards found by skill.
    """
    skills = intent.get('skills') or []
    skills_clause = ' OR '.join(f'skill_names:{_quote(s)}' for s in skills)
    broad_query = ' '.join(skills + (intent.get('roles') or [])) or intent.get('query') or ''
    exec_ed, not_exec_ed = _exec_ed_clauses()
    courses = f'content_type:course AND {scope.course_language_filter} AND ({scope.filter})'
    bases = {
        'course': f'{courses} AND {not_exec_ed}',
        'executive_education': f'{courses} AND {exec_ed}',
        'program': f'content_type:program AND {scope.program_language_filter} AND ({scope.filter})',
    }
    requests = []
    for item_type in constants.ITEM_TYPES:
        requests.append({'tier': 0, 'count_for': item_type, 'query': broad_query, 'filters': bases[item_type],
                         'hitsPerPage': 0, 'removeWordsIfNoResults': 'allOptional'})
        if skills_clause:
            requests.append({'tier': 0, 'count_for': item_type, 'query': intent.get('query') or '',
                             'filters': f'{bases[item_type]} AND ({skills_clause})', 'hitsPerPage': 0})
    return requests


def build_sub_requests(intent: dict, scope: CatalogScope, excluded_ids: list[str],
                       shortlist: int = constants.MAX_CANDIDATES) -> list[dict]:
    """
    Strict (skills) and broadened requests across every item type, plus counts per type. A type the
    learner asked for is a preference, not a filter: its own requests are added so it's well covered,
    while the other types stay in the results as alternatives ("More options").
    """
    every_type = set(constants.ITEM_TYPES) if programs_wanted(intent) else set(constants.ITEM_TYPES) - {'program'}
    requests = _hit_requests(intent, scope, excluded_ids, every_type, shortlist)
    preferred = preferred_types(intent)
    if preferred:
        # Only requests the general set doesn't already make: a preferred program, or courses with Executive
        # Education, would repeat it exactly.
        seen = {(req['query'], req['filters']) for req in requests}
        requests += [dict(req, preferred=True)
                     for req in _hit_requests(intent, scope, excluded_ids, preferred, shortlist)
                     if (req['query'], req['filters']) not in seen]
    return requests + _count_requests(intent, scope)


class SearchResults(list):
    """Candidates, plus the catalog's total matches per item type (for "See all (n)")."""

    def __init__(self, candidates=(), totals: dict | None = None):
        super().__init__(candidates)
        self.totals = totals or {}


def search(intent: dict, scope: CatalogScope, excluded_ids: list[str], *, catalog: CatalogSearch,
           timeout_s: float = 2.0, shortlist: int = constants.MAX_CANDIDATES) -> SearchResults:
    """
    Run the sub-requests in one catalog round trip; return candidates ``{hit, card, tier, rank}`` with the
    catalog totals per type. Raises ``RetrievalFailed`` when the catalog port reports a failure.
    """
    requests = build_sub_requests(intent, scope, excluded_ids, shortlist)
    queries = [{k: v for k, v in req.items() if k not in ('tier', 'count_for', 'preferred')} for req in requests]
    for query in queries:
        query.update(distinct=True, attributesToRetrieve=constants.ATTRIBUTES_TO_RETRIEVE)
    try:
        results = catalog.search(queries, scope=scope, timeout_s=timeout_s)
    except CatalogSearchError as exc:  # anything else is a bug, logged with its traceback by the orchestrator
        logger.warning('Personalized Search catalog search failed: %s', exc)
        raise RetrievalFailed(f'Catalog search failed ({type(exc).__name__}).') from exc
    out, totals = [], {}
    for req, result in zip(requests, results):
        if req.get('count_for'):
            if isinstance(result.get('nbHits'), int):
                totals[req['count_for']] = max(totals.get(req['count_for'], 0), result['nbHits'])
            continue
        for rank, hit in enumerate(result.get('hits') or []):
            if isinstance(hit, dict) and item_id_of(hit):
                out.append({'hit': hit, 'card': hydrate_card(hit), 'tier': req['tier'], 'rank': rank})
    return SearchResults(out, totals)


def prefilter(raw: list[dict], intent: dict, shortlist: int = constants.MAX_CANDIDATES) -> list[dict]:
    """
    Spec "Prefilter (deterministic)": dedupe, score, keep the top ``shortlist`` (20 by default) with type
    caps that scale with it.
    """
    scale = shortlist / constants.MAX_CANDIDATES
    best: dict[str, dict] = {}
    preferred = preferred_types(intent)
    level = intent.get('level')
    constraints = intent.get('constraints') or {}
    for cand in raw:
        card = cand['card']
        tier = 2 if card['item_type'] == 'executive_education' else cand['tier']
        score = tier - cand['rank'] / 100
        if level and (cand['hit'].get('level_type') or '').lower() == level:
            score += 1
        weeks = card['facts'].get('weeks_to_complete')
        if constraints.get('max_weeks') and weeks and weeks <= constraints['max_weeks']:
            score += 1
        if constraints.get('pacing') and card['facts'].get('pacing_type') == constraints['pacing']:
            score += 1
        min_hours = card['facts'].get('hours_per_week_min')
        if constraints.get('max_hours_per_week') and min_hours and min_hours <= constraints['max_hours_per_week']:
            score += 1
        if card['item_type'] in preferred:
            score += 2  # the learner's preferred type leads; the others stay as alternatives
        elif not preferred and card['item_type'] == 'program':
            score += constants.PROGRAM_LEAD_BOOST  # with no type asked for, related programs lead
        if card['item_id'] not in best or score > best[card['item_id']]['score']:
            best[card['item_id']] = dict(cand, score=score)
    counts = {t: 0 for t in constants.ITEM_TYPES}
    caps = {t: math.ceil((max(cap, constants.PREFERRED_TYPE_CAP) if t in preferred else cap) * scale)
            for t, cap in constants.CANDIDATE_CAPS.items()}
    ranked = sorted(best.values(), key=lambda c: -c['score'])
    taken: set[str] = set()
    # A preferred type's larger cap could let it and courses fill every place: first keep enough of each other
    # type for its "More options" section, then fill the rest by score within the caps.
    floors = {t: min(constants.MAX_ITEMS_PER_SECTION, caps[t]) for t in constants.ITEM_TYPES
              if preferred and t not in preferred}
    for limits in (floors, caps):
        for cand in ranked:
            item_type, item_id = cand['card']['item_type'], cand['card']['item_id']
            if len(taken) == shortlist:
                break
            if item_id not in taken and counts[item_type] < limits.get(item_type, 0):
                counts[item_type] += 1
                taken.add(item_id)
    return [cand for cand in ranked if cand['card']['item_id'] in taken]


def _weeks_bucket(weeks):
    if not weeks:
        return None
    return '<=4' if weeks <= 4 else ('5-12' if weeks <= 12 else '>12')


def attribute_value(attribute: str, candidate: dict):
    """A candidate's value for a follow-up attribute (level, time bucket, pacing or type), or None."""
    if attribute == 'level':
        return candidate['hit'].get('level_type') or None
    if attribute == 'time':
        return _weeks_bucket(candidate['card']['facts'].get('weeks_to_complete'))
    if attribute == 'pacing':
        return candidate['card']['facts'].get('pacing_type')
    if attribute == 'type':
        return candidate['card']['item_type']
    return None


def choose_followup_attribute(candidates: list[dict], intent: dict, asked: list[str]) -> str:
    """Spec "Follow-up attribute (R-15)": the most even split, ties in level, time, pacing, type order."""
    constraints = intent.get('constraints') or {}
    already = {
        'level': bool(intent.get('level')),
        'time': bool(constraints.get('max_weeks') or constraints.get('max_hours_per_week')),
        'pacing': bool(constraints.get('pacing')),
        'type': len(intent.get('item_types') or constants.ITEM_TYPES) < len(constants.ITEM_TYPES),
    }
    best, best_share = 'open', None
    for attr in ('level', 'time', 'pacing', 'type'):
        if attr in asked or already[attr]:
            continue
        buckets: dict[str, int] = {}
        for cand in candidates:
            value = attribute_value(attr, cand)
            if value:
                buckets[value] = buckets.get(value, 0) + 1
        if len(buckets) < 2 or min(buckets.values()) < 2:
            continue
        share = max(buckets.values()) / sum(buckets.values())
        if best_share is None or share < best_share:
            best, best_share = attr, share
    return best
