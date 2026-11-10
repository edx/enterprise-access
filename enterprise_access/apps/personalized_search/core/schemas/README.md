# Personalized Search wire contract

These schemas describe what `POST /api/v1/personalized-search/turns/` streams, so that any client can parse and render a turn. Other services, other frontends and offline analysis can all rely on them.

| File | Describes |
| --- | --- |
| `stream-parts.v1.schema.json` | One stream part. Each SSE frame is `data: <part JSON>`, and the stream ends with `data: [DONE]`. The parts are a subset of the AI SDK UI Message Stream v1, and the response carries the header `x-vercel-ai-ui-message-stream: v1`. |
| `block-catalog.v1.schema.json` | The `data` of a `data-block` part: a learning-page block (`page_header`, `item_section`, `insight`, `plan`) at catalog version `v: 1`. |

## Rules
- A turn starts with `start` and `data-session`, and ends with `finish` or `error`. Nothing follows either of those except `[DONE]`.
- `transient` parts (`data-status`, `data-timing`) are progress and diagnostics. Clients may ignore them, and history replay never includes them.
- The server hydrates item cards from catalog hits. The model only picks ids and writes `why` and `fit`, and anything that fails validation is dropped (KD-5).
- Clients should skip block types or `v` values they don't know, rather than fail.

## Versioning
- Additive changes, such as a new optional field, a new block type or a new transient part, keep `v1`.
- A change that breaks an existing client bumps the version: `v: 2` for blocks, a new stream protocol header value for parts.

The enterprise-access tests validate every part the orchestrator emits against these schemas (`tests/test_contract.py`).

## v1 additive changes

Each entry is optional for consumers; older clients ignore it.

| Date | Change |
| --- | --- |
| 2026-11-09 | `item_card.facts.hours_per_week_min` / `hours_per_week_max` (integers; courses and Executive Education) |
