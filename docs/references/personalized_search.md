# Personalized Search (chat)

The learner portal's Personalized Search tab is a chat that recommends learning from the learner's enterprise catalog, with results beside the chat that update as the learner refines. This app serves it. The design is in [`personalized-search-tech-spec.md`](personalized-search-tech-spec.md).

## Endpoints (`/api/v1/personalized-search/`)

| Endpoint | Purpose |
| --- | --- |
| `POST turns/` | Streams one turn as AI SDK UI Message Stream v1 over SSE, with the header `x-vercel-ai-ui-message-stream: v1`. `?mode=buffered` returns the parts as JSON instead. |
| `GET sessions/`, `GET sessions/<uuid>/` | The learner's past searches, and one search's stored parts for replay. |
| `GET/POST/DELETE item-feedback/` | Saved and not-interested items. Not-interested items are excluded from later turns. |
| `POST turns/<session>/<sequence>/feedback/` | Thumbs up or down on one reply (planned, KD-11). |
| `GET debug/` | The `?debug=true` model menu. 404 unless `PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED`, which is dev only. |

Every endpoint requires the enterprise-scoped learner permission (`personalized_search.has_learner_access`) and returns 404 while the `enterprise_access.personalized_search_enabled` WaffleFlag is off for the user. The portal shows the tab from a separate LMS flag, `enterprise.personalized_search_enabled`. Enable this app's flag first, for the same users, so a visible tab never gets a 404.

## Layout: a portable core behind ports

```
personalized_search/
  core/       pure Python, standard library only (tests/test_core.py enforces it)
    ports.py      ModelBackend, CatalogSearch, SessionStore (typing.Protocol), Usage
    orchestrator  the turn pipeline: intent -> retrieval -> compose, deadline, one log line per turn
    intent, retrieval, compose, model_registry, stream (SSE encoding), constants, errors
    schemas/      JSON Schemas for the stream parts and Block catalog v1 (the wire contract)
  adapters/   one outside dependency each
    algolia.py         CatalogSearch over AlgoliaSearchClient.multi_search_catalog_index
    openai_backend.py  ModelBackend for OpenAI (config passed in; no settings reads)
    xpert_backend.py   ModelBackend for the Xpert platform (optional)
    django_store.py    SessionStore, history and feedback over the app's tables
    memory_store.py    SessionStore in memory (harness, tests, other hosts)
  wiring.py   the composition root: the only module that reads settings and builds adapters
  models.py   ChatSession, ChatTurn, ItemFeedback (one migration, droppable)
```

Another service, a notebook or a test can run a turn with no Django, by supplying its own adapters. For example:

```python
from enterprise_access.apps.personalized_search.adapters.memory_store import InMemorySessionStore
from enterprise_access.apps.personalized_search.core.orchestrator import ModelChoice, Steps, TurnContext, run_turn

store = InMemorySessionStore()
session = store.load_or_create(session_uuid=None, lms_user_id=1, enterprise_customer_uuid='e', message='Lead a team')
store.lock(session)
steps = Steps(intent=my_intent_call, retrieve=my_catalog_search, compose=my_streaming_compose, persist=store.persist_turn)
ctx = TurnContext(session=session, message='Lead a team', intent_model=ModelChoice('m', None),
                  compose_model=ModelChoice('m', None), scope=my_scope)
for part in run_turn(ctx, steps):
    ...  # stream parts; core.stream.encode_part(part) gives the SSE frame
```

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `PERSONALIZED_SEARCH_INTENT_MODEL`, `_EFFORT` | `gpt-4.1-mini`, `None` | Intent step model |
| `PERSONALIZED_SEARCH_COMPOSE_MODEL`, `_EFFORT` | `gpt-5.4-mini`, `low` | Compose step model |
| `PERSONALIZED_SEARCH_OPENAI_MODELS` | allowlist with prices | The OpenAI models a turn may use. Prices are used only in the debug cost read-out. |
| `PERSONALIZED_SEARCH_INTENT_TIMEOUT_S`, `_COMPOSE_READ_TIMEOUT_S` | 3, 5 | Step timeouts, each also capped by the 20 s turn deadline. The Xpert platform uses `XPERT_REQUEST_TIMEOUT`. |
| `OPENAI_API_KEY`; `XPERT_API_BASE_URL`, `XPERT_API_CLIENT_ID` | empty | Backend credentials. Keys come only from settings. |
| `PERSONALIZED_SEARCH_ANALYTICS_COHORT`, `PERSONALIZED_SEARCH_ENGINE_LABEL` | `internal_pilot`, `b2b_mvp_v1` | Tags on every Segment event (planned, KD-12). See [`personalized_search_events.md`](personalized_search_events.md). |
| `PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED` | `False` | Dev only. Allows per-turn model overrides and the debug read-out. |
| `PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE` | `None` | Dev only. Uses the plain search key plus an explicit catalog filter instead of the learner's secured key. Also needs `ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH`. Never set it in a deployed environment. |

## Results update in place (KD-11)

Only items the learner marked "Not for me" are excluded from later turns. Items shown earlier can appear again in the refreshed results, and each card says whether it is new or updated since the previous turn. The redesign is additive within block catalog v1: older portals ignore the new block types.

## Removing the feature

The three tables belong to this app alone, and nothing outside it has a foreign key to them. To remove the feature, run `./manage.py migrate personalized_search zero`, then remove the app from `PROJECT_APPS`.

## Comparing backends

`./manage.py run_personalized_search_harness` runs the same turns through two or more model arms. By default those are OpenAI best fit and Xpert. `./manage.py report_personalized_search_harness` summarises a run. The harness needs no database (it uses the in-memory store) and a hard `--max-calls` budget. It also needs `PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE`, because there is no request to vend a secured key from.
