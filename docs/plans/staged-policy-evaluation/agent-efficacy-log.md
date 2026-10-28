# Agent efficacy log: staged budget evaluation

One row per chunk, filled in at build time. Compare the impl-plan's predicted size class and
blast radius with what the PR actually needed; a re-chunk is logged as its own row.

| Chunk | Predicted size | Actual diff (effective LoC, files) | Re-prompts | Re-chunked? | Blast radius held? | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | M (tests only) | 279 test lines, 4 files; no production code | 0 | No | Yes (tests only) | 4 Gherkin scenarios mapped to existing tests instead of duplicated; 16 new cases |
| C2 | S | 221 lines (81 production, 136 tests), 8 files | 2 (line-length fix; cohort set first so errors are attributed, from pre-PR review) | No | Yes | The no-request guard turned out necessary: WaffleFlag reads a request-wide cache before the request |
