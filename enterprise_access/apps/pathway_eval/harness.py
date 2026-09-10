"""
The harness runner: personas through the pipeline, traces out.

Much smaller than the plan originally allowed for, because **workflow step records
already are the trace.** Every step persists its input, output, timing and failure, so
nothing here builds a tracing layer -- it orchestrates personas x runs x career modes and
records which workflow uuid belongs to which cell.

Two career modes, and the delta between them is the point
---------------------------------------------------------
``auto`` follows whichever career the discovery workflow ranked first. ``oracle`` forces
the persona's expected career. Running both separates two failure modes that look
identical in a single-mode run: a bad pathway because the *career* was wrong, and a bad
pathway because the *courses* were wrong. Without the oracle arm, career-selection error
is silently charged to course retrieval.

Cost is bounded by construction
-------------------------------
A run issues paid model calls, so ``max_calls`` is counted and enforced *before* each
call rather than checked afterwards, and ``dry_run`` reports the plan while issuing none.
Both exist because the harness is the one place in this codebase that can spend real money
in a loop.
"""
import logging
from dataclasses import dataclass, field

from enterprise_access.apps.pathways.models import CareerDiscoveryWorkflow, ExtractIntentOutput, PathwayAssemblyWorkflow
from enterprise_access.apps.workflow.exceptions import UnitOfWorkException

logger = logging.getLogger(__name__)

CAREER_MODE_AUTO = 'auto'
CAREER_MODE_ORACLE = 'oracle'
CAREER_MODES = (CAREER_MODE_AUTO, CAREER_MODE_ORACLE)

# Each cell costs one career-discovery workflow plus one pathway workflow, and the latter
# includes a model call when re-ranking is enabled.
CALLS_PER_CELL = 2


@dataclass
class CellResult:
    """One persona in one career mode on one run."""

    persona_id: str
    career_mode: str
    run_index: int
    career_workflow_uuid: str = ''
    pathway_workflow_uuid: str = ''
    career_name: str = ''
    career_external_id: str = ''
    course_keys: list = field(default_factory=list)
    complete: bool = False
    violations: list = field(default_factory=list)
    unfilled_rungs: list = field(default_factory=list)
    rationale_count: int = 0
    enrichment_error: str = ''
    skipped_reason: str = ''
    error: str = ''

    @property
    def ran(self) -> bool:
        """Whether this cell actually executed a pipeline."""
        return not self.skipped_reason and not self.error

    def to_dict(self) -> dict:
        """Plain dict for JSON export and for the scorers."""
        return {
            'persona_id': self.persona_id,
            'career_mode': self.career_mode,
            'run_index': self.run_index,
            'career_workflow_uuid': str(self.career_workflow_uuid or ''),
            'pathway_workflow_uuid': str(self.pathway_workflow_uuid or ''),
            'career_name': self.career_name,
            'career_external_id': self.career_external_id,
            'course_keys': list(self.course_keys),
            'complete': self.complete,
            'violations': list(self.violations),
            'unfilled_rungs': list(self.unfilled_rungs),
            'rationale_count': self.rationale_count,
            'enrichment_error': self.enrichment_error,
            'skipped_reason': self.skipped_reason,
            'error': self.error,
        }


@dataclass
class HarnessBudget:
    """
    Counts paid calls and refuses to start a cell that would exceed the limit.

    Enforced before the call, not after: a limit checked afterwards has already spent the
    money it was meant to prevent.
    """

    max_calls: int | None = None
    calls_made: int = 0

    def can_afford(self, calls: int = CALLS_PER_CELL) -> bool:
        """Whether ``calls`` more calls are within budget."""
        if self.max_calls is None:
            return True
        return self.calls_made + calls <= self.max_calls

    def charge(self, calls: int = CALLS_PER_CELL) -> None:
        """Record calls as spent."""
        self.calls_made += calls


class PathwayHarness:
    """
    Runs personas through career discovery and pathway assembly.

    Holds no scoring logic: it produces traces, and ``scoring`` turns traces into metrics.
    That separation is architecture pattern 16 -- the harness owns no domain logic, and it
    also owns no judgement about what the results mean.
    """

    def __init__(self, *, runs: int = 1, career_modes=CAREER_MODES, max_calls: int | None = None,
                 dry_run: bool = False, customer_uuid: str = '', allow_unscoped: bool = False,
                 rerank_enabled: bool = True, enrich_enabled: bool = True):
        self.runs = runs
        self.career_modes = tuple(career_modes)
        self.budget = HarnessBudget(max_calls=max_calls)
        self.dry_run = dry_run
        self.customer_uuid = customer_uuid
        self.allow_unscoped = allow_unscoped
        self.rerank_enabled = rerank_enabled
        self.enrich_enabled = enrich_enabled
        self._last_intent: dict = {}
        self._last_profile: dict = {}

    def plan(self, personas) -> list:
        """
        The cells a run would execute, without executing any.

        Returned as ``CellResult`` objects with ``skipped_reason`` already filled in where
        a cell cannot run, so a dry run reports exactly the shape a real run would.
        """
        cells = []
        for run_index in range(1, self.runs + 1):
            for persona in personas:
                for career_mode in self.career_modes:
                    cell = CellResult(
                        persona_id=persona.id, career_mode=career_mode, run_index=run_index,
                    )
                    cell.skipped_reason = self.skip_reason(persona, career_mode)
                    cells.append(cell)
        return cells

    @staticmethod
    def skip_reason(persona, career_mode: str) -> str:
        """
        Why this cell cannot run, or an empty string.

        The oracle mode needs an expected career to force; a persona without one has
        nothing to be an oracle about, and running it as though it did would quietly make
        it a second auto-mode run.
        """
        if career_mode == CAREER_MODE_ORACLE and not persona.expected_careers:
            return 'no expected career to force in oracle mode'
        return ''

    def run(self, personas) -> dict:
        """
        Execute the persona set and return the traces.

        Returns ``cells``, ``calls_made``, ``budget_exhausted`` and ``personas_completed``.
        A cell that fails is recorded and the run continues: one persona's broken
        dependency should not cost the other seven.
        """
        cells = []
        budget_exhausted = False
        completed_personas = set()

        for cell in self.plan(personas):
            persona = next(p for p in personas if p.id == cell.persona_id)

            if cell.skipped_reason:
                cells.append(cell)
                continue

            if self.dry_run:
                cell.skipped_reason = 'dry run'
                cells.append(cell)
                continue

            if not self.budget.can_afford():
                # Stop starting new cells, but keep the ones already recorded.
                budget_exhausted = True
                cell.skipped_reason = 'max calls reached'
                cells.append(cell)
                continue

            self.budget.charge()
            self.execute_cell(cell, persona)
            cells.append(cell)
            if cell.ran:
                completed_personas.add(cell.persona_id)

        return {
            'cells': cells,
            'calls_made': self.budget.calls_made,
            'budget_exhausted': budget_exhausted,
            'personas_completed': len(completed_personas),
            'personas_total': len(personas),
        }

    def execute_cell(self, cell: CellResult, persona) -> None:
        """Run one persona in one career mode, recording the outcome on ``cell``."""
        try:
            career = self.resolve_career(cell, persona)
            if career is None:
                return
            self.assemble(cell, career)
        except UnitOfWorkException as exc:
            cell.error = f'{type(exc).__name__}: {exc}'
            logger.warning(
                'Harness cell failed (persona=%s mode=%s run=%d): %s',
                cell.persona_id, cell.career_mode, cell.run_index, exc,
            )

    def resolve_career(self, cell: CellResult, persona):
        """
        Pick the career for this cell, running discovery when the mode calls for it.

        Oracle mode still runs discovery -- the ``auto`` versus ``oracle`` delta is only
        meaningful if both arms paid the same intake cost, and the discovery trace is also
        what says whether the expected career was retrievable at all.
        """
        workflow = CareerDiscoveryWorkflow.objects.create(
            input_data=CareerDiscoveryWorkflow.generate_input_dict(persona.inputs),
        )
        workflow.execute()
        cell.career_workflow_uuid = workflow.uuid
        # Stashed so ``assemble`` can use the derived skills without re-reading the trace.
        self._last_intent = self.intent_from_workflow(workflow)
        # The persona's intake *is* the learner profile the rationale prompt expects.
        self._last_profile = dict(persona.inputs or {})

        candidates = workflow.career_candidates()

        if cell.career_mode == CAREER_MODE_ORACLE:
            expected = {career.external_id for career in persona.expected_careers}
            match = next(
                (c for c in candidates if c.get('external_id') in expected), None,
            )
            if match is None:
                # The expected career was not retrieved at all. Recorded as a skip rather
                # than fabricated, because forcing a career the pipeline cannot find would
                # measure a pathway no learner could ever reach.
                cell.skipped_reason = 'expected career not present in retrieved candidates'
                return None
            return match

        if not candidates:
            cell.skipped_reason = 'career discovery returned no candidates'
            return None
        return candidates[0]

    def assemble(self, cell: CellResult, career) -> None:
        """Run pathway assembly for the chosen career."""
        cell.career_name = career.get('name') or ''
        cell.career_external_id = career.get('external_id') or ''

        intent = self._last_intent or {}
        workflow = PathwayAssemblyWorkflow.objects.create(
            input_data=PathwayAssemblyWorkflow.generate_input_dict(
                career_name=cell.career_name,
                career_skills=self.career_skill_names(career),
                skills_required=intent.get('skills_required', []),
                skills_preferred=intent.get('skills_preferred', []),
                customer_uuid=self.customer_uuid,
                allow_unscoped=self.allow_unscoped,
                rerank_enabled=self.rerank_enabled,
                enrich_enabled=self.enrich_enabled,
                learner_profile=self._last_profile,
            ),
        )
        workflow.execute()
        cell.pathway_workflow_uuid = workflow.uuid

        output = (workflow.output_data or {}).get('assemble_pathway_output') or {}
        cell.course_keys = [course.get('key') for course in output.get('courses') or []]
        cell.complete = bool(output.get('complete'))
        cell.violations = list(output.get('violations') or [])
        cell.unfilled_rungs = list(output.get('unfilled_rungs') or [])

        # Recorded separately from the pathway: a pathway that shipped unexplained is a
        # different (and much milder) problem than one that failed to assemble.
        enrichment = (workflow.output_data or {}).get('enrich_rationale_output') or {}
        cell.rationale_count = len(enrichment.get('reasons') or {})
        cell.enrichment_error = enrichment.get('error') or ''

    @staticmethod
    def career_skill_names(career) -> list:
        """
        Read skill names off a career candidate.

        The candidate's ``skills`` are already flattened to names by
        ``career_candidate_from_hit``, but a raw hit carries dicts -- both shapes are
        accepted so a caller can pass either without a conversion step.
        """
        names = []
        for skill in career.get('skills') or []:
            if isinstance(skill, dict):
                name = skill.get('name')
            else:
                name = skill
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        return list(dict.fromkeys(names))

    @staticmethod
    def intent_from_workflow(workflow) -> dict:
        """
        The derived skills, read off the discovery workflow's persisted output.

        Reading the trace rather than re-deriving costs nothing and cannot disagree with
        what the pipeline actually used. Empty when unavailable: the pathway workflow
        treats intent skills as additive to the career's own, so a missing intent narrows
        the query rather than breaking it.
        """
        output = (workflow.output_data or {}).get(ExtractIntentOutput.KEY) or {}
        return {
            'skills_required': list(output.get('skills_required') or []),
            'skills_preferred': list(output.get('skills_preferred') or []),
        }
