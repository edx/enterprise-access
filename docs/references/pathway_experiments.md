# Pathway experiments: size variants and the judge

Two opt-in steps on `PathwayAssemblyWorkflow` that run **beside** the delivered pathway and
never change it. Code: `enterprise_access/apps/pathways/pathway_variants.py`, `judging.py`,
and `BuildVariantsStep` / `JudgePathwaysStep` in `models.py`.

```
... -> RerankCandidatesStep -> AssemblePathwayStep      (delivered: exactly 5, 2/2/1 quota)
                            -> BuildVariantsStep        (opt-in: other sizes, three strategies)
                            -> JudgePathwaysStep        (opt-in: scores delivered + variants)
                            -> EnrichRationaleStep      (delivered pathway only)
```

Both steps skip unless asked for, leaving no step record and a null output, so a run that
requests neither behaves exactly as before.

## Why

Product relaxed the definition of a pathway on 2026-09-23 to **two to five courses at any
level**. The delivered pathway still follows Decision 3 (exactly five, 2/2/1). The
September analysis also showed that the obvious implementation of the relaxed rule does not
produce it: a model told to "pick the 5" returned five courses 96% of the time, including
when a judge rated two or fewer of them on topic. How to build a shorter pathway is still an
open question, so these steps run candidate answers side by side on the same candidates.

## The three strategies

| Strategy | What it does | Paid calls |
| --- | --- | --- |
| `ranked_cut` | Cuts the re-rank order at each size. Sizes nest. | 0 |
| `model_pick` | Asks the model for exactly N courses, once per size | 1 per size |
| `model_sized` | Asks the model for 2–5 genuinely relevant courses; the model picks the length | 1 |

All three start from the delivered pathway's candidate window and relevance order. All
three enforce its eligibility rules and provider cap in code, whatever a model returns.
Levels are not constrained; the realised mix is recorded. Nothing is padded: a variant that
comes back short is recorded short, with `complete: false` and the reason in `dropped` or
`fabricated_keys`.

The two model arms share one prompt and differ only in their size sentence, so a difference
between them comes from the size rule alone. They run on the re-rank's backend and model by
default (`PATHWAYS_VARIANT_BACKEND` / `PATHWAYS_VARIANT_MODEL` override), so they differ from
`ranked_cut` in method, not in model.

## The judge

This is the September analysis's rubric, verbatim (`prompts.PATHWAY_JUDGE_SYSTEM_PROMPT`),
on its calibrated model (`PATHWAYS_JUDGE_MODEL`, default `gpt-5.4-mini`) at temperature 0.
It returns `good` / `weak` / `bad`, a reason, and a per-course on-topic flag. It **records
only**: nothing reads a verdict to decide what to deliver. That keeps its scores valid as
measurement, because a judge that also chose the pathway would be grading its own choice.

- **Pinned independently.** The judge's model is pinned separately from the model that builds
  pathways, so changing the builder never changes the ruler.
- **Identical course lists are judged once.** A later identical list reuses the verdict and
  names the original in `same_as`.
- **Failures are recorded, not raised.** A failed judgement carries `error` and no verdict.
- **Editing the rubric starts a new instrument.** Results from before and after an edit
  cannot be compared, so add a new constant rather than changing the existing one.

Reading the verdicts:

- **The verdict partly rewards coverage.** A short pathway whose courses are all on topic
  can still be rated `weak` for what it leaves out ("missing core analyst tools like SQL").
  When comparing sizes, read the verdict together with `n_on_topic / n_courses`: the verdict
  says whether the pathway is enough, and the ratio says whether it is padded.
- **A single verdict is noisy.** The analysis measured 8.6% self-disagreement, so compare
  arms over many careers, not a few.
- **Not quite the analysis's instrument.** The analysis judged career *families* and enforced
  its schema strictly. Here the career name stands in for the family's titles, and JSON mode
  carries the schema in the prompt. Check agreement on a sample before pooling with the
  analysis's figures.

## Running it

### API

`POST /api/v1/learner-pathways/pathway/` accepts three optional fields:

- `variant_sizes`: sizes from 2 to 5
- `variant_strategies`: any of the three above
- `judge`: true or false

Sizes alone run `ranked_cut`; strategies alone run every size from 2 to 5. The response then
adds `variants` (each with its `judgement`) and `judgement` (the delivered pathway's).

These fields are gated by the WaffleSwitch
`enterprise_access.learner_pathways_pathway_experiments`, off by default. With it off, a
request carrying them gets HTTP 400 rather than having them silently ignored.

### Batch collection (any careers)

```bash
./manage.py collect_pathway_variants \
    --careers-file careers.txt \
    --variant-strategy ranked_cut --variant-strategy model_pick --variant-strategy model_sized \
    --judge --unscoped --max-calls 500 \
    --output-json variants.json --output-csv variants.csv
```

- **Careers** are looked up by exact name through the jobs index's `name` facet. A text search
  misses the bare title: "Data Analyst" is not in the top twenty text hits. `--careers-file`
  takes one name per line, or a CSV with a `career_name`, `career`, `name`, `label` or
  `career family` column.
- **Spend is bounded.** `--dry-run` prints the plan and its cost bound. `--max-calls` is
  checked before each career, against the most it can cost, and only careers that reach the
  workflow are charged.
- **One career with all three strategies, every size and the judge costs at most 16 calls:**
  re-rank 1, `model_pick` 4, `model_sized` 1, and up to 10 judgements.
- **The CSV** has one row per pathway, with the delivered pathway first, so every variant sits
  beside its baseline.
- **For long runs, pass `--checkpoint runs.jsonl`.** Each career is appended the moment it
  finishes, and `--resume` then carries completed careers over, neither re-running nor
  re-charging them. Errors and budget skips are retried. The first real collection hung 40
  minutes in, on a DNS lookup no socket timeout covers, which is why this exists.

### Persona harness

`run_pathway_harness` takes the same `--variant-size`, `--variant-strategy` and `--judge`
flags. Its `--max-calls` still counts workflow executions. It prints the extra model calls
per cell separately.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `PATHWAYS_VARIANT_BACKEND` | `''` (same as `PATHWAYS_MODEL_BACKEND`) | Backend for the model arms: `openai` or `claude` |
| `PATHWAYS_VARIANT_MODEL` | `''` (the backend's default) | Model for the model arms |
| `PATHWAYS_JUDGE_BACKEND` | `openai` | Backend for the judge |
| `PATHWAYS_JUDGE_MODEL` | `gpt-5.4-mini` | The calibrated judge model |

Both experiment steps need a direct backend. `xpert` is refused per call, because it would
run its stored re-rank prompt in place of the one under test.
