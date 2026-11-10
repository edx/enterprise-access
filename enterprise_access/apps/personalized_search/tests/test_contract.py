"""Every part the orchestrator emits matches the published wire contract (``core/schemas``)."""
import json
from pathlib import Path

import jsonschema
from django.test import TestCase
from referencing import Registry, Resource

from enterprise_access.apps.personalized_search.core.errors import ModelError
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, TurnContext, run_turn
from enterprise_access.apps.personalized_search.tests.test_orchestrator import (
    COMPOSE,
    fake_steps,
    new_locked_session,
    openai_platform
)

SCHEMAS = Path(__file__).resolve().parent.parent / 'core' / 'schemas'
INSIGHT_AND_PLAN = COMPOSE.replace(
    '{"block":"followup"',
    '{"block":"item_section","item_type":"program","dek":"p","items":[{"item_id":"p-1","why":"w"},'
    '{"item_id":"p-2","why":"w"}]}\n{"block":"insight","title":"T","body":"B"}\n'
    '{"block":"plan","why":"Basics first.","steps":[{"item_id":"edX+C1","note":"n"},{"item_id":"p-1","note":"n"}]}\n'
    '{"block":"followup"')


def validator():
    """A validator for stream parts that resolves the block catalog schema by its $id."""
    resources = [(s['$id'], Resource.from_contents(s)) for s in
                 (json.loads(p.read_text()) for p in SCHEMAS.glob('*.schema.json'))]
    registry = Registry().with_resources(resources)
    schema = json.loads((SCHEMAS / 'stream-parts.v1.schema.json').read_text())
    return jsonschema.Draft202012Validator(schema, registry=registry)


class WireContractTests(TestCase):
    """Normal, rich, redirect and error turns validate part by part."""

    def turn(self, steps):
        """One turn's parts on a new locked session."""
        session = new_locked_session('Lead a team')
        ctx = TurnContext(session=session, message='Lead a team', intent_model=ModelChoice('gpt-4.1-mini', None),
                          compose_model=ModelChoice('gpt-5.4-mini', 'low'), scope=None, debug=True,
                          platform=openai_platform)
        return list(run_turn(ctx, steps))

    def test_turns_validate(self):
        check = validator()
        turns = [
            self.turn(fake_steps()),
            self.turn(fake_steps(compose_text=INSIGHT_AND_PLAN)),
            self.turn(fake_steps(intent_text='{"on_topic": false, "redirect_reason": "injection"}')),
            self.turn(fake_steps(intent_error=ModelError('x'))),
        ]
        types = set()
        for parts in turns:
            for part in parts:
                check.validate(part)
                types.add(part['data']['type'] if part['type'] == 'data-block' else part['type'])
        self.assertLessEqual({'page_header', 'item_section', 'insight', 'plan', 'error', 'finish'}, types)

    def test_schema_rejects_an_unhydrated_item(self):
        bad = {'type': 'data-block', 'id': 'b', 'data': {'v': 1, 'type': 'item_section', 'props': {
            'item_type': 'course', 'label': 'x', 'dek': '', 'fallback': False, 'items': [{'item_id': 'a'}]}}}
        with self.assertRaises(jsonschema.ValidationError):
            validator().validate(bad)
