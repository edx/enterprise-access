"""
Models for the learner pathways pipeline: the conditional workflow base, and the
concrete career-discovery workflow built on it.

The pathway pipeline needs one thing the provisioning workflows do not: a step that can
decide at run time that it has nothing to do. Provisioning steps are unconditional — every
step of provisioning an enterprise has to happen — so ``AbstractWorkflow`` has no notion
of skipping.

``AbstractConditionalWorkflow`` adds exactly that, and nothing else. It is a subclass
rather than a change to ``apps/workflow/``: provisioning is live, and it must be
impossible for this feature to alter its behaviour. If the pattern proves out, upstreaming
it into ``apps/workflow/`` is a later conversation with that code's owner.
"""
import logging
from typing import Optional

from attrs import define, field, make_class, validators
from django.utils.functional import cached_property

from enterprise_access.apps.pathways import api as pathways_api
from enterprise_access.apps.pathways import catalog_translation, course_retrieval, pathway_assembly, reranking
from enterprise_access.apps.prompts import api as prompts_api
from enterprise_access.apps.prompts.api_client import XpertAPIError
from enterprise_access.apps.workflow.exceptions import UnitOfWorkException
from enterprise_access.apps.workflow.models import AbstractWorkflow, AbstractWorkflowStep
from enterprise_access.apps.workflow.serialization import BaseInputOutput
from enterprise_access.toggles import learner_pathways_candidate_rerank_enabled

logger = logging.getLogger(__name__)


class AbstractConditionalWorkflow(AbstractWorkflow):
    """
    An ``AbstractWorkflow`` whose steps may opt out of executing.

    A step class may define::

        @classmethod
        def should_execute(cls, accumulated_output, workflow):
            return ...

    Returning ``False`` skips the step: **no step record is created**, so a skipped step
    is distinguishable from one that ran and produced nothing. Subsequent steps still
    execute and still receive the accumulated output of the steps that did run, with the
    skipped step's output key left unset.

    A step that does not define ``should_execute`` always executes, so a workflow whose
    steps all omit it behaves exactly as ``AbstractWorkflow`` does.

    .. no_pii: This model has no PII
    """

    class Meta:
        abstract = True

    @cached_property
    def input_class(self):
        """
        As ``AbstractWorkflow.input_class``, but with each field typed ``Optional``.

        See ``output_class`` for why.
        """
        return self._make_io_class('Input', 'input_class')

    @cached_property
    def output_class(self):
        """
        As ``AbstractWorkflow.output_class``, but with each field typed ``Optional``.

        The parent builds this class with ``field(type=step_class.output_class,
        default=None)`` -- the default is ``None`` but the declared type is not optional.
        cattrs generates its (un)structure functions from the declared type, so it emits
        code that dereferences every field unconditionally. Under ``AbstractWorkflow``
        that is safe, because every step always runs and therefore every field is always
        populated. Once a step can be skipped, its field stays ``None`` and
        ``to_dict()`` raises ``AttributeError: 'NoneType' object has no attribute ...``
        while serialising the workflow's own output.

        Declaring the fields ``Optional`` makes cattrs emit None-tolerant code, so a
        skipped step round-trips as a ``null`` rather than crashing the run.
        """
        return self._make_io_class('Output', 'output_class')

    def _make_io_class(self, suffix, step_attribute_name):
        """
        Build the dynamic workflow input/output class for this workflow's step list.

        Mirrors the parent's use of ``attrs.make_class`` over the steps' ``KEY`` fields,
        differing only in declaring each field ``Optional``.
        """
        class_name = self.__class__.__name__ + suffix
        attributes = {}
        for step_class in self.steps:
            step_io_class = getattr(step_class, step_attribute_name)
            attributes[step_io_class.KEY] = field(
                type=Optional[step_io_class],
                default=None,
            )
        return make_class(class_name, attributes, bases=(BaseInputOutput,))

    @staticmethod
    def step_should_execute(workflow_step_class, accumulated_output, workflow):
        """
        Whether ``workflow_step_class`` should run, defaulting to ``True``.

        Kept as a separate method so the default-on behaviour is testable directly and so
        a subclass can change the convention without reimplementing the loop.
        """
        should_execute = getattr(workflow_step_class, 'should_execute', None)
        if should_execute is None:
            return True
        return bool(should_execute(accumulated_output, workflow))

    def process_input(self, accumulated_output=None, **kwargs):
        """
        Execute each step in order, skipping any whose ``should_execute`` returns ``False``.

        Mirrors ``AbstractWorkflow.process_input`` -- including get-or-create of step
        records, skipping steps that already succeeded, and accumulating output -- with
        the conditional check added before a step record is created.

        Returns:
          An instance of ``self.output_class`` accumulating each executed step's output.
        """
        if self.succeeded_at:
            logger.info(
                '%s (uuid=%s) already succeeded at %s, skipping re-execution',
                self.__class__.__name__, self.uuid, self.succeeded_at,
            )
            return None

        accumulated_output = accumulated_output or self.output_class()

        logger.info(
            'Starting conditional workflow %s (uuid=%s) with steps=%s',
            self.__class__.__name__, self.uuid,
            [step_class.__name__ for step_class in self.steps],
        )

        preceding_step_record = None
        for workflow_step_class in self.steps:
            if not self.step_should_execute(workflow_step_class, accumulated_output, self):
                logger.info(
                    'Workflow %s (uuid=%s): step %s opted out, no step record created',
                    self.__class__.__name__, self.uuid, workflow_step_class.__name__,
                )
                continue

            input_object = self.get_input_object_for_step_type(workflow_step_class)
            input_data = input_object.to_dict() if input_object else {}
            step_record_kwargs = {
                'workflow_record_uuid': self.uuid,
                'defaults': {
                    'input_data': input_data,
                }
            }
            if preceding_step_record:
                step_record_kwargs['defaults']['preceding_step_uuid'] = preceding_step_record.uuid

            step_record, created = workflow_step_class.objects.get_or_create(**step_record_kwargs)
            logger.info(
                'Workflow %s (uuid=%s): step %s record %s (created=%s, step_uuid=%s)',
                self.__class__.__name__, self.uuid, workflow_step_class.__name__,
                'created' if created else 'reused', created, step_record.uuid,
            )
            preceding_step_record = step_record

            if step_record.succeeded_at:
                logger.info(
                    'Workflow %s (uuid=%s): step %s (step_uuid=%s) already succeeded at %s, skipping',
                    self.__class__.__name__, self.uuid, workflow_step_class.__name__,
                    step_record.uuid, step_record.succeeded_at,
                )
                setattr(
                    accumulated_output,
                    workflow_step_class.output_class.KEY,
                    step_record.output_object,
                )
                continue

            step_output = step_record.execute(accumulated_output=accumulated_output)
            setattr(
                accumulated_output,
                workflow_step_class.output_class.KEY,
                step_output,
            )

        logger.info(
            'Completed conditional workflow %s (uuid=%s)',
            self.__class__.__name__, self.uuid,
        )
        return accumulated_output


#############################################################################
# Career discovery: the concrete workflow behind POST learner-pathways/careers/
#############################################################################

# Xpert conversation IDs are keyed on the *step record* rather than the request ID the
# prompt endpoints use. A step can be re-executed outside the request that created it
# (the runbook's remediation is re-running a workflow), and the step UUID is the one
# identifier that ties an Xpert conversation back to a persisted trace either way.
CONVERSATION_ID_PREFIX = 'enterprise-access:career-discovery'

# Course descriptions are truncated before persistence. A full description can run to
# several kilobytes of marketing HTML, and five of them in one step record turns a
# trace into a blob; the re-ranker only needs enough to judge topical fit.
CANDIDATE_DESCRIPTION_CHARS = 1200

_is_str = validators.instance_of(str)
_is_int = validators.instance_of(int)
_is_str_list = validators.deep_iterable(
    member_validator=validators.instance_of(str),
    iterable_validator=validators.instance_of(list),
)


@define
class ExtractIntentInput(BaseInputOutput):
    """
    The learner's intake, verbatim. Identical to ``LearningIntentRequestSerializer``'s
    four fields, because the same prompt consumes both.
    """
    KEY = 'extract_intent_input'

    selected_goals: str = field(validator=_is_str)
    free_text: str = field(validator=_is_str)
    known_context: str = field(validator=_is_str)
    interested_industries: str = field(validator=_is_str)


@define
class ExtractIntentOutput(BaseInputOutput):
    """
    What the learner's intake means, in the vocabulary the jobs index can be searched in.
    """
    KEY = 'extract_intent_output'

    skills_required: list[str] = field(factory=list, validator=_is_str_list)
    skills_preferred: list[str] = field(factory=list, validator=_is_str_list)
    condensed_algolia_query: str = field(default='', validator=_is_str)


@define
class RetrieveCareersInput(BaseInputOutput):
    """
    Hard-filter values for the jobs search.

    Both default to empty, and the careers endpoint leaves them that way. The intake's
    ``interested_industries`` is learner free text ("healthcare, technology"), and a hard
    filter on a value that is not a facet value returns zero hits *silently* -- the
    failure mode the Chunk 3 diagnostic measured. Free text belongs in the text query,
    where partial matching applies; these fields exist for a caller that has grounded
    real facet values first.
    """
    KEY = 'retrieve_careers_input'

    industries: list[str] = field(factory=list, validator=_is_str_list)
    job_sources: list[str] = field(factory=list, validator=_is_str_list)


@define
class CareerCandidate(BaseInputOutput):
    """
    One career from the Lightcast taxonomy.

    Identified by ``external_id``: taxonomy names are neither unique nor stable, so a
    name cannot be a key. Carries no match percentage -- see
    ``pathways.api.career_candidate_from_hit``.
    """
    KEY = 'career_candidate'

    external_id: str = field(validator=_is_str)
    name: str = field(validator=_is_str)
    skills: list[str] = field(factory=list, validator=_is_str_list)
    industries: list[str] = field(factory=list, validator=_is_str_list)


@define
class RetrieveCareersOutput(BaseInputOutput):
    """
    The careers retrieved, plus what was actually asked of the index.

    ``query`` and ``hit_count`` are persisted because a full result set is not evidence
    that retrieval worked: relaxing a query buys volume, not relevance. Recording both
    lets a report tell a good retrieval from a padded one without re-running the search.
    """
    KEY = 'retrieve_careers_output'

    careers: list[CareerCandidate] = field(factory=list)
    query: str = field(default='', validator=_is_str)
    hit_count: int = field(default=0, validator=_is_int)


class ExtractIntentStepException(UnitOfWorkException):
    """Raised when learning intent could not be derived from the learner's intake."""


class RetrieveCareersStepException(UnitOfWorkException):
    """Raised when the jobs-index search for careers could not be completed."""


class ExtractIntentStep(AbstractWorkflowStep):
    """
    Derives skills and a search query from the learner's intake, via Xpert.

    Reuses the existing ``learner_intent`` prompt read-only, so this step and the live
    ``learning-intent`` endpoint cannot drift apart.

    .. no_pii: Stores no user identifier. ``input_data`` holds the learner-authored intake
        text submitted with the request, which is not linked to a user record.
    """
    exception_class = ExtractIntentStepException
    input_class = ExtractIntentInput
    output_class = ExtractIntentOutput

    def process_input(self, accumulated_output=None, **kwargs):
        result_dict = pathways_api.derive_learning_intent(
            intake=self.input_object.to_dict(),
            conversation_id=f'{CONVERSATION_ID_PREFIX}:{self.uuid}',
        )
        return self.output_class.from_dict(result_dict)


class RetrieveCareersStep(AbstractWorkflowStep):
    """
    Searches the Lightcast jobs index for careers matching the derived intent.

    .. no_pii: This model has no PII
    """
    exception_class = RetrieveCareersStepException
    input_class = RetrieveCareersInput
    output_class = RetrieveCareersOutput

    def process_input(self, accumulated_output=None, **kwargs):
        intent_output = getattr(accumulated_output, ExtractIntentOutput.KEY, None)
        if intent_output is None:
            raise self.exception_class(
                f'{self.__class__.__name__} requires {ExtractIntentOutput.KEY} in the accumulated output.'
            )

        input_object = self.input_object
        result_dict = pathways_api.retrieve_careers(
            intent=intent_output.to_dict(),
            industries=input_object.industries,
            job_sources=input_object.job_sources,
        )
        return self.output_class.from_dict(result_dict)


class CareerDiscoveryWorkflow(AbstractConditionalWorkflow):
    """
    Intake in, career candidates out.

    Subclasses the conditional base rather than ``AbstractWorkflow`` because the pathway
    workflows that extend this pipeline (facet translation onward) do have steps that opt
    out. Neither step here defines ``should_execute``, so execution is identical to
    ``AbstractWorkflow``'s today.

    .. no_pii: Stores no user identifier. ``input_data`` holds the learner-authored intake
        text submitted with the request, which is not linked to a user record.
    """
    steps = [
        ExtractIntentStep,
        RetrieveCareersStep,
    ]

    @classmethod
    def generate_input_dict(cls, intake_data):
        """
        Build ``input_data`` for a workflow record from validated intake fields.

        ``RetrieveCareersInput`` is deliberately left empty; see its docstring for why the
        intake's free-text industries are not piped into a hard filter.
        """
        return {
            ExtractIntentInput.KEY: {
                field_name: intake_data[field_name]
                for field_name in ('selected_goals', 'free_text', 'known_context', 'interested_industries')
            },
            RetrieveCareersInput.KEY: {},
        }

    def career_candidates(self):
        """
        The retrieved careers, or an empty list if the retrieval step never succeeded.

        Reads the persisted output rather than an in-memory result so a completed run can
        be re-serialized later without re-executing anything.
        """
        careers_output = (self.output_data or {}).get(RetrieveCareersOutput.KEY) or {}
        return careers_output.get('careers') or []


# ---------------------------------------------------------------------------------------
# Chunk 7: catalog translation. These steps belong to the pathway-assembly workflow, which
# is built in Chunk 10; they are defined here so their tables and behaviour land with the
# translation work rather than with the workflow that composes them.
# ---------------------------------------------------------------------------------------


@define
class SnapshotCatalogFacetsInput(BaseInputOutput):
    """
    Nothing is needed to take a snapshot beyond the credential the step already has.

    Kept as an explicit empty class rather than reusing ``Empty`` so the step has its own
    ``KEY`` in the workflow's input/output classes.
    """
    KEY = 'snapshot_catalog_facets_input'

    allow_unscoped: bool = field(default=False, validator=validators.instance_of(bool))


@define
class SnapshotCatalogFacetsOutput(BaseInputOutput):
    """
    The catalog's skill and subject vocabulary, as it exists in the searched scope.

    ``truncated`` names any facet that came back at Algolia's 1,000-value ceiling and is
    therefore incomplete. It is persisted because it changes how the next step's result
    should be read: a term unresolved against a truncated snapshot may still exist in the
    catalog, and that is the difference between "not in this catalog" and "not in the
    first thousand values".
    """
    KEY = 'snapshot_catalog_facets_output'

    skill_names: list[str] = field(factory=list, validator=_is_str_list)
    subjects: list[str] = field(factory=list, validator=_is_str_list)
    truncated: list[str] = field(factory=list, validator=_is_str_list)

    def as_facet_snapshot(self):
        """
        Rebuild the mapping the translation functions expect.

        ``skills.name`` is folded into ``skill_names`` on the way in, so this class has one
        list rather than two; the resolver only needs to know which values exist, and
        keeping a dotted attribute name off an attrs field avoids a serialization edge.
        """
        return {'skill_names': list(self.skill_names), 'skills.name': []}


@define
class TranslateToCatalogInput(BaseInputOutput):
    """
    The skill terms to translate: the selected career's skills plus the derived intent's.

    Both sources are passed in rather than read from the accumulated output, so this step
    is usable by a caller that already knows which career was chosen -- the learner's
    choice sits between career discovery and pathway assembly.
    """
    KEY = 'translate_to_catalog_input'

    career_skills: list[str] = field(factory=list, validator=_is_str_list)
    skills_required: list[str] = field(factory=list, validator=_is_str_list)
    skills_preferred: list[str] = field(factory=list, validator=_is_str_list)
    allow_unscoped: bool = field(default=False, validator=validators.instance_of(bool))


@define
class SkillFilter(BaseInputOutput):
    """One resolved skill, and how confidently it was resolved."""
    KEY = 'skill_filter'

    term: str = field(validator=_is_str)
    catalog_value: str = field(validator=_is_str)
    catalog_field: str = field(validator=_is_str)
    match_type: str = field(validator=_is_str)


@define
class TranslateToCatalogOutput(BaseInputOutput):
    """
    Catalog-valid facet values, split by how they may be used.

    ``unresolved`` and ``resolution_rate`` are first-class output, not log lines: a
    dropped skill was previously invisible, and "how much of this career could the catalog
    even express?" is the question the retrieval diagnostic showed we most need answered.
    """
    KEY = 'translate_to_catalog_output'

    strict: list[SkillFilter] = field(factory=list)
    boost: list[SkillFilter] = field(factory=list)
    unresolved: list[str] = field(factory=list, validator=_is_str_list)
    resolution_rate: float | None = field(default=None)
    refined: bool = field(default=False, validator=validators.instance_of(bool))


class SnapshotCatalogFacetsStepException(UnitOfWorkException):
    """Raised when the catalog facet vocabulary could not be read."""


class TranslateToCatalogStepException(UnitOfWorkException):
    """Raised when career skills could not be translated into catalog facet values."""


class SnapshotCatalogFacetsStep(AbstractWorkflowStep):
    """
    Reads the scoped catalog's facet vocabulary.

    Separate from translation because it is one cheap request whose result is reusable,
    while translation is pure computation. Splitting them means a re-run of a failed
    translation does not re-fetch the snapshot.

    .. no_pii: This model has no PII
    """
    exception_class = SnapshotCatalogFacetsStepException
    input_class = SnapshotCatalogFacetsInput
    output_class = SnapshotCatalogFacetsOutput

    def process_input(self, accumulated_output=None, **kwargs):
        snapshot = catalog_translation.snapshot_catalog_facets(
            allow_unscoped=self.input_object.allow_unscoped,
        )
        # The two skill facets are merged: the resolver only cares which values exist, and
        # `skill_names` already takes precedence on a collision.
        skill_names = list(dict.fromkeys(
            (snapshot.get('skill_names') or []) + (snapshot.get('skills.name') or [])
        ))
        return self.output_class(
            skill_names=skill_names,
            subjects=snapshot.get('subjects') or [],
            truncated=snapshot.get('truncated') or [],
        )


class TranslateToCatalogStep(AbstractWorkflowStep):
    """
    Resolves career and intent skills onto real catalog facet values.

    Runs the cheap pure-resolution pass first, then -- **only if terms remain
    unresolved** -- a facet-search refinement that costs one request per term. That
    condition is why this pipeline needs ``AbstractConditionalWorkflow``: on the common
    path the refinement is skipped entirely, and whether it fired is recorded on the
    output so the harness can count how often the snapshot was insufficient.

    .. no_pii: This model has no PII
    """
    exception_class = TranslateToCatalogStepException
    input_class = TranslateToCatalogInput
    output_class = TranslateToCatalogOutput

    def process_input(self, accumulated_output=None, **kwargs):
        snapshot_output = getattr(accumulated_output, SnapshotCatalogFacetsOutput.KEY, None)
        if snapshot_output is None:
            raise self.exception_class(
                'Cannot translate skills without a catalog facet snapshot; '
                f'{SnapshotCatalogFacetsStep.__name__} must run first.'
            )

        facet_snapshot = snapshot_output.as_facet_snapshot()
        terms = list(dict.fromkeys(
            self.input_object.career_skills +
            self.input_object.skills_required +
            self.input_object.skills_preferred
        ))

        translation = catalog_translation.translate_skills(
            terms=terms,
            facet_snapshot=facet_snapshot,
        )

        refined = False
        if translation['unresolved']:
            refinement = catalog_translation.refine_unmatched_skills(
                unresolved=translation['unresolved'],
                facet_snapshot=facet_snapshot,
                allow_unscoped=self.input_object.allow_unscoped,
            )
            translation = catalog_translation.merge_refinement(translation, refinement)
            refined = True

        return self.output_class(
            strict=[SkillFilter.from_dict(entry) for entry in translation['strict']],
            boost=[SkillFilter.from_dict(entry) for entry in translation['boost']],
            unresolved=translation['unresolved'],
            resolution_rate=translation['resolution_rate'],
            refined=refined,
        )


# ---------------------------------------------------------------------------------------
# Chunks 8-10: course retrieval, re-ranking, and pathway assembly.
# ---------------------------------------------------------------------------------------

# Trace prefix for the re-rank model call. Keyed on the step record for the same reason
# career discovery's is -- a step can be re-executed outside the request that created it.
RERANK_TRACE_PREFIX = 'enterprise-access:pathway-rerank'
ENRICH_TRACE_PREFIX = 'enterprise-access:pathway-rationale'


@define
class RetrieveCandidatesInput(BaseInputOutput):
    """
    Which career to retrieve courses for, and the scope to retrieve them in.

    ``career_name`` rather than ``external_id``: the catalog index knows nothing about
    Lightcast identifiers, so the name is what can actually be searched. The id stays on
    the career-discovery output for attribution.
    """
    KEY = 'retrieve_candidates_input'

    career_name: str = field(default='', validator=_is_str)
    customer_uuid: str = field(default='', validator=_is_str)
    allow_unscoped: bool = field(default=False, validator=validators.instance_of(bool))


@define
class CourseCandidate(BaseInputOutput):
    """
    One retrieved course, carrying what the re-ranker and assembler need.

    Both descriptions are held because the re-ranker judges topical fit and a title alone
    is often ambiguous ("Foundations of Client Care 2" says little about its subject).
    """
    KEY = 'course_candidate'

    key: str = field(validator=_is_str)
    title: str = field(default='', validator=_is_str)
    short_description: str = field(default='', validator=_is_str)
    full_description: str = field(default='', validator=_is_str)
    level_type: str = field(default='', validator=_is_str)
    partner: str = field(default='', validator=_is_str)
    language: str = field(default='', validator=_is_str)

    @classmethod
    def from_hit(cls, hit):
        """Build a candidate from a raw catalog hit."""
        candidate = pathway_assembly.Candidate.from_hit(hit)
        return cls(
            key=candidate.key,
            title=candidate.title,
            short_description=(hit.get('short_description') or '')[:CANDIDATE_DESCRIPTION_CHARS],
            full_description=(hit.get('full_description') or '')[:CANDIDATE_DESCRIPTION_CHARS],
            level_type=candidate.level_type,
            partner=candidate.partner,
            language=candidate.language,
        )

    def to_assembly_hit(self):
        """Render back into the hit shape ``pathway_assembly`` consumes."""
        return {
            'key': self.key,
            'title': self.title,
            'level_type': self.level_type,
            'partners': [{'name': self.partner}] if self.partner else [],
            'language': self.language,
        }


@define
class RetrieveCandidatesOutput(BaseInputOutput):
    """
    The retrieved candidate window, plus what was asked of the index.

    ``zero_hits`` and ``broadened`` are first-class output because they are different
    diagnoses: nothing in this catalog for this career, versus skill filters that
    over-constrained a set that does exist. The original plan recorded a "scope-only
    fallback" here instead, which measures a ladder step this design removed.

    ``strict_hit_count`` is kept alongside ``hit_count`` so a report can say how much of
    the window was precisely matched rather than merely broadly matched.
    """
    KEY = 'retrieve_candidates_output'

    courses: list[CourseCandidate] = field(factory=list)
    query: str = field(default='', validator=_is_str)
    hit_count: int = field(default=0, validator=_is_int)
    strict_filters_applied: list[str] = field(factory=list, validator=_is_str_list)
    strict_hit_count: int = field(default=0, validator=_is_int)
    strict_rungs_spanned: int = field(default=0, validator=_is_int)
    broadened: bool = field(default=False, validator=validators.instance_of(bool))
    zero_hits: bool = field(default=True, validator=validators.instance_of(bool))


@define
class RerankCandidatesInput(BaseInputOutput):
    """
    What the re-ranker needs beyond the candidate set it reads from accumulated output.

    ``enabled`` exists so the model call can be turned off per run without a code change.
    Chunk 9a's deterministic assembly produces a valid pathway on its own, so a run with
    the model disabled is a meaningful baseline rather than a broken one -- and it is the
    A/B that says what the model is worth.
    """
    KEY = 'rerank_candidates_input'

    career_name: str = field(default='', validator=_is_str)
    enabled: bool = field(default=True, validator=validators.instance_of(bool))


@define
class RerankCandidatesOutput(BaseInputOutput):
    """
    The re-ranked key order, and what had to be discarded to trust it.

    ``fabricated_keys`` turns the platform's known key-invention defect into a counted
    metric rather than an anecdote. ``backend``/``model``/token counts come straight from
    ``ModelResponse.to_trace_dict``, so model comparison is a query over these records.
    """
    KEY = 'rerank_candidates_output'

    ordered_keys: list[str] = field(factory=list, validator=_is_str_list)
    rationales: dict = field(factory=dict)
    fabricated_keys: list[str] = field(factory=list, validator=_is_str_list)
    executed: bool = field(default=False, validator=validators.instance_of(bool))
    backend: str = field(default='', validator=_is_str)
    model: str = field(default='', validator=_is_str)
    prompt_revision: str = field(default='', validator=_is_str)
    input_tokens: int | None = field(default=None)
    output_tokens: int | None = field(default=None)
    elapsed_ms: int = field(default=0, validator=_is_int)


@define
class AssemblePathwayInput(BaseInputOutput):
    """Nothing beyond what the preceding steps produced."""
    KEY = 'assemble_pathway_input'


@define
class PathwayCourse(BaseInputOutput):
    """One course in a delivered pathway, in its taught order."""
    KEY = 'pathway_course'

    key: str = field(validator=_is_str)
    title: str = field(default='', validator=_is_str)
    level_type: str = field(default='', validator=_is_str)
    partner: str = field(default='', validator=_is_str)
    rationale: str = field(default='', validator=_is_str)


@define
class AssemblePathwayOutput(BaseInputOutput):
    """
    The delivered pathway, or an explicit absence of one.

    ``violations`` carries the Tier 1 gate results. They are persisted rather than raised
    because a pathway that fails a correctness gate is a bug worth *seeing* in a harness
    run -- raising would hide it behind a failed workflow with no comparable trace.
    """
    KEY = 'assemble_pathway_output'

    courses: list[PathwayCourse] = field(factory=list)
    complete: bool = field(default=False, validator=validators.instance_of(bool))
    unfilled_rungs: list[str] = field(factory=list, validator=_is_str_list)
    level_mix: dict = field(factory=dict)
    ineligible: dict = field(factory=dict)
    violations: list[str] = field(factory=list, validator=_is_str_list)


@define
class EnrichRationaleInput(BaseInputOutput):
    """
    What the rationale prompt needs beyond the assembled pathway.

    ``learner_profile`` is the learner's intake, passed through to the existing
    ``recommendations_feedback`` prompt in the shape that endpoint already sends.
    """
    KEY = 'enrich_rationale_input'

    selected_career: str = field(default='', validator=_is_str)
    learner_profile: dict = field(factory=dict)
    enabled: bool = field(default=True, validator=validators.instance_of(bool))


@define
class EnrichRationaleOutput(BaseInputOutput):
    """
    One rationale per delivered course, plus the prompt revision that produced them.

    A course with no rationale is normal, not a failure: it renders without one, which is
    better than failing the pathway or inventing an explanation.
    """
    KEY = 'enrich_rationale_output'

    reasons: dict = field(factory=dict)
    executed: bool = field(default=False, validator=validators.instance_of(bool))
    prompt_revision: str = field(default='', validator=_is_str)
    error: str = field(default='', validator=_is_str)


class RetrieveCandidatesStepException(UnitOfWorkException):
    """Raised when course candidates could not be retrieved."""


class RerankCandidatesStepException(UnitOfWorkException):
    """Raised when the candidate set could not be re-ranked."""


class AssemblePathwayStepException(UnitOfWorkException):
    """Raised when a pathway could not be assembled from the candidate set."""


class EnrichRationaleStepException(UnitOfWorkException):
    """Raised when per-course rationales could not be generated."""


class RetrieveCandidatesStep(AbstractWorkflowStep):
    """
    Retrieves the candidate window for the selected career.

    One broad query, per the Chunk 3 gate, rather than the POC's four-step ladder.

    .. no_pii: This model has no PII
    """
    exception_class = RetrieveCandidatesStepException
    input_class = RetrieveCandidatesInput
    output_class = RetrieveCandidatesOutput

    def process_input(self, accumulated_output=None, **kwargs):
        translation_output = getattr(accumulated_output, TranslateToCatalogOutput.KEY, None)
        if translation_output is None:
            raise self.exception_class(
                'Cannot retrieve candidates without a catalog translation; '
                f'{TranslateToCatalogStep.__name__} must run first.'
            )

        result = course_retrieval.retrieve_candidate_courses(
            career_name=self.input_object.career_name,
            translation=translation_output.to_dict(),
            customer_uuid=self.input_object.customer_uuid,
            allow_unscoped=self.input_object.allow_unscoped,
        )
        return self.output_class(
            courses=[CourseCandidate.from_hit(hit) for hit in result['courses']],
            query=result['query'],
            hit_count=result['hit_count'],
            strict_filters_applied=result['strict_filters_applied'],
            strict_hit_count=result['strict_hit_count'],
            strict_rungs_spanned=result['strict_rungs_spanned'],
            broadened=result['broadened'],
            zero_hits=result['zero_hits'],
        )


class RerankCandidatesStep(AbstractWorkflowStep):
    """
    Orders the candidate set for topical fit, via the configured model backend.

    Skipped when disabled or when there is nothing to re-rank -- which is what
    ``AbstractConditionalWorkflow`` is for. A skipped re-rank is not a failure: Chunk 9a's
    deterministic assembly produces a valid pathway from the unordered candidate set, so
    the model's contribution is measurable as a delta rather than assumed.

    Chunk 9a handles the structural guarantees (level spread, provider cap, duplicates)
    deterministically, so nothing here asks the model for them. What is asked is the one
    thing assembly demonstrably cannot do: keep topically unrelated courses out.

    .. no_pii: This model has no PII
    """
    exception_class = RerankCandidatesStepException
    input_class = RerankCandidatesInput
    output_class = RerankCandidatesOutput

    @classmethod
    def should_execute(cls, accumulated_output, workflow):
        """
        Run only when the switch is on, the caller asked for it, and there is something
        to order.

        The administrator switch is checked *first* and independently of the workflow's
        own ``enabled`` input, so turning it off stops paid model calls for every caller
        at once -- including harness runs, which supply their own input and would
        otherwise ignore it.
        """
        if not learner_pathways_candidate_rerank_enabled():
            return False
        rerank_input = (workflow.input_data or {}).get(RerankCandidatesInput.KEY) or {}
        if not rerank_input.get('enabled', True):
            return False
        candidates_output = getattr(accumulated_output, RetrieveCandidatesOutput.KEY, None)
        return bool(candidates_output and candidates_output.courses)

    def process_input(self, accumulated_output=None, **kwargs):
        candidates_output = getattr(accumulated_output, RetrieveCandidatesOutput.KEY, None)
        if candidates_output is None:
            raise self.exception_class(
                'Cannot re-rank without a candidate set; '
                f'{RetrieveCandidatesStep.__name__} must run first.'
            )

        result = reranking.rerank_candidates(
            career_name=self.input_object.career_name,
            candidates=[candidate.to_dict() for candidate in candidates_output.courses],
            trace_id=f'{RERANK_TRACE_PREFIX}:{self.uuid}',
        )
        return self.output_class(
            ordered_keys=result['ordered_keys'],
            rationales=result['rationales'],
            fabricated_keys=result['fabricated_keys'],
            executed=True,
            backend=result['trace'].get('backend', ''),
            model=result['trace'].get('model', ''),
            prompt_revision=result.get('prompt_revision', ''),
            input_tokens=result['trace'].get('input_tokens'),
            output_tokens=result['trace'].get('output_tokens'),
            elapsed_ms=result['trace'].get('elapsed_ms', 0),
        )


class AssemblePathwayStep(AbstractWorkflowStep):
    """
    Selects the delivered five courses and records the Tier 1 gate results.

    Reads the re-rank order when it ran and falls back to retrieval order when it did not,
    so a disabled or skipped model call still yields a pathway.

    .. no_pii: This model has no PII
    """
    exception_class = AssemblePathwayStepException
    input_class = AssemblePathwayInput
    output_class = AssemblePathwayOutput

    def process_input(self, accumulated_output=None, **kwargs):
        candidates_output = getattr(accumulated_output, RetrieveCandidatesOutput.KEY, None)
        if candidates_output is None:
            raise self.exception_class(
                'Cannot assemble a pathway without a candidate set; '
                f'{RetrieveCandidatesStep.__name__} must run first.'
            )

        rerank_output = getattr(accumulated_output, RerankCandidatesOutput.KEY, None)
        ordered = self.order_candidates(candidates_output.courses, rerank_output)
        rationales = dict(rerank_output.rationales) if rerank_output else {}

        assembly = pathway_assembly.assemble_pathway(
            [candidate.to_assembly_hit() for candidate in ordered]
        )
        violations = (
            pathway_assembly.validate_pathway(assembly.courses)
            if assembly.is_complete else []
        )

        return self.output_class(
            courses=[
                PathwayCourse(
                    key=course.key,
                    title=course.title,
                    level_type=course.level_type,
                    partner=course.partner,
                    rationale=rationales.get(course.key, ''),
                )
                for course in assembly.courses
            ],
            complete=assembly.is_complete,
            unfilled_rungs=assembly.unfilled_rungs,
            level_mix=assembly.realised_level_mix,
            ineligible=assembly.ineligible,
            violations=violations,
        )

    @staticmethod
    def order_candidates(candidates, rerank_output):
        """
        Apply the re-rank order when one exists, keeping unranked candidates behind it.

        Unranked candidates are appended rather than dropped: the model may return fewer
        keys than it was given, and discarding the remainder would shrink the window that
        assembly needs to span the rungs.
        """
        if not rerank_output or not rerank_output.ordered_keys:
            return list(candidates)

        by_key = {candidate.key: candidate for candidate in candidates}
        ordered = [by_key[key] for key in rerank_output.ordered_keys if key in by_key]
        ranked_keys = {candidate.key for candidate in ordered}
        return ordered + [
            candidate for candidate in candidates if candidate.key not in ranked_keys
        ]


class EnrichRationaleStep(AbstractWorkflowStep):
    """
    Generates one rationale per delivered course, via the stored feedback prompt.

    Runs **after** assembly and on the delivered five, not the candidate twenty. Two
    reasons, and both are why this is a separate step rather than a field on the re-rank
    response:

    * It reuses the existing ``recommendations_feedback`` prompt read-only, so the wording
      a learner sees cannot drift from the live MFE endpoint's, and it stays
      admin-editable and versioned. A rationale taken off the re-rank response would come
      from a prompt chosen for ordering, and under the Claude backend from one that is not
      in the database at all.
    * Explaining and ordering are different jobs with different failure modes. A bad
      rationale must not be able to reorder a pathway.

    Skipped when disabled or when there is no pathway to explain -- a third genuine
    consumer of ``AbstractConditionalWorkflow``. A failure is recorded on the output
    rather than raised: a pathway with no rationales is still a pathway, and losing the
    explanations is a much smaller loss than losing the recommendation.

    .. no_pii: Stores no user identifier. ``input_data`` holds the learner-authored intake
        text submitted with the request, which is not linked to a user record.
    """
    exception_class = EnrichRationaleStepException
    input_class = EnrichRationaleInput
    output_class = EnrichRationaleOutput

    @classmethod
    def should_execute(cls, accumulated_output, workflow):
        """Run only when enabled and a complete pathway exists to explain."""
        enrich_input = (workflow.input_data or {}).get(EnrichRationaleInput.KEY) or {}
        if not enrich_input.get('enabled', True):
            return False
        assembly_output = getattr(accumulated_output, AssemblePathwayOutput.KEY, None)
        return bool(assembly_output and assembly_output.complete)

    def process_input(self, accumulated_output=None, **kwargs):
        assembly_output = getattr(accumulated_output, AssemblePathwayOutput.KEY, None)
        if assembly_output is None:
            raise self.exception_class(
                'Cannot enrich rationales without an assembled pathway; '
                f'{AssemblePathwayStep.__name__} must run first.'
            )

        course_keys = [course.key for course in assembly_output.courses]
        try:
            result = pathways_api.enrich_rationales(
                selected_career=self.input_object.selected_career,
                course_keys=course_keys,
                learner_profile=self.input_object.learner_profile,
                conversation_id=f'{ENRICH_TRACE_PREFIX}:{self.uuid}',
            )
        except (prompts_api.PromptError, XpertAPIError) as exc:
            # Recorded, not raised. The pathway is already assembled and valid.
            logger.warning('Rationale enrichment failed (%s); pathway ships unexplained.',
                           type(exc).__name__)
            return self.output_class(executed=True, error=f'{type(exc).__name__}: {exc}')

        return self.output_class(
            reasons=result['reasons'],
            executed=True,
            prompt_revision=result['prompt_revision'],
        )


class PathwayAssemblyWorkflow(AbstractConditionalWorkflow):
    """
    Selected career in, five ordered courses out.

    The full pathway pipeline: read the catalog's vocabulary, translate the career's
    skills into it, retrieve a candidate window, optionally re-rank, then assemble.

    Two steps here are conditional, which is what this workflow needs the conditional
    base for: the facet-search refinement inside ``TranslateToCatalogStep``, and
    ``RerankCandidatesStep`` as a whole.

    .. no_pii: Stores no user identifier. ``input_data`` holds a career name and skill
        terms, which are not linked to a user record.
    """
    steps = [
        SnapshotCatalogFacetsStep,
        TranslateToCatalogStep,
        RetrieveCandidatesStep,
        RerankCandidatesStep,
        AssemblePathwayStep,
        EnrichRationaleStep,
    ]

    @classmethod
    def generate_input_dict(cls, *, career_name, career_skills=None, skills_required=None,
                            skills_preferred=None, customer_uuid='', allow_unscoped=False,
                            rerank_enabled=True, enrich_enabled=True, learner_profile=None):
        """Build ``input_data`` for a pathway run."""
        return {
            SnapshotCatalogFacetsInput.KEY: {'allow_unscoped': allow_unscoped},
            TranslateToCatalogInput.KEY: {
                'career_skills': list(career_skills or []),
                'skills_required': list(skills_required or []),
                'skills_preferred': list(skills_preferred or []),
                'allow_unscoped': allow_unscoped,
            },
            RetrieveCandidatesInput.KEY: {
                'career_name': career_name,
                'customer_uuid': customer_uuid,
                'allow_unscoped': allow_unscoped,
            },
            RerankCandidatesInput.KEY: {
                'career_name': career_name,
                'enabled': rerank_enabled,
            },
            AssemblePathwayInput.KEY: {},
            EnrichRationaleInput.KEY: {
                'selected_career': career_name,
                'learner_profile': dict(learner_profile or {}),
                'enabled': enrich_enabled,
            },
        }

    def pathway(self):
        """
        The assembled pathway as a plain dict, or ``None`` if assembly never succeeded.

        Reads persisted output so a completed run can be re-serialized without
        re-executing anything, and folds in the rationales the enrichment step produced.
        Merging here rather than in ``AssemblePathwayStep`` keeps the two steps
        independent: assembly cannot depend on a step that runs after it, and a skipped or
        failed enrichment leaves the pathway intact with empty rationales.
        """
        output = (self.output_data or {}).get(AssemblePathwayOutput.KEY)
        if not output or not output.get('complete'):
            return None

        enrichment = (self.output_data or {}).get(EnrichRationaleOutput.KEY) or {}
        reasons = enrichment.get('reasons') or {}
        if not reasons:
            return output

        merged = dict(output)
        merged['courses'] = [
            {**course, 'rationale': reasons.get(course.get('key'), course.get('rationale', ''))}
            for course in output.get('courses') or []
        ]
        return merged
