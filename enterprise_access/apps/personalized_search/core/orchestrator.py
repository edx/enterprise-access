"""
Turn orchestrator (tech spec "Turn pipeline", steps 0-6): yields protocol-neutral part dicts.

Everything outside the pipeline is injected: the model, catalog and persistence calls (``Steps``),
the clock, ids, timeouts and how a model id maps to a platform name (``TurnContext``). The generator
never touches a request or a database. Closing it (client Stop) closes the model stream and skips
persistence.

Observability (chunk 21): every turn, whether it finishes, errors or is aborted, writes exactly one
``personalized_search.turn`` log record with ids, models, step timings and counts. It never includes
the learner's message or the reply text.
"""
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterator

from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core import intent as intent_step
from enterprise_access.apps.personalized_search.core import telemetry
from enterprise_access.apps.personalized_search.core.compose import SYSTEM_PROMPT as COMPOSE_SYSTEM
from enterprise_access.apps.personalized_search.core.compose import ComposeParser
from enterprise_access.apps.personalized_search.core.compose import build_user_content as compose_user
from enterprise_access.apps.personalized_search.core.errors import ModelError
from enterprise_access.apps.personalized_search.core.ports import Usage
from enterprise_access.apps.personalized_search.core.retrieval import (
    RetrievalFailed,
    choose_followup_attribute,
    prefilter
)

logger = logging.getLogger(__name__)
TURN_LOG_EVENT = 'personalized_search.turn'
# The only fields the turn log may carry: ids, models, timings and counts, never learner text
# (``pending_turn`` holds the message and reply until they're persisted, so it's excluded).
TURN_LOG_FIELDS = (
    'session_uuid', 'sequence', 'turn_uuid', 'backend', 'model_intent', 'model_compose', 'outcome', 'error',
    'redirect_reason', 'timing', 'item_count', 'dropped_lines', 'fallback', 'candidate_ids', 'item_ids',
    'total_ms',
)


@dataclass(frozen=True)
class ModelChoice:
    model: str
    effort: str | None


@dataclass
class TurnContext:
    """Everything the generator needs, loaded before the response starts (spec step 6)."""

    session: dict
    message: str
    intent_model: ModelChoice
    compose_model: ModelChoice
    scope: object
    debug: bool = False
    # Per-step timeouts (each is also capped by the time left before the turn deadline).
    intent_timeout_s: float = constants.DEFAULT_INTENT_TIMEOUT_S
    compose_timeout_s: float = constants.DEFAULT_COMPOSE_READ_TIMEOUT_S
    # How many candidates the compose step chooses from (the prefilter's shortlist).
    shortlist: int = constants.MAX_CANDIDATES
    # Model id -> platform name for logs and timing ('openai', 'xpert'); the model registry's platform_of.
    platform: Callable[[str], str] = lambda model: 'unknown'
    clock: Callable[[], float] = time.monotonic
    id_factory: Callable[[], str] = lambda: str(uuid.uuid4())
    t0: float = field(default=0.0)
    # Event tags from the host (KD-12): ``cohort`` and ``engine``.
    analytics: dict = field(default_factory=dict)


@dataclass
class Steps:
    """Injectable step functions (built by ``wiring.steps`` in enterprise-access, fakes in tests)."""

    # (system, user, model, effort, timeout_s) -> (text, Usage)
    intent: Callable[..., tuple[str, Usage]]
    # (intent, scope, excluded_ids, timeout_s=, shortlist=) -> raw candidates
    retrieve: Callable[..., list[dict]]
    # (system, user, model, effort, timeout_s, usage) -> Iterator[str]
    compose: Callable[..., Iterator[str]]
    # (session, turn) -> None: write a finished turn (``SessionStore.persist_turn``). None: don't persist.
    persist: Callable[[dict, dict], None] | None = None
    # (lms_user_id, event, properties) -> None: send a usage event (KD-12). None: send nothing.
    emit: Callable[[object, str, dict], None] | None = None


def _ms(ctx: TurnContext, at: float | None) -> int | None:
    return round((at - ctx.t0) * 1000) if at is not None else None


def _excluded_ids(session: dict) -> list[str]:
    """
    Items this turn must not show: those the learner dismissed. Results update in place (KD-11), so
    items shown in earlier turns may appear again; cards new to the conversation are flagged instead.
    """
    return list(dict.fromkeys(session.get('not_interested_ids', [])))


def _pending_turn(ctx: TurnContext, record: dict, *, reply: str, intent: dict, item_ids: list, attribute: str,
                  timing: dict) -> dict:
    """What ``Steps.persist`` writes once the stream finishes (parts are added by ``run_turn``)."""
    return {'uuid': record['turn_uuid'], 'user_message': ctx.message, 'reply_text': reply, 'intent': intent,
            'item_ids': item_ids, 'follow_up_attribute': attribute, 'backend': record['backend'], 'timing': timing}


def _session_part(session: dict, sequence: int) -> dict:
    return {'type': 'data-session', 'id': 'session',
            'data': {'session_uuid': session['uuid'], 'turn_sequence': sequence, 'title': session['title']}}


def platform_of(ctx: TurnContext) -> str:
    """'openai', 'xpert' or 'mixed', from the turn's per-step models."""
    platforms = {ctx.platform(c.model) for c in (ctx.intent_model, ctx.compose_model)}
    return platforms.pop() if len(platforms) == 1 else 'mixed'


def run_turn(ctx: TurnContext, steps: Steps) -> Iterator[dict]:
    """
    Run one turn and log it once. The outcome is ``done``, ``redirect``, ``error`` (with its code) or
    ``aborted`` (the client stopped; nothing is persisted).
    """
    ctx.t0 = ctx.t0 or ctx.clock()
    record = {
        'session_uuid': ctx.session['uuid'], 'sequence': ctx.session['turn_count'] + 1,
        'backend': platform_of(ctx), 'model_intent': ctx.intent_model.model,
        'model_compose': ctx.compose_model.model, 'outcome': 'aborted', 'error': None, 'timing': {},
        'item_count': 0, 'dropped_lines': 0, 'fallback': False, 'candidate_ids': [], 'item_ids': [],
    }
    parts = _turn_parts(ctx, steps, record)
    emitted: list[dict] = []
    try:
        for part in parts:
            if part['type'] == 'error':
                record['outcome'], record['error'] = 'error', part['errorText']
            elif part['type'] == 'finish':
                # Spec step 5-6: persist before finish; if that fails, the turn ends in an error instead.
                if steps.persist and record.get('pending_turn'):
                    try:
                        steps.persist(ctx.session, dict(record['pending_turn'], parts=emitted))
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.exception('Personalized Search could not persist turn %s of session %s.',
                                         record['sequence'], record['session_uuid'])
                        record['outcome'], record['error'] = 'error', 'internal'
                        yield {'type': 'error', 'errorText': 'internal'}
                        return
                record['outcome'] = 'redirect' if record.get('redirected') else 'done'
                record['blocks'] = [p['data'] for p in emitted if p['type'] == 'data-block']
            emitted.append(part)
            yield part
    except Exception:  # pylint: disable=broad-exception-caught
        # A bug or an unexpected failure in any step: end the turn with an error the client can show,
        # and log it as an error rather than an abort.
        logger.exception('Personalized Search turn %s of session %s failed unexpectedly.',
                         record['sequence'], record['session_uuid'])
        record['outcome'], record['error'] = 'error', 'internal'
        yield {'type': 'error', 'errorText': 'internal'}
    finally:
        parts.close()
        record['item_count'] = len(record['item_ids'])
        record['total_ms'] = _ms(ctx, ctx.clock())
        logged = {key: record[key] for key in TURN_LOG_FIELDS if key in record}
        logger.info('%s %s', TURN_LOG_EVENT, json.dumps(logged, sort_keys=True))
        _send_event(ctx, steps, record)


def _send_event(ctx: TurnContext, steps: Steps, record: dict) -> None:
    """Send the turn's usage event. Never blocks the turn's outcome or raises (KD-12)."""
    if not steps.emit:
        return
    try:
        name, properties = telemetry.turn_event(ctx, record)
        steps.emit(ctx.session.get('lms_user_id'), name, properties)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception('Personalized Search could not send the usage event for turn %s.', record['sequence'])


def _turn_parts(ctx: TurnContext, steps: Steps, record: dict) -> Iterator[dict]:
    """The turn itself; fills ``record`` (ids, timings, counts) as it goes."""
    # pylint: disable=too-many-locals,too-many-statements
    session = ctx.session
    sequence = session['turn_count'] + 1
    timing = {'intent_ms': None, 'retrieval_ms': None, 'first_text_ms': None, 'first_section_ms': None,
              'complete_ms': None, 'dropped_lines': 0}
    record['timing'] = timing
    debug: dict = {'candidates': 0, 'followup_attribute': None, 'catalog': getattr(ctx.scope, 'label', None)}
    usages: dict[str, Usage] = {}

    def remaining() -> float:
        return max(0.5, constants.TURN_DEADLINE_S - (ctx.clock() - ctx.t0))

    def timing_part() -> dict:
        data = {'backend': platform_of(ctx), 'model_intent': ctx.intent_model.model,
                'model_compose': ctx.compose_model.model,
                **timing}
        if ctx.debug:
            costs = [u.cost_usd() for u in usages.values() if u.cost_usd() is not None]
            data['debug'] = dict(debug, intent=usages['intent'].as_dict() if 'intent' in usages else None,
                                 compose=usages['compose'].as_dict() if 'compose' in usages else None,
                                 cost_usd=round(sum(costs), 6) if costs else None)
        return {'type': 'data-timing', 'id': 'timing', 'transient': True, 'data': data}

    record.update(stage='intent', usages=usages, excluded_ids=_excluded_ids(session))
    record['turn_uuid'] = ctx.id_factory()
    yield {'type': 'start', 'messageId': f"turn_{record['turn_uuid']}"}
    yield _session_part(session, sequence)
    # Join keys for the portal's interaction events (KD-12); stored, so a reopened search keeps them.
    yield {'type': 'data-analytics', 'id': 'analytics', 'data': telemetry.join_keys(ctx, record)}
    yield {'type': 'data-status', 'data': {'step': 'understanding'}, 'transient': True}

    # Step 1: intent
    try:
        text, usage = steps.intent(
            system=intent_step.SYSTEM_PROMPT,
            user=intent_step.build_user_content(message=ctx.message, prior_intent=session['merged_intent'],
                                                history=session['history']),
            model=ctx.intent_model.model, effort=ctx.intent_model.effort,
            timeout_s=min(ctx.intent_timeout_s, remaining()),
        )
        usages['intent'] = usage
        result = intent_step.parse(text)
    except ModelError:
        yield {'type': 'error', 'errorText': 'model_failed'}
        return
    except intent_step.IntentInvalid:
        yield {'type': 'error', 'errorText': 'intent_invalid'}
        return
    timing['intent_ms'] = _ms(ctx, ctx.clock())
    debug['intent_result'] = {'on_topic': result['on_topic'], 'redirect_reason': result['redirect_reason'],
                              'merge': result['merge']}

    if not result['on_topic']:
        # Redirect path (REQ-008): fixed reply, opening chips, no retrieval or compose.
        record['redirected'] = True
        record['redirect_reason'] = result['redirect_reason']
        yield {'type': 'text-start', 'id': 'reply'}
        yield {'type': 'text-delta', 'id': 'reply', 'delta': constants.REDIRECT_REPLY}
        timing['first_text_ms'] = _ms(ctx, ctx.clock())
        yield {'type': 'text-end', 'id': 'reply'}
        yield {'type': 'data-followups', 'id': 'followups',
               'data': {'question': constants.FOLLOWUP_TEMPLATES['open'][0], 'attribute': 'open',
                        'suggestions': constants.OPENING_CHIPS}}
        session['history'].append({'user': ctx.message, 'reply': constants.REDIRECT_REPLY})
        session['turn_count'] = sequence
        session['title'] = session['title'] or session['first_prompt'][:constants.CAPS['session_title']]
        record['pending_turn'] = _pending_turn(ctx, record, reply=constants.REDIRECT_REPLY, intent=result,
                                               item_ids=[], attribute='open', timing=timing)
        timing['complete_ms'] = _ms(ctx, ctx.clock())
        yield _session_part(session, sequence)
        yield timing_part()
        yield {'type': 'finish'}
        return

    merged = intent_step.merge(session['merged_intent'], result['intent'], result['merge'], message=ctx.message)
    debug['merged_intent'] = merged
    record.update(merged_intent=merged, merge_mode=result['merge'], stage='search')

    # Steps 2-3: retrieval, prefilter, follow-up attribute
    yield {'type': 'data-status', 'data': {'step': 'searching'}, 'transient': True}
    retrieval_started = ctx.clock()
    try:
        raw = steps.retrieve(merged, ctx.scope, _excluded_ids(session),
                             timeout_s=min(constants.RETRIEVAL_TIMEOUT_S, remaining()), shortlist=ctx.shortlist)
        candidates = prefilter(raw, merged, ctx.shortlist)
        totals = getattr(raw, 'totals', {})
    except RetrievalFailed:
        yield {'type': 'error', 'errorText': 'retrieval_failed'}
        return
    timing['retrieval_ms'] = round((ctx.clock() - retrieval_started) * 1000)
    record['candidate_ids'] = [c['card']['item_id'] for c in candidates]
    attribute = choose_followup_attribute(candidates, merged, session['asked_attributes'])
    debug.update(candidates=len(candidates), followup_attribute=attribute)
    record.update(followup_attribute=attribute, stage='compose')

    # Step 4: compose
    yield {'type': 'data-status', 'data': {'step': 'writing'}, 'transient': True}
    parser = ComposeParser(candidates=candidates, shown_ids=_excluded_ids(session), followup_attribute=attribute,
                           intent=merged, totals=totals, seen_ids=session['shown_item_ids'])
    record['item_ids'] = parser.emitted_ids  # the parser's own list, so an aborted turn logs what it showed
    record.update(placements=parser.placements, dropped=parser.dropped)
    usages['compose'] = Usage(model=ctx.compose_model.model, effort=ctx.compose_model.effort)
    stream = steps.compose(
        system=COMPOSE_SYSTEM,
        user=compose_user(merged_intent=merged, followup_attribute=attribute, asked=session['asked_attributes'],
                          history=session['history'], message=ctx.message, candidates=candidates,
                          language=getattr(ctx.scope, 'language_name', None) or 'English'),
        model=ctx.compose_model.model, effort=ctx.compose_model.effort,
        timeout_s=min(ctx.compose_timeout_s, remaining()), usage=usages['compose'],
    )

    def emit(parts):
        for part in parts:
            if part['type'] == 'text-delta' and timing['first_text_ms'] is None:
                timing['first_text_ms'] = _ms(ctx, ctx.clock())
            is_section = part['type'] == 'data-block' and part['data']['type'] == 'item_section'
            if is_section and timing['first_section_ms'] is None:
                timing['first_section_ms'] = _ms(ctx, ctx.clock())
            yield part

    try:
        for delta in stream:
            yield from emit(parser.feed(delta))
            if ctx.clock() - ctx.t0 > constants.TURN_DEADLINE_S:
                yield {'type': 'error', 'errorText': 'deadline_exceeded'}
                return
        yield from emit(parser.finish())
    except ModelError:
        yield {'type': 'error', 'errorText': 'model_failed'}
        return
    finally:
        stream.close()
    timing['dropped_lines'] = parser.dropped_lines
    debug['dropped'] = parser.dropped
    debug['followup_switched_from'] = parser.followup_switched_from
    record.update(dropped_lines=parser.dropped_lines, fallback=parser.used_fallback)

    # Step 5: persist, final session title, timing; step 6: finish
    session['merged_intent'] = merged
    session['history'].append({'user': ctx.message, 'reply': parser.reply.strip()})
    session['shown_item_ids'] = list(dict.fromkeys(session['shown_item_ids'] + parser.emitted_ids))
    attribute = parser.attribute  # 'open' when the results shown don't differ on the planned attribute
    if attribute != 'open' and attribute not in session['asked_attributes']:
        session['asked_attributes'].append(attribute)
    session['turn_count'] = sequence
    fallback_title = session['first_prompt'][:constants.CAPS['session_title']]
    session['title'] = session['title'] or parser.session_title or fallback_title
    record['pending_turn'] = _pending_turn(ctx, record, reply=parser.reply.strip(), intent=result,
                                           item_ids=list(parser.emitted_ids), attribute=attribute, timing=timing)
    timing['complete_ms'] = _ms(ctx, ctx.clock())
    yield _session_part(session, sequence)
    yield timing_part()
    yield {'type': 'finish'}
