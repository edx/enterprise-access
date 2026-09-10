"""
Domain-layer API for re-ranking a candidate course set with a model.

Deliberately narrow. Chunk 9a's ``pathway_assembly`` already guarantees the *structural*
properties of a pathway — five courses, spread across levels, no duplicates, no more than
two from one provider — deterministically and testably. Asking a model for those as well
would be asking it to reproduce arithmetic, and any disagreement would then have to be
adjudicated. So nothing here requests a level mix or a de-duplication.

What is asked for is the one thing measurement showed assembly cannot do: **topical
relevance.** With ``removeWordsIfNoResults: allOptional`` the candidate window is wide
enough to contain the right rungs and loose enough to contain the wrong subjects — a live
``python programming`` query returned ``AI in Architectural Design: Introduction``, and
``biomedical engineer`` returned ``Water and Wastewater Treatment Engineering``. Ordering
by topical fit is what moves those to the back of the window, where assembly will not
reach them.

The model's output is treated as untrusted
------------------------------------------
It returns *keys*, never course records, and every key is checked against the candidate
set it was given. A key that was not in the input is dropped and counted in
``fabricated_keys`` — the platform has a known key-invention defect, and counting it makes
it a metric rather than an anecdote. A response that is unusable in full degrades to the
retrieval order rather than failing the pathway, because a worse ordering is a better
outcome than no pathway at all.
"""
import json
import logging

from enterprise_access.apps.pathways.model_backends import ModelBackendError, get_model_backend
from enterprise_access.apps.prompts.models import PromptType

logger = logging.getLogger(__name__)

# Used only when the Claude backend is selected. The Xpert backend ignores it and uses its
# stored, admin-editable row instead -- which is why this is a fallback and not the
# canonical prompt: a prompt that mattered would belong in the database.
FALLBACK_SYSTEM_PROMPT = (
    'You order a list of candidate courses by how well each one prepares a learner for a '
    'named career. Judge topical relevance only: do not consider difficulty, provider, or '
    'duplication, which are handled separately.\n\n'
    'Return JSON only, matching: {"ordered_keys": ["<key>", ...], '
    '"rationales": {"<key>": "<one sentence on why this course fits the career>"}}\n\n'
    'Rules: use only keys that appear in the input; include every key you judge relevant, '
    'most relevant first; omit a key entirely rather than inventing one; keep each '
    'rationale under 30 words.'
)

# Descriptions are truncated before they reach the model. The full text is marketing copy
# whose tail rarely changes a relevance judgement, and 20 untruncated descriptions is a
# large, mostly wasted prompt.
DESCRIPTION_CHARS_FOR_MODEL = 400


def build_user_content(*, career_name: str, candidates: list[dict]) -> str:
    """
    Render the request the model sees.

    Only the fields a relevance judgement needs are included. ``level_type`` and
    ``partner`` are deliberately withheld: they are what assembly uses, and offering them
    invites the model to optimise for constraints it is not being asked about.
    """
    return json.dumps(
        {
            'career': career_name,
            'candidates': [
                {
                    'key': candidate.get('key', ''),
                    'title': candidate.get('title', ''),
                    'description': (
                        candidate.get('short_description') or
                        candidate.get('full_description') or
                        ''
                    )[:DESCRIPTION_CHARS_FOR_MODEL],
                }
                for candidate in candidates
            ],
        },
        separators=(',', ':'),
    )


def parse_rerank_response(payload, allowed_keys) -> dict:
    """
    Validate a model response against the candidate set it was given.

    Returns ``ordered_keys``, ``rationales`` and ``fabricated_keys``. A malformed payload
    yields empty lists rather than raising: the caller degrades to retrieval order, and a
    bad response should cost the ordering, not the pathway.
    """
    if not isinstance(payload, dict):
        logger.warning('Re-rank response was not a JSON object; ignoring the ordering.')
        return {'ordered_keys': [], 'rationales': {}, 'fabricated_keys': []}

    raw_keys = payload.get('ordered_keys')
    if not isinstance(raw_keys, list):
        logger.warning('Re-rank response had no ordered_keys list; ignoring the ordering.')
        return {'ordered_keys': [], 'rationales': {}, 'fabricated_keys': []}

    allowed = set(allowed_keys)
    ordered_keys: list[str] = []
    fabricated: list[str] = []
    for key in raw_keys:
        if not isinstance(key, str) or not key:
            continue
        if key in ordered_keys:
            # A repeated key is not a fabrication, just noise -- the first wins.
            continue
        if key in allowed:
            ordered_keys.append(key)
        else:
            fabricated.append(key)

    if fabricated:
        logger.warning(
            'Re-rank returned %d key(s) absent from the candidate set; dropped.',
            len(fabricated),
        )

    raw_rationales = payload.get('rationales')
    rationales = {}
    if isinstance(raw_rationales, dict):
        rationales = {
            key: value for key, value in raw_rationales.items()
            if isinstance(key, str) and key in allowed and isinstance(value, str)
        }

    return {
        'ordered_keys': ordered_keys,
        'rationales': rationales,
        'fabricated_keys': fabricated,
    }


def rerank_candidates(*, career_name: str, candidates: list[dict], trace_id: str,
                      backend=None) -> dict:
    """
    Order a candidate set by topical relevance to a career.

    Args:
        career_name: The selected career's display name.
        candidates: ``CourseCandidate`` dicts, in retrieval order.
        trace_id: Ties the model call to the persisted step record.
        backend: Overrides the configured backend. For tests and comparison runs.

    Returns:
        ``ordered_keys``, ``rationales``, ``fabricated_keys``, ``prompt_revision`` and a
        ``trace`` dict from ``ModelResponse.to_trace_dict``. A backend failure or an
        unparseable response returns empty ordering rather than raising, so the caller
        degrades to retrieval order.
    """
    allowed_keys = [candidate.get('key', '') for candidate in candidates]
    model_backend = backend or get_model_backend(prompt_type=PromptType.CANDIDATE_RERANK)

    empty = {
        'ordered_keys': [], 'rationales': {}, 'fabricated_keys': [],
        'prompt_revision': '', 'trace': {},
    }

    try:
        response = model_backend.complete(
            system_prompt=FALLBACK_SYSTEM_PROMPT,
            user_content=build_user_content(career_name=career_name, candidates=candidates),
            trace_id=trace_id,
        )
    except ModelBackendError as exc:
        # Logged without the prompt or the response body, per the backend contract.
        logger.warning('Re-rank skipped: model backend failed (%s).', type(exc).__name__)
        return empty

    try:
        payload = response.as_json()
    except ModelBackendError:
        logger.warning('Re-rank skipped: response was not JSON.')
        return {**empty, 'trace': response.to_trace_dict()}

    result = parse_rerank_response(payload, allowed_keys)
    result['prompt_revision'] = str(response.metadata.get('prompt_revision', '') or '')
    result['trace'] = response.to_trace_dict()
    return result
