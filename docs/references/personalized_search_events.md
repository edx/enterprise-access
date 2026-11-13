# Personalized Search: Segment events

The event dictionary for Personalized Search (tech spec KD-12, REQ-017). Register each event in the Segment tracking plan with these typed properties before release.

**Rule:** events carry raw facts and join keys. No event carries a count, rate, total, age or cost; the analytics warehouse derives those by joining events to each other and to existing tables. No event carries learner-written text or model prose. Intent skills and roles are model-normalised labels (cut to 40 characters), not learner text.

## Names

- **Server:** `edx.server.enterprise-access.personalized-search.<object>.<action>`. They are sent with `enterprise_access.apps.track.segment.track_event`, whose Segment `user_id` is the LMS user id.
- **Portal:** `edx.ui.enterprise.learner_portal.personalized_search.<location>.<action>`. They are sent with `sendEnterpriseTrackEvent`, which adds `enterpriseUUID`.

## Join keys (on every event)

| Property | Type | Meaning |
| --- | --- | --- |
| `conversation_id` | uuid, nullable | The session (`ChatSession.uuid`). Null before a conversation exists. The same opaque id is in the portal's page URL (`?session=`) so a reload reopens the conversation; it is scoped to the learner on the server |
| `turn_id` | string, nullable | The turn's message id. Null on events that aren't about one turn |
| `turn_index` | int, nullable | The turn's sequence in the session, from 1. Null on events that aren't about one turn |
| `enterprise_customer_uuid` | uuid | The enterprise the turn ran in |
| `cohort` | string | `PERSONALIZED_SEARCH_ANALYTICS_COHORT`, e.g. `internal_pilot` or `public` |
| `engine` | string | `PERSONALIZED_SEARCH_ENGINE_LABEL`, e.g. `b2b_mvp_v1` |
| `is_debug` | bool | The turn used debug model overrides (not merely an environment that allows them) |
| `viewport` | string | Portal events only: the Paragon breakpoint name |

The turn-scoped keys are null on tab and history events and on the first `chat.prompt_submitted` of a conversation, which fire before any conversation or turn exists: register them as nullable. Server session events (`history.session-opened`, `history.session-resumed`) carry no `turn_id`, and `history.session-opened` no `turn_index`.

Item events also carry `item_key` and `item_type`. Array properties hold primitives only. Parallel arrays (for example `shown_item_keys` and `shown_item_positions`) line up by index.

## Server events

| Event | Fires when | Properties |
| --- | --- | --- |
| `turn.completed` | A turn finishes and is stored | **Outcome:** `outcome` (`done`, `no_match`, `redirected`), `redirect_reason`, `degraded` (`intent` or `compose`: that step's model call failed, so the server built the results; null otherwise).<br>**Intent:** `intent_merge_mode` (`refine`, `replace`), `intent_skills[]`, `intent_roles[]`, `intent_level`, `intent_item_types[]`, `intent_max_weeks`, `intent_max_hours_per_week`, `intent_pacing`.<br>**Items:** `shown_item_keys[]`, `shown_item_types[]`, `shown_item_sections[]`, `shown_item_positions[]`, `shown_item_statuses[]`, `retrieved_item_keys[]`, `excluded_item_keys[]`, `dropped_reasons[]` (codes such as `not_json` or `section_no_valid_items:course`; never the model's text), `followup_attribute`.<br>**Models:** `intent_model`, `intent_effort`, `compose_model`, `compose_effort`.<br>**Tokens:** `intent_input_tokens`, `intent_cached_tokens`, `intent_output_tokens`, `intent_reasoning_tokens`, and the same four for `compose_`.<br>**Blocks:** `shown_block_types[]` and `shown_block_digests[]` (parallel, stream order): each block's type and a 16-character hash of its content, without the server's per-turn card fields (`status`, `section`, `position`).<br>**Timing:** `intent_ms`, `search_ms`, `first_text_ms`, `first_tiles_ms`, `page_done_ms`.<br>**Prompt:** `prompt_length`.<br>**Language:** `content_locale` (the catalog records' language, e.g. `en` or `es`) |
| `turn.failed` | A turn errors or hits the deadline | `error_type`, `stage` (`intent`, `search`, `compose`, `persist`), `elapsed_ms` |
| `turn.stopped` | The learner stops a turn (the stream is closed early) | `stage`, `elapsed_ms` |
| `turn.rated` | Thumbs up or down on a reply | `rating` (`1`, `-1`, or `null` to clear) |
| `history.session-opened` | A stored conversation is loaded: reopened from History, or restored from the page URL (`?session=`) on a reload or shared link. A History reopen follows a portal `history.history_opened` (the panel opening); a URL restore doesn't | join keys only |
| `history.session-resumed` | A message is sent from a reopened search | join keys only |

## Portal events

| Event | Fires when | Properties |
| --- | --- | --- |
| `tab.tab_viewed` | The tab loads | `entry_point` (`nav_tab`, `deep_link`, `pathways_redirect`, `skills_quiz_redirect`) |
| `chat.prompt_submitted` | A message is sent | `prompt_source` (`typed`, `starter_chip`, `followup_chip`, `suggested_chip`, `more_like_this`), `chip_label` (fixed chip copy only; never an AI-suggested chip's text) |
| `results.results_rendered` | A live turn's results finish rendering (not on history replay) | join keys only |
| `results.result_clicked` | A card is opened | `item_key`, `item_type`, `item_section`, `item_position` |
| `results.item_save_toggled` | Save or unsave | `item_key`, `item_type`, `item_section`, `item_position`, `saved` |
| `results.item_dismissed` | Not for me, Undo, or Include again on the Excluded tab (`item_section: excluded_tab`) | `item_key`, `item_type`, `item_section`, `item_position`, `dismissed`, `replaced_by_item_key` |
| `results.explanation_opened` | Why this?, Why these? or the suggested order's info opens, including on hover | `scope` (`tile`, `set`, `order`), `item_key` (tile only) |
| `chat.new_chat_started` | New chat | join keys of the chat being left |
| `results.saved_tab_opened` | The Saved tab opens | join keys only |
| `results.excluded_tab_opened` | The Excluded tab opens | join keys only |
| `history.history_opened` | The History panel opens | join keys only |

`item_section` and `item_position` come from the card as the server sent it, so the portal computes nothing. A reserve card that fills a removed one carries its `item_section` and a null `item_position`.

## Deriving the metrics

| Question | Derived from |
| --- | --- |
| Items shown per type, picks per turn | Flatten `turn.completed.shown_item_*` |
| No dismissed item comes back (target zero) | `turn.completed.shown_item_keys` joined to earlier `results.item_dismissed` with `dismissed = true` (and no later undo) for the same `conversation_id` and `item_key` |
| No-match rate; catalog gaps | `turn.completed` where `outcome = no_match`, grouped by `intent_skills` and `intent_roles` |
| Dropped and excluded rates | `dropped_reasons`, `excluded_item_keys` over `shown_item_keys` |
| Cost per turn | Token counts joined to a model price table by `*_model` |
| How often a refinement repeats a block unchanged (the case for model-written diffs, KD-13) | Per `conversation_id`, count a block as repeated when its digest appears in the previous turn's `shown_block_digests` (digests leave out server-set fields: status, section, position, total, reserve) |
| Time to results | Percentiles of `first_text_ms`, `first_tiles_ms` and `page_done_ms` |
| Topic shift | `intent_merge_mode = replace` with `turn_index > 1` |
| Click-through by position or section | `result_clicked` over `results_rendered`, joined on `turn_id` |
| Enrollments from picks (7, 14 and 21 days) | `result_clicked` joined to LMS enrollments on `user_id` + `item_key` |
| Relevance | `item_save_toggled`, `item_dismissed`, `turn.rated` |
| Refinement depth, prompts and chips that work | `prompt_submitted.prompt_source` and turns per `conversation_id` |
| Give-up points | `turn.stopped.stage`, `new_chat_started` joined to the abandoned conversation's turns |
| Return behaviour | `history.*`, `results.saved_tab_opened` |

## Not sent yet

- `profile_updated`: there is no preference editor.
- `history_session_renamed` and `history_session_deleted`: there are no rename or delete endpoints.
- `learning_page_viewed`: results update in place; reopening is `history.session-opened`.
- `ontology_signals_used` and `opening_source`: there is no ontology or opening set.
- `subsidy_covered` and `enrollable_count`: there is no coverage source. Picks come from the learner's entitled catalog scope by construction.
