# Career discovery workflow

`POST /api/v1/learner-pathways/careers/` — learner intake in, career candidates out, as a
persisted two-step workflow. Code: `enterprise_access/apps/pathways/` (steps, workflow,
domain API) and `enterprise_access/apps/api/v1/views/pathways.py` (endpoint).

```
intake (4 fields) -> ExtractIntentStep (Xpert, learner_intent prompt)
                  -> RetrieveCareersStep (Algolia jobs index)
                  -> [{external_id, name, skills, industries}]
```

Gated on `LEARNER_PATHWAYS_SERVER_PIPELINE_ENABLED` (default `False` → 404) and on the
existing `LEARNER_PATHWAYS_LEARNER_ROLE` via a new
`LEARNER_PATHWAYS_CAREER_DISCOVERY_PERMISSION`.

## Why a workflow rather than a view function

The step records *are* the trace. Each carries its own input, output, timing and failure,
so diagnosing a bad run is a query rather than a reproduction, and re-executing a workflow
skips the steps that already succeeded. That is also why nothing needed a bespoke tracing
layer for the evaluation harness — the response returns `workflow_uuid` and the harness
reads the records.

`CareerDiscoveryWorkflow` subclasses `AbstractConditionalWorkflow`, not
`AbstractWorkflow`. Neither of its steps defines `should_execute` today, so execution is
identical; the base is there because the pathway workflows that extend this pipeline do
have steps that opt out, and because its `Optional`-typed dynamic IO classes are what let
a skipped step round-trip as `null`.

## Skills are boosts, industries are hard filters

Ported from the MFE's `careerRetrieval.ts`, and the asymmetry is the load-bearing part:

| Signal | Algolia parameter | Why |
| --- | --- | --- |
| Required skills (max 4) | `optionalFilters`, unscored | An unmatched *hard* skill filter returns zero hits and says nothing about why |
| Preferred skills (max 2) | `optionalFilters`, `<score=1>` | Weaker signal, so a weaker boost |
| Industries, job sources | `filters` | Caller is expected to have grounded these against the index already |

Compound artifacts (`"SQL & Python"`, `"Excel + Tableau"`) are dropped before filtering —
they match nothing and spend a filter slot.

**The intake's `interested_industries` is deliberately *not* piped into the hard filter.**
It is learner free text ("healthcare, technology"), and a hard filter on a value that is
not a facet value returns zero hits silently. Free text belongs in the text query, where
partial matching applies. `RetrieveCareersInput.industries` exists for a caller that has
grounded real facet values first, and the endpoint leaves it empty.

## Careers are identified by `external_id`, and carry no match percentage

`external_id` is the Lightcast job id (`ET` + 16 hex, e.g. `ETE78CD2CDFFFAC66B`). Taxonomy
names are neither unique nor stable, so a name cannot be a key — and the evaluation
personas record expected careers as `external_id`s for the same reason. A hit missing
either its `external_id` or its name is dropped rather than given a placeholder: a
fabricated id would corrupt the harness's ground-truth comparison.

There is no match-percentage field anywhere in the pipeline or the response. The
client-side POC hardcoded `0.95` on every card; the MFE removed it deliberately, because
no verified compatible domain value exists.

## What the step output records, and why

`RetrieveCareersOutput` persists `query` and `hit_count` alongside the careers. A full
result set is not evidence that retrieval worked — relaxing a query buys volume, not
relevance — so a report needs both numbers to tell a real retrieval from a padded one
without re-running the search.

## Gotchas found while building

* **A per-action `throttle_scope` needs a class-level sentinel.** DRF's `as_view()`
  rejects any `@action` initkwarg that is not also an attribute on the viewset class, so
  `throttle_scope: str | None = None` on the class is load-bearing, not decoration. Without
  it the router raises `TypeError` at import time and every URL in the service fails to
  resolve.
* **Two viewsets can share a router prefix.** `learner-pathways` is registered twice, with
  different basenames, so the careers endpoint sits beside the prompt endpoints without
  touching them. Neither viewset has a `list` route, so nothing collides.
* **The Xpert conversation ID is keyed on the step record, not the request ID.** A step can
  be re-executed outside the request that created it, and the step UUID is the one
  identifier that ties an Xpert conversation back to a persisted trace either way.
* **Step tables persist learner-authored free text** (the intake), with no user
  identifier. They are annotated `.. no_pii:` on that basis; if a user linkage is ever
  added, the retirement pipeline has to be part of that change.
