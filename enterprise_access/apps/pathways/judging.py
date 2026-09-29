"""
Domain-layer API for judging a pathway with a model: the analysis rubric, in the product.

The September 2026 analysis judged every generated pathway with a fixed rubric and a fixed
model, and two checks are what made its verdicts worth quoting: human-curated edX programs
scored 84% of courses on topic under the same rubric (so the judge recognises quality), and
its verdicts separated by held-out career skills that no retrieval step had seen (good 31%,
bad 7%). This module runs that instrument inside the pipeline, so every run can be scored
the same way without a separate offline pass.

It records; it never decides
----------------------------
A verdict is written to the run and returned when asked for. Nothing here changes which
courses are delivered, and that is deliberate: a judge that also chose the pathway would be
grading its own choice, and its scores would stop measuring anything. Selection belongs to
retrieval, re-ranking and assembly; this is the ruler held up to their output.

What is and is not identical to the analysis
--------------------------------------------
* The rubric text and output schema are verbatim (``prompts.PATHWAY_JUDGE_*``), and the
  model and temperature default to the analysis's (``PATHWAYS_JUDGE_MODEL``, 0).
* The user message has the analysis's layout, with one unavoidable difference: the analysis
  judged a *career family* and listed its job titles, and this pipeline has one career, so
  the career name stands in for the title list.
* The analysis enforced the schema with a strict ``json_schema`` response format; the
  direct backends here enforce JSON and carry the schema in the system prompt, as every
  other prompt in this app does.

So treat the verdicts as the same instrument in kind, and check agreement on a sample
before pooling them with the analysis's figures.

Model output is untrusted, as everywhere else in this app: a verdict outside the rubric's
three words is an error, and course keys the judge returns that were not in the pathway are
dropped and counted rather than trusted.

A second rubric, opt-in
-----------------------
``rubric='v2'`` runs ``prompts.PATHWAY_JUDGE_SYSTEM_PROMPT_V2``, written after a reviewer's
ratings showed v1 -- topical fit alone -- calling pathways good that he did not. It adds a
flag per course for the reasons he gave (too specific, redundant, wrong level, wrong role)
and shows the judge what v1 never saw: the career's description, its family's job titles
and each course's level in context. It is a separate instrument with no calibration behind
it yet, so a v2 verdict is compared with v2 verdicts only. v1 stays the default, and its
system prompt and user content are exactly what they were.
"""
import logging

from django.conf import settings

from enterprise_access.apps.pathways.model_backends import (
    ModelBackendConfigurationError,
    ModelBackendError,
    get_direct_backend
)
from enterprise_access.apps.pathways.prompts import (
    PATHWAY_JUDGE_OUTPUT_SCHEMA,
    PATHWAY_JUDGE_OUTPUT_SCHEMA_V2,
    PATHWAY_JUDGE_SYSTEM_PROMPT,
    PATHWAY_JUDGE_SYSTEM_PROMPT_V2,
    PATHWAY_JUDGE_V2_BOOLEAN_FLAGS,
    PATHWAY_JUDGE_VERDICTS
)
from enterprise_access.apps.prompts.api import compose_system_prompt

logger = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = compose_system_prompt(PATHWAY_JUDGE_SYSTEM_PROMPT, PATHWAY_JUDGE_OUTPUT_SCHEMA)
JUDGE_SYSTEM_PROMPT_V2 = compose_system_prompt(PATHWAY_JUDGE_SYSTEM_PROMPT_V2, PATHWAY_JUDGE_OUTPUT_SCHEMA_V2)

# The rubrics a run can be judged under. v1 is the calibrated default; see the module
# docstring for v2.
RUBRIC_V1 = 'v1'
RUBRIC_V2 = 'v2'
JUDGE_RUBRICS = (RUBRIC_V1, RUBRIC_V2)

# The analysis ran the judge at temperature 0. Pinned rather than configurable: a judge
# whose answers vary between runs of the same pathway makes every comparison noisier, and
# the analysis already measured 8.6% self-disagreement at 0.
JUDGE_TEMPERATURE = 0

# The analysis showed the judge the four skills retrieval filtered on, each course's first
# eight skill tags, and 280 characters of its description. Kept identical so the judge sees
# a pathway the way it did when it was calibrated.
CAREER_SKILLS_SHOWN = 4
COURSE_SKILLS_SHOWN = 8
DESCRIPTION_CHARS_SHOWN = 280

# What v2 shows beyond v1: more of the career's skills, its description, and the family's
# job titles. Course rendering (level, 280 characters, eight skills) is v1's.
V2_CAREER_SKILLS_SHOWN = 8
V2_CAREER_DESCRIPTION_CHARS = 600
V2_FAMILY_TITLES_SHOWN = 15

# The per-course flags v2 records, in the order they are exported.
V2_FLAG_ORDER = ('too_specific', 'redundant_with', 'level_mismatch', 'role_misfit')


def normalise_rubrics(rubrics) -> list[str]:
    """
    De-duplicate rubric names in canonical order; none at all means v1 alone.

    Raises:
        ValueError: A name not in ``JUDGE_RUBRICS``.
    """
    requested = set(rubrics or [])
    unknown = sorted(str(rubric) for rubric in requested - set(JUDGE_RUBRICS))
    if unknown:
        raise ValueError(f'Unknown judge rubrics {unknown}; expected {list(JUDGE_RUBRICS)}.')
    return [rubric for rubric in JUDGE_RUBRICS if rubric in requested] or [RUBRIC_V1]


def get_judge_backend():
    """
    The backend the judge runs on: ``PATHWAYS_JUDGE_BACKEND`` / ``PATHWAYS_JUDGE_MODEL``.

    Deliberately independent of ``PATHWAYS_MODEL_BACKEND``. The judge is a measuring
    instrument; if it followed whichever model builds pathways, a model change would move
    the builder and the ruler at once.
    """
    return get_direct_backend(
        backend_name=settings.PATHWAYS_JUDGE_BACKEND,
        model=settings.PATHWAYS_JUDGE_MODEL,
        temperature=JUDGE_TEMPERATURE,
    )


def build_user_content(*, career_name: str, career_skills: list[str], courses: list[dict]) -> str:
    """
    Render one pathway the way the analysis rendered it.

    ``courses`` are dicts carrying ``key``, ``title``, ``level_type``, a description
    (``short_description`` or ``full_description``) and ``skill_names``.
    """
    skills = ', '.join(list(career_skills or [])[:CAREER_SKILLS_SHOWN]) or '(none resolved)'
    lines = [
        f'CAREER FAMILY: {career_name}',
        f'job titles in this family: {career_name}',
        f'skills this career needs (Lightcast): {skills}',
        '',
        f'RECOMMENDED PATHWAY ({len(courses)} courses):',
    ]
    return '\n'.join(lines + _course_lines(courses))


def build_user_content_v2(*, career_name: str, career_skills: list[str], courses: list[dict],
                          career_description: str = '', family_titles=None, family_size: int = 0) -> str:
    """
    Render one pathway for the v2 rubric: v1's layout, plus the family and the work.

    The family's job titles replace v1's stand-in (the career name alone) when they are
    known, with the family's size when more exist than are shown; the career's description
    and its first ``V2_CAREER_SKILLS_SHOWN`` skills follow. Courses render exactly as in v1.
    """
    titles = list(dict.fromkeys(
        title.strip() for title in (family_titles or []) if isinstance(title, str) and title.strip()
    ))
    shown = titles[:V2_FAMILY_TITLES_SHOWN] or [career_name]
    total = max(int(family_size or 0), len(titles), len(shown))
    title_label = (
        f'job titles in this family ({len(shown)} of {total} shown)' if total > len(shown)
        else 'job titles in this family'
    )
    skills = ', '.join(list(career_skills or [])[:V2_CAREER_SKILLS_SHOWN]) or '(none resolved)'
    lines = [
        f'CAREER FAMILY: {career_name}',
        f'family size: {total} job title{"s" if total != 1 else ""}',
        f"{title_label}: {'; '.join(shown)}",
    ]
    description = ' '.join((career_description or '').split())[:V2_CAREER_DESCRIPTION_CHARS]
    if description:
        lines.append(f'what this work involves: {description}')
    lines += [
        f'skills this career needs (Lightcast): {skills}',
        '',
        f'RECOMMENDED PATHWAY ({len(courses)} courses, in the order they would be taken):',
    ]
    return '\n'.join(lines + _course_lines(courses))


def _course_lines(courses: list[dict]) -> list[str]:
    """One pathway's courses as the analysis rendered them, shared by both rubrics."""
    lines = []
    for position, course in enumerate(courses, 1):
        level = course.get('level_type') or 'level unknown'
        lines.append(f"{position}. [{course.get('key', '')}] {course.get('title', '')} ({level})")
        blurb = (course.get('short_description') or course.get('full_description') or '').strip()
        if blurb:
            lines.append(f'   about: {blurb[:DESCRIPTION_CHARS_SHOWN]}')
        course_skills = list(course.get('skill_names') or [])[:COURSE_SKILLS_SHOWN]
        if course_skills:
            lines.append(f"   course skills: {', '.join(course_skills)}")
    return lines


def parse_judgement(payload, supplied_keys) -> dict:
    """
    Validate a judge response against the pathway it was shown.

    Returns ``verdict``, ``reason``, ``on_topic`` (key to bool, supplied keys only),
    ``fabricated_keys``, ``unjudged_keys`` and ``error``. Never raises: an unusable
    response is recorded as an error with no verdict, because a missing verdict is a gap
    in the data and an invented one is a lie in it.
    """
    result = {
        'verdict': '', 'reason': '', 'on_topic': {},
        'fabricated_keys': [], 'unjudged_keys': list(supplied_keys), 'error': '',
    }
    if not isinstance(payload, dict):
        result['error'] = 'judge response was not a JSON object'
        return result

    verdict = payload.get('verdict')
    if verdict not in PATHWAY_JUDGE_VERDICTS:
        result['error'] = f'judge returned verdict {verdict!r}, expected one of {PATHWAY_JUDGE_VERDICTS}'
        return result
    result['verdict'] = verdict
    reason = payload.get('reason')
    result['reason'] = reason.strip() if isinstance(reason, str) else ''

    supplied = list(supplied_keys)
    allowed = set(supplied)
    on_topic: dict = {}
    fabricated: list = []
    for entry in payload.get('courses') or []:
        if not isinstance(entry, dict):
            continue
        key, flag = entry.get('key'), entry.get('on_topic')
        if not isinstance(key, str) or not key:
            continue
        if key not in allowed:
            if key not in fabricated:
                fabricated.append(key)
            continue
        if key in on_topic or not isinstance(flag, bool):
            # A repeated key keeps its first answer; a non-boolean is no answer at all.
            continue
        on_topic[key] = flag

    if fabricated:
        logger.warning('Judge returned %d key(s) absent from the pathway; dropped.', len(fabricated))

    result['on_topic'] = on_topic
    result['fabricated_keys'] = fabricated
    result['unjudged_keys'] = [key for key in supplied if key not in on_topic]
    return result


def parse_judgement_v2(payload, supplied_keys) -> dict:
    """
    Validate a v2 judge response: ``parse_judgement``'s fields, plus ``flags``.

    ``flags`` maps each course judged on topic or off it to its ``too_specific``,
    ``redundant_with``, ``level_mismatch`` and ``role_misfit``, read from the same entry
    ``parse_judgement`` accepted. The rules are v1's, applied as strictly: a key that was
    not supplied is a fabrication and carries no flags; a boolean flag that is not a boolean
    is not raised; and ``redundant_with`` must name another course in this pathway, or it
    is recorded as ``''``, since a redundancy with a course the learner never sees is no
    redundancy at all.
    """
    result = parse_judgement(payload, supplied_keys)
    result['flags'] = {}
    if result['error']:
        return result

    allowed = set(supplied_keys)
    flags: dict = {}
    invalid_redundancies = 0
    for entry in payload.get('courses') or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get('key')
        if key not in result['on_topic'] or key in flags or not isinstance(entry.get('on_topic'), bool):
            continue
        redundant_with = entry.get('redundant_with')
        if not isinstance(redundant_with, str) or redundant_with not in allowed or redundant_with == key:
            invalid_redundancies += int(bool(redundant_with))
            redundant_with = ''
        flags[key] = {
            **{name: entry.get(name) is True for name in PATHWAY_JUDGE_V2_BOOLEAN_FLAGS},
            'redundant_with': redundant_with,
        }

    if invalid_redundancies:
        logger.info('Judge named %d redundancy target(s) outside the pathway; cleared.', invalid_redundancies)
    result['flags'] = {
        key: {name: flags[key][name] for name in V2_FLAG_ORDER} for key in supplied_keys if key in flags
    }
    return result


def is_flagged(course_flags: dict) -> bool:
    """Whether a v2 judge raised any flag on one course."""
    return any(bool((course_flags or {}).get(name)) for name in V2_FLAG_ORDER)


def judge_pathway(*, career_name: str, career_skills: list[str], courses: list[dict],
                  trace_id: str, backend=None, rubric: str = RUBRIC_V1, career_description: str = '',
                  family_titles=None, family_size: int = 0) -> dict:
    """
    Judge one pathway under one rubric.

    Args:
        career_name: The career the pathway was built for.
        career_skills: That career's skills; v1 shows the first ``CAREER_SKILLS_SHOWN``,
            v2 the first ``V2_CAREER_SKILLS_SHOWN``.
        courses: The pathway's courses, in order, as described in ``build_user_content``.
        trace_id: Ties the model call to the persisted step record.
        backend: Overrides ``get_judge_backend()``. For tests and comparison runs.
        rubric: ``'v1'`` (the default, the calibrated instrument) or ``'v2'``.
        career_description, family_titles, family_size: What v2 shows of the career and
            its family. v1 ignores them, so its request is exactly what it always was.

    Returns:
        ``parse_judgement``'s fields plus ``n_on_topic``, ``n_courses``, ``trace``,
        ``rubric`` and ``flags`` (per-course v2 flags; empty under v1). A configuration,
        request or parse failure is returned in ``error`` rather than raised, so one failed
        judgement cannot cost the run its pathway.

    Raises:
        ValueError: An unknown rubric, before any call is made.
    """
    if rubric not in JUDGE_RUBRICS:
        raise ValueError(f'Unknown judge rubric {rubric!r}; expected one of {list(JUDGE_RUBRICS)}.')
    keys = [course.get('key', '') for course in courses]
    result = {
        'verdict': '', 'reason': '', 'on_topic': {}, 'fabricated_keys': [],
        'unjudged_keys': keys, 'error': '', 'n_on_topic': 0, 'n_courses': len(courses),
        'trace': {}, 'rubric': rubric, 'flags': {},
    }

    if rubric == RUBRIC_V2:
        system_prompt, parse = JUDGE_SYSTEM_PROMPT_V2, parse_judgement_v2
        user_content = build_user_content_v2(
            career_name=career_name, career_skills=career_skills, courses=courses,
            career_description=career_description, family_titles=family_titles, family_size=family_size,
        )
    else:
        system_prompt, parse = JUDGE_SYSTEM_PROMPT, parse_judgement
        user_content = build_user_content(career_name=career_name, career_skills=career_skills, courses=courses)

    try:
        model_backend = backend or get_judge_backend()
        response = model_backend.complete(
            system_prompt=system_prompt,
            user_content=user_content,
            trace_id=trace_id,
        )
    except ModelBackendConfigurationError as exc:
        result['error'] = f'judge not configured: {exc}'
        return result
    except ModelBackendError as exc:
        # The exception type only: backend messages can echo the request body.
        result['error'] = f'judge request failed ({type(exc).__name__})'
        return result

    result['trace'] = response.to_trace_dict()
    try:
        payload = response.as_json()
    except ModelBackendError:
        result['error'] = 'judge response was not JSON'
        return result

    result.update(parse(payload, keys))
    result['n_on_topic'] = sum(1 for flag in result['on_topic'].values() if flag)
    return result
