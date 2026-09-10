0037 Moving the learner pathway pipeline server-side
*****************************************************

Status
======
**In progress** (September 2026)

Context
=======
Learner pathway generation — turning a learner's stated goals into an ordered set of
recommended courses — currently runs in the ``frontend-app-learner-portal-enterprise``
MFE. The MFE calls Xpert to derive intent, searches the Lightcast jobs index for careers,
searches the enterprise catalog index for courses, and assembles a recommendation, all
client-side.

That placement has four consequences we want to remove:

* **No trace.** A bad recommendation cannot be diagnosed after the fact. There is no
  record of what was asked of Xpert, what the index returned, or which stage went wrong.
* **No evaluation.** Quality cannot be measured, so it cannot be improved deliberately.
  The pipeline's output has never been scored against expert judgement.
* **Prompts outside versioning.** One Xpert call (the skill-translation refinement) has no
  server-side home at all, so it sits outside prompt versioning and rate limiting.
* **Retrieval papered over with a ladder.** The MFE tries four progressively broader
  queries and stops at the first that returns anything. The ladder exists because the
  first query is too narrow, and it hides *why* a result set was thin.

Before building, we measured the pipeline against the production indexes. Five findings
shaped every decision below, and each is recorded with its evidence in
``docs/references/algolia_search.md``:

1. **Both Algolia indexes AND every query word**, with no ``removeWordsIfNoResults``
   configured. An eight-word query returns *zero* hits rather than poor ones. On the jobs
   index a single common word ("Become") is enough to return nothing.
2. **Retrieval, not ranking, is the binding constraint.** Best measured recall@20 against
   expert-authored ground truth was 23% overall and **0% for every technology persona**.
3. **The catalog's skill vocabulary is Lightcast-canonical.** ``Python`` does not exist as
   a facet value; ``Python (Programming Language)`` does. Exact-match grounding therefore
   drops the most in-demand technical skills silently.
4. **31% of courses carry no skill tags at all** (1,272 of 4,094), making them
   structurally unreachable by any skill-facet query.
5. **Relevance ranking is heavily introductory at rank 5 and recovers by rank 20.** A
   ``data analyst`` query returns five introductory courses in its top five and 16/3/1
   across introductory/intermediate/advanced in its top twenty.

Decision
========
We decided to move the pipeline into ``enterprise-access`` and build it on the existing
``enterprise_access.apps.workflow`` pattern (ADR 0025) rather than a new framework, as two
workflows behind two endpoints.

**Why the existing workflow pattern.** Step records already persist ``input_data``,
``output_data``, ``succeeded_at``, ``failed_at`` and ``exception_message`` per execution.
That is exactly the trace the evaluation harness needed, so the harness reads step records
instead of running a parallel implementation — which means we measure the real pipeline
rather than something that resembles it. This was the single strongest argument, and it
made the harness substantially smaller than planned.

Two workflows, because the learner's career selection splits the flow:

.. mermaid::

   flowchart TB
       subgraph CD["CareerDiscoveryWorkflow — POST /learner-pathways/careers/"]
           direction TB
           EI["ExtractIntentStep<br/><i>Xpert: learner_intent prompt</i>"]
           RC["RetrieveCareersStep<br/><i>Lightcast jobs index</i>"]
           EI --> RC
       end

       LEARNER(["Learner chooses a career"])

       subgraph PA["PathwayAssemblyWorkflow — POST /learner-pathways/pathway/"]
           direction TB
           SF["SnapshotCatalogFacetsStep<br/><i>one zero-hit search</i>"]
           TC["TranslateToCatalogStep<br/><i>skill vocabulary resolution</i>"]
           RCa["RetrieveCandidatesStep<br/><i>~20 candidates, one broad query</i>"]
           RR["RerankCandidatesStep<br/><i>topical relevance only</i>"]
           AP["AssemblePathwayStep<br/><i>5 courses, deterministic</i>"]
           ER["EnrichRationaleStep<br/><i>Xpert: recommendations_feedback</i>"]
           SF --> TC --> RCa --> RR --> AP --> ER
       end

       CD --> LEARNER --> PA

       classDef conditional stroke-dasharray: 5 5
       class TC,RR,ER conditional

Steps drawn with a dashed border are **conditional** — they decide at run time that they
have nothing to do. That capability does not exist in ``apps/workflow``, so we added
``AbstractConditionalWorkflow`` locally in the pathways app (see *Divergences* below).

Six decisions inside that shape are worth recording, because each was driven by a
measurement rather than a preference.

**1. One broad query, then curate — replacing the retrieval ladder.**
``RetrieveCandidatesStep`` issues a single search with ``removeWordsIfNoResults:
allOptional`` and retrieves **20** candidates, not 5. The ladder existed to compensate for
a too-narrow query; widening once and selecting afterwards addresses the cause. Because
relaxation buys *volume* rather than relevance, the step persists the query and hit count
so a report can tell a good retrieval from a padded one without re-running anything.

**2. Skill terms are resolved against the catalog's own vocabulary, not matched exactly.**
``TranslateToCatalogStep`` reads the scoped catalog's facet values and resolves each term
onto them, recovering ``Python`` → ``Python (Programming Language)`` without a
hand-maintained alias map or a model call. High-confidence matches become hard filters;
weaker containment matches become boosts, because a wrong hard filter returns nothing.

The facet snapshot is capped by Algolia at 1,000 values and the live skill facets return
exactly 1,000 — i.e. they are truncated. A snapshot-only design therefore loses the long
tail silently, which is disproportionately the non-technology vocabulary. So a
**conditional** second pass over Algolia's facet-search endpoint recovers what the
snapshot could not serve, at one request per unresolved term. Facet-search results are
re-validated under the same rules, which is what stops ``AWS`` resolving to "AWS Certified
Solutions Architect Associate" merely because that is the highest-count candidate.

**3. Pathway structure is deterministic; only topical relevance goes to a model.**
A pathway is five courses, ordered roughly by difficulty, with no duplicates. Product
reported the defect that motivated this: *"you'll get 2 intro courses from different
providers, so 2 101 courses but zero 102 courses."*

That is a **selection** defect, not a content gap — the intermediate courses exist, they
sit below the rank-5 cut. ``AssemblePathwayStep`` therefore selects five from the twenty
under a level quota (2 introductory / 2 intermediate / 1 advanced) and a cap of two
courses per provider. It is pure computation, so it is directly testable, and none of it
is delegated to a model: asking a model to reproduce arithmetic invites a disagreement
someone then has to adjudicate.

Two non-obvious properties came out of live measurement:

* **The scarcest rung claims provider capacity first.** A single relevance-ordered pass
  lets the most plentiful rung spend the scarce resource: ``Data Analyst`` returned 17
  candidates spanning all three rungs and still assembled to 5/0/0, because the two
  introductory picks used one provider's entire allowance and every intermediate candidate
  belonged to that provider. Filling rungs in ascending order of availability fixed it.
* **A strict skill filter can make the level mix worse**, by narrowing the window before
  assembly can span it. When the strict set is thin or sits on a single rung, a second
  unfiltered search runs and its hits are **appended, not substituted** — the precise
  courses keep their rank and assembly gets the width it needs.

**4. Rationale generation is its own step, reusing the live prompt.**
``EnrichRationaleStep`` reuses the existing ``recommendations_feedback`` prompt read-only,
exactly as ``ExtractIntentStep`` reuses ``learner_intent``. It runs on the delivered five
rather than the candidate twenty. We initially folded rationales into the re-rank response
and reversed that: it would have taken learner-facing wording from a prompt chosen for
*ordering*, and paid to explain four courses for every one that ships.

**5. Model access goes through an adapter, selected by configuration.**
``apps/pathways/model_backends/`` presents one interface over Xpert and a direct
reasoning-model call, both returning content, token counts and elapsed milliseconds. Model
comparison is therefore a query over persisted step records rather than a bespoke rig.
Callers ask for a backend by configuration, never by import, so switching for a comparison
run needs no deploy. Unreported token counts are ``None`` rather than ``0``: zero is a
measurement, and a cost report that silently treats unknown as free is worse than one that
says it does not know.

**6. Quality has a written bar, in three tiers, committed before the next run.**
A single aggregate recall number is the wrong shape for a ship decision here, for reasons
that are properties of this evaluation rather than opinions: eight scoreable personas
quantise any aggregate at 12.5 percentage points; averaging hides a measured 0%-vs-40%
technology gap that an aggregate bar of 30% could be met *around*; and recall measures
agreement with the ground-truth author, not learner value.

So: **Tier 1** gates correctness (exactly five courses or none, valid keys, no duplicates,
in the pinned catalog, English, no more than two per provider) — these are bugs, not
quality judgements, and a run that fails them is not scored at all. **Tier 2** is the ship
bar, as per-persona pass/fail plus a passing count and a no-split-scores-zero rule.
**Tier 3** is tracked and never gating. Level mix is deliberately Tier 3: a rung can be
genuinely empty in the catalog, and ``level_type`` disagrees with course titles in 19–36%
of cases, so a gate on it would measure the metadata's noise.

Divergences from ADR 0025's pattern
===================================
**Conditional execution, added locally.** ``AbstractWorkflow.process_input`` iterates
``self.steps`` unconditionally. Three steps in this pipeline need to opt out at run time,
so ``AbstractConditionalWorkflow`` in the pathways app adds a ``should_execute``
classmethod defaulting to ``True``. It lives here rather than in ``apps/workflow`` because
provisioning is live and it must be impossible for this feature to alter its behaviour. If
it earns its place, upstreaming it is a later conversation with that code's owner.

This turned out to require more than the anticipated few lines. The parent builds its
generated input/output classes with ``field(type=step_class.output_class, default=None)``
— the default is ``None`` but the declared type is not ``Optional``, so ``cattrs`` emits
unconditional dereferences. That is safe while every step runs and fatal once one can
skip, so ``input_class`` and ``output_class`` are overridden to declare
``Optional[...]``. A test pins that a skipped step's output round-trips as null.

**The retrieval ladder was not given framework support.** "Try N strategies in order, stop
at the first satisfying a predicate" would turn the step list into a step tree, changing
the generated IO classes, the ``preceding_step_uuid`` linkage and the accumulated-output
threading. The ladder only existed because retrieval was too narrow; decision 1 above
removes the need for it, so building for it would have meant optimising something the fix
deletes.

**Latency is unchanged and unbudgeted.** Steps run inline and synchronously, so a
learner-facing pathway request costs the same wall clock as today's client-side flow.
ADR 0025 notes async-via-Celery is envisioned but unbuilt. The endpoints are behind a
feature flag and unwired from any frontend, so this is not yet a user-facing concern — but
it needs a budget before it becomes one.

Consequences
============
* Every pathway generation leaves an inspectable per-step trace, queryable in Django admin
  and by the evaluation harness, without a separate tracing layer.
* Re-running a failed workflow skips already-succeeded steps, so a failed enrichment does
  not re-run intent extraction. During evaluation, where runs are counted in hundreds,
  that matters more than in production.
* Quality is measurable. ``run_pathway_harness`` produces traces and
  ``report_pathway_harness`` scores them into the three tiers, so a re-score never needs a
  re-run — which matters because a run costs money.
* The endpoints are gated by ``LEARNER_PATHWAYS_SERVER_PIPELINE_ENABLED`` (default
  ``False``, so they 404) and by an RBAC role. No MFE calls them; rollback is revoking
  access, not reverting behaviour.
* **The measured bar is not met.** 23% recall@20 and 0% on technology is far below
  anything worth putting in front of a learner. This ADR records the shape and the
  instrumentation; it does not claim the quality problem is solved. Two Tier 2 numbers
  remain unsigned by product, and the pipeline has not yet been run end to end against
  live personas.

Alternatives Considered
=======================
* **A new pipeline framework.** Rejected: ``apps/workflow`` already provides persisted,
  composable, resumable multi-step execution with two ADRs behind it and production usage
  in provisioning. Composition work here is writing steps, not writing a framework.
* **Keeping the pipeline in the MFE and adding client-side telemetry.** Rejected: it does
  not address prompt versioning, cannot make retrieval measurable against ground truth,
  and leaves the one unhosted Xpert call unhosted.
* **Asking the model to produce the whole pathway, structure included.** Rejected on the
  measurement in decision 3: the structural constraints are arithmetic over retrieved
  candidates and are cheaper, testable and more reliable as deterministic code. It also
  keeps the model's contribution isolated enough to be measured as a delta — a run with
  re-ranking disabled is a meaningful baseline, not a broken run.
* **Configurable composition** (``steps`` backed by an admin row, so reordering and A/B-ing
  is configuration rather than a deploy). Deferred, not rejected. It is the right shape for
  the model-comparison experiments, but ``input_class`` and ``output_class`` are cached
  properties derived from ``self.steps``, so stored ``input_data`` is only meaningful
  against the composition that produced it. That needs versioned composition rows and a
  stamp on each run, and it is worth doing once the pipeline shape settles.

References
==========
* ADR 0025 — abstract workflow pattern
* ADR 0028 — why attrs for workflow IO
* ``docs/references/algolia_search.md`` — the measured index behaviour behind the
  decisions above
* ``docs/references/career_discovery_workflow.md`` — the career-side query shape
