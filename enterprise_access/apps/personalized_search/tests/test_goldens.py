"""
Golden ``.sse`` files: one deterministic turn per outcome, byte for byte as the turn endpoint streams it.

Clients (the learner portal's parser and reducer tests) consume these files as fixtures. To rewrite
them after a deliberate stream change: ``PERSONALIZED_SEARCH_REWRITE_GOLDENS=1 pytest <this file>``.
"""
import itertools
import json
import os
import uuid
from pathlib import Path
from unittest import TestCase

from enterprise_access.apps.personalized_search.adapters.memory_store import InMemorySessionStore
from enterprise_access.apps.personalized_search.core.errors import ModelError
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, Steps, TurnContext, run_turn
from enterprise_access.apps.personalized_search.core.ports import Usage
from enterprise_access.apps.personalized_search.core.retrieval import RetrievalFailed
from enterprise_access.apps.personalized_search.core.stream import DONE_FRAME, sse_frames
from enterprise_access.apps.personalized_search.tests.helpers import default_candidates
from enterprise_access.apps.personalized_search.tests.test_contract import validator

GOLDEN = Path(__file__).resolve().parent / 'fixtures' / 'golden'
REWRITE = os.environ.get('PERSONALIZED_SEARCH_REWRITE_GOLDENS') == '1'
ON_TOPIC = json.dumps({'on_topic': True, 'redirect_reason': None, 'merge': 'replace',
                       'intent': {'query': 'team leadership', 'skills': ['Leadership'], 'level': 'introductory'}})
COMPOSE = ('Here are picks for leading a team.\n@@BLOCKS@@\n'
           '{"block":"page_header","title":"Lead with confidence","dek":"Short, practical picks."}\n'
           '{"block":"item_section","item_type":"course","dek":"Start here","items":['
           '{"item_id":"edX+C1","why":"Covers the basics you asked for.","fit":["Introductory","4 weeks"]},'
           '{"item_id":"edX+C2","why":"Builds on it.","fit":["Next step"]}]}\n'
           '{"block":"item_section","item_type":"program","dek":"Go deeper","items":['
           '{"item_id":"p-1","why":"A full certificate.","fit":["Certificate"]}]}\n'
           '{"block":"insight","title":"Start with people skills","body":"New leads grow fastest by practising feedback."}\n'
           '{"block":"plan","steps":[{"item_id":"edX+C1","note":"Start"},{"item_id":"p-1","note":"Then"}]}\n'
           '{"block":"followup","question":"How much time can you give it?","suggestions":["Under 4 weeks",'
           '"1 to 3 months"],"session_title":"Leading a team"}\n')
NO_MATCH = ('Nothing in your catalog covers underwater basket weaving.\n@@BLOCKS@@\n'
            '{"block":"followup","question":"Is there another skill you want?","suggestions":["Data","AI"]}\n')


def steps(intent_text=ON_TOPIC, compose_text=COMPOSE, retrieve=None, compose_error=None):
    """Stubbed model and catalog steps; compose streams in 7-character deltas."""
    def compose(**_):
        for i in range(0, len(compose_text), 7):
            yield compose_text[i:i + 7]
        if compose_error:
            raise compose_error

    return Steps(intent=lambda **_: (intent_text, Usage(model='gpt-4.1-mini', effort=None)),
                 retrieve=retrieve or (lambda *a, **kw: default_candidates()), compose=compose)


CASES = {
    'normal_turn': ('I want to lead a team', steps()),
    'off_topic_turn': ('What is the weather today?',
                       steps(intent_text='{"on_topic": false, "redirect_reason": "off_topic"}')),
    'no_match_turn': ('Underwater basket weaving', steps(retrieve=lambda *a, **kw: [], compose_text=NO_MATCH)),
    'retrieval_failed_turn': ('I want to lead a team',
                              steps(retrieve=lambda *a, **kw: (_ for _ in ()).throw(RetrievalFailed('x')))),
    'model_failed_turn': ('I want to lead a team', steps(compose_error=ModelError('x'))),
}


def render(name: str) -> str:
    """The SSE body for one case: fixed clock, ids and session."""
    message, case_steps = CASES[name]
    session = InMemorySessionStore().load_or_create(session_uuid=None, lms_user_id=1,
                                                    enterprise_customer_uuid=str(uuid.UUID(int=1)), message=message)
    session['uuid'] = str(uuid.UUID(int=0xA11CE))
    ticks = itertools.count()
    ctx = TurnContext(session=session, message=message, intent_model=ModelChoice('gpt-4.1-mini', None),
                      compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None,
                      clock=lambda: next(ticks) * 0.05, id_factory=lambda: str(uuid.UUID(int=0xB0B)),
                      platform=lambda _model: 'openai')
    return ''.join(sse_frames(run_turn(ctx, case_steps), lambda: None))


class GoldenStreamTests(TestCase):
    """The goldens match what the orchestrator streams today, and follow the published schemas."""

    def test_goldens_match(self):
        for name in CASES:
            path = GOLDEN / f'{name}.sse'
            body = render(name)
            if REWRITE:
                path.write_text(body, encoding='utf-8')
            self.assertEqual(path.read_text(encoding='utf-8'), body, f'{path.name} is stale; see the module docstring')

    def test_goldens_follow_the_schemas(self):
        check = validator()
        for path in sorted(GOLDEN.glob('*.sse')):
            frames = [f for f in path.read_text(encoding='utf-8').split('\n\n') if f]
            self.assertEqual(frames[-1] + '\n\n', DONE_FRAME, path.name)
            for frame in frames[:-1]:
                check.validate(json.loads(frame[len('data: '):]))

    def test_every_outcome_has_a_golden(self):
        self.assertEqual(sorted(p.stem for p in GOLDEN.glob('*.sse')), sorted(CASES))
