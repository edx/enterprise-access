# Evaluation personas

One YAML file per persona. These files are the harness's ground truth: every quality
metric is computed relative to what they claim. Authoring them is expert work, not
engineering work, and it is the long pole of the whole evaluation.

## Schema

```yaml
id: p001-example-slug         # required, unique, kebab-case; also the filename
domain: technology             # required; technology | business | healthcare | trades | ...
tier: core                     # core | edge   (edge = deliberately thin coverage)

inputs:                        # required; validated by LearningIntentRequestSerializer
  selected_goals: "..."        # all four are required and must be non-blank
  free_text: "..."
  known_context: "..."
  interested_industries: "..."

expected:
  ground_truth_status: placeholder   # placeholder | expert_authored
  expect_no_coverage: false          # true = the catalog genuinely cannot serve this
  careers:
    - external_id: "ETE78CD2CDFFFAC66B"   # Lightcast id, REQUIRED
      name: "Data Analyst Consultant"     # optional, for humans
  courses:
    - key: "IBM+DA0101EN"                 # Algolia catalog course key, REQUIRED
      title: "Analyzing Data with Python" # optional, for humans
      note: "why an expert picks this"    # optional

catalog:
  enterprise_uuid: "..."       # optional until an enterprise is pinned
  snapshot_date: 2026-09-09    # YAML date; when the expectations were authored

notes: "..."                   # optional free text
```

## The four rules that will reject your file

**1. Courses are identified by catalog key, never by title.** Duplicate titles under
different keys are one of the defects being measured, so a title cannot identify a
course. Titles are welcome *alongside* the key.

**2. A course key is `<org>+<number>`, not a course-run key.** The catalog index's `key`
field holds `HarvardX+ER22.1x`, `IBM+DA0101EN`, `CodeSignal+164`. A `course-v1:...` run
key is a valid platform identifier but it appears nowhere in the index, so it can never
match a hit and would score as a miss no matter how good retrieval is. The loader
rejects run keys explicitly for this reason.

**3. Careers are identified by Lightcast `external_id`** — `ET` followed by 16 hex
digits. Career titles are neither unique nor stable in the taxonomy index.

**4. `expect_no_coverage: true` and a list of expected courses are contradictory.** A
persona cannot both be uncoverable and have a correct answer.

## `ground_truth_status` — read this before adding a persona

`placeholder` means the expectations are not expert judgement. Placeholders exist so the
harness can be exercised before ground-truth authoring finishes; they are excluded from
headline metrics. **Do not promote a persona to `expert_authored` because it looks
plausible** — only because someone qualified in that domain chose those courses.

The one honest exception is a persona whose expectation is *absence*
(`expect_no_coverage: true`) verified directly against the index. Absence is a fact about
the catalog, not a judgement, so it can be `expert_authored` on the strength of the
verification alone. `p010-welder` is that case.

## Where the shipped set came from

`p001`–`p009` were imported from `Learner Rec Persona Testing.xlsx`, authored by the
product team, which records intake inputs, expected careers, expected courses and notes
from three observed runs. Expected-course *titles* were resolved to catalog course keys
against the live index on 2026-09-09; **only exact title matches were kept**. `p010` is a
deliberately-added zero-coverage case.

Three things were deliberately *not* carried over, and each is recorded in the affected
persona's `notes`:

- **`Product Management (Professional Certificate)`** (p005) is a `content_type:program`
  record, and programs carry a null `key`. It cannot be expressed as an expected course
  key. Open product question: should a pathway be able to recommend a program?
- **`Foundations of Client Care 2: ...`** (p004) could not be resolved — the title looks
  truncated in the source sheet, and the catalog's Osmosis courses are named
  `Client Care: <topic>` (`OsmosisFromElsevier+CC1`..`CC6`). Needs the author to confirm.
- **Three expected careers** (`Nurse Practitioner`, `Product Manager`, `Data Analyst`)
  have **no exact entry in the Lightcast jobs index** — only qualified variants
  (151, 372 and 314 of them respectively). Rather than substitute a near-miss and invent
  ground truth, those personas ship with `careers: []` and an explanation. This is the
  same canonical-vs-colloquial mismatch as the skills vocabulary, one level up, and it
  independently confirms the p005 author note "No Product Manager career".

`p006` and `p007` have no expected courses yet and therefore report
`has_ground_truth == False`. Keep them anyway: they are the input-shape boundary cases —
`p006` is ~1,100 characters of conversational prose (which returns **zero** Algolia hits,
because the index ANDs every query word), and `p007` is ~60 characters total. Neither
failure mode is visible from the other personas.

## Coverage of the shipped set, and what it is missing

Six domains: technology (4), finance, engineering, healthcare, business, trades. The
technology personas are the ones that matter most and score worst — 0% recall@20 under
every configuration — because 92% of courses tagged with the "Artificial Intelligence"
subject carry no skill tags at all.

Known gaps worth filling: no persona has more than 5 expected courses (so recall is
coarse), `p008`/`p009` have 1 each, and there is no persona for the frontline retail,
transport or allied-health roles that the catalog analysis found unservable.

## Finding the identifiers

Both indexes are queryable read-only with the plain search key. To find a course key:

```bash
curl -s -X POST "https://<APP_ID>-dsn.algolia.net/1/indexes/<CATALOG_INDEX>/query" \
  -H "X-Algolia-API-Key: <SEARCH_KEY>" -H "X-Algolia-Application-Id: <APP_ID>" \
  -H "Content-Type: application/json" \
  -d '{"query":"data analysis","filters":"content_type:course","hitsPerPage":10,
       "removeWordsIfNoResults":"allOptional",
       "attributesToRetrieve":["key","title","level_type"]}'
```

`removeWordsIfNoResults` matters: the index ANDs every query word, so searching a full
course title (8+ words) returns **zero** hits without it. Confirm the hit's `title`
matches what you meant before recording its key — with that flag on, Algolia will always
return *something*.

Swap the index for the jobs index and retrieve `["name","external_id"]` to find a career.

Note that a bare index query is **not** scoped to any enterprise catalog. Once an
enterprise is pinned, expectations should be re-checked against that enterprise's scoped
catalog, because a course that exists in the index may not be in the customer's catalog.
