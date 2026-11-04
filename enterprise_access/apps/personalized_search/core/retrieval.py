"""
Retrieval (tech spec "Retrieval", S-4): up to four Algolia sub-requests, a deterministic
prefilter to 20 candidates, and the follow-up attribute. Also hydrates ItemCards from hits.

Sub-requests go to the catalog in one round trip with a per-call read timeout, through the
``CatalogSearch`` port (``adapters/algolia.py`` in enterprise-access: ``multi_search_catalog_index``).
Queries and hits use Algolia's catalog-index vocabulary (filters, ``content_type``, ``aggregation_key``);
another catalog only has to accept and return that shape.
"""
import html
import re
from dataclasses import dataclass

from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.ports import CatalogSearch

EXEC_ED = 'Executive Education'


@dataclass(frozen=True)
class CatalogScope:
    """
    How to scope catalog reads. Normal: the learner's secured key plus the enterprise filter.
    Dev only: the plain key plus an explicit catalog filter (``PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE``).
    ``secured_key`` is opaque here; only the catalog adapter reads it.
    """

    filter: str
    secured_key: object | None = None
    allow_unscoped: bool = False
    label: str | None = None


class RetrievalFailed(Exception):
    """Algolia couldn't be searched."""


def _quote(value: str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def _strip_html(text: str | None) -> str:
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', text or ''))).strip()


def item_type_of(hit: dict) -> str:
    """course, executive_education or program, from content_type and learning_type."""
    if hit.get('content_type') == 'program':
        return 'program'
    if hit.get('learning_type') == EXEC_ED or hit.get('course_type') == 'executive-education-2u':
        return 'executive_education'
    return 'course'


def item_id_of(hit: dict) -> str | None:
    """Course key, or the program uuid from aggregation_key."""
    if hit.get('content_type') == 'program':
        aggregation_key = hit.get('aggregation_key') or ''
        return aggregation_key.split(':')[-1] or None
    return hit.get('key')


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
                 'pacing_type': run.get('pacing_type')}
    return {
        'item_id': item_id, 'item_type': item_type, 'title': hit.get('title') or '',
        'image_url': image, 'partner_name': partner_name, 'partner_logo_url': partner_logo,
        'link': link, 'facts': facts,
    }


def compose_view(candidate: dict) -> dict:
    """The candidate as the compose prompt sees it (spec "LLM step contracts")."""
    hit, card = candidate['hit'], candidate['card']
    description = hit.get('subtitle') if card['item_type'] == 'program' else hit.get('short_description')
    return {
        'item_id': card['item_id'], 'item_type': card['item_type'], 'title': card['title'],
        'partner': card['partner_name'], 'level': hit.get('level_type'),
        'pacing': card['facts'].get('pacing_type'), 'weeks': card['facts'].get('weeks_to_complete'),
        'short_description': _strip_html(description)[:300], 'skills': (hit.get('skill_names') or [])[:6],
    }


def build_sub_requests(intent: dict, scope: CatalogScope, excluded_ids: list[str]) -> list[dict]:
    """Strict (skills) and broadened sub-requests for courses and programs, per the item types asked for."""
    types = set(intent.get('item_types') or constants.ITEM_TYPES)
    skills = intent.get('skills') or []
    skills_clause = ' OR '.join(f'skill_names:{_quote(s)}' for s in skills)
    broad_query = ' '.join(skills + (intent.get('roles') or [])) or intent.get('query') or ''
    requests = []
    if types & {'course', 'executive_education'}:
        course_filter = f'content_type:course AND language:"English" AND ({scope.filter})'
        if types & {'course', 'executive_education'} == {'executive_education'}:
            course_filter += f' AND learning_type:{_quote(EXEC_ED)}'
        elif 'executive_education' not in types:
            course_filter += f' AND NOT learning_type:{_quote(EXEC_ED)}'
        course_filter += ''.join(f' AND NOT key:{_quote(i)}' for i in excluded_ids)
        if skills_clause:
            requests.append({'tier': 2, 'query': intent.get('query') or '',
                             'filters': f'{course_filter} AND ({skills_clause})', 'hitsPerPage': 30})
        requests.append({'tier': 1, 'query': broad_query, 'filters': course_filter, 'hitsPerPage': 30,
                         'removeWordsIfNoResults': 'allOptional'})
    if 'program' in types:
        program_filter = f'content_type:program AND ({scope.filter})'
        program_filter += ''.join(f' AND NOT aggregation_key:{_quote("program:" + i)}' for i in excluded_ids)
        if skills_clause:
            requests.append({'tier': 2, 'query': intent.get('query') or '',
                             'filters': f'{program_filter} AND ({skills_clause})', 'hitsPerPage': 15})
        requests.append({'tier': 1, 'query': broad_query, 'filters': program_filter, 'hitsPerPage': 15,
                         'removeWordsIfNoResults': 'allOptional'})
    return requests


def search(intent: dict, scope: CatalogScope, excluded_ids: list[str], *, catalog: CatalogSearch,
           timeout_s: float = 2.0) -> list[dict]:
    """Run the sub-requests in one catalog round trip; return candidates ``{hit, card, tier, rank}``."""
    requests = build_sub_requests(intent, scope, excluded_ids)
    queries = [{k: v for k, v in req.items() if k != 'tier'} for req in requests]
    for query in queries:
        query.update(distinct=True, attributesToRetrieve=constants.ATTRIBUTES_TO_RETRIEVE)
    try:
        results = catalog.search(queries, scope=scope, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001  # AlgoliaSearchError / configuration errors
        raise RetrievalFailed(f'Catalog search failed ({type(exc).__name__}).') from exc
    out = []
    for req, result in zip(requests, results):
        for rank, hit in enumerate(result.get('hits') or []):
            if isinstance(hit, dict) and item_id_of(hit):
                out.append({'hit': hit, 'card': hydrate_card(hit), 'tier': req['tier'], 'rank': rank})
    return out


def prefilter(raw: list[dict], intent: dict) -> list[dict]:
    """Spec "Prefilter (deterministic)": dedupe, score, keep the top 20 with type caps."""
    best: dict[str, dict] = {}
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
        if card['item_id'] not in best or score > best[card['item_id']]['score']:
            best[card['item_id']] = dict(cand, score=score)
    counts = {t: 0 for t in constants.ITEM_TYPES}
    kept = []
    for cand in sorted(best.values(), key=lambda c: -c['score']):
        item_type = cand['card']['item_type']
        if counts[item_type] < constants.CANDIDATE_CAPS[item_type]:
            counts[item_type] += 1
            kept.append(cand)
        if len(kept) == constants.MAX_CANDIDATES:
            break
    return kept


def _weeks_bucket(weeks):
    if not weeks:
        return None
    return '<=4' if weeks <= 4 else ('5-12' if weeks <= 12 else '>12')


def choose_followup_attribute(candidates: list[dict], intent: dict, asked: list[str]) -> str:
    """Spec "Follow-up attribute (R-15)": the most even split, ties in level, time, pacing, type order."""
    constraints = intent.get('constraints') or {}
    already = {
        'level': bool(intent.get('level')), 'time': bool(constraints.get('max_weeks')),
        'pacing': bool(constraints.get('pacing')),
        'type': len(intent.get('item_types') or constants.ITEM_TYPES) < len(constants.ITEM_TYPES),
    }
    values = {
        'level': lambda c: (c['hit'].get('level_type') or None),
        'time': lambda c: _weeks_bucket(c['card']['facts'].get('weeks_to_complete')),
        'pacing': lambda c: c['card']['facts'].get('pacing_type'),
        'type': lambda c: c['card']['item_type'],
    }
    best, best_share = 'open', None
    for attr in ('level', 'time', 'pacing', 'type'):
        if attr in asked or already[attr]:
            continue
        buckets: dict[str, int] = {}
        for cand in candidates:
            value = values[attr](cand)
            if value:
                buckets[value] = buckets.get(value, 0) + 1
        if len(buckets) < 2 or min(buckets.values()) < 2:
            continue
        share = max(buckets.values()) / sum(buckets.values())
        if best_share is None or share < best_share:
            best, best_share = attr, share
    return best
