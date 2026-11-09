# Personalized Search: Segment events

The event dictionary for Personalized Search (tech spec KD-12, REQ-017). Register each event in the Segment tracking plan with these typed properties before release.

**Rule:** events carry raw facts and join keys. No event carries a count, rate, total, age or cost; the analytics warehouse derives those by joining events to each other and to existing tables. No event carries learner-written text or model prose.

## Names

- **Server:** `edx.server.enterprise-access.personalized-search.<object>.<action>`. They are sent with `enterprise_access.apps.track.segment.track_event`, whose Segment `user_id` is the LMS user id.
- **Portal:** `edx.ui.enterprise.learner_portal.personalized_search.<location>.<action>`. They are sent with `sendEnterpriseTrackEvent`, which adds `enterpriseUUID`.

## Join keys (on every event)

| Property | Type | Meaning |
| --- | --- | --- |
| `conversation_id` | uuid | The session (`ChatSession.uuid`) |
| `turn_id` | string | The turn's message id |
| `turn_index` | int | The turn's sequence in the session, from 1 |
| `enterprise_customer_uuid` | uuid | The enterprise the turn ran in |
| `cohort` | string | `PERSONALIZED_SEARCH_ANALYTICS_COHORT`, e.g. `internal_pilot` or `public` |
| `engine` | string | `PERSONALIZED_SEARCH_ENGINE_LABEL`, e.g. `b2b_mvp_v1` |
| `is_debug` | bool | The turn used debug model overrides |
| `viewport` | string | Portal events only: the Paragon breakpoint name |

Item events also carry `item_key` and `item_type`. Array properties hold primitives only. Parallel arrays (for example `shown_item_keys` and `shown_item_positions`) line up by index.

## Server events

| Event | Fires when | Properties |
| --- | --- | --- |
| `turn.completed` | A turn finishes and is stored | **Outcome:** `outcome` (`done`, `no_match`, `redirected`), `redirect_reason`.<br>**Intent:** `intent_merge_mode` (`refine`, `replace`), `intent_skills[]`, `intent_roles[]`, `intent_level`, `intent_item_types[]`, `intent_max_weeks`, `intent_pacing`.<br>**Items:** `shown_item_keys[]`, `shown_item_types[]`, `shown_item_sections[]`, `shown_item_positions[]`, `shown_item_statuses[]`, `retrieved_item_keys[]`, `excluded_item_keys[]`, `dropped_reasons[]`, `followup_attribute`.<br>**Models:** `intent_model`, `intent_effort`, `compose_model`, `compose_effort`.<br>**Tokens:** `intent_input_tokens`, `intent_cached_tokens`, `intent_output_tokens`, `intent_reasoning_tokens`, and the same four for `compose_`.<br>**Timing:** `intent_ms`, `search_ms`, `first_text_ms`, `first_tiles_ms`, `page_done_ms`.<br>**Prompt:** `prompt_length` |
| `turn.failed` | A turn errors or hits the deadline | `error_type`, `stage` (`intent`, `search`, `compose`), `elapsed_ms` |
| `turn.stopped` | The learner stops a turn (the stream is closed early) | `stage`, `elapsed_ms` |
| `turn.rated` | Thumbs up or down on a reply | `rating` (`1`, `-1`, or `null` to clear) |
| `history.session-opened` | A past search is reopened | join keys only |
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
| `results.explanation_opened` | Why this?, Why these? or the suggested order's info opens | `scope` (`tile`, `set`, `order`), `item_key` (tile only) |
| `chat.new_chat_started` | New chat | join keys of the chat being left |
| `results.saved_tab_opened` | The Saved tab opens | join keys only |
| `results.excluded_tab_opened` | The Excluded tab opens | join keys only |
| `history.history_opened` | The History panel opens | join keys only |

`item_section` and `item_position` come from the card as the server sent it, so the portal computes nothing.

## Deriving the metrics

| Question | Derived from |
| --- | --- |
| Items shown per type, picks per turn | Flatten `turn.completed.shown_item_*` |
| Nothing shown twice (target zero) | Duplicate `shown_item_keys` per `conversation_id` with `status` other than `updated` |
| No-match rate; catalog gaps | `turn.completed` where `outcome = no_match`, grouped by `intent_skills` and `intent_roles` |
| Dropped and excluded rates | `dropped_reasons`, `excluded_item_keys` over `shown_item_keys` |
| Cost per turn | Token counts joined to a model price table by `*_model` |
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
