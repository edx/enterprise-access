"""Shared fixtures for Personalized Search tests: Algolia hits and candidates."""
from enterprise_access.apps.personalized_search.core.retrieval import hydrate_card


def course_hit(key, *, level='Introductory', weeks=4, pacing='self_paced', exec_ed=False, skills=('Leadership',)):
    return {
        'key': key, 'aggregation_key': f'course:{key}', 'content_type': 'course',
        'learning_type': 'Executive Education' if exec_ed else 'Course',
        'course_type': 'executive-education-2u' if exec_ed else 'verified-audit',
        'title': f'Course {key}', 'short_description': '<p>Learn <b>things</b></p>', 'level_type': level,
        'partners': [{'name': 'edX', 'logo_image_url': 'https://logo/edx.png'}],
        'card_image_url': None, 'original_image_url': f'https://img/{key}.png', 'skill_names': list(skills),
        'advertised_course_run': {'weeks_to_complete': weeks, 'pacing_type': pacing},
    }


def program_hit(uuid, *, level='Intermediate'):
    return {
        'aggregation_key': f'program:{uuid}', 'content_type': 'program', 'title': f'Program {uuid}',
        'subtitle': 'A program', 'level_type': level, 'type': 'Professional Certificate',
        'authoring_organizations': [{'key': 'MITx', 'logo_image_url': 'https://logo/mit.png'}],
        'card_image_url': f'https://img/{uuid}.png', 'course_keys': ['a', 'b', 'c'], 'skill_names': ['Leadership'],
    }


def candidate(hit, tier=2, rank=0):
    return {'hit': hit, 'card': hydrate_card(hit), 'tier': tier, 'rank': rank}


def default_candidates():
    return [
        candidate(course_hit('edX+C1')), candidate(course_hit('edX+C2', level='Intermediate', weeks=8)),
        candidate(course_hit('edX+C3', pacing='instructor_paced')), candidate(course_hit('edX+C4', weeks=12)),
        candidate(course_hit('Yale+E1', exec_ed=True)), candidate(course_hit('Yale+E2', exec_ed=True)),
        candidate(program_hit('p-1')), candidate(program_hit('p-2')),
    ]
