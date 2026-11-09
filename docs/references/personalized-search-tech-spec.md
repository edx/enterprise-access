# Tech Spec: Personalized Search (Chat) — POC

> Public copy of the Personalized Search tech spec. Internal deployment details are kept out of this copy.

> **Document Classification**
> **Type:** Plan Document (pre-build intent).
> This document captures how we intended to build BEFORE building. It becomes historical after release. Post-build truth lives in ADRs.

> **Conventions used in this spec**
> - **Ground every load-bearing claim about the existing system.** Tag each as **`[Specified]`** — verified against code or the PRD, with a `file:line` citation — or **`[Assumed]`** — not yet verified. An `[Assumed]` claim on a load-bearing path must be promoted to an Open Question (with an owner) before the Ready-to-Build gate; the gate's first criterion is that no load-bearing claim is still `[Assumed]`.
> - **Pre-build decisions live in Key Decisions**, not in ADRs. "Why this vs. that" reasoning belongs here while the design can still churn; ADRs are minted post-build at Knowledge Banking from the decisions that survived the build.

| Field | Value |
| --- | --- |
| Author | Eng Lead (co-authored with Claude) |
| Status | Draft |
| PRD Link | PRD: Personalized Search (Chat), Draft (internal) |
| Engineering Lead | Eng Lead |
| Reviewers | TBD |
| Created | 2026-10-26 |
| Last Updated | 2026-11-04 |

## Background & Context

### Problem Summary

Learners with a goal ("get promoted to an executive role") have two ways to find learning today, and neither adapts. Find a Course makes them turn the goal into keywords and filters. The Pathways quiz builds one fixed pathway that doesn't respond to follow-ups (PRD, Executive Summary).

This POC is an **internal demo**. It shows that a streaming chat, beside an editorial learning page the AI lays out, can recommend relevant items from the learner's entitled catalog. Reply text starts within 2 s, and the first recommendations appear within 5 s at p90 (KD-9). Retrieval reuses enterprise-access's existing pathways building blocks: the Algolia client, the secured key and the model backends. The design language follows the Claude HTML prototype, built with standard Paragon components rather than the prototype's custom styling.

#### In scope (POC iteration 1)

| # | Capability | PRD reference | Build phase |
| --- | --- | --- | --- |
| S-1 | Streaming chat: reply text streams while results are prepared | R-05, R-08, NFR-01 | 1 |
| S-2 | Dual-pane UI: chat pane + editorial learning page (KD-1) | Replaces UX-01 layout | 1 |
| S-3 | Dynamic UI generation: the AI response sets the learning page's title, intro, sections, cards and follow-ups, within a fixed, server-validated block catalog (KD-5) | Prototype `curatePrompt`; R-08, R-15 | 1 |
| S-4 | Ranking via Algolia through the existing pathways retrieval and rerank code, extended to **courses, Executive Education and programs** the learner's enterprise catalog contains, and scoped to that enterprise | R-06, R-07, R-11–R-13 (without ontology) | 1 |
| S-5 | No item repeated within a session (`shown_item_ids` per conversation) | R-18 | 1 |
| S-6 | Save / Not interested on cards, with a Saved view | Prototype, Figma A4/A4b (not in PRD) | 2 |
| S-7 | Session history: list, reopen and continue past chats, stored in enterprise-access (KD-7) | R-33–R-36 (subset) | 2 |
| S-8 | Mobile layout: chat/page toggle under 768 px | Figma M1–M2; NFR-05 (subset) | 2 |
| S-9 | Off-topic and prompt-injection redirect | R-10 | 1 |

**Phase 1 is the demo cut line.** It delivers S-1–S-5 and S-9 end to end. If Phase 2 slips, the demo still runs.

#### Out of scope (deferred from this POC iteration)

| Deferred | PRD reference | Why deferred / POC stand-in |
| --- | --- | --- |
| Learner Ontology: knowledge graph, event ingestion, JSON-LD API, confidence decay, retirement deletion | R-23–R-28, NFR-04 (ontology writes) | Explicitly deferred. The POC keeps only the conversation's own cumulative intent, with no cross-session learner model |
| Ontology-seeded opening turn | R-03 | Needs the ontology. POC opening uses catalog popularity or fixed prompts (decided in design) |
| Excluding items the learner already enrolled in or completed | R-12 (partial) | Needs enrollment signals from the ontology |
| Org Ontology and org-goal alignment | R-29–R-32, UX-07, K-09 | Already V2 in the PRD |
| Retiring the Pathways tab, quiz and banner, and the redirect | R-02, RK-06 | POC runs beside Pathways. **Amended 2026-11-04:** it has its own flags (KD-8), so the two tabs roll out independently |
| Single-column inline trays, scroll pills, sticky slim bar, tile pop animation | R-16 layout, R-19, R-20, R-22, UX-01, UX-05 | Superseded by the dual pane (KD-1) |
| Search inside history, rename, delete, clear all, 90-day purge job, time grouping | R-34 (search), R-37, R-38 (purge) | Demo needs reopen and continue only |
| Segment instrumentation, KPI dashboards, thumbs feedback | R-39, R-40, K-01–K-09 | Internal demo; no success metrics reported |
| Production security and privacy review, prompt retention policy, no-training terms | NFR-02 (partial) | Internal demo on stage data. Auth and per-enterprise scoping stay in scope |
| Formal WCAG 2.1 AA audit and screen-reader testing | NFR-03 (partial) | Paragon defaults + keyboard basics only |
| Full browser matrix, iOS Safari keyboard and safe-area spike | NFR-05 (partial), RK-08 | Demo on current Chrome desktop + one mobile viewport |
| Rate-limit tuning, load and scale testing, cost guardrails | NFR-04, RK-05 | A per-learner throttle is enforced (`personalized_search_turn`, 20/min per NFR-04). Global capacity is protected by the per-user demo audience (KD-8), not by tuning. Cost is measured, not enforced |
| LinkedIn or résumé import, enrollment from chat, sharing or exporting history | PRD out of scope | Already out in the PRD |
| **Infrastructure changes:** a separate ASGI/uvicorn deployment, a gunicorn worker-class change (gthread or gevent), proxy or load-balancer tuning, new datastores | NFR-01 (scale), NFR-04 | v1 streams from the existing sync workers (KD-3). Guardrails: a per-turn deadline, model timeouts, a per-learner throttle, a per-session turn lock, and an `X-Accel-Buffering: no` response header |
| Resumable streams (reconnect mid-turn after a network drop) | — | A dropped turn shows "Retry". The partial turn isn't saved, and the learner resends |
| OpenAI backend for chat | — | **Amended 2026-10-29 (KD-2):** `openai` also gets `stream()` so local/devstack work runs on a live model. `backend_unsupported` now only applies to a backend with no `stream()` |
| **Streaming frameworks and libraries:** Vercel `ai` / `@ai-sdk/react` (`useChat`), assistant-ui, AG-UI / CopilotKit, `fetch-event-source` / `EventSource` polyfills, sse-starlette / Django Channels, heartbeat or keep-alive machinery | NFR-01 | Eng Lead, 2026-10-28: the POC proves the approach with no streaming framework, and frameworks are a post-POC decision. v1 uses a plain Django `StreamingHttpResponse` generator, plus a `fetch` + `ReadableStream` reader and a hand-written parser (`parseSse`, `applyPart`). KD-4 borrows only the AI SDK v1 *frame shape*; no Vercel code ships. Because the encoder and parser are a matched adapter pair (KD-4), adopting a framework later swaps only those two files |

### Current State — verified against code

*How does the system behave today? What is missing or broken? This section is grounded, not
recalled: every load-bearing claim cites `file:line` in the real repo and is tagged
`[Specified]`. If you cannot cite it, tag it `[Assumed]` and open an Open Question — do not
write it as fact. Grounding happens here, as part of drafting the spec; there is no separate
architecture pass or artifact.*

**Grounded against:**

| Repo | Branch | Commit (short SHA) | At head of main/master? |
| --- | --- | --- | --- |
| enterprise-access | `origin/main` (worktree) | b6f3ef6f | Yes, as of 2026-10-26 fetch |
| frontend-app-learner-portal-enterprise | `edx/master` (worktree) | 46c7149a | Yes, as of 2026-10-26 fetch |
| Xpert Platform service (internal) | default branch | – | Yes, as of 2026-10-26 |
| edx-enterprise (flag definition only) | `chore/sync-ai-instructions-2026-10-14` (developer checkout) | 2b971490 | **No**, it's a feature branch. The cited flag lines (`enterprise/toggles.py:107-117,188-192`) are identical on `origin/master` @ 8f8585a2 (fetched 2026-10-27) |
| @edx/frontend-platform (installed package) | `node_modules` of the developer's portal checkout | 8.5.1 | Same version as the portal's lockfile; read only to cite auth internals |
| Deploy configuration and the stage catalog index (internal) | – | – | Read 2026-10-27; details kept out of this copy |

Worktrees live in `personalized-search/worktrees/`; the developer's own checkouts (feature branches with uncommitted work) were not used for citations.

Paths are relative to each repo root. `ea/` = `enterprise_access/` in enterprise-access.

**Ranking pipeline (enterprise-access `apps/pathways`)**

- *[Specified]* Pathways is a server-side pipeline exposed as `POST /api/v1/learner-pathways/careers/` and `.../pathway/` (`ea/apps/api/v1/urls.py:14-16`; `ea/apps/api/v1/views/pathways.py:78-88,175-185`). Requests need JWT auth, are throttled to 30 pathway calls per hour (`DEFAULT_THROTTLE_RATES`, `ea/settings/base.py:186-198`), and return 404 when the `learner_pathways_server_pipeline` WaffleSwitch is off (`ea/toggles.py:51,93-95`; `views/pathways.py:101,201`).
- *[Specified]* A pathway request runs these steps in order, with no concurrency anywhere (grep for `async def|ThreadPool|celery` in `apps/pathways` finds nothing):
  1. Facet snapshot.
  2. Skill translation.
  3. Candidate retrieval: Algolia, `hitsPerPage=50` (`ea/apps/pathways/course_retrieval.py:49`), with one broadening search added if fewer than 15 hits come back (`:268-284`).
  4. LLM rerank (`ea/apps/pathways/reranking.py:137-182`).
  5. Deterministic 2/2/1 level assembly (`pathway_assembly.py:195`).
  6. Xpert rationale enrichment.
  
  Each step is saved as a workflow row (`ea/apps/workflow/models.py:25,42-69`).
- *[Specified]* Retrieval only returns courses. `content_type:course` and English-only clauses are always added (`course_retrieval.py:160-161`), and the facet snapshot is limited to courses (`catalog_translation.py:56,93`). No program fields or filters exist in the pipeline (grep: `program`).
- *[Specified]* Results are scoped to the enterprise twice: through a secured Algolia key vended by enterprise-catalog (`ea/apps/pathways/models.py:43-68`), and through an explicit `enterprise_customer_uuids` filter (`course_retrieval.py:150-168`). Pathways fetches a new key at each step without caching. A 30-minute cached helper, `get_and_cache_secured_algolia_search_keys`, already exists (`ea/apps/bffs/api.py:93`; TTL `ea/settings/base.py:659`).
- *[Specified]* Rerank contract:
  - Input: `{career, candidates:[{key, title, description≤400}]}` (`reranking.py:54-82`).
  - Output: `{ordered_keys, rationales}` (`ea/apps/pathways/prompts.py:72-94`).
  - Keys the model invents are dropped (`reranking.py:85-134`), and any failure falls back to retrieval order (`:168-177`).
- *[Specified]* Model backends are `xpert` (default), `claude` (`claude-sonnet-5`) and `openai` (`gpt-4o`) (`ea/settings/base.py:557-566`). The interface is `complete(...) -> ModelResponse`, which returns one string (`model_backends/base.py:48-65,124`). Neither SDK client sets a timeout (`claude.py:68`, `openai.py:74`), and `max_tokens` is 4096. Nothing streams.
- *[Specified]* Persisted pathway records store no user identifier (`ea/apps/pathways/models.py:1103-1104`). No model holds per-user conversations, saved items or history.

**Serving (enterprise-access)**

- *[Specified]* The app is WSGI-only: `WSGI_APPLICATION` is set (`ea/settings/base.py:150`), there's no ASGI app, and no async views (grep: `async def|text/event-stream`). Gunicorn runs `workers = 2`, `timeout = 300`, and sets no `worker_class` (`ea/docker_gunicorn_configuration.py:6-11`). `gevent` is installed (`requirements/production.in:4`). The only streaming response in the repo is a CSV proxy (`ea/apps/api/v1/views/subsidy_access_policy.py:1539`).
- *[Specified]* `gevent==26.9.0` and `gunicorn==26.2.0` are pinned only in `requirements/production.txt:265-269`. The MySQL driver is `mysqlclient==2.3.0` (`requirements/production.txt:335`), a C extension that gevent cannot make cooperative. A gevent worker that runs a query would block every greenlet on it, so switching worker class isn't a quick option.
- *[Specified]* The service's own code already acknowledges that sync workers are scarce: the export throttle in `DEFAULT_THROTTLE_RATES` is commented "each export holds one of the 2 sync gunicorn workers for the whole download" (`ea/settings/base.py:186-198`).
- *[Specified]* No middleware buffers responses (no GZip or ConditionalGet; `ea/settings/base.py:102-131`). Django runs every middleware's `process_response` *before* a `StreamingHttpResponse` generator is consumed, so request-scoped state is already gone inside the generator. That includes `crum` (`:130`), `RequestCacheMiddleware` (`:105`), simple_history (`:128`) and the edx monitoring middleware (`:106-108,124`). Authentication, permission checks and every DB read for a turn must finish before the response is returned.
- *[Specified]* Cookie-JWT requests are rebuilt by `JwtAuthCookieMiddleware` (`ea/settings/base.py:113`, `JWT_AUTH` `:319-330`). `JwtAuthentication` enforces CSRF only when the JWT came from a cookie, so a cookie-authenticated POST must send `X-CSRFToken`. The CSRF cookie is `enterprise_access_csrftoken` (`:285`), and the token endpoint is mounted at `ea/urls.py:50`.
- *[Specified]* `anthropic==2.1.0` and `openai==3.37.0` are pinned in `requirements/base.txt:19,277`.
- *[Specified, internal deploy configuration]* Deployed environments run a small number of sync workers, so the number of turns that can stream at once is bounded by the deployed worker count (OQ-1). The production and stage request paths add no response buffering or timeout overrides that are visible in configuration; the remaining unknowns are checked empirically in the first stage smoke (OQ-4). Cross-origin cookie-JWT + CSRF calls from the portal origin are already allowed, as the existing axios client relies on them (OQ-2).

**Authorization (enterprise-access)**

- *[Specified]* Personalized Search requests carry the enterprise customer uuid, so its endpoints use a new enterprise-scoped learner permission checked against that uuid (Security Design).

**Xpert Platform (internal service)**

- *[Specified]* The message API fixes its own model (`gpt-4o-mini`) and has no `model` or `response_format` field.
- *[Specified]* With `stream: true`, each chunk is plain JSON holding the whole message so far, with no `data:` framing and no done event (spike 2, below).
- *[Specified]* Sessions live in enterprise-access (KD-7); the Xpert backend is called statelessly, with the history in each request.
- *[Specified]* enterprise-access calls Xpert through `XpertAPIClient.send_message`, which posts to `/v1/message` with `client_id`, `system_message`, `messages`, `conversation_id`, `stream: False` and `tags` (`ea/apps/prompts/api_client.py:140,207-221,268-319`; `stream` is hard-coded `False` at `:218,280`).
  - Every call sends a fresh conversation id (`ea/apps/api/v1/views/prompt.py:69-85`; `ea/apps/pathways/models.py:242,359,639-640`).
  - It sends no history and no `user_id`.
  - Timeout comes from `XPERT_REQUEST_TIMEOUT`, default 45 s (`ea/settings/base.py:523`).

**Learner portal (frontend-app-learner-portal-enterprise)**

- *[Specified]* Dashboard tabs are Paragon `Tabs` built in `useDashboardTabs` (`src/components/dashboard/data/useDashboardTabs.jsx:77-100,172-255`). The tab is chosen with `?tab=`, not a route path (`:75`).
- *[Specified]* The Pathways tab is gated by the BFF flag `enterpriseAiPathwaysOperatorEnabled` AND an env allowlist of customer UUIDs (`useDashboardTabs.jsx:64-68`; `src/index.tsx:69-74`). Mobile "More..." overflow comes from Paragon itself.
- *[Specified]* Detail routes are `:courseType?/course/:courseKey` and `program/:programUUID` (`src/routes.tsx:69,104`). Existing cards: `SearchCourseCard` (`src/components/search/SearchCourseCard.jsx:90-117`) and `SearchProgramCard` (`SearchProgramCard.jsx:127-169`). Paragon is `^22.17.0` (`package.json:21`). `useMediaQuery`/`breakpoints` are already used (`src/components/course/BrowseAndRequestAlert.jsx:3,17`); Sheet, Drawer and ModalLayer are not.
- *[Specified]* All API calls use the axios `getAuthenticatedHttpClient()`. There's no fetch-stream, EventSource or SSE code (grep: `ReadableStream|getReader|EventSource|text/event-stream`).
- *[Specified]* The portal locks React 18.3.1 and mounts with `createRoot` inside `StrictMode` (`src/index.tsx:2,15,18-22`). Other locked versions: zustand 5.0.14, @tanstack/react-query 5.75.0, TypeScript **4.9.5** (`package-lock.json`). Jest compiles only `lodash-es|@edx|@openedx|@2uinc` from node_modules (`jest.config.js:14`). There is no SSE parser, markdown renderer or JSON-patch library; the only sanitizer is `dompurify` (`package.json:29`).
- *[Specified]* In frontend-platform 8.5.1, the authenticated axios client does three things a raw `fetch` must copy:
  - It sets `withCredentials = true`.
  - It refreshes the JWT cookie before each request through `jwtTokenService.getJwtToken()` (`auth/AxiosJwtAuthService.js:411-429`). `AxiosJwtTokenService.js:61-75,143-172` de-duplicates concurrent refreshes.
  - It attaches `X-CSRFToken` from `csrfTokenService.getCsrfToken(url, CSRF_TOKEN_API_PATH)` on POST, PUT, PATCH and DELETE (`AxiosJwtAuthService.js:433-441`; `auth/interceptors/createCsrfTokenProviderInterceptor.js:27,41`).
  
  The authenticated client does **not** send `USE-JWT-COOKIE`; only the CSRF-token client does (`auth/AxiosCsrfTokenService.js:22`). So the streaming client does this before each turn:
  1. `await getAuthService().getJwtTokenService().getJwtToken()`
  2. `await getAuthService().getCsrfTokenService().getCsrfToken(url)`
  3. `fetch(url, { method: 'POST', credentials: 'include', headers: { 'X-CSRFToken': token, 'Content-Type': 'application/json' }, signal })`
  
  The remaining stage end-to-end check is OQ-2.
- *[Specified]* Pathways frontend state is a zustand store persisted to localStorage (`.../pathways-tab/state/pathwaysStore.ts:40,86-89`), with no react-query.
- *[Specified]* Tests run on Jest + RTL. `jest-fetch-mock` is enabled globally (`src/setupTest.js:5,43`); there's no msw and no `TextDecoder`/`ReadableStream` polyfill.

### Relevant Architecture

| System | Role in This Feature | Owner |
| --- | --- | --- |
| frontend-app-learner-portal-enterprise | New dashboard tab: dual-pane chat + learning page, streaming client, card actions, history, mobile layout | Enterprise (Learner Rec) |
| enterprise-access | New `apps/personalized_search` (streaming turn endpoint, sessions, feedback); reuses the `apps/pathways` model backends (adding `stream()`), the Algolia client (adding multi-query) and the secured-key cache | Enterprise (Learner Rec) |
| enterprise-catalog | Vends the secured Algolia key scoped to the enterprise's catalogs | Enterprise |
| Algolia (catalog index) | Candidate retrieval and ranking | Enterprise / 2U |
| LLM provider: Claude (direct) or Xpert, switchable (KD-2) | Intent, plus the compose step that writes the reply and lays out the page | Learner Rec; Xpert Platform team when `xpert` is selected |

## Requirements

*Carried from the PRD (source of truth for what to build), restated here with stable REQ-IDs
so each design decision and the Traceability section can reference them. Tag any requirement
the design assumes but the PRD does not yet state as `[Assumed]` and open an Open Question.*

Priorities: **P0** = Phase 1, the demo cut line. **P1** = Phase 2. Acceptance criteria (AC) are what the demo is checked against.

| REQ-ID | Requirement | Type | Priority | Source | Acceptance criteria |
| --- | --- | --- | --- | --- | --- |
| REQ-001 | Each AI turn's reply text streams token by token, followed by the turn's page blocks | Functional | P0 | S-1; R-05, R-08, NFR-01 | Reply text appears progressively, not all at once. While a turn is processing, the composer is disabled and a status line shows the current step (UX-03). The learner can stop a turn mid-stream: it shows as "Stopped", its partial blocks are discarded, and nothing is saved |
| REQ-002 | A dual-pane layout with chat on one side and the AI-composed learning page on the other | Functional | P0 | S-2; KD-1 | At ≥ 768 px both panes are visible. The page pane shows the latest turn's page. Earlier turns' pages stay reachable from their chat message ("View page") |
| REQ-003 | The learning page is composed by the AI each turn from the block catalog v1 only | Functional | P0 | S-3; R-08, R-15; prototype `curatePrompt` | Every rendered block passed server-side validation. Unknown block types or versions don't render. No LLM-authored HTML reaches the DOM. Blocks appear one at a time as they're validated |
| REQ-004 | Candidates come from an enterprise-scoped Algolia search over courses, Executive Education and programs, then are ranked by the LLM. Only ids present in that search response can be displayed | Functional | P0 | S-4; R-06, R-07, R-11–R-13 (no ontology) | 0 displayed items missing from that turn's search response (guardrail). Up to 5 items per turn and up to 3 per type section. A turn with no qualifying items is text-only plus a clarifying question (R-07), never padded |
| REQ-005 | Each message is turned into a structured intent (skills, roles, level, constraints) merged with earlier turns. Raw text is never the only query | Functional | P0 | R-05, R-09 | The intent JSON is stored on the turn. A follow-up such as "something shorter" narrows the previous intent rather than replacing it. Career-advancement goals broaden to adjacent roles (R-09) |
| REQ-006 | At most one adaptive follow-up question per turn, chosen from the remaining candidates' facets, never repeating an attribute already asked in the session, plus 3–4 suggestion chips | Functional | P0 | R-08, R-15 | `follow_up_attribute` is stored per turn. No attribute repeats within a session. Clicking a chip sends it as the next message |
| REQ-007 | No dismissed item comes back, and results update in place across turns (**amended 2026-11-09, KD-11**) | Functional | P0 | S-5; R-18 | Items the learner marked "Not for me" are excluded in the Algolia query and never passed to the ranker (guardrail: 0 reappearances). Items already shown may stay in the refreshed results; each card says whether it is new or updated since the previous turn. *Was:* no item appears twice in a session |
| REQ-008 | Off-topic, unsafe or prompt-injection messages get a text-only redirect to learning goals | Functional | P0 | S-9; R-10 | No blocks and no retrieval call for such turns. The reply is a short redirect plus suggestion chips. Checked against a fixed list of adversarial prompts |
| REQ-009 | Save and Not interested on each card, plus a Saved view | Functional | P1 | S-6; prototype | Saving survives a reload and shows in a Saved view. Not interested hides the card (with Undo) and excludes the item from all later turns for this learner in this enterprise |
| REQ-010 | Session history: list, reopen and continue the learner's past sessions in the current enterprise | Functional | P1 | S-7; R-33–R-36, R-38 (subset) | The list is newest first, 20 per page, with title and date. Reopening renders the session exactly as stored, with no animation, under a "Past search · {date}" banner (R-35). Sending a message continues it with `shown_item_ids` restored (R-36). A learner only ever sees their own sessions for the current enterprise |
| REQ-011 | Mobile layout: below 768 px, a chat/page toggle | Functional | P1 | S-8; NFR-05 (subset) | At 375 px there's no horizontal scroll and the toggle switches panes. A new page auto-shows a "New picks" indicator on the Page toggle |
| REQ-012 | Performance: reply text starts within 2 s, the first item section renders within 5 s, and the page completes within 7 s, all at p90 per turn (KD-9) | NFR | P0 | NFR-01 ("tiles render within 5 s" read as the first tiles), K-05 | Measured server-side from view entry via `ChatTurn.timing` and the turn log over the 30-prompt demo script on stage. p90 `first_text_ms` < 2000, `first_section_ms` < 5000, `complete_ms` < 7000 |
| REQ-013 | Security: JWT learner RBAC scoped to the requested enterprise, session ownership enforced, results scoped to entitled catalogs | NFR | P0 | NFR-02 (partial) | Requests for another enterprise, or for another learner's session, return 403/404. The Algolia query always carries the enterprise filter and the secured key |
| REQ-014 | The feature is gated by its own flags: one in the LMS for the tab, one in enterprise-access for the endpoints | NFR | P0 | KD-8 (Eng Lead; amended 2026-11-04 from reusing the Pathways flags). Closer to the PRD's dedicated `FEATURE_PERSONALIZED_SEARCH` (NFR-05) | enterprise-access flag off for the user → every endpoint returns 404. LMS flag off for the user → the tab is hidden. Pathways flags have no effect on this feature |
| REQ-015 | Accessibility basics | NFR | P0 | NFR-03 (partial) | Paragon components only. The composer and chips are keyboard-operable. Each new AI turn is announced through a polite live region. `prefers-reduced-motion` is honoured |
| REQ-016 | The results pane is a decision guide: a top pick with its reason, a short "Best for…" comparison, then the remaining options by type, plus a read-only summary of what the AI knows about the learner and a visible caveat when the results can't meet a stated goal | Functional | P1 | Design review 2026-11-09 (KD-11) | The first screen at 1440×1024 shows the top pick and the comparison. Chat replies are one or two sentences and never describe the results as a "page" |
| REQ-017 | Usage telemetry: Segment events with raw facts and join keys, aggregated downstream | NFR | P1 | PRD R-40, R-46–R-50 (KD-12) | Every event in `personalized_search_events.md` fires with its join keys; no event carries learner text or a computed aggregate |

## Key Decisions

*Pre-build decisions and the reasoning behind them ("why this vs. that") — the
architecture-justification / tradeoff record. These are decisions **of this spec**, provisional
until built. Each is grounded in the Current State above and cites the REQ(s) it serves. After
release, the decisions that survived the build are minted as ADRs at Knowledge Banking; the
`Rejected Alternative` column is the fuller Alternatives Considered table's short form.*

| # | Decision | Why (tradeoff / justification) | Rejected Alternative | REQs |
| --- | --- | --- | --- | --- |
| KD-1 | **Dual-pane layout:** chat on one side; an editorial learning page on the other that the AI re-lays out each turn. Follows the Claude prototype's design language, built with Paragon. Under 768 px the panes become a chat/page toggle | The Eng Lead chose the prototype's editorial experience as what the demo should show. Keeping the page separate lets the AI compose a whole page (title, intro, grouped sections) instead of loose rows of tiles. Cost: it departs from the PRD layout (R-16, R-19–R-22, UX-01, UX-05), so the PRD owner must sign off or the PRD must be updated (OQ-3) | PRD single scrolling column with up to 5 tiles under each reply (rejected: no room for page-level editorial structure). Figma Concept B, inline chips (rejected for this iteration) | REQ-002, REQ-003, REQ-011 |
| KD-2 | **LLM path is switchable per environment.** A `stream()` method is added to the model-backend interface and implemented for `claude` (Anthropic `messages.stream`, real deltas, explicit timeouts), `xpert` (Xpert `stream: true`; its cumulative chunks are turned into deltas server-side) and, **from 2026-10-29, `openai`** (Chat Completions `stream=True`; Eng Lead: OpenAI is the live backend for local/devstack work, Anthropic deferred). Both yield the same `TextDelta`/`Done` events, so nothing above the backend knows which one ran. The existing `PATHWAYS_MODEL_BACKEND` setting picks it (`ea/settings/base.py:557`) | Lets the demo run on Xpert, which is already in production and needs no new secret, or on Claude, which gives model choice, structured output and lower latency, without code changes. Cost: two `stream()` implementations. With Xpert we also carry its specifics: the caller sends the history every turn, there's no `response_format` or model override, a classifier runs on every message, and each turn leaves a copy in Xpert's conversation store that we don't control (OQ-5) | Xpert only (rejected: no structured output or model choice, and the delta-diffing is fragile). Claude only (rejected: depends on `ANTHROPIC_API_KEY` being in stage, OQ-6, with no fallback). OpenAI `stream()` (deferred: not needed for the demo) | REQ-001, REQ-005, REQ-012 |
| KD-3 | **Streaming host: a sync `StreamingHttpResponse` on the existing enterprise-access sync gunicorn workers, with no infrastructure changes in v1.** Auth, permission checks, the session load and catalog scoping happen *before* the response is returned. The generator only calls Algolia and the LLM, and writes the turn at the end using plain data captured up front. Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`. A 20 s hard deadline per turn is enforced through per-call timeouts, so no call blocks longer than the time left. That means no heartbeats are needed | Streaming needs no structural change. Today's pathways request already holds a sync worker for its whole serial chain of up to 3 LLM calls, which is longer than a chat turn. A chat turn holds one for its own duration (target ≤ 5 s, hard deadline 20 s) and just sends bytes sooner. A sync streaming response already exists in the repo (`ea/apps/api/v1/views/subsidy_access_policy.py:1539`). Cost: each in-flight turn occupies one sync worker, so concurrency is capped by the deployed sync worker count (OQ-1, answered). That's acceptable for an internal demo with a per-learner throttle, a per-session turn lock and a per-user demo audience | A separate ASGI/uvicorn deployment of the same codebase (rejected for v1: new infrastructure; this is the v2 path). gthread workers (rejected for v1: changes the whole service's runtime). gevent workers (rejected: `mysqlclient` blocks under gevent, `requirements/production.txt:335`). No streaming (rejected: NFR-01 asks for text within 2 s) | REQ-001, REQ-012 |
| KD-4 | **Wire protocol: Vercel AI SDK "UI Message Stream" v1 over SSE**, encoded by hand in Django and parsed by our own client. The encoder (`stream_protocol.py`) and the frontend parser are a matched adapter pair; nothing else in either codebase knows the protocol. **Exit criterion:** if the proxy buffers SSE (OQ-4), switch to the fallback, `?mode=buffered`, which returns the same `parts[]` as one JSON body. The frontend reducer consumes the identical part sequence either way, so only the transport adapter changes | It's the most widely adopted documented chat-stream protocol with a language-neutral spec, so a Python server can emit it (FastAPI template; pydantic-ai adapter). Its typed `data-*` parts with id-upsert fit page blocks exactly. Choosing a standard keeps later client swaps cheap (`useChat`, assistant-ui). Cost: the protocol is versioned by Vercel, not a standards body, so we pin to v1 and keep it behind the adapter pair. The buffered fallback does **not** relieve worker pinning (a buffered turn holds its worker just as long); pinning is relieved by the rerank kill switch (degraded mode, no model calls) in v1 and by the ASGI deployment in v2 | AG-UI (rejected: its main React client, CopilotKit, is unsupported without its runtime, and it brings more ceremony than one agent needs; still the nearest alternative, since assistant-ui reads both). Custom SSE events (rejected: no ecosystem client can read them). A2UI (rejected: pre-1.0, and the LLM writes the UI spec directly) | REQ-001, REQ-003 |
| KD-5 | **Generative UI = server-validated whole blocks from a closed, versioned catalog.** The compose LLM writes one JSON block per line. Django validates each block against its schema, keeps only item ids that are in the turn's candidate set and not already shown, fills in card data from the Algolia hits, and only then emits a `data-block` part. The LLM never writes HTML, JSX or item data, and never invents ids. The frontend drops unknown block types or versions | This puts "the AI recommends, the catalog decides" into code. The page still fills in progressively, one block at a time, and no unvalidated prop can reach the DOM. Cost: no character-level streaming inside a block (the reply text above it does stream) | Partial-JSON streaming of tool arguments (rejected: a card could render before its id is validated). LLM-authored markup or MCP Apps iframes (rejected: breaks Paragon, a11y and analytics, and is unsafe) | REQ-003, REQ-004, REQ-007, REQ-008 |
| KD-6 | **Frontend = Pathways-style layering in an isolated `src/components/dashboard/main-content/personalized-search/` folder, with our own stream reducer.** Layers: presentational Paragon components; a controller hook (the only place React context is resolved); hook-free workflows with typed input and output; ports/adapters (`ChatTransport` with `SseChatTransport` + `FixtureChatTransport`, `SessionsRepository`, `FeedbackRepository`); a pure `applyPart(state, part)` reducer; a zustand store; and a block registry (`type → component`). There are no imports from `learner-pathways/` and no new runtime dependency | Mirrors the documented contract at `usePathwaysController.ts:13-25`. The fixture transport lets the frontend be built and demoed before the backend exists. Reopening a session replays the stored parts through the same reducer, so history needs no second renderer. Writing our own parser avoids TS 4.9.5 and Jest-ESM friction (`jest.config.js:14`) | `@ai-sdk/react` `useChat` (rejected for v1: typings unverified on TS 4.9.5, needs an ESM transform, and owns message state outside zustand; it can be dropped in later because the protocol is standard). assistant-ui (rejected: pre-1.0 churn, and it needs Paragon re-styling). Extending the Pathways folder (rejected: ties the POC to the feature it replaces). A separate MFE or plugin slot (rejected: the portal has no plugin framework) | REQ-002, REQ-003, REQ-010, REQ-011 |
| KD-7 | **Sessions are stored in enterprise-access.** New `ChatSession`, `ChatTurn` and `ItemFeedback` tables are owned by the learner (`lms_user_id`) and scoped to an enterprise. Each turn stores the final `parts[]` exactly as streamed. Continuing a session loads the last N turns from our DB and sends them as LLM `messages`. The browser-facing `session_uuid` and the upstream conversation id sent to Xpert are separate server-generated uuids. The upstream id is never serialized | Xpert isn't designed as a per-learner session store. Storing the streamed parts makes reopening an exact replay. Cost: these are the first enterprise-access tables that tie learner-written text to a user, so they need PII annotations, and retention is deferred | Xpert `conversation_history` (rejected: see above). Browser `localStorage` (rejected: per-device, lost on clear, and can't be audited) | REQ-007, REQ-009, REQ-010, REQ-013 |
| KD-8 | **Two dedicated per-user flags (amended 2026-11-04, Eng Lead).** Frontend: the tab shows when the edx-enterprise `WaffleFlag` `enterprise.personalized_search_enabled` is on; the portal reads `enterpriseFeatures.enterprisePersonalizedSearchEnabled` and gates the tab on it **alone** (no Pathways flag, no customer allowlist). Backend: every `/api/v1/personalized-search/` endpoint returns 404 unless the enterprise-access `WaffleFlag` `enterprise_access.personalized_search_enabled` is on for the request's user; this replaces the shared `learner_pathways_server_pipeline` switch for Personalized Search only (Pathways keeps its switch). **Rollout:** enable the enterprise-access flag first, then the LMS flag, for the same users, so a visible tab never meets a 404; either flag alone kills the feature. **Override safety:** the enterprise-access flag can't be turned on from a URL only while `WAFFLE_OVERRIDE` stays off (django-waffle's default; asserted in `tests/test_toggles.py`) and the flag's own "Testing" mode is never enabled in Django admin | Personalized Search rolls out and rolls back independently of Pathways, per user, in both layers. enterprise-access can't read the LMS flag without an LMS round trip on every turn, so each layer owns its own flag (Eng Lead chose this over reading the LMS flag). Cost: two flags to keep in step, and an edx-enterprise release plus an edx-platform bump before the tab flag exists anywhere (OQ-10) | **Original 2026-10-27 decision:** reuse the Pathways flags (`enterprise.ai_pathways_operator_enabled` + the MFE allowlist; the `learner_pathways_server_pipeline` switch). Superseded: both tabs showed together and neither could be killed alone. Reading the LMS flag from enterprise-access through the learner's `enterprise-learner` call (rejected: an LMS call on every turn, history and feedback request, and an LMS outage turns the feature off). A switch instead of a flag (rejected: no per-user rollout) | REQ-014 |
| KD-9 | **Latency strategy: a fast model per step, plus a progressive target.** On the `claude` backend, intent and compose default to Claude Haiku 4.5 (`claude-haiku-4-5-20251001`). Each step's model is its own setting, so `claude-sonnet-5` can be switched in. Compose emits item sections first, then the follow-up, then the optional insight and plan. The NFR-01 "tiles within 5 s" target is measured as the **first item section within 5 s at p90**, with the full page within 7 s at p90. A Phase 0 spike measures real throughput on the recorded compose prompt for both models before Phase 1 commits | A full turn is about 600–750 output tokens. At large-model streaming speeds that likely exceeds a 3.5 s compose window, so the whole page couldn't finish in 5 s. A fast model plus putting the tiles first keeps the learner-visible metrics (text within 2 s, tiles within 5 s) while keeping the editorial insight and plan. Cost: possibly lower writing quality than Sonnet (switchable per step), and NFR-01 is read as first tiles rather than all tiles (OQ-3) | Keep Sonnet and shrink the output: a one-sentence why, no insight or plan (rejected: loses the editorial design, still at risk). Measure first and decide later (rejected: blocks Phase 1 planning) | REQ-001, REQ-003, REQ-012 |
| KD-10 | **Portability: ports and adapters (Eng Lead, 2026-11-04).** Same features, fewest dependencies, so other teams and services can reuse or parse the feature. **enterprise-access:** `apps/personalized_search/core/` is pure Python (standard library only; an import-guard test fails on any Django, DRF, HTTP-client, model-SDK, Algolia or other-app import) and holds the turn pipeline (`orchestrator`, `intent`, `retrieval`, `compose`), the stream encoder, the model registry and `ports.py` (`ModelBackend`, `CatalogSearch`, `SessionStore` protocols). `adapters/` hold one outside dependency each: `algolia` (`multi_search_catalog_index`), `openai_backend`, `xpert_backend`, `django_store` (sessions, history, feedback) and `memory_store` (harness and tests, no database). `wiring.py` is the composition root and the only reader of settings. `core/schemas/` publishes JSON Schemas for the stream parts and Block catalog v1, validated in tests. **Portal:** `core/` (framework-free TS: stream, transports with injected http/CSRF/base URL, workflows, selectors), `ui/` (props-only Paragon components; `EditorialItemCard` takes injected `getItemHref` + `LinkComponent`), `blocks/` (extensible registry: `register(type, {render, slot, validate})`, whose validator `applyPart` uses), `adapters/portal/` (the only module that imports the host app), `feature/` (store factory + Provider, controller with injected dependencies, the tab). Behaviour is unchanged: 9 scripted turns produce byte-identical SSE before and after the refactor (internal equivalence evidence) | Follows the pathway harness's pattern (catalog access and models injected, `pathways/catalog_access.py:34-62`; the harness depends only on the workflow app), which already let the comparison harness run offline. Ports make tiers carve cleanly (Xpert, debug and harness are optional adapters registered in `wiring`), make the core testable without a database, and let another host plug in its own catalog, model or store. Cost: more modules and a composition root to keep current; the schemas need a versioning policy | Reuse `pathways/model_backends` (rejected: non-streaming `complete()` only, the Xpert backend swaps in stored prompts, and importing it couples Personalized Search to the feature it replaces). Keep direct `django.conf.settings` reads and module singletons in the pipeline (rejected: the core can't run outside Django, and model choice branches on model names) | REQ-003, REQ-004, REQ-012, REQ-014 |
| KD-11 | **Results pane as a decision guide (design review, 2026-11-09).** The pane leads with a top pick (reason plus up to three preference checks), then a "Compare top options" row of 2–3 "Best for…" cards, then "More options" by type (two cards per type, a one-line explanation, and "See all (n)" from the search total). A read-only "Why these?" popover lists what the AI inferred as chips, and a caveat block warns when the results can't meet a stated goal. Results update in place across turns: only "Not for me" items are excluded, and each card carries `status` (`new` or `updated`). Saved moves to a "For you \| Saved (n)" tab. Chat replies shrink to a sentence on what changed. All additions are optional block types and props, so catalog v1 stands (G-20) | Learners get one clear starting point instead of equal-weight sections, the chat stops repeating the results, and refining no longer hides earlier work behind "View page" links. Cost: a larger compose output (per-turn cap 5 → 12 items, `max_tokens` ~1800), so first-section latency is re-measured against REQ-012 | Keep a new page per turn (rejected: learners re-scan everything each turn). Coverage badges per item (deferred: no coverage source in the pipeline). An edit-preferences gear (deferred: not designed) | REQ-003, REQ-007, REQ-009, REQ-016 |
| KD-12 | **Telemetry: raw facts and join keys; the warehouse aggregates.** Server-side Segment events (`edx.server.enterprise-access.personalized-search.*`, through `apps/track/segment.track_event`) carry what only the server knows: intent, shown, excluded and shortlisted item keys, drop reasons, raw token counts and timings. The portal sends interaction events (`edx.ui.enterprise.learner_portal.personalized_search.<location>.<action>`) with the ids of what was used and the join keys from a `data-analytics` stream part. No event carries a count, rate, cost or age; those are derived downstream by joining events (and LMS enrollments on `user_id` + `item_key`). Core stays stdlib-only behind an `EventSink` port | One source of truth per fact, no client-side arithmetic to drift, and nothing blocks a turn (sending fails silently). Cost: the dashboards depend on documented joins (`personalized_search_events.md`) | Client-computed metrics (rejected: duplicated logic, ad blockers drop them). Pre-aggregated server metrics (rejected: fixes the questions in code) | REQ-017 |

*If this spec supersedes an earlier (e.g. ungrounded) draft, add a **Superseded Decisions**
table here dispositioning each prior decision (Reversed / Replaced / Carried / Reshaped) with
what grounding found — so the reasoning survives the rewrite.*

## Proposed Design

### High-Level Architecture

Drawn to C4 **Level 1 (System Context)**, **Level 2 (Container)** and **Level 3 (Component)**. Sources are Graphviz files next to the PNGs. L1/L2 are re-rendered with `dot -Tpng -Gdpi=160 <file>.dot -o <file>.png`; the L3 diagrams are templates (`*.dot.tmpl`) whose nodes carry the PR that builds each component, rendered by `diagrams/render.py` from `diagrams/pr-map.json`, and re-rendered whenever a PR is opened, split or closed. Edge labels name the decisions they implement. Updated 2026-11-04: OpenAI is the default model backend with Xpert optional (KD-2 amended), and the two flags (KD-8 amended).

**Level 1: System Context**

![C4 Level 1 System Context](diagrams/c4-l1-system-context.png)

**Level 2: Containers**

![C4 Level 2 Containers](diagrams/c4-l2-container.png)

**Level 3: Components**

Drawn with the KD-10 boundaries, so which dependencies stay out of the core is visible. Solid = Required (Phase 1), bold border = Enhancement (Phase 2), dashed = Optional (comparison and dev tooling).

![C4 Level 3 enterprise-access components](diagrams/c4-l3-enterprise-access.png)

![C4 Level 3 learner portal components](diagrams/c4-l3-portal.png)

- Algolia moves fully server-side. The browser never calls Algolia or the LLM directly; both go through enterprise-access (S-4).
- The cache holds the secured Algolia key using the existing 30-minute helper (`ea/apps/bffs/api.py:93`) and the catalog facet snapshot.
- *[Specified]* The enterprise-access DB is MySQL: the production driver is `mysqlclient==2.3.0` (`requirements/production.txt:335`). The engine itself is set in deploy config (`ea/settings/base.py:157`).

### Architecture in one paragraph

**The server drives the UI, over a standard chat stream.** For each turn, enterprise-access works out the intent, retrieves from Algolia, and has the LLM compose the reply and the learning page. It validates every block against the catalog and streams the result to the portal as AI SDK UI Message Stream v1 parts (KD-4). The portal is a renderer: a pure reducer folds parts into state, and a block registry maps each validated block to a Paragon component (KD-5, KD-6). The turn's final `parts[]` are stored, so reopening a session is a replay (KD-7).

### Turn pipeline (enterprise-access)

**Clock:** every timing is measured from the moment the view is entered (`t0`). `first_text_ms` is `t0` → the first `text-delta` written. `first_section_ms` is `t0` → the first `item_section` `data-block` written. `complete_ms` is `t0` → `finish` written.

**Pre-stream: runs synchronously in the view, before a `StreamingHttpResponse` is returned (KD-3)**

1. Gate: `personalized_search_enabled()` (the enterprise-access flag, per user), else `NotFound()` (KD-8, amended 2026-11-04).
2. Authenticate with `JwtAuthentication` (cookie). Authorize the enterprise-scoped learner permission with the request's `enterprise_customer_uuid` (Security Design). Apply the throttle `personalized_search_turn`.
3. Validate the body (API Contracts). Resolve the per-step model backends (`get_model_backend(prompt_type=…)` once for intent and once for compose); if the configured backend has no `stream()`, return **503 `backend_unsupported`** JSON (no shipped backend hits this after the 2026-10-29 KD-2 amendment; it guards a future backend).
4. Vend or read the secured Algolia key (Retrieval). If enterprise-catalog fails → **503** JSON. This runs **before** the session lock, so a 503 never leaves a session locked. Nothing has streamed yet.
5. Lock the session inside `transaction.atomic()` (the project doesn't set `ATOMIC_REQUESTS`), committing before the response is returned:
   - If `session_uuid` is given, load it with `select_for_update()`, filtering on `lms_user_id` **and** `enterprise_customer_uuid` (missing → 404).
   - If `active_turn_started_at` is within the last 20 s, return **409 `turn_in_progress`**. Otherwise set it to now. This serializes turns per session, so `sequence` (`turn_count + 1`) and `shown_item_ids` can't race.
   - If there's no `session_uuid`, create the session with `turn_count=0` (hidden from history until a turn persists), `first_prompt` = the message cut to 500 characters, and `last_message_at` = now.
   - From here on, every exit path (error, abort, success) clears `active_turn_started_at` in a `finally`.
6. Load everything the generator needs into a frozen `TurnContext` dataclass:
   - learner, session and upstream conversation ids; the turn uuid and sequence
   - the last 4 turns' `{user_message, reply_text}` and the merged prior intent
   - `shown_item_ids`, `asked_attributes`, and `not_interested_ids` (from `ItemFeedback`)
   - the secured key
   - the backend, per-step models and the kill-switch state
   - injectable `clock` and `id_factory`, so tests and golden files are deterministic

   The generator never touches `request`, `crum`, RequestCache or monitoring.
7. Return `StreamingHttpResponse` over a **closing wrapper generator** (`try: yield from map(encode_part, gen) finally: gen.close()`), not a bare `map(...)`. Django 5.2 registers a closer only for iterables that have `.close()`, so a bare `map` would never deliver `GeneratorExit` to the orchestrator on client disconnect (Abort).

**Stream: the generator**

| Step | What happens | Model call | Budget (p90 target, from `t0`) |
| --- | --- | --- | --- |
| 0 | Emit `start`, `data-session` (title `null` unless already set), `data-status{understanding}` | – | 150 ms (pre-stream) |
| 1 | **Intent** (contract below). Merge with the prior intent. If `on_topic=false`, take the **redirect path** | 1 (small) | +800 ms |
| 2 | Emit `data-status{searching}`. **Retrieval:** one Algolia multi-query round trip | – | +400 ms |
| 3 | **Prefilter** and choose the **follow-up attribute** (Retrieval) | – | +50 ms |
| 4 | Emit `data-status{writing}`. **Compose** with `stream()`. The reply streams as `text-delta`. Each completed line after the marker is validated and hydrated, then emitted as `data-block` or `data-followups` | 1 (streamed) | first text ≤ 2.0 s; first `item_section` ≤ 5.0 s; compose done ≤ 7.0 s |
| 5 | Emit `data-session` again with the final `title`, then `data-timing`. **Persist** in one transaction: `ChatTurn`; the session's `shown_item_ids`, `asked_attributes`, `merged_intent`, `turn_count`, `item_count`, `last_message_at`, `title` (set on the first turn from the compose `session_title`, else the first prompt cut to 60 characters); `active_turn_started_at=None` | – | +30 ms |
| 6 | Emit `finish`, then `data: [DONE]`. If persisting failed, emit `error: internal` instead of `finish`, so the client never sees a finished turn that wasn't saved | – | – |

- The step budgets are planning targets checked against measured per-step p90s, not a guarantee that p90s add up.
- **No retries inside the deadline.** A failed step emits `error` and ends the stream, and `active_turn_started_at` is cleared in a `finally`.

**Deadline, timeouts and heartbeats.** A sync generator can only act between blocking calls, so the 20 s deadline is enforced through per-call timeouts:
- each call's timeout is `min(step timeout, deadline − elapsed)`
- step timeouts: intent 3 s (`PERSONALIZED_SEARCH_INTENT_TIMEOUT_S`); Algolia 2 s; compose 5 s per read (`PERSONALIZED_SEARCH_COMPOSE_READ_TIMEOUT_S`) plus a deadline check after every chunk. **Amended 2026-11-04:** these are settings with the spec values as defaults (the dev endpoint had widened them to 8 s / 15 s); devstack may widen them. The Xpert backend uses `XPERT_REQUEST_TIMEOUT` instead (its intent p90 was 4.5 s in the comparison, over 3 s), still capped by the time left before the 20 s deadline. Whether Xpert stays usable at production limits is part of OQ-5/KD-2
- No call can block longer than the remaining deadline (20 s), which is under every known idle timeout on the request path (OQ-4). So v1 sends **no heartbeats**. They'd only be emitted between steps, if added later.

**Abort.** If writing to the socket raises (the client stopped or navigated away), the generator closes the model stream, clears `active_turn_started_at`, and **does not persist the turn**. Shown ids are unchanged, so resending is safe. On the frontend, a stopped turn is shown as "Stopped" and its partial blocks are discarded. The page pane reverts to the previous turn's page.

**Redirect path (REQ-008):**
- No retrieval and no compose.
- Emits a fixed redirect reply ("I can help you find learning in your organization's catalog. What would you like to learn?") as text parts, then `data-followups` with the **opening chips** (below) and `attribute: "open"`.
- Persists a turn with no items (one model call in total). The page pane keeps the previous page.

**Degraded mode (rerank kill switch on, KD-8): no model calls at all.**
- The raw message (first 12 words, like `MAX_QUERY_WORDS`, `course_retrieval.py:88`) is the Algolia query. All three item types are queried.
- Up to 3 candidates per type are emitted in retrieval order, with at most 5 in total, as `item_section` blocks with template deks and no `why` or `fit`.
- Header template: "Results for "{message}"". Reply: "Here's what I found in your catalog." Follow-up: the opening chips.
- **Documented exception:** with no model there's no off-topic check, so REQ-008 isn't guaranteed in degraded mode.

**Opening state (client-side, no server turn; confirmed by the Eng Lead 2026-10-28).**
- The chat shows a greeting ("Hi! What do you want to learn? I search your organization's catalog and build you a learning page.") with the **opening chips**: "Prepare for a leadership role", "Learn AI for my job", "Build data skills", "Something quick, under 4 weeks". These are the prototype's new-learner chips (from the prototype).
- The page pane shows "Your learning page is empty. Tell me what you want to learn and I'll build it."
- This stands in for R-03's popularity opening, which needs the ontology (Out of scope), so the opening renders instantly.

**Text-only turns** (no qualifying items, R-07): no blocks are emitted. The page pane keeps the previous turn's page. The chat shows the reply, the clarifying follow-up, and a "See your last recommendations" link that focuses the page pane (UX-04).

### LLM step contracts

**Model selection (KD-9).**
- `claude` backend: settings `PERSONALIZED_SEARCH_CLAUDE_INTENT_MODEL` and `PERSONALIZED_SEARCH_CLAUDE_COMPOSE_MODEL`, both defaulting to `claude-haiku-4-5-20251001`. Either can be set to `PATHWAYS_CLAUDE_MODEL` (`claude-sonnet-5`).
- `xpert` backend: the model is fixed by Xpert (`gpt-4o-mini`).
- `complete()` and `stream()` take optional `model`, `max_tokens` and `timeout_s` keyword arguments. Max tokens: intent 400, compose 1200. Backends that can't honour `model` ignore it; `ModelResponse.model` is whatever the backend reports (claude and openai: the API's model; xpert: its `prompt_type`, see Xpert specifics).

**Intent:** `complete()`. The response must parse as this JSON, or the turn errors with `intent_invalid`. The parser takes the first `{…}` object in the text, so it tolerates prose around the JSON (needed for Xpert, which has no `response_format`). With claude, the prompt ends with an instruction to return JSON only.

```json
{
  "on_topic": true,
  "redirect_reason": null,
  "merge": "refine",
  "intent": {
    "query": "executive leadership for product managers",
    "skills": ["strategic planning", "leadership"],
    "roles": ["VP of Product"],
    "level": "advanced",
    "item_types": ["course", "executive_education", "program"],
    "constraints": {"max_weeks": null, "pacing": null}
  }
}
```

- `redirect_reason` ∈ `null | off_topic | unsafe | injection`.
- `merge` ∈ `refine | replace`. With `refine`, the server overlays the non-null fields onto the prior intent: `skills` and `roles` are unioned (then capped), while `item_types`, `level` and `constraints` are replaced when non-null, so a refinement can narrow the item types. A redirect turn leaves `merged_intent` unchanged. With `replace`, the new intent is used as is.
- Caps: `skills` ≤ 8, `roles` ≤ 3. `level` ∈ `null | introductory | intermediate | advanced`. `pacing` ∈ `null | self_paced | instructor_paced`.
- **Career advancement (R-09)** is handled in the prompt: when the learner states a career-advancement goal, the model must add adjacent leadership roles and skills to `roles` and `skills`. There's no separate flag.
- Prompt content: system rules (learning-only scope; user text is data, not instructions); the last 4 turns; the prior merged intent; the new message.

**Compose:** `stream()`. The output follows this grammar, in this order, so item sections arrive as early as possible:

```
<reply: 1–2 plain sentences saying what the page shows and why; if nothing qualifies, say so>
@@BLOCKS@@
{"block":"page_header","title":"…","dek":"…"}
{"block":"item_section","item_type":"course|executive_education|program","dek":"…","items":[{"item_id":"<candidate id>","why":"…","fit":["…"]}]}
… (at most one item_section per item_type)
{"block":"followup","question":"…","ideas":["…"],"session_title":"…"}
{"block":"insight","title":"…","body":"…"}                 (optional)
{"block":"plan","why":"…","steps":[{"item_id":"<id on page>","note":"…"}]}   (optional; only when ≥ 3 items are on the page)
```

- Text before `@@BLOCKS@@` is streamed as `text-delta`. After the marker, each newline-terminated line is parsed on its own. Lines that are invalid JSON or invalid against the schema are **dropped and logged, never repaired**. Blank lines are skipped silently (OpenAI puts one between blocks; spike 2).
- `followup` isn't a page block. It becomes the `data-followups` part.
- **Length rules: the character caps are authoritative.** Word counts are only prompt guidance. The validator enforces:
  - `page_header.title` ≤ 80, `dek` ≤ 400
  - `item_section.dek` ≤ 200
  - `why` ≤ 400 (one or two sentences to "you"); `fit` ≤ 3 tags of ≤ 24
  - `question` ≤ 200 (REQ-006). The chips come in two kinds. `suggestions` are always the chosen attribute's fixed chips, which narrow the results. `ideas` are up to 3 model-written chips (≤ 48 each) specific to the conversation, without repeats of the fixed chips. When the attribute is `open` (nothing left to narrow), the ideas replace the fixed opening chips; the opening chips return only when the model wrote no ideas
  - `session_title` ≤ 60
  - `insight.title` ≤ 60, `body` ≤ 600; `plan.steps` 2–4, `note` ≤ 80, optional `why` ≤ 160 (why this order: the best fit for the goal first, then each step building on the last, foundations before advanced and shorter before longer)
- Prompt content: the merged intent; the follow-up attribute from step 3 and the attributes already asked; the ≤ 20 candidates as `{item_id, item_type, title, partner, level, pacing, weeks, short_description (HTML stripped; programs use subtitle) ≤ 300 chars, skills ≤ 6}`; and the last 4 turns. It follows the prototype's editorial rules (from the prototype): stay on topic, no hype, never pad, choose only from the candidates.
- **Estimated output: about 600–750 tokens** for a full turn. The Phase 0 spike measures real throughput (KD-9).

**Server-side validation and hydration (KD-5):**
- `item_id` must be in the turn's candidate set, not in `shown_item_ids`, and not already emitted in this turn. Otherwise it's dropped. A section left empty is dropped.
- At most **one section per `item_type`**, **3 items per section** and **5 items per turn** (R-07). The excess is truncated in emitted order.
- Fields over their caps are truncated (text) or dropped (lists).
- `plan` steps must reference items already emitted on the page.
- **Missing follow-up:** if no valid `followup` line arrives, the server emits `data-followups` with a template question for the chosen attribute (e.g. level: "What level are you aiming for?") and default chips for it. `session_title` then falls back to the first prompt.
- **Fallback section:** if no valid `item_section` has been emitted by the end of the stream and candidates exist, the server emits one deterministic `item_section`. It uses the type with the most candidates, takes its top 3, has no `why`, and is flagged `"fallback": true`.
- **No candidates:** compose still runs, with an empty candidate list, so the reply and clarifying follow-up are written by the model. The parser drops every page block (including `page_header`, so the page pane keeps the previous page) and emits no fallback section. The turn is text-only (R-07).

**Xpert specifics** (optional backend, edx/enterprise-access#351): the Xpert Platform takes our prompts as its system message and the history in each request; its streamed response is decoded into text deltas by `adapters/xpert_backend.py`. Whether it may be used beyond the internal demo is OQ-5.

### Stream contract (KD-4): AI SDK UI Message Stream v1

**Framing**
- Response headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `x-vercel-ai-ui-message-stream: v1` (the dev endpoint omitted the last one; sent since 2026-11-04 and asserted in the view tests).
- The parts and blocks are published as JSON Schemas (`apps/personalized_search/core/schemas/`), and every part the orchestrator emits is validated against them in tests (KD-10).
- Each part is one `data: <json>\n\n` frame. The stream ends with `data: [DONE]\n\n`.
- No heartbeats in v1 (see Deadline; confirmed by the Eng Lead 2026-10-28, consistent with the no-streaming-framework rule in Out of scope).

**Parts used**

| Part `type` | Payload | Persisted in `ChatTurn.parts` | Frontend effect |
| --- | --- | --- | --- |
| `start` | `{messageId: "turn_<ChatTurn.uuid>"}` | yes | open an assistant message |
| `data-session` | `id:"session"`, `data:{session_uuid, turn_sequence, title}`; sent at step 0 and again at step 5 (upsert by id) | yes (final value) | set the active session and its title |
| `data-status` | `data:{step: understanding\|searching\|writing}`, `transient:true` | **no** | status line; composer disabled |
| `text-start` / `text-delta` / `text-end` | `id:"reply"`, `delta` | yes, stored as `text-start`, **one** `text-delta` holding the full reply, and `text-end` | stream the reply into the chat pane |
| `data-block` | `id:"blk_<n>"`, `data:{v:1, type, props}` | yes | upsert by `id` into this turn's page |
| `data-followups` | `id:"followups"`, `data:{question, attribute, suggestions[], ideas[]?}` | yes | follow-up question + chips under the reply |
| `data-timing` | `id:"timing"`, `data:{backend, model_intent, model_compose, intent_ms, retrieval_ms, first_text_ms, first_section_ms, complete_ms, dropped_lines}`, `transient:true` | **no** (stored in `ChatTurn.timing`) | none (dev overlay only) |
| `error` | `{errorText: "<code>"}`: `intent_invalid`, `retrieval_failed`, `model_failed`, `deadline_exceeded`, `internal` | **no**; the turn isn't persisted | inline `Alert` + Retry (UX-04); partial blocks are discarded |
| `finish` | `{}` | yes | re-enable the composer |

- The encoder lives in `apps/personalized_search/stream_protocol.py` as pure functions: `encode_part(dict) -> bytes`, `done() -> bytes`. It's the only backend module that knows the protocol.
- The orchestrator yields protocol-neutral dicts; the view maps them through the encoder.

**Buffered fallback (KD-4 exit):**
- `?mode=buffered` returns `200 application/json {"parts":[…]}` after the turn completes, with the same non-transient parts plus `data-timing`. Errors still come back as an `error` part inside `parts`, with HTTP 200.
- On the frontend, `BufferedChatTransport` yields those parts in order, so the reducer runs unchanged. It's selected with `PERSONALIZED_SEARCH_TRANSPORT=buffered`.

**Worked examples (golden files).**
- **Canonical copies** live in enterprise-access `apps/personalized_search/tests/fixtures/`, generated by the orchestrator with fake steps (intent, retrieval, compose) and a `TurnContext` carrying a fixed `clock` and `id_factory`. A conformance test checks every golden `data-block` against Block catalog v1 and the ItemCard table.
- They're **copied** into the portal at `src/components/dashboard/main-content/personalized-search/stream/__fixtures__/`.
- Each repo has a contract test: the backend regenerates and diffs; the portal reducer consumes them. A change to the contract updates both copies in the same pair of PRs.
- Files:
  - `normal_turn.sse`: start, session, 3× status, text, header, course + program sections, followups, insight, plan, session, finish.
  - `off_topic_turn.sse`
  - `no_match_turn.sse`: text-only plus a clarifying follow-up.
  - `degraded_turn.sse`
  - `error_turn.sse`: text, `page_header`, one `item_section`, then `error:model_failed` as the last part before `[DONE]` (blocks included so the client's discard-on-error path is exercised).

### Block catalog v1 (KD-5)

All blocks are `{v: 1, type, props}`. The frontend renders only `v === 1` and the types it registers; unknown types are dropped, so new types and optional props are additive within v1 (G-20).

| `type` | `props` | Paragon rendering |
| --- | --- | --- |
| `page_header` | `title` (≤ 80 chars), `dek` (≤ 400 chars), `label` (server-set: "Your learning page") | `Container` + heading + lead paragraph |
| `insight` | `title` (≤ 60), `body` (≤ 600) | `Alert` (variant `info`, no dismiss) |
| `item_section` | `item_type` (`course\|executive_education\|program`); `label` (server-set with the actual count, e.g. "Courses · 3 picks", per R-18); `dek` (≤ 200); `fallback` (bool); `items: ItemCard[1..3]`. At most one section per `item_type` per turn | section heading + `CardGrid` of `EditorialItemCard` |
| `plan` | `steps: [{item_id, note ≤ 80}]` (2–4), optional `why` (≤ 160). Emitted only when ≥ 3 items are on the page | "A suggested order" `ol`, with an info button explaining how the steps were picked and why in this order |

`ItemCard` is **server-hydrated from the Algolia hit**. The LLM supplies only `item_id`, `why` and `fit`.

| Field | Source |
| --- | --- |
| `item_id` | course and exec ed: `key`; program: `aggregation_key.split(':').pop()` (uuid). Programs have no `key` (stage index, OQ-7); the portal does the same at `SearchProgramCard.jsx:76` |
| `item_type` | `content_type` + `learning_type == "Executive Education"` → `executive_education` |
| `title`, `image_url` | `title`; `card_image_url`, falling back to `original_image_url` for courses (`SearchCourseCard.jsx:102`). Programs have no `original_image_url`, so they fall back to `banner_image_url` |
| `partner_name`, `partner_logo_url` | course and exec ed: `partners[0].name` / `partners[0].logo_image_url`; program: `authoring_organizations[0].key` / `.logo_image_url` (courses have no `authoring_organizations`) |
| `link` | course and exec ed: `{kind:"course", course_key, course_type}`, which the frontend turns into a URL with `getLinkToCourse` (`src/components/course/data/utils.jsx:873-889`, which adds the exec-ed path segment). Program: `{kind:"program", program_uuid}` → `/{slug}/program/{uuid}` |
| `facts` | `level_type`; course and exec ed: `advertised_course_run.weeks_to_complete` and `advertised_course_run.pacing_type` (not top-level); program: `type` (short name, e.g. `MicroMasters`) and `len(course_keys)`. On courses, `type` is a course-type UUID and isn't used; `course_type` is |
| `why`, `fit` | compose LLM, validated |

**Additive v1 types and props (KD-11, built in the follow-up series):**

| Addition | Shape | Rendering |
| --- | --- | --- |
| `profile` | `summary` (≤ 200), `chips[]` (≤ 6, each ≤ 40), built from the merged intent with no model call, after the results. A preference stays only when at least half the shown results that have the fact meet it (the goal always stays), so "Why these?" never cites something that no longer drives the results | "Why these?" `Popover` |
| `top_pick` | `item: ItemCard`, `reason` (≤ 160), `checks[]` (≤ 3, each ≤ 40) | Top pick card plus details |
| `compare` | `options[2..3]{best_for ≤ 60, item: ItemCard}` | Three mini cards under "Best for…" headings |
| `caveat` | `title` (≤ 60), `body` (≤ 300); at most one per turn | Warning `Alert` |
| `item_section.total`, `.reserve` | search total for the type; up to 2 next-best cards | "See all (n)"; fills a dismissed card |
| card `status`, `section`, `position`; `facts.hours_per_week_min/max` | optional | Updated/New badge; telemetry join keys; hours per week |

**Cards:** a new `EditorialItemCard` built on Paragon `Card` that shows the prototype's "Why this fits you" and `fit` badges. It doesn't reuse `SearchCourseCard` or `SearchProgramCard`: those require Algolia-hit shapes and search tracking context (`SearchCourseCard.jsx:15-17,34,82`; `SearchProgramCard.jsx:66`), and they have no place for `why`.

### Retrieval (S-4)

Reuse points in enterprise-access (all *[Specified]*):
- `AlgoliaSearchClient` (`ea/apps/api_client/algolia_client.py:135-260`) takes a per-call `secured_key: SecuredAlgoliaKey` and refuses a missing or expired key (`:224-259`).
- **It has no multi-query method.** The pinned `algoliasearch==3.0.0` exposes `SearchClient.multiple_queries(queries)`, which POSTs to `1/indexes/*/queries`. **New work:** `AlgoliaSearchClient.multi_search_catalog_index(requests: list[dict], *, secured_key) -> list[dict]`, mirroring `search_catalog_index`'s key resolution and error mapping (`AlgoliaSearchError`).
- The secured key is vended by `EnterpriseCatalogUserV1ApiClient.get_secured_algolia_api_key` (`ea/apps/api_client/enterprise_catalog_client.py:273-294`).
  - The existing `_catalog_access` reads the request through `crum` (`ea/apps/pathways/models.py:43-68`), so **it can't be called inside the generator**. The chat view gets the key pre-stream with `get_and_cache_secured_algolia_search_keys(request, enterprise_customer_uuid)` (`ea/apps/bffs/api.py:93-119`), then `SecuredAlgoliaKey.from_response_payload(...)` (`algolia_client.py:96`).
  - If the cached key `is_expired()` (`:123`), it is re-vended once by calling `EnterpriseCatalogUserV1ApiClient.get_secured_algolia_api_key` directly, without writing to the BFF's shared cache entry (`get_and_cache_secured_algolia_search_keys` has no bypass parameter). That covers the cache's fixed 30-minute TTL possibly outliving the key's `valid_until`.
- Filter-building patterns come from `build_course_filters` (`ea/apps/pathways/course_retrieval.py:150-168`), and query-building from `build_course_query` (`:109`). Their course-only clause (`:160`) is not reused as is.

**One `multiple_queries` call, up to 4 sub-requests**, all on `client.catalog_index_name`:

| # | Sub-request | `filters` | `query` |
| --- | --- | --- | --- |
| 1 | courses (incl. exec ed), strict | `content_type:course AND language:"<Language>" AND metadata_language:"<locale>" AND enterprise_customer_uuids:"<uuid>" AND (skill_names:"s1" OR …) AND NOT key:"<excluded>" …`, plus `AND learning_type:"Executive Education"` when `item_types` is only `executive_education`, or `AND NOT learning_type:"Executive Education"` when it's only `course` | `intent.query` |
| 2 | courses, broadened | same without the skills clause | skills + roles joined, `removeWordsIfNoResults=allOptional` |
| 3 | programs, strict | `content_type:program AND metadata_language:"<locale>" AND enterprise_customer_uuids:"<uuid>" AND (skill_names:… ) AND NOT aggregation_key:"program:<shown>" …` | `intent.query` |
| 4 | programs, broadened | same without skills | as #2 |

- **Content language (2026-11-09), as Learner Pathways does it.** The portal sends the learner's locale from `getPathwaysSupportedLocale()` (its `getLocale()`, kept only when the MFE's `SUPPORTED_ALGOLIA_LANGUAGE_FILTERS` lists it). The server maps it through `PERSONALIZED_SEARCH_CONTENT_LANGUAGES` (`en` → English, `es` → Spanish), and falls back to English otherwise. The catalog index holds every course and program twice, once per `metadata_language` (`en` and `es`, with translated title and description). So every sub-request filters `metadata_language:"<locale>"`, and course sub-requests also filter `language:"<Language>"`, the teaching language, as Pathways does. Programs get only the metadata filter, since their `language` lists their courses' languages. Before this, only `language:"English"` was applied, so a turn could return Spanish-translated records. Skill names are English on both record sets, so the intent step keeps `skills` in English. The compose prompt carries `RESPONSE LANGUAGE: <Language>`, and every learner-facing string the model writes follows it. The server's fixed copy (redirect reply, fixed chips, profile labels) is still English.
- Program sub-requests are omitted when `item_types` excludes `program`. Course sub-requests are omitted when it excludes both `course` and `executive_education`.
- Each sub-request uses `hitsPerPage` 30 for courses and 15 for programs, `distinct: true` (the index is sharded per content item and collapses on `aggregation_key`), and an `attributesToRetrieve` card list: `key, aggregation_key, content_type, learning_type, course_type, title, subtitle, short_description, level_type, partners, authoring_organizations, card_image_url, original_image_url, banner_image_url, skill_names, advertised_course_run, type, course_keys, language`.
- The `NOT` clauses come from `shown_item_ids` plus not-interested ids. Stored ids aren't typed, so **every excluded id goes into both clause sets**: `NOT key:"<id>"` on course sub-requests and `NOT aggregation_key:"program:<id>"` on program sub-requests. A non-matching clause is harmless.
- Every Algolia call has a per-call timeout of `min(2 s, deadline − elapsed)`, passed through algoliasearch 3.0.0's request options (**new work**: `SearchClient.create` today uses library defaults, `algolia_client.py:178`). A timeout maps to `AlgoliaSearchError` → `error: retrieval_failed`.
- *[Specified, stage catalog index, 2026-10-27]* Facts from the stage catalog index, read with a search-only key:
  - Exec ed records are `content_type:course` with `learning_type:"Executive Education"` (exact casing) and `course_type: executive-education-2u`. All are `language: English`, so the English filter drops none.
  - `content_type`, `learning_type`, `language`, `course_type` and `enterprise_customer_uuids` are all filterable. `enterprise_*_uuids` can't be read back from hits.
  - On programs, `language` is a list; `language:"English"` still matches.
  - **Exec ed `skill_names` is empty on stage, so the strict (skills-OR) course sub-request never returns exec ed.** Exec ed arrives only through the broadened sub-request (#2). The prefilter therefore doesn't penalize the broadened tier for `executive_education` items.
  - Exec ed `short_description` is HTML. Tags are stripped before it goes into the compose prompt.
  - Programs have no `short_description`, so `subtitle` is used instead.

**Prefilter (deterministic):**
1. Dedupe by item id.
2. Classify `item_type` (exec ed = `learning_type == "Executive Education"`).
3. Score = tier (strict 2, broadened 1; **`executive_education` items always score tier 2**, because exec ed has no skills on stage and only arrives through the broadened sub-request) + level match (+1) + max-weeks constraint met (+1) − Algolia rank/100.
4. After merging all sub-request hits, keep the top 20 with type caps: ≤ 10 course, ≤ 5 executive_education, ≤ 5 program. `constraints.pacing` adds +1 for a match; it scores, it doesn't filter.

**Follow-up attribute (R-15):**
- Candidate attributes are `level` (`level_type`), `pacing` (`advertised_course_run.pacing_type`), `time` (`advertised_course_run.weeks_to_complete` bucketed as ≤ 4, 5–12, > 12) and `type` (`item_type`). Drop those already in `asked_attributes` or already set in the merged intent.
- Pick the attribute with the most even split across the 20 candidates (≥ 2 distinct values, each with ≥ 2 items). "Most even" = the smallest largest-bucket share; candidates with a null value for the attribute are left out of its buckets (programs have no pacing or weeks). Ties go in the order `level`, `time`, `pacing`, `type`. If none qualifies, use `open` (an open invitation). `open` is never added to `asked_attributes`, so it can repeat.

**Ranking:** the compose step selects and orders from the 20. The pathways rerank contract's guarantee (drop invented keys, `reranking.py:85-134`) is enforced by the KD-5 validator, so there's no separate rerank call (that saves one LLM round trip).

### Backend module map (enterprise-access)

*Planning-time map. The as-built layout follows KD-10 (see "Module layout as built" below): the same responsibilities, split into `core/`, `adapters/` and `wiring.py`.*

| Module | Responsibility |
| --- | --- |
| `apps/personalized_search/apps.py` | `PersonalizedSearchConfig`; add to `PROJECT_APPS` (`ea/settings/base.py:81-97`) |
| `models.py` | `ChatSession`, `ChatTurn`, `ItemFeedback` (Data Model) |
| `context.py` | the frozen `TurnContext` dataclass; `build_turn_context(request, validated_data)`. All pre-stream I/O happens here |
| `orchestrator.py` | `run_turn(ctx, *, intent_step, retrieve, compose_step) -> Iterator[dict]` (clock and id factory come from `ctx`), which yields protocol-neutral part dicts. Has the redirect, degraded, deadline and abort handling |
| `intent.py` | intent prompt, schema validation, merge |
| `retrieval.py` | build the multi-query requests, prefilter, choose the follow-up attribute |
| `compose.py` | compose prompt; `ComposeStreamParser` (marker split + line parser) |
| `blocks.py` | Block validators written with `attrs` `@define`, as `CourseCandidate` is (`ea/apps/pathways/models.py:659-699`), so no new dependency; also caps, hydration from hits, and the fallback section |
| `stream_protocol.py` | AI SDK v1 encoder (the only protocol-aware module) |
| `persistence.py` | `persist_turn(ctx, parts, …)` (one transaction) |
| `ea/apps/api/v1/views/personalized_search.py` | `PersonalizedSearchTurnView` (streaming), `SessionViewSet` (list/retrieve), `ItemFeedbackViewSet` |
| `ea/apps/pathways/model_backends/base.py` (+ `claude.py`, `xpert.py`) | **Additions:** `StreamEvent = TextDelta(text) \| Done(response: ModelResponse)`; `ModelBackend.stream(*, system_prompt, messages: list[dict], trace_id, timeout_s) -> Iterator[StreamEvent]`, which raises `ModelBackendConfigurationError` by default (a backend without `stream()` is unsupported); `complete()` gains an optional `timeout_s` |
| `claude.py` | `stream()` via `client.messages.stream(...)` yielding `text` deltas. The client is constructed with `timeout` (`:68`). `model`, `max_tokens` and `timeout_s` are per call (intent: `PERSONALIZED_SEARCH_CLAUDE_INTENT_MODEL`, 400 tokens; compose: `PERSONALIZED_SEARCH_CLAUDE_COMPOSE_MODEL`, 1200 tokens; KD-9) |
| `openai.py` | `stream()` via `client.chat.completions.create(..., stream=True, stream_options={"include_usage": True})` yielding `delta.content`. **No `response_format`** on `stream()`: compose is prose plus JSON lines, and `json_object` mode is rejected unless a message says "JSON" (spike 2). `model`/`max_tokens`/`timeout_s` per call; model defaults to `PATHWAYS_OPENAI_MODEL` (`gpt-4o`) |
| `xpert.py` | `stream()` via a new `XpertAPIClient.send_message_stream(...)` with `'stream': True` (today hard-coded `False`, `ea/apps/prompts/api_client.py:218,280`). It reads cumulative JSON objects incrementally, emits the new suffix as deltas, passes up to 4 prior turns in `messages` (already a list, `:268`), and takes an explicit timeout. The `personalized_search_intent` / `personalized_search_compose` prompt rows are seeded in the `prompts` app (Xpert specifics). All three backends' `_complete` gain the optional `model`, `max_tokens` and `timeout_s` (`claude.py:71`, `xpert.py:47`, `openai.py:77`) |
| `ea/apps/api_client/algolia_client.py` | **Addition:** `multi_search_catalog_index` |
| `ea/apps/core/rules.py`, `constants.py` | **Additions:** an enterprise-scoped learner permission (Security Design) |

### Frontend module map (KD-6)

*Planning-time map. The as-built layout follows KD-10 (`core/`, `ui/`, `blocks/`, `adapters/portal/`, `feature/`); see "Module layout as built" below.*

All new code lives under `src/components/dashboard/main-content/personalized-search/` and imports nothing from `learner-pathways/`.

| Folder / file | Layer | Responsibility |
| --- | --- | --- |
| `PersonalizedSearchTab.tsx` | UI root | Dual pane at ≥ 768 px. Below 768 px, **Phase 1** stacks the chat above the page in a single column; **Phase 2** replaces that with the chat/page toggle (`useMediaQuery`). Uses only the controller |
| `chat/` (`ChatPane`, `MessageList`, `AssistantMessage`, `Composer`, `FollowUpChips`, `StatusLine`) | presentational | Props only; `Form.Control as="textarea"`, `Chip`, `Skeleton`, `Alert` |
| `page/` (`LearningPagePane`, `PageHeaderBlock`, `InsightBlock`, `ItemSectionBlock`, `PlanBlock`, `EditorialItemCard`) | presentational | Props only |
| `page/blockRegistry.ts` | registry | `{page_header: PageHeaderBlock, …}`; `renderBlock(block)` returns `null` for an unknown `type` or `v` |
| `history/` (`HistoryPanel`, `PastSearchBanner`), `saved/SavedView` | presentational (Phase 2) | `Sheet` for history at all widths |
| `hooks/usePersonalizedSearchController.ts` | controller | The only place hooks or context are resolved: `useEnterpriseCustomer`, `useIntl`, `getAuthService`. Exposes `send`, `stop`, `retry`, `newChat`, `openSession`, `toggleSave`, `dismiss` |
| `workflows/` (`sendTurnWorkflow.ts`, `reopenSessionWorkflow.ts`, `itemFeedbackWorkflow.ts`, `types.ts`) | workflows (hook-free) | Typed input and result. For example, `sendTurn({transport, store, request})` iterates `transport.send()` and dispatches each part to the store |
| `stream/` (`parseSse.ts`, `applyPart.ts`, `types.ts`) | protocol adapter | `parseSse(ReadableStream) → AsyncIterable<Part>`; `applyPart(state, part) → state` is pure. These are the only files that know the protocol |
| `services/` (`ChatTransport.ts` interface, `SseChatTransport.ts`, `BufferedChatTransport.ts`, `FixtureChatTransport.ts`, `sessionsRepository.ts`, `feedbackRepository.ts`) | ports and adapters | SSE: refresh the JWT, get the CSRF token, `fetch` POST with `credentials:'include'` and an `AbortController` (Current State). Buffered: the same request with `?mode=buffered`, yielding the returned `parts` (KD-4 fallback). Fixture: replays the `.sse` golden files with configurable delays. Repositories use `getAuthenticatedHttpClient()` |
| `state/personalizedSearchStore.ts` | zustand | `{sessionUuid, turns[], activeTurnId, viewedTurnId, status, savedIds, mode: 'live' \| 'past', mobilePane, hasNewPicks}` (`viewedTurnId` drives "View page"; the last two are Phase 2 mobile state); setters only; **not** persisted to localStorage (the server is the record) |
| `messages.ts` | i18n | `defineMessages`, ids `learner.personalizedSearch.*` |

Transport selection: `getConfig().PERSONALIZED_SEARCH_TRANSPORT` is `fixture` (local development and demos without a backend), `buffered` (KD-4 fallback), or unset for SSE. It's a new optional env var.

**Tab registration** in `useDashboardTabs.jsx`: add the key `personalized-search` to `dashboard/data/constants.js:13-23` and `tabState` (`:77-100`). The entry in `allTabs` (`:172-255`) follows the programs entry shape (`:191-204`), visible and available when `isLearnerPathwaysEnabled` (`:64-68`) (KD-8). Title: "Personalized Search" with a "Beta" badge, the same as the Pathways title (`:217-225`).

**Tab gate (amended 2026-11-04, KD-8):** the Personalized Search tab is visible and available when `enterpriseFeatures.enterprisePersonalizedSearchEnabled` is true, and nothing else; the Pathways tab keeps `isLearnerPathwaysEnabled`.

### Module layout as built (KD-10)

**enterprise-access `apps/personalized_search/`**

| Path | Role | Depends on |
| --- | --- | --- |
| `core/constants.py`, `errors.py`, `stream.py`, `ports.py`, `model_registry.py` | Limits and copy; shared errors; SSE encoder + stored parts; the ports (`ModelBackend`, `CatalogSearch`, `SessionStore`, `Usage`); model id → backend routing | standard library |
| `core/intent.py`, `retrieval.py`, `compose.py`, `orchestrator.py` | The turn pipeline; retrieval takes a `CatalogSearch`; the orchestrator takes `Steps` (intent, retrieve, compose, persist) and a `TurnContext` (clock, ids, timeouts, platform names) | `core/` only |
| `core/schemas/` | JSON Schemas: `stream-parts.v1`, `block-catalog.v1`, plus a README with the versioning rule | – |
| `adapters/algolia.py` | `AlgoliaCatalog` over `AlgoliaSearchClient.multi_search_catalog_index` | `api_client` |
| `adapters/openai_backend.py`, `adapters/xpert_backend.py` | `ModelBackend`s; configuration passed to the constructor | `openai` (lazy), `requests` |
| `adapters/django_store.py`, `adapters/memory_store.py` | `SessionStore` (+ history and feedback reads) on the ORM; the same contract in memory | Django ORM; nothing |
| `wiring.py` | Composition root: settings → registry, catalog, store, `TurnContext` | Django settings, `bffs` |
| `renderers.py`, `models.py`, `migrations/`, `harness.py`, `management/` | DRF event-stream renderer; tables; comparison harness (memory store, injected judge and personas) | Django |

**Portal `src/components/dashboard/main-content/personalized-search/`**

| Module | Role | May import |
| --- | --- | --- |
| `core/` | stream types, `parseSse`, `applyPart`, fixtures, transports, workflows, selectors | nothing from React, `@edx/*` or the host app |
| `ui/` | presentational Paragon components and `messages.ts` (ids unchanged) | Paragon, `react-intl` via `@edx/frontend-platform/i18n`; not `adapters/` or the host app |
| `blocks/` | the block registry and the four default blocks | `core/`, `ui/` |
| `adapters/portal/` | config (one base-URL resolver), auth http client, links (`getLinkToCourse` + `Link`), repositories, enterprise context, transport selection | the host app |
| `feature/` | store factory + Provider, controller hook with injected dependencies, `PersonalizedSearchTab` | all of the above |

Guard tests enforce the "may import" columns in both repos.

### Data Model

**New Models / Tables**

All three models are in `apps/personalized_search`, built on `model_utils` `TimeStampedModel` (+ `SoftDeletableModel` where noted), matching `SubsidyRequest` (`ea/apps/subsidy_request/models.py:18,41`).

| Model / Table | Fields | Relationships | Notes |
| --- | --- | --- | --- |
| `ChatSession` (TimeStamped, SoftDeletable) | `uuid` UUID PK (default uuid4, **sent to the browser**); `upstream_conversation_id` UUID (default uuid4, **server-only**; sent to Xpert as `conversation_id` and never serialized); `lms_user_id` int, indexed; `turn_count` int, default 0; `active_turn_started_at` datetime, nullable; `enterprise_customer_uuid` UUID; `title` char(60), nullable; `first_prompt` char(500) (set at creation); `last_message_at` datetime (default now); `item_count` int, default 0; `shown_item_ids` JSON list[str]; `asked_attributes` JSON list[str]; `merged_intent` JSON | has many `ChatTurn` | Index (`lms_user_id`, `enterprise_customer_uuid`, `-last_message_at`). `.. pii: first_prompt and title hold learner-written text. .. pii_types: other. .. pii_retirement: retained` (stage-only POC; retirement is deferred with retention, see Out of scope; convention at `ea/apps/customer_billing/models.py:1050-1053`, checked by `make pii_check`). The list endpoint returns only sessions with `turn_count > 0`, so a failed first turn leaves an invisible empty session |
| `ChatTurn` (TimeStamped) | `id` BigAuto; `uuid` UUID (default uuid4, unique; becomes `messageId` `turn_<uuid>`); `session` FK; `sequence` int; `user_message` char(500); `reply_text` text (the coalesced reply, used to build history for the next turn); `intent` JSON; `parts` JSON (the stored stream: transient parts and `data-timing` removed, text deltas coalesced into a single `text-delta` between `text-start` and `text-end`, so every stored part is still a protocol part); `item_ids` JSON list[str]; `follow_up_attribute` char(32); `backend` char(16); `timing` JSON | belongs to `ChatSession` | Unique (`session`, `sequence`). PII annotation as above (`user_message`, `parts`). Written once, after a successful stream |
| `ItemFeedback` (TimeStamped) | `id` BigAuto; `lms_user_id` int; `enterprise_customer_uuid` UUID; `item_id` char(255); `item_type` char(32); `kind` char(16) `saved\|not_interested`; `session` FK, nullable; `card_snapshot` JSON (the `ItemCard` at save time) | optional `ChatSession` | Unique (`lms_user_id`, `enterprise_customer_uuid`, `item_id`, `kind`). `.. no_pii` beyond user linkage |

**Migrations Required**

| Migration | Description | Risk (table size, locks) | Rollback Strategy |
| --- | --- | --- | --- |
| `personalized_search/0001_initial` | Creates the 3 new tables | None (new, empty tables) | Turn the switch off. Tables are left in place; a later `migrate personalized_search zero` drops them (run it **before** reverting the code, or Django can't unapply a missing migration file) |
| `prompts/0004_…_prompt_type` | Adds the two `PromptType` choices (choices-only alter) | None (no schema change on MySQL) | Reverse migration |
| `prompts/0005_seed_personalized_search_prompts` | `get_or_create` the intent and compose rows | Two rows in an existing table; existing rows untouched | Reverse deletes only those two rows |

Retention and purge are deferred (Out of scope). The POC stores data only in stage.

### API Contracts

**New Endpoints**

Base: `/api/v1/personalized-search/`. All endpoints:
- 404 when the enterprise-access flag is off for the user (KD-8).
- `JwtAuthentication` (cookie) and `IsAuthenticated`.
- `@permission_required(PERSONALIZED_SEARCH_LEARNER_PERMISSION, fn=get_or_fetch_enterprise_uuid_for_bff_request)` (`ea/apps/api/utils.py:84-92`). It reads the query param or the body, so it works for GET and POST.
- Session and feedback rows are additionally filtered by `lms_user_id = request.user.lms_user_id` (`ea/apps/core/models.py:20`; mapping `ea/settings/base.py:345-350`).

| Method | Path | Request Body | Response | Auth | Notes |
| --- | --- | --- | --- | --- | --- |
| POST | `turns/` | `{"enterprise_customer_uuid": uuid, "session_uuid": uuid\|null, "message": str(1..500)}` | `200 text/event-stream` (Stream contract); with `?mode=buffered`, `200 {"parts":[…]}` | learner permission + CSRF | Throttle scope `personalized_search_turn` (`20/min` per PRD NFR-04, tightened once OQ-1 lands). `session_uuid: null` creates a session |
| GET | `sessions/?enterprise_customer_uuid=&page=` | – | `200 {"count","next","previous","results":[{"uuid","title","first_prompt","item_count","last_message_at","created"}]}`, 20 per page, newest first | learner permission | Phase 2 (REQ-010) |
| GET | `sessions/{uuid}/?enterprise_customer_uuid=` | – | `200 {"uuid","title","created","turns":[{"sequence","user_message","parts":[…],"created"}]}` | learner permission + owner | Another learner's or another enterprise's session → 404 |
| GET | `item-feedback/?enterprise_customer_uuid=&kind=saved` | – | `200 {"results":[{"item_id","item_type","kind","card_snapshot","created"}]}` | learner permission | Phase 2 (REQ-009) |
| POST | `item-feedback/` | `{"enterprise_customer_uuid","item_id","item_type","kind","session_uuid"?,"card_snapshot"}` | `201` (idempotent: an existing row → `200`) | learner permission + CSRF | `not_interested` rows exclude that item from every later turn for this learner in this enterprise. `TurnContext` loads them from `ItemFeedback` (the table ships in Phase 1 and stays empty until Phase 2) |
| DELETE | `item-feedback/?enterprise_customer_uuid=&item_id=&kind=` | – | `204` | learner permission + CSRF | Undo |

**Modified Endpoints**

| Method | Path | Change | Breaking? | Migration Path |
| --- | --- | --- | --- | --- |
| – | – | No existing endpoint changes. `XpertAPIClient` gains a streaming method alongside `send_message`; `ModelBackend` gains `stream()` with a default that raises | No | – |

**API Error Codes**

| Code | Condition | Client Action |
| --- | --- | --- |
| 400 | Body invalid (message empty or > 500 characters, bad uuid) | Show a validation message; keep the draft |
| 403 | No learner role for that enterprise | Log it and show the "Personalized Search isn't available right now" Alert with no Retry (tab visibility comes from the flags, so the tab entry itself stays) |
| 404 | **Turn endpoint:** flag off for the user. **Session endpoints:** session not found or not owned | Turn 404: show a "Personalized Search isn't available right now" `Alert`, because the tab is already hidden by the LMS flag and a 404 here means the enterprise-access flag is off. Session 404: drop the session from the list |
| 409 | `turn_in_progress`: another turn on this session is still streaming | Keep the composer disabled and retry once after 2 s; if that also returns 409, show "Your previous message is still being answered. Try again in a moment." + Retry |
| 429 | Throttled | "You're sending messages quickly. Try again in a minute." |
| 503 | enterprise-catalog failed to vend the secured key, or `backend_unsupported` (before the stream) | Inline `Alert` "We couldn't search your catalog right now. Try again." + Retry |
| in-stream `error` part | `intent_invalid`, `retrieval_failed`, `model_failed`, `deadline_exceeded`, `internal`. A step that hits its **own** timeout emits its step's code (`model_failed`, `retrieval_failed`); `deadline_exceeded` is used only when the remaining turn deadline was the binding limit | Replace the pending turn with an `Alert` "Something went wrong building your page. Try again." and Retry (UX-04). Nothing was persisted |

### Sequence Diagrams

One turn, at container level (consistent with the C4 L2 diagram):

```mermaid
sequenceDiagram
    participant MFE as Learner Portal tab
    participant EA as enterprise-access (sync worker)
    participant DB as enterprise-access DB
    participant EC as enterprise-catalog
    participant AL as Algolia
    participant LLM as LLM (claude | xpert)
    MFE->>EA: POST /personalized-search/turns/ (cookie JWT, X-CSRFToken)
    EA->>DB: lock/create session (409 if a turn is active), last 4 turns, shown + not-interested ids
    EA->>EC: secured key (cached 30 min)
    EA-->>MFE: 200 text/event-stream: start, data-session
    EA->>LLM: intent (complete, JSON)
    EA-->>MFE: data-status searching
    EA->>AL: multiple_queries (≤4 sub-requests)
    EA-->>MFE: data-status writing
    EA->>LLM: compose (stream)
    loop tokens
      LLM-->>EA: delta
      EA-->>MFE: text-delta / validated data-block
    end
    EA-->>MFE: data-followups, (insight, plan), data-session(title), data-timing
    EA->>DB: persist ChatTurn + session update (one transaction)
    EA-->>MFE: finish, [DONE]
```

### Integration Points

| External System | Protocol (REST / gRPC / Event) | Contract | Failure Mode (Timeout / 5xx / Queue full) | Fallback |
| --- | --- | --- | --- | --- |
| enterprise-catalog | REST | `get_secured_algolia_api_key` (`enterprise_catalog_client.py:273-294`) | 5xx or timeout, before the stream | 503 JSON before the stream starts; no unscoped fallback (`ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH=False`, `ea/settings/base.py:545`) |
| Algolia | REST (`multiple_queries`) | Retrieval | timeout (2 s) or 5xx | `error: retrieval_failed` |
| Anthropic | HTTPS streaming | `messages.stream` | timeout (intent 3 s; compose 5 s per read, total bounded by the turn deadline), 5xx | `error: model_failed`; no retry inside the deadline |
| Xpert | HTTPS (cumulative-JSON stream) | `/v1/message` `stream:true` | timeout or malformed chunk | `error: model_failed` |

### Event / Message Contracts

No queues. Besides the SSE stream (see Stream contract), the feature sends Segment usage events (KD-12): server-side events from enterprise-access and interaction events from the portal, joined downstream on `conversation_id`, `turn_id` and `item_key`. The event list, properties and join recipes are in [`personalized_search_events.md`](personalized_search_events.md).

## Non-Functional Design

### Performance Budget

| Metric | Target | Measurement | Current Baseline |
| --- | --- | --- | --- |
| Time to first reply text (`first_text_ms`) | p90 < 2 s (NFR-01) | `ChatTurn.timing` + the structured turn log, measured from view entry, over the 30-prompt demo script on stage | None (new) |
| Time to first item section (`first_section_ms`) | p90 < 5 s (NFR-01 "tiles", K-05; KD-9) | same | The pathways request runs 3 LLM calls in series, unstreamed (Current State) |
| Time to page complete (`complete_ms`) | p90 < 7 s (KD-9) | same | – |
| Compose output tokens | ~600–750 per full turn (estimate) | `ModelResponse.output_tokens` in the timing | Phase 0 spike measures tokens/s per model |
| Pre-stream (auth + DB + key) | < 150 ms (cached key) | log line before the response is returned | – |
| Intent call | p90 < 900 ms | `intent_ms` | – |
| Algolia multi-query | p90 < 400 ms (NFR-01 sets < 500 ms excluding LLM) | `retrieval_ms` | – |
| Session list / reopen | < 1 s / < 2 s (NFR-01) | request timing | – |
| Concurrent turns | bounded by the deployed sync worker count | deploy configuration (OQ-1) | small in stage; demo users kept ≤ 3 |

### Scalability Considerations

**What breaks first:** sync worker capacity. Stage runs few sync workers. Each in-flight turn holds a worker for up to about 7 s at p90 (20 s worst case), so 10× demo load would queue other enterprise-access requests (Risk 1). Mitigations for v1 are the throttle, the 20 s deadline and gating to an internal audience. The path to scale is out of scope for v1: a separate ASGI deployment of the same code, routed only for `/api/v1/personalized-search/turns/`. LLM cost scales linearly at 2 calls per turn (1 when redirected, 0 when degraded).

### Security Design

| Concern | Approach | Validation |
| --- | --- | --- |
| Authentication | `JwtAuthentication` with the cookie JWT; `X-CSRFToken` on POST and DELETE (Current State) | Unit tests: missing CSRF → 403; anonymous → 401 |
| Authorization | **New** `PERSONALIZED_SEARCH_LEARNER_PERMISSION` built from `_has_implicit_access_to_role(_, enterprise_customer_uuid, LEARNER_PATHWAYS_LEARNER_ROLE)` plus an explicit-assignment twin (`ea/apps/core/rules.py:12-35`), resolved with `fn=get_or_fetch_enterprise_uuid_for_bff_request` (`ea/apps/api/utils.py:84-92`), which reads the query param or the body. `get_enterprise_uuid_from_request_data` (`:36-49`) reads only the body, so it would fail on GET. It is scoped to the requested enterprise (Risk 7). The explicit twin matches only an assignment for exactly the requested enterprise: all-enterprise assignments (`*` context, `applies_to_all_contexts`) do **not** grant this learner permission. Every session and feedback query also filters on `lms_user_id` | Tests: a learner of enterprise A asking for enterprise B → 403; learner X reading learner Y's session → 404 |
| Data at rest | Enterprise-access DB (stage). Prompts and replies stored per KD-7. No secrets stored | `make pii_check` passes with the new annotations |
| Data in transit | HTTPS throughout. Secured Algolia key and model API keys stay server-side; the browser never sees Algolia, the LLM, Xpert or upstream conversation ids | Code review: the stream contract carries no keys or upstream ids |
| Input validation | `message` 1–500 characters (R-04); uuids validated. The user text is passed to the LLM as delimited data, and the intent prompt instructs the model to treat it as data (REQ-008). The model's output is never rendered as HTML: text renders as plain text, and blocks pass through KD-5 validation | Adversarial prompt list in tests; a "block with an `<script>` title renders as text" test |
| PII handling | First enterprise-access tables storing learner-written text (`first_prompt`, `user_message`, `parts`). PII annotations; no prompt text in logs (log ids, lengths and timings only); retention deferred | Log review in the Phase 1 demo dry run |

### Accessibility Impact

- Paragon components only: `Card`, `CardGrid`, `Chip`, `Alert`, `Form.Control`, `Sheet`, `Skeleton`, `Stack`, `IconButton`, `useMediaQuery`. All exist in 22.17.0.
- Chat messages sit in a list. A polite live region announces "New reply, N picks on your learning page" once per turn, not per token (NFR-03 subset).
- Focus returns to the composer when the turn finishes (it's disabled mid-turn). Chips and cards are keyboard-operable.
- `prefers-reduced-motion` disables the block entrance transition.
- A full WCAG audit is out of scope.

## Alternatives Considered

Survey of mature "streaming chat + generative UI" architectures, done 2026-10-27 against current docs. Versions were checked on npm and PyPI; no code was run.

| # | Alternative | Pros | Cons | Why Not Chosen |
| --- | --- | --- | --- | --- |
| 1 | **Vercel AI SDK UI** (`ai` / `@ai-sdk/react` 4.x, AI SDK 7; Apache-2.0). UI Message Stream v1 over SSE ([protocol](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol)) | Widely adopted; spec'd for any backend language; typed `data-*` parts upsert by id ([streaming data](https://ai-sdk.dev/docs/ai-sdk-ui/streaming-data)); custom `ChatTransport` ([transport](https://ai-sdk.dev/docs/ai-sdk-ui/transport)); React 18 peer OK | Protocol versioned by Vercel; resumable streams need Node + Redis ([resume](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams)); `stop()` only closes the socket; TS ≥ 5 typings vs our 4.9.5 unverified; ESM in Jest | **Protocol chosen (KD-4); the client library isn't (KD-6).** We emit the protocol from Django and parse it ourselves |
| 2 | **AG-UI** (`ag-ui-protocol` 1.0 Python, `@ag-ui/client`; MIT) ([events](https://docs.ag-ui.com/concepts/events)) | Open protocol with an official Python SDK; JSON-Patch state deltas; carries any UI spec | Main React client CopilotKit is "not officially supported" without its runtime; non-TS runtimes need a commercial tier ([runtime](https://docs.copilotkit.ai/backend/copilot-runtime)); more ceremony than one agent needs | Nearest fallback. assistant-ui speaks both, so a later switch only changes the encoder/parser pair |
| 3 | **Google A2UI** v0.9 (Apache-2.0) ([spec](https://a2ui.org/specification/v0.9-a2ui/)) | Declarative "trusted catalog" security model; React renderer | Pre-1.0 and churning; the LLM authors the UI spec, so server-side id validation still has to be added; adjacency-list model is heavy for a fixed block list | We borrow the idea (closed catalog, flat blocks, ids) in KD-5 without taking the dependency |
| 4 | **MCP Apps / MCP-UI / OpenAI Apps SDK** ([docs](https://modelcontextprotocol.io/docs/extensions/apps)) | Standard for third-party UI inside Claude or ChatGPT | Sandboxed iframe HTML breaks Paragon theming, focus flow and analytics; server-authored HTML | Built for embedding in someone else's host, not our own MFE |
| 5 | **OpenAI ChatKit** ([guide](https://developers.openai.com/api/docs/guides/chatkit)) | Turnkey widgets; Python server SDK | UI loads from OpenAI's CDN; fixed widget vocabulary that isn't Paragon; hosted path is being shut down 2026-11-30 | Vendor lock-in, and it can't use our design system |
| 6 | **assistant-ui** 0.15 (MIT) ([runtimes](https://www.assistant-ui.com/docs/architecture.md)) | Headless chat primitives; adapters for AI SDK, AG-UI and external store | Pre-1.0 API churn; primitives need Paragon styling | Kept as a later chat-shell swap behind the controller (KD-6) |
| 7 | **LangGraph generative UI** ([docs](https://docs.langchain.com/langsmith/generative-ui-react)) | `push_ui_message` + `useStream` | Requires LangGraph Agent Server; remote components rendered in shadow DOM | Platform lock-in; undermines Paragon |
| 8 | **json-render** (Vercel Labs), **OpenUI/Thesys C1**, **Hashbrown** | Catalog-spec ideas; progressive spec streaming | json-render needs React ≥ 19.2; OpenUI is a hosted DSL; Hashbrown is Node-orchestrated with small adoption | Blocked by React version, hosting or maturity |
| 9 | **Partial-JSON UI streaming** (tool-argument deltas parsed on the client) | Fastest-looking page fill | A card can render before its id is validated, breaking "the AI recommends, the catalog decides"; Anthropic's eager input streaming can emit invalid JSON ([docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming)) | Rejected by KD-5 |
| 10 | **Serving alternatives:** ASGI/uvicorn deployment; gthread; gevent | Cheap concurrent streams (ASGI, gevent); simple config change (gthread) | New infrastructure (ASGI); whole-service runtime change (gthread); `mysqlclient` blocks under gevent ([analysis](https://carsonip.me/posts/python-mysqlclient-doesnt-work-well-with-gevent/)) | v1 makes no infrastructure changes (KD-3). ASGI is the v2 path |
| 11 | **Xpert as the session store** | Already stores conversations | Not designed as a per-learner session store | Rejected by KD-7 |

*This is the fuller companion to Key Decisions above (each KD's rejected alternative expands
here). After release, the chosen approach and its alternatives are minted as an ADR.*

## Risks & Open Questions

### Technical Risks

| # | Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- | --- |
| 1 | **Worker pinning.** Each in-flight turn holds one of the few sync workers for its whole duration: up to about 7 s at p90, 20 s worst case. Concurrent demo users could starve other enterprise-access traffic (BFFs, checkout) | Medium | High | Tight `personalized_search_turn` throttle; 20 s hard deadline; model-client timeouts; demo audience gated by a per-user flag (KD-8); keep concurrent demo users ≤ 3 in stage (OQ-1). v2: ASGI deployment |
| 2 | **Proxy buffers SSE** (OQ-4), so text arrives all at once and NFR-01's 2 s first-text target is missed | Medium | Medium | `X-Accel-Buffering: no` and `Cache-Control: no-cache`. Nothing breaks, because the reducer handles a buffered body the same way. The KD-4 fallback (`?mode=buffered`) is a transport-only switch |
| 3 | **Latency targets missed** (first text 2 s, first section 5 s, complete 7 s) with two LLM calls in series plus Algolia | Medium | Medium | KD-9: a fast model per step and tiles first; a Phase 0 throughput spike; small strict intent call; one Algolia round trip; prefilter to 20; no retry loops; `ChatTurn.timing` identifies the slow step |
| 4 | **Compose output fails validation** (bad JSON line, unknown ids), so the page is empty or partial | Medium | Medium | Invalid lines are dropped, never repaired; ids are checked against the candidate set; if no valid `item_section` arrives, a deterministic fallback section is built from the top prefiltered candidates and labelled as such; the reply text still streams |
| 5 | **Xpert backend specifics** (KD-2): no `response_format`, the caller sends the history every turn, the classifier runs after the last token, and each turn leaves a copy in Xpert's conversation store we don't control | High (when `xpert` is selected) | Medium | Server-generated conversation ids that never reach the browser; send at most the last 4 turns (8 messages); JSONL parsing tolerant of prose; OQ-5 on retention. |
| 6 | **First enterprise-access tables to store learner-written text tied to `lms_user_id`** | Certain | Medium | PII annotations on the models; soft delete; internal stage demo only; retention and purge are deferred and explicitly out of scope; no prompts in logs |
| 7 | **Permission scope.** Every endpoint must check the enterprise named in the request | Certain | High | New endpoints use an enterprise-scoped learner permission with the requested enterprise, and every query also filters on the learner's own id and that enterprise |
| 8 | **Middleware state teardown in streaming generators.** `crum`, RequestCache and monitoring are empty inside the generator, so code that relies on them breaks silently | Medium | Medium | Rule: the generator receives a plain `TurnContext` dataclass and never touches `request`. A unit test asserts the generator runs with `crum.get_current_request()` set to `None` |
| 9 | **Vercel changes the protocol** | Low | Low | Pinned to v1; one encoder/parser pair; golden-file tests |
| 10 | ~~Both tabs show wherever Pathways is enabled~~ Resolved 2026-11-04 by KD-8's dedicated flags. New: **the two flags drift** (tab visible, endpoints 404) | Low | Low | Enable enterprise-access first, then the LMS flag, for the same users; the portal shows the "not available" alert on a 404 |

### Open Questions

*Each open question gets a stable ID (OQ-1, OQ-2, …) so decisions, requirements, and build
steps can cite the gate they wait on. Any load-bearing `[Assumed]` claim in this spec must have
a corresponding OQ here. The **Gates** column names the specific decision, section, or build
step that cannot proceed until this resolves — this is what the Ready-to-Build checklist checks.*

| OQ-ID | Question | Impact on Design | Gates (decision / section / build step) | Owner | Target Date |
| --- | --- | --- | --- | --- | --- |
| OQ-1 | ~~How many turns can stream at once in stage?~~ **Answered 2026-10-27 from deploy configuration:** few, because each turn holds a sync worker. The throttle stays per learner (20/min); capacity is protected by the per-user demo audience; keep concurrent demo users ≤ 3 | – | Phase 1 demo sizing | Eng Lead | Closed |
| OQ-2 | ~~Does the raw cookie-JWT + `X-CSRFToken` `fetch` authenticate cross-origin on stage?~~ **Answered from config 2026-10-27:** the portal origin is allowed, with credentials, for CORS and CSRF (Current State). This is the same setup the axios client already uses. **Devstack half confirmed 2026-11-03 (spike 3):** the cookie-JWT (`USE-JWT-COOKIE: true`) + enterprise-access's own `X-CSRFToken` `fetch` streams incrementally (18 reads, first at 501 ms); without CSRF it's rejected with **401** (not 403); the view needs an `EventStreamRenderer` or `Accept: text/event-stream` gets 406. Remaining: the stage runtime confirmation folded into the Phase 1 first-deploy smoke | – | Phase 1 first-deploy smoke | Eng Lead | Closed (smoke in Phase 1) |
| OQ-3 | ~~Does the PRD owner accept the POC's deviations (KD-1 dual pane, KD-8 flag reuse, KD-9 reading of NFR-01)?~~ **Resolved 2026-10-27 (Eng Lead): the deviations are accepted for the POC.** Revisit before any non-demo release | – | – | Eng Lead | Closed |
| OQ-4 | **Partially answered 2026-10-27.** Configuration on the request path sets no buffering or timeout overrides, and the 20 s turn deadline is under every known idle timeout. Still unverified: whether any proxy on the path buffers or compresses `text/event-stream` | If anything buffers, text arrives all at once (Risk 2) and the KD-4 fallback applies | KD-4 exit criterion | Eng Lead: check empirically in the Phase 1 first-deploy smoke (time-to-first-byte vs first `text-delta`) | Phase 1 |
| OQ-5 | Xpert team: what retention applies to conversations sent from this feature, and can it be turned off? | Determines whether the optional `xpert` backend is acceptable beyond the internal demo | Enabling `xpert` for anyone outside the demo audience | Eng Lead | Open |
| OQ-6 | Is `ANTHROPIC_API_KEY` configured for enterprise-access in stage? | Without it, only `xpert` works (`ea/settings/base.py:559-563`; the claude backend raises without a key) | Using `claude` in the demo | Eng Lead | **Deferred (Eng Lead, 2026-10-27).** Phase 1 runs on whichever backend is configured; if the key is absent, it's `xpert` |
| OQ-7 | ~~Do exec ed records surface under `content_type:course` + English, with `learning_type:"Executive Education"`?~~ **Answered 2026-10-27 from the stage Algolia index:** yes. Attribute corrections were applied to Retrieval and the Block catalog (see the Retrieval facts) | – | – | Eng Lead | Closed |
| OQ-8 | Which OpenAI models are the intent and compose defaults, and is NFR-01 held at first tiles ≤ 5 s / page ≤ 7 s (p90)? | The 2026-10-29 model benchmark found no model that is both top-quality (judged ≥ 4.4) and inside the targets: `gpt-5.4-mini` low fits the targets at judged 4.0; the quality tier projects 6.4–9.9 s to first tiles. No model met the 800 ms intent budget (best p90 1,126 ms) | KD-9 OpenAI settings | Eng Lead | **Decided 2026-11-03 (Eng Lead): best fit.** Compose `gpt-5.4-mini` (`low`), intent `gpt-4.1-mini`; NFR-01 targets stand. Xpert stays switchable for a side-by-side comparison |
| OQ-9 | Should the compose fallback section still be added when compose deliberately shows nothing (its reply says no candidate fits)? | In the 2026-11-03 OpenAI vs Xpert comparison the rule added three unrelated courses under a correct "nothing fits" reply on 5 of 6 fallback turns; the judge scored those pages 1–2. Proposal: add the fallback only when section lines were invalid (dropped), and make a deliberately empty page the R-07 text-only turn | Compose rules ("Fallback section") | Eng Lead | **Open** |
| OQ-10 | edx-enterprise ≥ 9.1.0 declares `requires-python = ">=3.12"` (since edx-enterprise #2654), but some LMS deployments still build on Python 3.11 and pin an earlier edx-enterprise, so they can't install the release that adds `enterprise.personalized_search_enabled`. How does the flag reach those deployments? | Without it, the LMS flag doesn't exist there and the tab can't be shown; enterprise-access work is unaffected | Enabling the tab on a Python 3.11 LMS; the edx-platform bump in the merge order | Eng Lead, with the edx-enterprise maintainers and the edx-platform owners | Options: (a) relax `requires-python` to `>=3.11` and release again (CI no longer tests 3.11); (b) backport the flag onto a 9.0.x release; (c) wait for the LMS to move to 3.12. **Open** |
| OQ-11 | ~~Is there a pyopenssl/cryptography conflict that blocks shipping?~~ **Answered 2026-11-04: no.** The warning (`pyopenssl 26.3.0 requires cryptography<50`, with enterprise-access on `cryptography==50.0.2`) is a stale package in the devstack image only: pyopenssl left enterprise-access requirements in #317, snowflake-connector 5.x doesn't need it, and the production image installs `production.txt` only. the edx-platform release branches and openedx-platform master are each internally consistent, and edx-enterprise 9.2.0 changes no dependencies. Optional cleanup: `make dev_requirements` (pip-sync) in the devstack container | – | – | Eng Lead | Closed |

## Dependencies

| Dependency | Type (Internal / External) | Owner | What We Need | By When | Status |
| --- | --- | --- | --- | --- | --- |
| enterprise-catalog secured Algolia key | Internal | Enterprise | Existing key-vending endpoint; reuse `get_and_cache_secured_algolia_search_keys` | Phase 1 | Exists |
| Algolia enterprise index | External | Enterprise / 2U | Programs and exec ed present with the card attributes listed under Retrieval | Phase 0 | Verified on stage (OQ-7) |
| Anthropic API key in stage | Internal (secret) | Learner Rec | `ANTHROPIC_API_KEY` for the `claude` backend (OQ-6) | Phase 1 | Unknown |
| Xpert Platform | Internal | Xpert Platform team | `/v1/message` with `stream: true` for the `xpert` backend; retention answer (OQ-5) | Phase 1 | Exists; OQ open |
| Stage infrastructure | Internal | DevOps | Answers only, no changes: worker capacity (OQ-1, answered) and SSE behaviour on the request path (OQ-4, partial) | Phase 1 | OQ-4 partial |
| edx-enterprise flag | Internal | Enterprise | New `enterprise.personalized_search_enabled` WaffleFlag (edx-enterprise release 9.2.0), then an edx-platform bump to that release (OQ-10), then turned on for demo users in stage | Demo | Draft PR open; blocked on OQ-10 where the LMS runs Python 3.11 |
| PRD owner sign-off | Internal | Product owner | Accept the KD-1 and KD-8 deviations (OQ-3) | Before demo | Closed 2026-10-27: Eng Lead accepted the deviations for the POC; revisit before any non-demo rollout |

## Operational Design

### Monitoring & Alerting

| Metric / Alert | Condition | Severity (P1 / P2 / P3) | Response |
| --- | --- | --- | --- |
| Per-turn structured log (`personalized_search.turn`) | Always. Fields: session uuid, sequence, backend, step timings, item count, dropped-block count, fallback flag, candidate ids and emitted item ids (catalog ids, not PII; used to audit REQ-004), error code. A separate pre-stream line logs the pre-stream duration. Rejected requests (409/429/503) are seen in request metrics, not this log. No prompt text | – | Source for the p90 numbers |
| Custom monitoring attributes | Set **pre-stream** only (middleware state is gone in the generator) | – | – |
| Alerts | None for the internal demo | P3 | Watch logs during demo runs |

### Feature Flags

Two dedicated flags (KD-8, amended 2026-11-04); the Pathways flags no longer affect this feature.

| Flag Name | Default | Controls | Rollback Behavior |
| --- | --- | --- | --- |
| `enterprise_access.personalized_search_enabled` (WaffleFlag, per user, new) | OFF | Every `/api/v1/personalized-search/` endpoint (404 when off) | Remove the user or turn it off → endpoints 404 and the tab shows a "not available" alert; Pathways unaffected |
| `enterprise_access.learner_pathways_disable_candidate_rerank` (WaffleSwitch kill switch, `ea/toggles.py:87,98-104`) | OFF (models run) | ON → chat degraded mode: no model calls, deterministic results, template text | Flip it OFF again |
| `enterprise.personalized_search_enabled` (edx-enterprise WaffleFlag, per user, new) | OFF | Tab visibility (`enterpriseFeatures.enterprisePersonalizedSearchEnabled`), alone | Remove users from the flag |
| `PERSONALIZED_SEARCH_INTENT_TIMEOUT_S` / `_COMPOSE_READ_TIMEOUT_S` (settings, new) | 3 / 5 | Step timeouts (Deadline) | Change the setting |
| `PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED` (setting, dev only) | False | `?debug=true` per-turn model overrides and the debug menu endpoint | Leave False outside dev |
| `PATHWAYS_MODEL_BACKEND` (setting) | `xpert` | Which LLM backend chat uses (KD-2) | Change the setting |
| `PERSONALIZED_SEARCH_TRANSPORT` (MFE env, new, optional) | unset (SSE) | `fixture` or `buffered` override the SSE transport | Unset |

### Rollback Plan

1. Turn the `enterprise_access.personalized_search_enabled` flag off (Django admin), or the LMS flag to hide the tab. The endpoints return 404 immediately, with no deploy.
2. If only model cost or latency is the problem: turn the rerank kill switch on instead (degraded mode).
3. Code rollback is a revert. No existing endpoint or table is modified, so a revert has no side effects. The new tables can stay until `migrate personalized_search zero`.

## Build Phases (High-Level)

| Phase | What's Built | Depends On | Enables | Done when |
| --- | --- | --- | --- | --- |
| **Phase 0: contracts, fixtures, spikes** | Block and part types (TS + Python); `stream_protocol.py` encoder; orchestrator skeleton with fake steps generating the golden `.sse` files; `parseSse` + `applyPart` + `FixtureChatTransport` + `BufferedChatTransport`. **Spikes:** (a) compose throughput for Haiku 4.5 and Sonnet 5 on a recorded prompt (KD-9); (b) Xpert `stream:true` chunk framing; (c) ~~OQ-7 index check~~ (done 2026-10-27); (d) devstack `fetch` + CSRF smoke against a local streaming view. **Demo assets** (owner: Eng Lead): an internal demo script with 30 prompts (10 open goals, 10 refinement follow-ups, 5 career-advancement, 5 narrow-catalog) and 12 adversarial prompts (4 off-topic, 4 unsafe, 4 injection), plus the stage demo customer and users | This spec | Frontend and backend proceed in parallel | The encoder output matches the golden files; the portal reducer tests pass on the copied files; spike results recorded in this spec (KD-9 numbers, Xpert framing, OQ-7); the demo script exists |
| **Phase 1: demo core (cut line)**, REQ-001–008, 012–015 | **Backend:** models + migration + `prompts` choices and seed migrations; per-session turn lock; permission; `TurnContext`; intent; `multi_search_catalog_index`; retrieval + prefilter; compose parser + validators + hydration; orchestrator (redirect, degraded, deadline, abort); `stream()` for claude and xpert; turn view + throttle. **Frontend:** tab registration; dual pane; chat pane; learning page + block registry + `EditorialItemCard`; `SseChatTransport`; controller + `sendTurn` | Phase 0; OQ-6 (claude) or the xpert backend | The internal demo | The 30-prompt demo script runs on stage with p90 `first_text_ms` < 2 s, `first_section_ms` < 5 s and `complete_ms` < 7 s (KD-9); 0 displayed items outside the search response; 0 repeats per session; the adversarial prompts redirect |
| **Phase 2: retention features**, REQ-009–011 | Sessions list/retrieve endpoints + `HistoryPanel` + `PastSearchBanner` + `reopenSession` (replays the stored parts); item-feedback endpoints + Save / Not interested + `SavedView`; mobile chat/page toggle | Phase 1 | The full POC scope | Reopen renders identically to live (snapshot test from the same parts); continuing restores the exclusions; at 375 px there's no horizontal scroll |

**Test strategy**
- **Backend:**
  - The orchestrator runs with fake steps (scripted deltas) and a fake Algolia client: normal, redirect, no-match, degraded, deadline, client abort (write raises) and invalid block lines.
  - Encoder golden files.
  - Permission matrix (enterprise A/B, learner X/Y).
  - A test asserting the generator works with `crum.get_current_request() is None`.
  - A second turn on the same session while one is active returns 409. A persist failure emits `error: internal`, never `finish`.
  - Validator tests: duplicate `item_type` sections, ids not in the candidate set, a missing `followup` line (template fallback), and over-cap fields.
  - claude `stream()` against a stubbed SDK stream; xpert cumulative-to-delta diffing.
- **Frontend:**
  - `parseSse` handles chunks split mid-frame.
  - `applyPart` is a pure reducer, tested from the golden `.sse` files.
  - Block registry drops unknown types.
  - Controller tests with `FixtureChatTransport` under jest-fetch-mock (`src/setupTest.js:43`); `TextDecoder`/`ReadableStream` come from Node 24 globals, or a polyfill in the setup file if jsdom hides them.
  - RTL tests for the panes and the mobile toggle.
- **Stage:** the 30-prompt demo script (Phase 1 done-when) plus the adversarial list. The p90 numbers come from the turn logs.


## Traceability

*The audit line closing the loop back to the PRD. List every REQ-ID
from the Requirements table and confirm the design addresses it — the target state is **"REQs
unaddressed: none."** Any REQ left unaddressed must name the Open Question or decision that
explains why, so nothing falls through silently.*

| REQ | Addressed by |
| --- | --- |
| REQ-001 | KD-3, KD-4, KD-9, Stream contract, Turn pipeline step 4, Abort |
| REQ-002 | KD-1, KD-6, Frontend module map |
| REQ-003 | KD-5, KD-9 (block order), Block catalog v1, Compose contract, Block registry |
| REQ-004 | Retrieval, KD-5 validator (5/3 caps, candidate-set ids) |
| REQ-005 | Intent contract (merge), `ChatSession.merged_intent` |
| REQ-006 | Follow-up attribute (Retrieval), `data-followups`, `asked_attributes` |
| REQ-007 | Not-interested ids in the Algolia `NOT` clauses and the validator; card `status` (KD-11) |
| REQ-008 | Intent `on_topic`, redirect path |
| REQ-009 | `ItemFeedback`, item-feedback endpoints, Phase 2 |
| REQ-010 | KD-7, sessions endpoints, reopen-by-replay, Phase 2 |
| REQ-011 | KD-1, `PersonalizedSearchTab` toggle, Phase 2 |
| REQ-012 | KD-9, Performance Budget, `ChatTurn.timing`, Phase 1 done-when |
| REQ-013 | Security Design (new permission + ownership) |
| REQ-014 | KD-8, Feature Flags |
| REQ-015 | Accessibility Impact |
| REQ-016 | KD-11, additive block types, results-in-place status |
| REQ-017 | KD-12, `personalized_search_events.md` |

**REQs covered:** REQ-001 through REQ-017
**REQs unaddressed:** none. REQ-014 departs from PRD NFR-05 and REQ-002 from the PRD layout; the Eng Lead accepted both for the POC (OQ-3, closed).

## Decisions recorded before merge (2026-11-09)

Closed by the Eng Lead from the review queue; each follows existing repository convention.

| Topic | Decision |
| --- | --- |
| Algolia timeout (per call) | The per-call timeout bounds each host attempt, not the whole call. The 20 s turn deadline bounds the total, and the retrieval step passes the time left into each call. The spec's `min(2 s, deadline − elapsed)` is read per attempt. |
| Prompts | Intent and compose prompts stay in code (`core/intent.py`, `core/compose.py`), versioned with the code. No seeded `PromptType` rows. The seed-migration rows described under "Xpert specifics" are not built. |
| Xpert backend (optional adapter) | Off unless `XPERT_API_BASE_URL` and `XPERT_API_CLIENT_ID` are set. Do not set them where the Personalized Search flags are on until retention of conversation content by that service is confirmed (OQ-5). The default backend is OpenAI. |
| OpenAI backend | Sending learner text to OpenAI needs security, privacy and vendor approval before the flags are enabled outside stage. This gates enablement, not merging, because both flags default to off. |
| PII retirement | The three tables keep learner-written text and use `pii_retirement: retained` with the reason stated in each model. Retirement is deferred with retention. Do not enable either flag outside stage until a retirement path (a hard delete by `lms_user_id`) ships. A session soft-deletes by default, so any purge must use `delete(soft=False)`. |
| Flag overrides and schema versions | WaffleFlag override safety (keep `WAFFLE_OVERRIDE` off, never use the flag's Testing mode) is part of the runbook. Schemas are `v1`; a breaking change bumps the version. |

## Changelog

The full changelog is maintained with the team. Highlights:

| Date | Change |
| --- | --- |
| 2026-10-26 to 2026-10-28 | Spec drafted and grounded; KD-1 to KD-9; requirements, contracts, data model, phases |
| 2026-10-29 | KD-2 amended: OpenAI is the live backend; Xpert stream framing confirmed (spike 2); OpenAI model benchmark |
| 2026-11-03 | OQ-2 devstack half confirmed (spike 3); OQ-8 decided (best-fit OpenAI defaults); OQ-9 opened (fallback section) |
| 2026-11-04 | KD-8 amended (two dedicated flags); KD-10 added (portability: ports and adapters, JSON Schemas); timeouts restored as settings; stream header sent; C4 L3 diagrams; OQ-10 opened, OQ-11 closed |
| 2026-11-09 | Review decisions recorded (timeouts, prompts, optional Xpert adapter, PII retirement) |
| 2026-11-09 | KD-11 (results pane as a decision guide; results update in place, REQ-007 amended; REQ-016) and KD-12 (telemetry; REQ-017) from the design and analytics reviews |
| 2026-11-09 | Content language follows the learner's portal locale (`metadata_language` + `language` filters, model text in that language), as Learner Pathways does |
| 2026-11-09 | `plan.why`: why the suggested order |
| 2026-11-09 | `profile` built after the results from the preferences they reflect |
| 2026-11-09 | Follow-up chips: fixed narrowing chips plus up to 3 model-written ideas, which replace the opening chips when nothing is left to narrow |
