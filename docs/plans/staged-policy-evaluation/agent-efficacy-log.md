# Agent efficacy log: staged budget evaluation

One row per chunk, filled in at build time. Compare the impl-plan's predicted size class and
blast radius with what the PR actually needed; a re-chunk is logged as its own row.

| Chunk | Predicted size | Actual diff (effective LoC, files) | Re-prompts | Re-chunked? | Blast radius held? | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | M (tests only) | 279 test lines, 4 files; no production code | 0 | No | Yes (tests only) | 4 Gherkin scenarios mapped to existing tests instead of duplicated; 16 new cases |
