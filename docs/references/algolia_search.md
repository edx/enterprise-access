# Algolia search from enterprise-access

How to query the catalog and jobs indexes, and the index behaviours that will bite you.
Measured against the production indexes on 2026-09-09.

Client: `enterprise_access/apps/api_client/algolia_client.py`.

## Two indexes, two credentials, not interchangeable

| Index | Credential | Why |
| --- | --- | --- |
| Catalog (`enterprise_catalog_incremental_prod`) | Enterprise-scoped **secured** key | Scoping is the point — it keeps results inside the learner's catalog |
| Jobs / Lightcast taxonomy (`prod_taxonomy`) | Plain search key | Secured keys can't read it |

A secured key sets `restrictIndices` to the catalog index and its replicas
(enterprise-catalog's `generate_secured_api_key`), so sending one to the jobs index fails
with an opaque Algolia error. The learner portal MFE encodes the same constraint as
`unsupportedSecuredAlgoliaIndices = [ALGOLIA_INDEX_NAME_JOBS]`. `search_jobs_index()`
refuses before issuing the request, including when the *configured* search key turns out
to be a secured key (they base64-decode to a querystring containing `restrictIndices`).

**The write key must never be configured here.** enterprise-catalog's `AlgoliaSearchClient`
is an indexing/administration client built on `ALGOLIA.API_KEY` and has no `search()`.
This client is search-only by construction.

### Secured keys are vended per user, which constrains where you can use them

`get_secured_algolia_api_key()` lives only on `EnterpriseCatalogUserV1ApiClient`, a
`BaseUserApiClient` — it forwards user context, and the generated key carries a
`userToken`. So a secured key is reachable from a request-backed code path and **not**
from a management command or a Celery task. Offline tooling has to either accept unscoped
results (`ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH`, off by default) or go through
enterprise-catalog.

Note also that `bffs.api.get_and_cache_secured_algolia_search_keys` caches for a fixed
`SECURED_ALGOLIA_API_KEY_CACHE_TIMEOUT` and does **not** parse `valid_until`, despite its
docstring saying so. `SecuredAlgoliaKey.is_expired()` checks it properly; treat a key with
no `valid_until` as expired rather than assuming validity.

## A search-only key cannot enumerate an index

* No `browse` ACL — `/browse` returns 403.
* Pagination is capped: with `hitsPerPage: 1000` the catalog index reports `nbPages: 1`
  against `nbHits: 4094`. Page 1 and beyond return nothing.

So you cannot build the set of course keys in an index with a search key. Establishing
that a course is *absent* requires a browse-scoped key or enterprise-catalog's
`contains_content_items`. Anything else is a probe, not a proof.

`key` is also neither filterable (`filters: 'key:"IBM+DA0101EN"'` → 0 hits for a course
that exists) nor searchable (`restrictSearchableAttributes: ['key']` → HTTP 400).

## Scoping to an enterprise customer needs no secured key

`enterprise_customer_uuids` is in the catalog index's `attributesForFaceting`
(`enterprise-catalog`'s `apps/catalog/algolia_utils.py`), so this works with the plain
search key:

```
filters: 'content_type:course AND enterprise_customer_uuids:<uuid>'
```

It is *also* in `unretrievableAttributes`, which means it never appears on a hit — but
**that does not block faceting on it.** `facets: ['enterprise_customer_uuids']` returns
values and counts, so customer UUIDs are enumerable (capped at 1,000 like any facet) and a
candidate UUID can be verified by its hit count.

Two consequences. Evaluation and diagnostic work can be scoped to a real customer without
the secured-key machinery, which needs a request and a user token and so cannot run from a
management command. And `unretrievableAttributes` should not be read as "private" — it
hides the value from a *hit*, not from an aggregate.

Measured 2026-09-10: the broadest customers see 4,057 of 4,094 courses (99.1%), so
per-skill counts scoped to one differ from unscoped by about a single course. Academy
customers are the exception at 13–16 courses.

## Course keys are `<org>+<number>`, not run keys

The catalog index's `key` field holds `HarvardX+ER22.1x`, `IBM+DA0101EN`,
`CodeSignal+164`. A `course-v1:...` **course-run** key appears nowhere in the index. This
is a silent failure mode: run keys look like course identifiers, so code or ground-truth
data that uses them matches nothing and reads as a relevance problem.

`aggregation_key` is `course:<key>`; `objectID` is `course-<uuid>-customer-uuids-<n>` and
is not derivable from the course key.

## Query semantics: every word is ANDed, and there is no fallback configured

This is the single most surprising behaviour. Measured on one persona's goal text,
filtered to `content_type:course`:

| Query words | Hits |
| --- | --- |
| 1 (`Move`) | 533 |
| 4 (`Move into a data`) | 90 |
| 5 | 4 |
| 6 | 1 |
| 8 or more | **0** |

`removeWordsIfNoResults` is not configured on the index, so a verbose query returns
**zero hits, not poor hits**. A five-word career title (`Medical Surgical Registered
Nurse Manager`) returns 0. Passing `removeWordsIfNoResults: 'allOptional'` turns that
24-word query into 348 hits and the career title into 121. (`'lastWords'` does not
rescue a long query.)

Two consequences:

1. Any natural-language query — a learner's free text, or a verbose model-generated
   `condensed_algolia_query` — silently returns nothing. This is the mechanism behind a
   retrieval ladder always descending to its widest step.
2. **Relaxing the query is worth a lot, but it is not sufficient.** Against
   product-authored ground truth, `removeWordsIfNoResults: 'allOptional'` moved
   recall@20 from **12% to 23%** overall and **21% to 40%** for non-technology personas.
   One search parameter is the cheapest available improvement.

   It is still not evidence of success on its own: four personas went from 0 hits to 20
   hits with recall unchanged at 0%. **Measure expected-key recall, never hit count** —
   a full result set of the wrong courses is the same pathology as a scope-only fallback
   wearing a different hat.

Keep text queries to a few words. In the diagnostic, the *shortest* strategy (a bare
career title) was the only one that retrieved anything at all without `allOptional`.

## The skill facet vocabulary is Lightcast-canonical, and short names are absent

`skill_names` holds disambiguated Lightcast forms. The short name a learner or a model
would produce is usually **not a facet value at all**:

| What you'd write | Hits | What the index actually holds | Hits |
| --- | --- | --- | --- |
| `Python` | 0 | `Python (Programming Language)` | 95 |
| `SQL` | 0 | `SQL (Programming Language)` | 42 |
| `Java` | 0 | `Java (Programming Language)` | 27 |
| `Excel` | 0 | `Microsoft Excel` | 26 |

Verified by direct `facetFilters` counts, not by reading the facet list — the facet
vocabulary response is capped at `maxValuesPerFacet` (1000), so "missing from the list"
is not evidence of absence.

Some names *are* canonical as-is (`Data Analysis`, `Machine Learning`, `Project
Management`, `Nursing`, `Leadership`). So the mismatch is systematic but not uniform, and
it has a predictable shape: `X` → `X (Programming Language)` / `X (Python Package)` / a
vendor-qualified form.

Exact-match grounding against a facet snapshot therefore drops the most in-demand
technical skills silently. That is a vocabulary-normalisation problem with a mechanical
fix, not an LLM-paraphrasing problem.

## Facet counts are inflated ~75-80x; `nbHits` under `facetFilters` is exact

The catalog index de-duplicates at query time (`distinct` on `aggregation_key`) but
**facet counts are computed before de-duplication**. The `subjects` facet reports 79,392
for "Business & Management" against a real 1,015 courses. Any code or dashboard that
displays a raw facet count is displaying a wrong number, roughly 75-80x too high.

Use `nbHits` under `facetFilters` with `hitsPerPage: 0` instead — that is exact
(verified against distinct keys returned on slices of 8, 35 and 1,015).

The same caution applies to the facet-*search* endpoint's `count` field, which is
additionally unfiltered by `content_type`. Treat those as candidate vocabulary only.

## 31% of courses carry no skills at all

A full census of all 4,094 courses found **1,272 (31.1%) with both `skill_names` and
`skills` empty**; the median tagged course carries 5 skills.

Do not sample by relevance rank to measure this. An earlier pass using the top 1,000
hits reported 13.1%, less than half the true rate, because relevance rank is
popularity-biased toward well-tagged content. Enumerate by slicing on a facet instead.

Concentration matters more than the average: **130 of 141 "Artificial Intelligence"
subject courses (92%) are untagged**, as are 115 of 122 Google Cloud courses and all 62
CodeSignal courses. Those courses cannot be retrieved by any skill-facet query, whatever
the vocabulary handling.

## Two thirds of jobs have no skills, deterministically

In a full census of 43,513 English-language jobs, **29,525 (67.9%) have an empty
`skills` array**. The rule is exact, with no exceptions observed: a job carries skills
**iff** it carries `job_sources: course_skill`. All 13,988 such jobs have skills; all
29,525 industry-only jobs have none.

So "the career resolved to a Lightcast entry" and "that entry has usable skills" are two
separate gates, and for two thirds of careers skill-based course retrieval has nothing to
work with. Check `job_sources` before relying on `skills.name`.

`prod_taxonomy` also supports numeric filters on `id` (`filters: 'id >= X AND id <= Y'`),
which makes full enumeration by bisection feasible there despite the pagination cap.

## Settings

```python
ALGOLIA_APP_ID = ''
ALGOLIA_SEARCH_API_KEY = ''            # plain, search ACL only
ALGOLIA_CATALOG_INDEX_NAME = ''
ALGOLIA_JOBS_INDEX_NAME = ''
ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH = False   # diagnostics only
```

Index names and the plain search key are configured per environment in
`edx-internal/frontends/frontend-app-learner-portal-enterprise/*_config.yml`; the search
key is search-ACL-only and already ships in the MFE bundle.

## Local development

The devstack `enterprise-access` container has **no network egress** — DNS fails and even
a raw-IP connection times out — so live Algolia calls cannot be made from inside it, and
`algoliasearch` cannot be `pip install`ed there without staging the wheel via
`docker cp`. Unit tests mock the client and are unaffected; anything that needs a real
Algolia response has to run outside the container.
