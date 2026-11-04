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
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from enterprise_access.apps.personalized_search.adapters.memory_store import InMemorySessionStore
from enterprise_access.apps.personalized_search.core.errors import ModelError
from enterprise_access.apps.personalized_search.core.harness_scoring import recall, score_intent
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, Steps, TurnContext, run_turn
from enterprise_access.apps.personalized_search.core.retrieval import compose_view

CASES_PATH = Path(__file__).parent / 'fixtures' / 'harness_cases.json'
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

    @classmethod
    def parse(cls, spec: str, check_model: Callable[[str], None] = lambda model: None) -> 'Arm':
        """
        ``NAME=INTENT_MODEL[@EFFORT],COMPOSE_MODEL[@EFFORT]``, e.g. ``best-fit=gpt-4.1-mini,gpt-5.4-mini@low``.
        ``check_model`` refuses unknown models (the model registry's ``check_model``).
        """
        try:
            name, models = spec.split('=', 1)
            intent_spec, compose_spec = models.split(',', 1)
        except ValueError as exc:
            raise ValueError(f'Bad arm {spec!r}; expected NAME=INTENT[@EFFORT],COMPOSE[@EFFORT].') from exc

        def choice(text: str) -> ModelChoice:
            model, _, effort = text.strip().partition('@')
            check_model(model)
            return ModelChoice(model, effort or None)

        return cls(name.strip(), choice(intent_spec), choice(compose_spec))


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

    def spend(self) -> None:
        """Count one paid call, or raise if it would exceed the cap."""
        if self.max_calls is not None and self.used >= self.max_calls:
            raise BudgetExhausted()
        self.used += 1


@dataclass
class _Recorder:
    """What the wrapped steps saw during one turn."""

    candidate_ids: list = field(default_factory=list)
    candidates: list = field(default_factory=list)


def _wrap_steps(base: Steps, budget: Budget, recorder: _Recorder, retrieval_cache: dict) -> Steps:
    """Steps that spend budget before each model call, memoise retrieval and record candidates."""
    def intent(**kwargs):
        budget.spend()
        return base.intent(**kwargs)

    def retrieve(merged, scope, excluded):
        key = json.dumps([merged, sorted(excluded)], sort_keys=True)
        if key not in retrieval_cache:
            retrieval_cache[key] = base.retrieve(merged, scope, excluded)
        raw = retrieval_cache[key]
        recorder.candidate_ids = list(dict.fromkeys(c['card']['item_id'] for c in raw))
        recorder.candidates = raw
        return raw

    def compose(**kwargs):
        budget.spend()
        return base.compose(**kwargs)

    return Steps(intent=intent, retrieve=retrieve, compose=compose)  # no persist: harness turns aren't saved


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
                for item in block['props']['items']:
                    out['item_ids'].append(item['item_id'])
                    out['item_types'].append(item['item_type'])
        elif kind == 'data-followups':
            out['followups'] = part['data']
        elif kind == 'data-timing':
            out['timing'] = {k: v for k, v in part['data'].items() if k != 'debug'}
            out['debug'] = part['data'].get('debug') or {}
        elif kind == 'error':
            out['status'], out['error'] = 'error', part['errorText']
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
                try:
                    parts = list(run_turn(ctx, _wrap_steps(steps, budget, recorder, retrieval_cache)))
                except BudgetExhausted:
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
