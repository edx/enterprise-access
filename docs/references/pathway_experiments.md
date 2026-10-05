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

## Shape arms

Two further arms build a pathway to a fixed level **shape**, written as courses per rung,
`Introductory/Intermediate/Advanced`: `2/0/0` is two introductory courses, `2/2/1` the
delivered ladder. Any shape totalling 2 to 5 courses is accepted.

| Strategy | What it does | Paid calls |
| --- | --- | --- |
| `shape_cut` | Fills each rung's quota from the re-rank order, scarcest rung first, as assembly does, but never backfills | 0 |
| `shape_pick` | Asks the model for the shape, showing it only candidates on the shape's rungs | 1 per shape |

- **Counts are enforced in code**, as the provider cap is. A course beyond its rung's count is
  dropped as `over_level_quota`, and a key from a rung the model was never shown counts as a
  fabrication.
- **A variant is complete only if it meets its shape rung for rung.** A shape the window
  cannot fill is recorded short, never padded from another rung.
- **`shape_pick` shares the size arms' prompt**, with a shape sentence
  (`SELECTION_SHAPE_INSTRUCTION`) as its size rule.
- **Labels name the shape**, as in `shape_pick:2/2/1`, and each variant records `shape`.
- **Not on the API.** `variant_strategies` there still accepts the three size arms only. The
  shape arms are reached through `generate_input_dict(variant_shapes=...)` and
  `collect_pathway_variants --variant-shape`.

### Picking the best per shape: `select_pathway_shapes`

For a human review of shapes, `pathway_eval.shape_review` picks the judge's best pathway in
each of four tiers from a finished collection, offline:

| Tier | Rule |
| --- | --- |
| Introductory, 2 | exactly `2/0/0` |
| Intermediate, 2 | exactly `0/2/0` |
| Full ladder | at least one course on every rung |
| Another shape | courses on exactly two rungs, or Advanced only |

- **Tiers follow the realised mix, not the requested shape.** A pathway is placed by the
  levels its courses actually landed on.
- **Only complete, gate-clean, judged pathways compete.** The delivered pathway competes too.
- **Ranking within a tier:** verdict, then the share of courses on topic, then the number on
  topic. The last prefers a fuller ladder when both are clean.
- **The judge still decides nothing in the pipeline.** Selection happens after the fact, on
  exported runs.
- **A pick is best-of-N, so its verdict is biased upward.** Quote per-arm rates from the
  collection, never the picks'.

```bash
./manage.py collect_pathway_variants --careers-file careers.txt \
    --variant-strategy model_sized --variant-strategy shape_cut --variant-strategy shape_pick \
    --variant-shape 2/0/0 --variant-shape 0/2/0 --variant-shape 2/2/1 --variant-shape 2/1/0 \
    --judge --unscoped --include-candidates --checkpoint runs.jsonl --resume
./manage.py select_pathway_shapes --checkpoint runs.jsonl --output-json picks.json --output-csv picks.csv
```

`--include-candidates` records each career's candidate window, in the relevance order the
variants were built from. A review needs it to offer replacements from what the search found.

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

## Bench round 1 follow-ups: editorial policy, `shape_pick_v2`, judge v2

A reviewer rated 35 generated pathways. The v1 judge agreed with him on 11 of the 25 it
called good, and in 17 of his 24 course swaps his replacement ranked *below* the course it
replaced in the re-rank order. So the gap is in selection criteria, not retrieval. Three
opt-in additions follow from that. None of them changes a run that does not ask for it, and
none is on the API.

### Editorial exclusions and seats

An editorial policy (`pathway_editorial`, `EditorialPolicy`) is applied only when a run opts
in with `generate_input_dict(editorial_policy=True)` (the active rules) or
`editorial_snapshot=<EditorialPolicy.to_dict()>` (a fixed policy, which wins).

- **Exclusions apply everywhere, including the delivered pathway.** `eligible_candidates`
  rejects an excluded key as `editorial_excluded` and counts it with the other reasons. Keys
  match ignoring case, as the editorial app matches them. An opted-in run's delivered
  pathway can differ from a plain run's only by these exclusions.
- **Seats apply to the shape arms only.** The editorial planner (`plan_seats`) seats courses
  on a shape's rungs once per shape, and every shape arm shares that plan:
  - `shape_cut` places the seats, then fills each rung's open places from relevance order.
  - `shape_pick` and `shape_pick_v2` show the seats under `already_chosen` and ask for the
    remaining counts only. When the seats fill the shape, no call is made.
- **Seats count like any chosen course**, against rung quotas and the provider cap. They are
  also checked here against the window and the shape. A seat that does not fit is dropped as
  `seat_rejected`, and a model that re-picks a seated key has it dropped as `already_seated`.
- **What ran is recorded.** Each variant has `seats`, and `BuildVariantsOutput` records the
  policy it applied as `editorial_policy`.
- **A planner failure costs only that shape's arms.** It is recorded as the variant's
  `error`. A policy that was requested and cannot be loaded fails the step instead, because
  a run labelled as following a policy it did not follow is worse than no run.

### `shape_pick_v2`

This is `shape_pick` with a second selection prompt, `PATHWAY_SELECTION_SYSTEM_PROMPT_V2`. The
v1 prompt is untouched, so both arms can run side by side. It keeps v1's frame and adds the
reviewer's recurring reasons as general rules that name no course, provider or career:

- **Generality:** transferable skills over one vendor's or institution's practice, unless the
  career is defined by that tool.
- **Complementarity:** no two courses cover the same ground.
- **Role fit:** leading people, specialist depth or individual contribution, as the titles
  imply.
- **Coherence:** later courses build on earlier ones and stay on the same language or stack.
- **Level honesty:** two introductory places get one broad foundation and one focused course.

The model is also shown the career's Lightcast description, the family's size and first 12
job titles, and each candidate's skill tags.

**One repair round.** The model breaks the provider cap even when told the count, and a
seated course makes it worse. On the bench windows (2026-09-29), a seated course from a
provider that dominated the window led the model to choose two more from that provider, and
the cap left 9 of Sales Manager's 12 shapes short. So when the code refuses picks for breaking
a stated rule, `shape_pick_v2` asks once more. Those rules are the provider cap, a rung's
count, a repeat, or a key it was never shown (`REPAIRABLE_DROPS`, plus fabrications). The
two content rules below (`other_ecosystem`, `level_mismatch`) are in `REPAIRABLE_DROPS` too:
the model is not told them, but the gap is ours to fill.

- The repair call shows everything accepted so far as `already_chosen`.
- It asks only for the places still open.
- It shows only candidates the rules still allow.
- The answer goes through `apply_selection` again, so every rule holds.

A model that simply returns fewer courses gets no repair. That is an honest "nothing else
fits", and it stays short. The round is recorded on the variant as `repair` (`attempted`,
`added`, what the second answer had refused, any `error`). The arm therefore costs up to two
calls per shape, and `estimated_model_calls` bounds it that way.

### Judge rubric v2

`--judge-rubric v2` (repeatable with `v1`) adds a second instrument,
`PATHWAY_JUDGE_SYSTEM_PROMPT_V2`. It uses the same verdict scale and adds per-course flags:
`too_specific`, `redundant_with` (another key in the pathway, or `''`), `level_mismatch` and
`role_misfit`. It judges quality only, never business policy. It is shown the family's job
titles, the career description and eight career skills.

- **v1 stays the default and byte-identical.** Its prompt, schema and user content are
  pinned by digest in `test_prompts.py` and `test_judging.py`.
- **Editing v1 starts a new instrument, so v2 is a separate constant.** v2 has no
  calibration behind it yet. Compare v2 verdicts only with other v2 verdicts, and never pool
  them with v1's or the analysis's.
- **The two rubrics are kept apart everywhere.** Each judgement records its `rubric` (older
  records read as v1). Identical course lists are reused within a rubric, never across
  rubrics. `workflow.judgements(rubric)` reads one rubric, and `variants()` adds
  `judgement_v2` beside `judgement` when v2 ran.
- **Each rubric is a paid call per pathway,** and the cost bound counts every rubric.
- **`select_pathway_shapes --rubric v2`** ranks picks by v2. After verdict and on-topic ties,
  the pathway with fewer flagged courses wins, and every pick records the rubric that chose
  it.

```bash
./manage.py collect_pathway_variants --careers-file careers.txt \
    --variant-strategy shape_cut --variant-strategy shape_pick --variant-strategy shape_pick_v2 \
    --variant-shape 2/0/0 --variant-shape 2/2/1 \
    --editorial-snapshot policy.json --judge --judge-rubric v1 --judge-rubric v2 \
    --unscoped --include-candidates --checkpoint runs.jsonl --resume
./manage.py select_pathway_shapes --checkpoint runs.jsonl --rubric v2 --output-csv picks-v2.csv
```

The collection's CSV adds `seats` and `verdict_v2` as its last two columns. Its summary
counts v2 verdicts separately (`good_v2`, `weak_v2`, `bad_v2`).

## Content rules, on by default (2026-10-05)

A product reviewer rated 167 generated pathways over two review rounds. Three of his findings
are now rules in code. They apply to the delivered pathway (`assemble_pathway`) **and** to
every arm here, so an arm compared with the delivered pathway is compared under the same
rules.

| Rule | Where it applies | Counted as | Off switch |
| --- | --- | --- | --- |
| No capstone courses | `eligible_candidates` (everything), and a Tier 1 violation in `validate_pathway` | `capstone` (in `ineligible`) | None: always on |
| One vendor ecosystem | Assembly (both passes), `ranked_cut`, `shape_cut`, `apply_selection` (every model arm), the repair round | `other_ecosystem` | Kill switch, or `single_ecosystem=False` per call |
| Level honesty | Assembly (both passes), `shape_cut`, `apply_selection` under a rung quota (`shape_pick`, `shape_pick_v2`, repair) | `level_mismatch` | `level_honesty=False` per call |

- **No capstones.** Agreed with product on 2026-09-10 and never implemented. The reviewer
  asked again in round 2 ("no capstone courses!"). A course is a capstone when its *title*
  matches `capstone` or `final project` (whole words, any case). On the 20 stored round-2
  windows (539 distinct courses) that removes 11 courses, 21 window places, all of them named
  capstones. Before the rule, 2 of 20 delivered pathways and 28 of 500 variants contained one.
- **One ecosystem** (`ecosystems.py`): a pathway may teach one vendor's products (Microsoft,
  Google, AWS and others), or none, but not two. In round 2, the 14 of 81 rated pathways that
  spanned two vendors were rated good 14% of the time (2 of 14), against 70% for the rest (47
  of 67). Within the same careers it was 14% against 56%, and they drew 1.21 dropped courses per
  pathway against 0.33. These figures were re-measured with the current detector (title plus
  skill tags). An earlier count of 16 pathways (25% against 69%) used an older reading. Also,
  62% of the courses he rejected were vendor-specific, against 19% of those he endorsed. A refused course is skipped and the place goes to the next allowed candidate. In
  assembly that means the delivered pathway is shortened only when no allowed course is left.
- **Level honesty.** `level_type` disagrees with the title 19–36% of the time. A course whose
  title cue reads advanced (`TITLE_LEVEL_CUES`) is not placed on an Introductory place, and
  one whose cue reads introductory is not placed on an Advanced place. A title with both cues,
  or "beyond the basics", is left alone. On the stored windows, 6 eligible courses (12 window
  places) would ever be refused. **The limit:** this rule catches only an explicit
  contradiction in the wording. A course that is hard by subject, such as "Introduction to
  Post-Quantum Cryptography" on an introductory place, passes. Catching those needs a
  different signal, such as a calibrated judge's level flag.

**Kill switch:** `enterprise_access.learner_pathways_disable_single_ecosystem` (a
`WaffleSwitch`). Off, which is the default, means the rule applies. On turns the rule off for
every caller that does not choose for itself. An explicit `single_ecosystem=True` or `False`
wins over the switch. That is how an experiment replay compares with and without the rule. The
delivered pathway records what happened on `AssemblePathwayOutput`: `refused` holds the counts
by reason, and `single_ecosystem` says whether the rule ran.

**Effect on the stored windows** (20 round-2 careers, replayed through the app's code):
- **Delivered pathway:** 10 of 20 change, and none becomes incomplete. 21 courses are refused
  for ecosystem, and 3 for level.
- **`shape_cut`:** 17 of the 240 shapes come up short, against 9 before. The 8 new shortfalls
  break down as follows:
  - 2 from capstones (Data Analyst `0/0/2`, `0/1/2`).
  - 1 from the ecosystem rule (Solutions Architect `2/3/0`).
  - 4 from level honesty (Sales Consultant and Sales Manager `0/0/2`, `0/1/2`, where "Equity
    Markets Fundamentals" was the second Advanced course).
  - 1 from the ecosystem and level rules together (Solutions Architect `0/1/2`).

  `shape_cut` never backfills, so this is the expected cost.

**Replays.** `replay_shape_review --single-ecosystem` turns the rule on and
`--no-single-ecosystem` turns it off. Without either flag the app's default applies. The
summary line prints the value that was resolved, and each replay record stores it as
`replay.single_ecosystem`.

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
  re-charging them. Errors and budget skips are retried. The first real collection stalled
  repeatedly, which is why this exists. The stalls turned out to be a logging deadlock
  triggered by discarded SDK clients (now fixed by sharing one client per configuration; see
  `shared_sdk_client`), not the network hangs they first looked like.

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
