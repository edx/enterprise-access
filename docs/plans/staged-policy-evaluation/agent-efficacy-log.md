# Agent efficacy log: staged budget evaluation

One row per chunk, filled in at build time. Compare the impl-plan's predicted size class and
blast radius with what the PR actually needed; a re-chunk is logged as its own row.

| Chunk | Predicted size | Actual diff (effective LoC, files) | Re-prompts | Re-chunked? | Blast radius held? | Notes |
| --- | --- | --- | --- | --- | --- | --- |
