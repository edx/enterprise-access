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
    PATHWAY_JUDGE_SYSTEM_PROMPT,
    PATHWAY_JUDGE_VERDICTS
)
from enterprise_access.apps.prompts.api import compose_system_prompt

logger = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = compose_system_prompt(PATHWAY_JUDGE_SYSTEM_PROMPT, PATHWAY_JUDGE_OUTPUT_SCHEMA)

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
    for position, course in enumerate(courses, 1):
        level = course.get('level_type') or 'level unknown'
        lines.append(f"{position}. [{course.get('key', '')}] {course.get('title', '')} ({level})")
        blurb = (course.get('short_description') or course.get('full_description') or '').strip()
        if blurb:
            lines.append(f'   about: {blurb[:DESCRIPTION_CHARS_SHOWN]}')
        course_skills = list(course.get('skill_names') or [])[:COURSE_SKILLS_SHOWN]
        if course_skills:
            lines.append(f"   course skills: {', '.join(course_skills)}")
    return '\n'.join(lines)


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


def judge_pathway(*, career_name: str, career_skills: list[str], courses: list[dict],
                  trace_id: str, backend=None) -> dict:
    """
    Judge one pathway.

    Args:
        career_name: The career the pathway was built for.
        career_skills: That career's skills; the first ``CAREER_SKILLS_SHOWN`` are shown.
        courses: The pathway's courses, in order, as described in ``build_user_content``.
        trace_id: Ties the model call to the persisted step record.
        backend: Overrides ``get_judge_backend()``. For tests and comparison runs.

    Returns:
        ``parse_judgement``'s fields plus ``n_on_topic``, ``n_courses`` and ``trace``.
        A configuration, request or parse failure is returned in ``error`` rather than
        raised, so one failed judgement cannot cost the run its pathway.
    """
    keys = [course.get('key', '') for course in courses]
    result = {
        'verdict': '', 'reason': '', 'on_topic': {}, 'fabricated_keys': [],
        'unjudged_keys': keys, 'error': '', 'n_on_topic': 0, 'n_courses': len(courses),
        'trace': {},
    }

    try:
        model_backend = backend or get_judge_backend()
        response = model_backend.complete(
            system_prompt=JUDGE_SYSTEM_PROMPT,
            user_content=build_user_content(
                career_name=career_name, career_skills=career_skills, courses=courses,
            ),
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

    result.update(parse_judgement(payload, keys))
    result['n_on_topic'] = sum(1 for flag in result['on_topic'].values() if flag)
    return result
