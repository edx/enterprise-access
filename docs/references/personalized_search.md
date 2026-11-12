# Personalized Search (chat)

The learner portal's Personalized Search tab is a chat that recommends learning from the learner's enterprise catalog, with results beside the chat that update as the learner refines. This app serves it. The design is in [`personalized-search-tech-spec.md`](personalized-search-tech-spec.md).

## Endpoints (`/api/v1/personalized-search/`)

| Endpoint | Purpose |
| --- | --- |
| `POST turns/` | Streams one turn as AI SDK UI Message Stream v1 over SSE, with the header `x-vercel-ai-ui-message-stream: v1`. `?mode=buffered` returns the parts as JSON instead. The body takes an optional `locale` (the portal's language, e.g. `es`). |
| `GET sessions/`, `GET sessions/<uuid>/` | The learner's past searches, and one search's stored parts for replay. |
| `GET/POST/DELETE item-feedback/` | Saved and not-interested items. Not-interested items are excluded from later turns. |
| `POST sessions/<uuid>/turns/<sequence>/feedback/` | Thumbs up (1), down (-1) or cleared (null) on one of the learner's replies. |
| `GET debug/` | The `?debug=true` model menu. 404 unless the requesting user may use debug mode: `PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED` is on **and** the user is an enterprise-access superuser or on the `enterprise_access.personalized_search_debug` flag. |

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
| `PERSONALIZED_SEARCH_INTENT_MODEL`, `_EFFORT` | `gpt-6-luna`, `None` | Intent step model |
| `PERSONALIZED_SEARCH_COMPOSE_MODEL`, `_EFFORT` | `gpt-6-luna`, `None` | Compose step model |
| `PERSONALIZED_SEARCH_SHORTLIST_SIZE`, `_SIZES` | `20`, `{'gpt-6.1-terra': 40}` | Candidates compose chooses from: the default, and per compose model |
| `PERSONALIZED_SEARCH_OPENAI_MODELS` | allowlist with prices | The OpenAI models a turn may use. Prices are used only in the debug cost read-out. |
| `PERSONALIZED_SEARCH_INTENT_TIMEOUT_S`, `_COMPOSE_READ_TIMEOUT_S` | 3, 5 | Step timeouts, each also capped by the 20 s turn deadline. The Xpert platform uses `XPERT_REQUEST_TIMEOUT`. |
| `OPENAI_API_KEY`; `XPERT_API_BASE_URL`, `XPERT_API_CLIENT_ID` | empty | Backend credentials. Keys come only from settings. |
| `PERSONALIZED_SEARCH_ANALYTICS_COHORT`, `PERSONALIZED_SEARCH_ENGINE_LABEL` | `internal_pilot`, `b2b_mvp_v1` | Tags on every Segment event (planned, KD-12). See [`personalized_search_events.md`](personalized_search_events.md). |
| `PERSONALIZED_SEARCH_CONTENT_LANGUAGES` | `{'en': 'English', 'es': 'Spanish'}` | Portal locale → Algolia `language` name. A turn's `locale` (from the portal) picks `metadata_language` records and, for courses, the teaching language, and the model writes in that language. Unlisted locales use English. Mirrors the portal's `SUPPORTED_ALGOLIA_LANGUAGE_FILTERS` for Learner Pathways. |
| `PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED` | `False` | Turns debug mode on for the environment. Even then it applies only to enterprise-access superusers and users on the `enterprise_access.personalized_search_debug` WaffleFlag; everyone else's `debug` object is ignored. Debug mode covers per-turn model overrides, the debug catalog, the debug read-out and the debug menu. `is_superuser` is never set from the JWT (only `administrator` maps to `is_staff`), so superusers are granted in enterprise-access's Django admin. |
| `PERSONALIZED_SEARCH_DEBUG_CATALOG_SCOPE` | `None` | Stage only. The catalog searched by turns that carry the `debug` flag (the portal's `?debug=true`), when `PERSONALIZED_SEARCH_DEBUG_OVERRIDES_ENABLED` is on: `{'filter', 'label'}` plus optional `'app_id'`, `'search_api_key'`, `'index_name'` for another Algolia app or index, e.g. the 2U customer in the prod catalog. Uses that plain key with the filter, so it also needs `ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH`. Those turns' usage events have `is_debug: true`; other turns keep the learner's own catalog. |
| `PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE` | `None` | Dev only. Uses the plain search key plus an explicit catalog filter instead of the learner's secured key. Also needs `ALGOLIA_ALLOW_UNSCOPED_CATALOG_SEARCH`. Never set it in a deployed environment. |

## Results update in place (KD-11)

Only items the learner marked "Not for me" are excluded from later turns. Items shown earlier can appear again in the refreshed results, and each card says whether it is new or updated since the previous turn. The redesign is additive within block catalog v1: older portals ignore the new block types.

## When a model call fails

The OpenAI client doesn't retry (`max_retries=0`), so the orchestrator does, once per step:

- **Intent** is retried if at least 4 s of the turn deadline are left. If it fails again, the turn skips classification (the message is the query; a later turn refines the previous intent) and the server builds the results.
- **Compose** is retried if no text has streamed yet and at least 6 s are left. If it fails again, the server builds the results from the shortlist: a fixed reply, the top match, a comparison of the top three, and up to three items per type. They pass through the same block validation as model output.
- A configuration error (a bad key or model) is never retried and ends the turn with `model_failed`, as does a compose failure after text has streamed.

The turn log records `model_failures`, `retries` and `degraded` (`intent`, `compose` or empty), and the debug read-out shows `retries` and `degraded`. Harness rows for server-built turns have status `degraded` so comparisons don't count them as model output.

## Removing the feature

The three tables belong to this app alone, and nothing outside it has a foreign key to them. To remove the feature, run `./manage.py migrate personalized_search zero`, then remove the app from `PROJECT_APPS`.

## Comparing backends

`./manage.py run_personalized_search_harness` runs the same turns through two or more model arms. By default those are OpenAI best fit and Xpert. `./manage.py report_personalized_search_harness` summarises a run. The harness needs no database (it uses the in-memory store) and a hard `--max-calls` budget. It also needs `PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE`, because there is no request to vend a secured key from.
