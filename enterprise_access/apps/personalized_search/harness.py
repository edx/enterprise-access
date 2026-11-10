"""
Personalized Search harness: the same turns through two or more model arms, results side by side.

Follows ``apps/pathway_eval``'s shape (personas × runs × arms, a call budget enforced *before* each
paid call, ``dry_run``, JSONL out, a separate report) and #297's rule of one variable between arms:
every arm gets the same prompts and the same seeded session, and retrieval is memoised by merged
intent, so two arms that understand a message the same way search the same candidates. Arms that
understand it differently search differently, which is part of what a learner would experience.

Cases come from two places:
- the demo script (``fixtures/harness_cases.json``): open goals, refinements (seeded with their prior
  turn and intent), career, narrow-catalog and adversarial prompts, each with an expected intent outcome;
- the ``pathway_eval`` personas: an opening message built from each persona's intake, scored by
  recall of the expert-authored course keys.

It runs the real orchestrator (``run_turn``), so the numbers are a learner's turn, not a model call.
Like the core, it needs no Django or database: sessions are in memory, and the model check, judge call
and persona loader are passed in (``run_personalized_search_harness`` wires them from settings).

Conversations (``run_conversations``) carry one session through three turns per persona
(``fixtures/harness_conversations.json``), so a run measures how many turns it takes to reach relevant
results, not just the first page. Arms may also set a higher compose effort for the first turn only, a
larger shortlist, or a fuller candidate view (``Arm.parse``); those apply only inside the harness.
"""
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from enterprise_access.apps.personalized_search.adapters.memory_store import InMemorySessionStore
from enterprise_access.apps.personalized_search.core import compose as compose_step
from enterprise_access.apps.personalized_search.core import constants, retrieval
from enterprise_access.apps.personalized_search.core.errors import ModelError
from enterprise_access.apps.personalized_search.core.harness_scoring import recall, score_intent
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, Steps, TurnContext, run_turn
from enterprise_access.apps.personalized_search.core.retrieval import compose_view

CASES_PATH = Path(__file__).parent / 'fixtures' / 'harness_cases.json'
CONVERSATIONS_PATH = Path(__file__).parent / 'fixtures' / 'harness_conversations.json'
CONVERSATION_STYLES = ('fixed', 'adaptive')
# Extra catalog attributes the rich candidate view reads.
RICH_ATTRIBUTES = ['outcome', 'prerequisites', 'subjects', 'learning_items', 'course_details']
JUDGE_MODEL = 'gpt-5.4-mini'  # the judge #297 calibrated its rubric on
# pylint: disable=line-too-long
JUDGE_RUBRIC = """You are grading one AI-generated learning page for an enterprise learner. You see the learner's message, the candidate items the AI could choose from, and what it put on the page. Grade strictly; 3 is acceptable, 5 is excellent.

Return ONLY JSON: {"relevance": 1-5, "why_quality": 1-5, "editorial": 1-5, "followup": 1-5, "overall": 1-5, "notes": "one sentence"}
- relevance: are the picks the best-fitting candidates for the learner's goal and constraints, with off-topic candidates rejected and no padding?
- why_quality: are the reasons specific to this learner and concrete about skills practised, plain and free of hype?
- editorial: is the header specific and well written, and is the chat reply accurate about what the page shows?
- followup: does the follow-up question narrow the next results, with natural suggestions?
- overall: would you show this page to the learner as is?"""  # noqa: E501
# pylint: enable=line-too-long


class BudgetExhausted(Exception):
    """The next call would exceed ``max_calls``."""


@dataclass(frozen=True)
class Arm:
    """One model configuration under comparison."""

    name: str
    intent: ModelChoice
    compose: ModelChoice
    # Compose effort for a conversation's first turn only, when there's no history to lean on.
    cold_effort: str | None = None
    # Shortlist size sent to compose (default constants.MAX_CANDIDATES); type caps and hits scale with it.
    candidates: int | None = None
    # 'rich' adds outcomes, prerequisites, subjects and more skills to each candidate compose sees.
    view: str = 'standard'

    @classmethod
    def parse(cls, spec: str, check_model: Callable[[str], None] = lambda model: None,
              check_effort: Callable[[str, str | None], object] = lambda model, effort: None) -> 'Arm':
        """
        ``NAME=INTENT_MODEL[@EFFORT],COMPOSE_MODEL[@EFFORT][+cold:EFFORT][+cands:N][+view:rich]``, e.g.
        ``best-fit=gpt-4.1-mini,gpt-5.4-mini@low`` or ``luna=gpt-6-luna,gpt-6-luna@none+cold:high+cands:40``.
        ``check_model`` refuses unknown models and ``check_effort`` efforts a model doesn't take (the model
        registry's ``check_model`` and ``resolve_effort``).
        """
        try:
            name, models = spec.split('=', 1)
            intent_spec, compose_spec = models.split(',', 1)
            compose_spec, *option_specs = compose_spec.split('+')
            options = dict(option.split(':', 1) for option in option_specs)
        except ValueError as exc:
            raise ValueError(f'Bad arm {spec!r}; expected NAME=INTENT[@EFFORT],COMPOSE[@EFFORT][+OPTION:VALUE].') \
                from exc
        unknown = set(options) - {'cold', 'cands', 'view'}
        if unknown or options.get('view', 'rich') not in ('standard', 'rich'):
            raise ValueError(f'Bad arm {spec!r}; options are +cold:EFFORT, +cands:N and +view:standard|rich.')

        def choice(text: str) -> ModelChoice:
            model, _, effort = text.strip().partition('@')
            check_model(model)
            check_effort(model, effort or None)
            return ModelChoice(model, effort or None)

        compose = choice(compose_spec)
        if 'cold' in options:
            check_effort(compose.model, options['cold'])
        candidates = int(options['cands']) if 'cands' in options else None
        if candidates is not None and candidates < 1:
            raise ValueError(f'Bad arm {spec!r}; +cands needs a positive number.')
        return cls(name.strip(), choice(intent_spec), compose, cold_effort=options.get('cold'),
                   candidates=candidates, view=options.get('view', 'standard'))

    def compose_for_turn(self, turn_index: int) -> ModelChoice:
        """The compose model for a turn: the cold-start effort on the first turn when the arm sets one."""
        if turn_index == 0 and self.cold_effort:
            return ModelChoice(self.compose.model, self.cold_effort)
        return self.compose

    @property
    def options(self) -> dict:
        """The harness-only options, for records and the retrieval memo."""
        return {'cold': self.cold_effort, 'cands': self.candidates or constants.MAX_CANDIDATES, 'view': self.view}


def rich_compose_view(candidate: dict) -> dict:
    """``compose_view`` plus outcomes, prerequisites, subjects and up to 8 skills (``+view:rich``)."""
    hit = candidate['hit']
    strip = retrieval._strip_html  # pylint: disable=protected-access
    view = compose_view(candidate)
    view['skills'] = (hit.get('skill_names') or [])[:8]
    view['subjects'] = (hit.get('subjects') or [])[:4]
    if candidate['card']['item_type'] == 'program':
        items = [i.get('title') if isinstance(i, dict) else i for i in hit.get('learning_items') or []]
        view['you_will_learn'] = [strip(i)[:120] for i in items if i][:5]
        courses = [c.get('title') for c in hit.get('course_details') or [] if isinstance(c, dict)]
        view['courses'] = [c for c in courses if c][:6]
    else:
        view['you_will_learn'] = strip(hit.get('outcome'))[:400]
        view['prerequisites'] = strip(hit.get('prerequisites'))[:150]
    return view


@contextlib.contextmanager
def turn_deadline(seconds: float | None):
    """Harness only: a longer turn deadline, so slow arms finish and can be judged (their timings still count)."""
    saved = constants.TURN_DEADLINE_S
    constants.TURN_DEADLINE_S = seconds or saved
    try:
        yield
    finally:
        constants.TURN_DEADLINE_S = saved


@contextlib.contextmanager
def model_sections_only(enabled: bool):
    """
    Harness only: no server-built sections (the empty-page fallback, related-program and alternative
    sections, and refills of a section whose items the model had already used), so a page shows only what
    the model chose.
    """
    # pylint: disable=protected-access
    saved = compose_step.ComposeParser._server_section
    if enabled:
        compose_step.ComposeParser._server_section = lambda self, *args, **kwargs: []
    try:
        yield
    finally:
        compose_step.ComposeParser._server_section = saved


@contextlib.contextmanager
def arm_overrides(arm: Arm):
    """
    Apply an arm's candidate view for the turns run inside the block, by swapping the module values the core
    reads at call time. Harness only: turns run one at a time in one process. (The shortlist size is the turn
    context's ``shortlist``, set from ``+cands``.)
    """
    saved = []

    def swap(target, name, value):
        saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    try:
        if arm.view == 'rich':
            swap(constants, 'ATTRIBUTES_TO_RETRIEVE', constants.ATTRIBUTES_TO_RETRIEVE + RICH_ATTRIBUTES)
            swap(compose_step, 'compose_view', rich_compose_view)
        yield
    finally:
        for target, name, value in reversed(saved):
            setattr(target, name, value)


@dataclass(frozen=True)
class Case:
    """One turn to run in every arm."""

    id: str
    message: str
    kind: str  # 'demo' or 'persona'
    prior_intent: dict | None = None
    history: tuple = ()
    expect: dict | None = None
    expected_course_keys: tuple = ()


def demo_cases(path: Path = CASES_PATH) -> list[Case]:
    """The demo-script cases with their expected intent outcomes."""
    data = json.loads(path.read_text())
    return [Case(id=c['id'], message=c['message'], kind='demo', prior_intent=c.get('prior_intent'),
                 history=tuple(c.get('history') or ()), expect=c.get('expect')) for c in data['cases']]


def persona_cases(load_personas: Callable[[], list], persona_ids: list[str] | None = None) -> list[Case]:
    """
    An opening message from each persona's intake, with its expected courses. ``load_personas`` returns
    objects with ``id``, ``inputs`` and ``expected_course_keys`` (``pathway_eval.personas.load_personas``).
    """
    out = []
    for persona in load_personas():
        if persona_ids and persona.id not in persona_ids:
            continue
        inputs = persona.inputs
        parts = [inputs.get('selected_goals'), inputs.get('free_text'), inputs.get('known_context')]
        message = ' '.join(p.strip().rstrip('.') + '.' for p in parts if p and p.strip())[:500]
        out.append(Case(id=persona.id, message=message, kind='persona',
                        expected_course_keys=tuple(persona.expected_course_keys)))
    return out


@dataclass
class Budget:
    """Counts paid calls and refuses the one that would exceed the cap."""

    max_calls: int | None
    used: int = 0
    exhausted: bool = False  # run_turn swallows the exception below, so the run checks this flag instead

    def spend(self) -> None:
        """Count one paid call, or raise if it would exceed the cap."""
        if self.max_calls is not None and self.used >= self.max_calls:
            self.exhausted = True
            raise BudgetExhausted()
        self.used += 1


@dataclass
class _Recorder:
    """What the wrapped steps saw during one turn."""

    candidate_ids: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    # Why a model call failed (the backend's message: exception type and model, never the request).
    model_error: str | None = None
    compose_usage: object = None


def _wrap_steps(base: Steps, budget: Budget, recorder: _Recorder, retrieval_cache: dict,
                memo_tag: dict | None = None) -> Steps:
    """
    Steps that spend budget before each model call, memoise retrieval and record candidates. ``memo_tag``
    separates arms whose retrieval differs (shortlist size, candidate view).
    """
    def intent(**kwargs):
        budget.spend()
        try:
            return base.intent(**kwargs)
        except ModelError as exc:
            recorder.model_error = f'intent: {exc}'
            raise

    def retrieve(merged, scope, excluded, **kwargs):
        # The timeout isn't part of the memo key; the shortlist size is (it sets how many hits are asked for).
        key = json.dumps([merged, sorted(excluded), memo_tag, kwargs.get('shortlist')], sort_keys=True)
        if key not in retrieval_cache:
            retrieval_cache[key] = base.retrieve(merged, scope, excluded, **kwargs)
        raw = retrieval_cache[key]
        recorder.candidate_ids = list(dict.fromkeys(c['card']['item_id'] for c in raw))
        recorder.candidates = raw
        return raw

    def compose(**kwargs):
        budget.spend()
        recorder.compose_usage = kwargs['usage']
        return _recording_errors(base.compose(**kwargs), recorder)

    return Steps(intent=intent, retrieve=retrieve, compose=compose)  # no persist: harness turns aren't saved


def _recording_errors(stream: Iterator[str], recorder: _Recorder) -> Iterator[str]:
    """Pass a compose stream through, noting why it failed."""
    try:
        yield from stream
    except ModelError as exc:
        recorder.model_error = f'compose: {exc}'
        raise
    finally:
        close = getattr(stream, 'close', None)
        if close:
            close()


def _seeded_session(case: Case) -> dict:
    """A throwaway in-memory session carrying the case's prior turns and intent."""
    session = InMemorySessionStore().load_or_create(session_uuid=None, lms_user_id=-1,
                                                    enterprise_customer_uuid=str(uuid.UUID(int=0)),
                                                    message=case.message)
    if case.history:
        session['history'] = [{'user': message, 'reply': ''} for message in case.history]
        session['turn_count'] = len(case.history)
    session['merged_intent'] = case.prior_intent
    return session


def _summarize_turn(parts: list[dict]) -> dict:
    """Reply, picks, follow-ups, timing and debug detail from one turn's parts."""
    out = {'status': 'done', 'error': None, 'item_ids': [], 'item_types': [], 'fallback': False, 'reply': '',
           'blocks': [], 'followups': None, 'timing': None, 'debug': {}}
    for part in parts:
        kind = part['type']
        if kind == 'text-delta':
            out['reply'] += part['delta']
        elif kind == 'data-block':
            block = part['data']
            out['blocks'].append(block)
            if block['type'] == 'item_section':
                out['fallback'] = out['fallback'] or block['props'].get('fallback', False)
        elif kind == 'data-followups':
            out['followups'] = part['data']
        elif kind == 'data-timing':
            out['timing'] = {k: v for k, v in part['data'].items() if k != 'debug'}
            out['debug'] = part['data'].get('debug') or {}
        elif kind == 'error':
            out['status'], out['error'] = 'error', part['errorText']
    # Every card on the page, the top pick and comparison included, as the multi-turn path counts them.
    items = page_items(out['blocks'])
    out['item_ids'], out['item_types'] = [i['item_id'] for i in items], [i['item_type'] for i in items]
    return out


def _judge(case: Case, turn: dict, candidates: list, budget: Budget, complete_json: Callable) -> dict:
    """Blind rubric score for one page; skipped when the page has no items."""
    if not turn['item_ids']:
        return {'skipped': 'no items on the page'}
    budget.spend()
    page = '\n'.join(json.dumps(b) for b in turn['blocks'])
    content = (f'LEARNER MESSAGE: {case.message}\nMERGED INTENT: {json.dumps(turn["debug"].get("merged_intent"))}\n\n'
               'CANDIDATES\n' + '\n'.join(json.dumps(compose_view(c)) for c in candidates) +
               f'\n\nCHAT REPLY\n{turn["reply"]}\n\nPAGE BLOCKS\n{page}\n\nFOLLOW-UP\n{json.dumps(turn["followups"])}')
    try:
        text, _ = complete_json(model=JUDGE_MODEL, effort='low', system=JUDGE_RUBRIC, user=content,
                                max_tokens=600, timeout_s=60)
        return json.loads(text[text.index('{'): text.rindex('}') + 1])
    except (ModelError, ValueError) as exc:
        return {'error': type(exc).__name__}


def run(*, cases: list[Case], arms: list[Arm], scope, steps: Steps, complete_json: Callable, runs: int = 1,
        max_calls: int | None = None, judge: bool = False, log: Callable = lambda msg: None,
        turn_context: Callable[..., TurnContext] = TurnContext) -> Iterator[dict]:
    """
    Yield one result dict per (run, case, arm). Arms alternate order per case to spread drift.
    ``complete_json`` makes the judge's call; ``turn_context`` builds each turn's context (the host's
    timeouts and platform names: ``wiring.turn_context`` in enterprise-access).
    """
    budget = Budget(max_calls)
    retrieval_cache: dict = {}
    for run_index in range(runs):
        for case_index, case in enumerate(cases):
            ordered = arms if case_index % 2 == 0 else list(reversed(arms))
            for arm in ordered:
                recorder = _Recorder()
                ctx = turn_context(session=_seeded_session(case), message=case.message, intent_model=arm.intent,
                                   compose_model=arm.compose, scope=scope, debug=True)
                started = time.monotonic()
                parts = list(run_turn(ctx, _wrap_steps(steps, budget, recorder, retrieval_cache)))
                if budget.exhausted:  # run_turn turned the refused call into an error turn: drop it and stop
                    log(f'Stopped: max_calls={max_calls} reached after {budget.used} calls.')
                    return
                turn = _summarize_turn(parts)
                record = {
                    'run': run_index, 'case_id': case.id, 'case_kind': case.kind, 'arm': arm.name,
                    'models': [f'{arm.intent.model}{"@" + arm.intent.effort if arm.intent.effort else ""}',
                               f'{arm.compose.model}{"@" + arm.compose.effort if arm.compose.effort else ""}'],
                    'message': case.message, 'wall_ms': round((time.monotonic() - started) * 1000),
                    **{k: turn[k] for k in ('status', 'error', 'item_ids', 'item_types', 'fallback', 'reply',
                                            'followups', 'timing')},
                    'redirected': (turn['debug'].get('intent_result') or {}).get('on_topic') is False,
                    'merged_intent': turn['debug'].get('merged_intent'),
                    'dropped': turn['debug'].get('dropped'),
                    'candidate_ids': recorder.candidate_ids,
                    'cost_usd': turn['debug'].get('cost_usd'),
                    'intent_score': score_intent(case.expect, turn['debug']),
                    'page_recall': recall(list(case.expected_course_keys), turn['item_ids']),
                    'candidate_recall': recall(list(case.expected_course_keys), recorder.candidate_ids),
                }
                if judge:
                    try:
                        record['judge'] = _judge(case, turn, recorder.candidates, budget, complete_json)
                    except BudgetExhausted:
                        log(f'Stopped: max_calls={max_calls} reached before judging.')
                        yield record
                        return
                log(f"{case.id} [{arm.name}] {record['status']} {record['wall_ms']} ms items={len(record['item_ids'])}")
                yield record


def plan(cases: list[Case], arms: list[Arm], runs: int, judge: bool) -> dict:
    """What a run would do, for ``--dry-run``: an upper bound on paid calls (redirects skip compose)."""
    turns = len(cases) * len(arms) * runs
    return {'cases': len(cases), 'arms': [a.name for a in arms], 'runs': runs, 'turns': turns,
            'max_paid_calls': turns * (3 if judge else 2)}


@dataclass(frozen=True)
class Conversation:
    """One persona's three-turn conversation: the opening from its intake, then two scripted turns."""

    id: str
    goal: str
    messages: tuple  # the opening, then the scripted turns
    keywords: tuple = ()
    expected_course_keys: tuple = ()


def conversations(load_personas: Callable[[], list], persona_ids: list[str] | None = None,
                  path: Path = CONVERSATIONS_PATH) -> list[Conversation]:
    """The scripted conversations, each opened with its persona's message (as ``persona_cases`` builds it)."""
    openings = {case.id: case for case in persona_cases(load_personas, persona_ids)}
    out = []
    for script in json.loads(path.read_text())['conversations']:
        opening = openings.get(script['persona_id'])
        if not opening:
            continue
        out.append(Conversation(id=opening.id, goal=script['goal'], messages=(opening.message, *script['turns']),
                                keywords=tuple(script.get('keywords') or ()),
                                expected_course_keys=opening.expected_course_keys))
    return out


def choose_chip(followups: dict | None, keywords: tuple) -> str | None:
    """
    The adaptive learner's reply: the follow-up chip (fixed suggestions, then the model's ideas) with the
    most keyword hits, ties to the first; None when no chip matches any keyword.
    """
    chips = list((followups or {}).get('suggestions') or []) + list((followups or {}).get('ideas') or [])
    best, best_hits = None, 0
    for chip in chips:
        hits = sum(1 for word in keywords if word.casefold() in chip.casefold())
        if hits > best_hits:
            best, best_hits = chip, hits
    return best


def _partial_usage(recorder: _Recorder) -> dict | None:
    """A failed turn's compose timing and tokens, so far as the stream got (no timing part is sent)."""
    usage = recorder.compose_usage
    return usage.as_dict() if usage is not None and hasattr(usage, 'as_dict') else None


def _label(choice: ModelChoice) -> str:
    """``model@effort``, or the model alone."""
    return f'{choice.model}@{choice.effort}' if choice.effort else choice.model


def page_items(blocks: list[dict]) -> list[dict]:
    """Every item a page shows, top pick and comparison first, without repeats."""
    items = []
    for block in blocks:
        props = block.get('props') or {}
        if block['type'] == 'top_pick':
            items.append(props['item'])
        elif block['type'] == 'compare':
            items += [option['item'] for option in props.get('options') or []]
        elif block['type'] == 'item_section':
            items += props.get('items') or []
    return list({item['item_id']: item for item in items}.values())


def _turn_shape(blocks: list[dict]) -> dict:
    """What the page held, for the automatic screen: block order, top pick type and section types."""
    top_pick = next((b['props']['item'] for b in blocks if b['type'] == 'top_pick'), None)
    sections = [b['props'] for b in blocks if b['type'] == 'item_section']
    return {
        'block_types': [b['type'] for b in blocks],
        'top_pick_type': top_pick['item_type'] if top_pick else None,
        'section_types': [s['item_type'] for s in sections],
        'section_counts': [len(s.get('items') or []) for s in sections],
        'caveat': any(b['type'] == 'caveat' for b in blocks),
    }


def run_conversations(*, conversation_list: list[Conversation], arms: list[Arm], scope, steps: Steps,
                      style: str = 'fixed', runs: int = 1, max_calls: int | None = None,
                      server_sections: bool = True, log: Callable = lambda msg: None,
                      turn_context: Callable[..., TurnContext] = TurnContext) -> Iterator[dict]:
    """
    Yield one result dict per (run, conversation, arm, turn). Each arm carries one in-memory session
    through the conversation's turns, as the portal would. ``style`` 'fixed' sends the scripted turns;
    'adaptive' sends the chip that best matches the persona's keywords (``choose_chip``), or the script.
    ``server_sections`` False shows only the sections the model chose (``model_sections_only``).
    """
    if style not in CONVERSATION_STYLES:
        raise ValueError(f'Unknown conversation style {style!r}.')
    budget = Budget(max_calls)
    retrieval_cache: dict = {}
    for run_index in range(runs):
        for conversation_index, conversation in enumerate(conversation_list):
            ordered = arms if conversation_index % 2 == 0 else list(reversed(arms))
            for arm in ordered:
                session = _seeded_session(Case(id=conversation.id, message=conversation.messages[0], kind='persona'))
                followups = None
                for turn_index, scripted in enumerate(conversation.messages):
                    chip = choose_chip(followups, conversation.keywords) if style == 'adaptive' and turn_index else None
                    message = chip or scripted
                    recorder = _Recorder()
                    ctx = turn_context(session=session, message=message, intent_model=arm.intent,
                                       compose_model=arm.compose_for_turn(turn_index), scope=scope, debug=True)
                    if arm.candidates:
                        ctx.shortlist = arm.candidates
                    started = time.monotonic()
                    with arm_overrides(arm), model_sections_only(not server_sections):
                        parts = list(run_turn(ctx, _wrap_steps(steps, budget, recorder, retrieval_cache,
                                                               memo_tag=arm.options)))
                    if budget.exhausted:
                        log(f'Stopped: max_calls={max_calls} reached after {budget.used} calls.')
                        return
                    turn = _summarize_turn(parts)
                    followups = turn['followups']
                    blocks = [{'type': b['type'], 'props': b['props']} for b in turn['blocks']]
                    shown = [item['item_id'] for item in page_items(blocks)]
                    compose_model = arm.compose_for_turn(turn_index)
                    record = {
                        'run': run_index, 'conversation_id': conversation.id, 'turn_index': turn_index,
                        'style': style, 'arm': arm.name, 'arm_options': dict(arm.options, cands=ctx.shortlist),
                        'server_sections': server_sections,
                        'models': [_label(arm.intent), _label(compose_model)],
                        'message': message, 'chosen_chip': chip, 'goal': conversation.goal,
                        'wall_ms': round((time.monotonic() - started) * 1000),
                        **{k: turn[k] for k in ('status', 'error', 'fallback', 'reply', 'followups', 'timing')},
                        **_turn_shape(blocks), 'blocks': blocks, 'page_item_ids': shown,
                        'redirected': (turn['debug'].get('intent_result') or {}).get('on_topic') is False,
                        'merged_intent': turn['debug'].get('merged_intent'),
                        'dropped': turn['debug'].get('dropped'),
                        'candidate_ids': recorder.candidate_ids,
                        'candidates_shortlisted': turn['debug'].get('candidates'),
                        'usage': {'intent': turn['debug'].get('intent'),
                                  'compose': turn['debug'].get('compose') or _partial_usage(recorder)},
                        'model_error': recorder.model_error,
                        'cost_usd': turn['debug'].get('cost_usd'),
                        'page_recall': recall(list(conversation.expected_course_keys), shown),
                        'candidate_recall': recall(list(conversation.expected_course_keys), recorder.candidate_ids),
                    }
                    log(f"{conversation.id} [{arm.name}] turn {turn_index + 1} {record['status']} "
                        f"{record['wall_ms']} ms items={len(shown)}")
                    yield record


def plan_conversations(conversation_list: list[Conversation], arms: list[Arm], runs: int) -> dict:
    """What a conversation run would do, for ``--dry-run``: an upper bound on paid calls."""
    turns = sum(len(c.messages) for c in conversation_list) * len(arms) * runs
    return {'conversations': len(conversation_list), 'arms': [a.name for a in arms], 'runs': runs, 'turns': turns,
            'max_paid_calls': turns * 2}
