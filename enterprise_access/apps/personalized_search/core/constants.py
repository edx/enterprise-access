"""Constants for Personalized Search (Chat). Copy and limits come from the tech spec."""

ITEM_TYPES = ('course', 'executive_education', 'program')
MAX_MESSAGE_CHARS = 500
HISTORY_TURNS = 4
TURN_DEADLINE_S = 20
# Catalog search read timeout (spec "Retrieval": 2 s), also capped by the time left before the deadline.
RETRIEVAL_TIMEOUT_S = 2
# The spec's step timeouts: intent 3 s, compose 5 s per read. Deployments set them through
# PERSONALIZED_SEARCH_INTENT_TIMEOUT_S / _COMPOSE_READ_TIMEOUT_S; a slower backend can bring its own.
DEFAULT_INTENT_TIMEOUT_S = 3
DEFAULT_COMPOSE_READ_TIMEOUT_S = 5
# A model step that fails (timeout, connection, 5xx) is tried once more when at least this much of the turn
# deadline is left; otherwise, or when the retry fails too, the turn falls back to server-built results.
INTENT_RETRY_MIN_S = 4
COMPOSE_RETRY_MIN_S = 6
SESSION_LOCK_S = 20
SESSION_TTL_S = 24 * 60 * 60
# Saved and not-interested rows kept per learner, enterprise and kind; adding one more drops the oldest.
MAX_ITEM_FEEDBACK_PER_KIND = 500
# Dismissed items sent to the catalog search as NOT filters (the most recent ones); compose still drops them all.
MAX_RETRIEVAL_EXCLUDED_IDS = 50
INTENT_MAX_TOKENS = 400
COMPOSE_MAX_TOKENS = 1800  # room for the top pick and the comparison (KD-11)

# Prefilter caps (spec "Retrieval": top 20 with type caps).
CANDIDATE_CAPS = {'course': 10, 'executive_education': 5, 'program': 5}
MAX_CANDIDATES = 20
# A type the learner prefers may fill up to this many shortlist places (the others keep their caps).
PREFERRED_TYPE_CAP = 10
# With no type asked for, related programs lead: their prefilter score gets this boost.
PROGRAM_LEAD_BOOST = 0.5  # less than a tier, so it reorders within a tier and never lifts a keyword-only program
MAX_ITEMS_PER_SECTION = 3
MAX_ITEMS_PER_TURN = 12  # top pick + up to 3 compared + sections (KD-11)
MAX_COMPARE_OPTIONS = 3
# Next-best cards per section: the portal pads a row to MAX_ITEMS_PER_SECTION cards from them at first render,
# and fills a card the learner removes ("Not for me") from what's left.
RESERVE_PER_SECTION = 2

PAGE_HEADER_LABEL = 'Your results'
SECTION_LABELS = {'course': 'Courses', 'executive_education': 'Executive education', 'program': 'Programs'}

REDIRECT_REPLY = (
    "I can help you find learning in your organization's catalog. What would you like to learn?"
)
# The server's reply per redirect reason (REQ-008); no model writes them.
REDIRECT_REPLIES = {
    'off_topic': REDIRECT_REPLY,
    'injection': REDIRECT_REPLY,
    'unsafe': "I can't help with that. I can help you find learning in your organization's catalog.",
    'external_provider': ("I can only search your organization's catalog, not other course sites. "
                          "What would you like to learn?"),
    # NEEDS PRODUCT AND LEGAL REVIEW before the feature flag widens beyond stage: wording and the resources named.
    # Kept neutral and non-clinical; the turn shows no cards, chips or follow-up question with it.
    'distress': ("I'm sorry you're going through this, and I'm glad you said something. This chat can only help "
                 "you find learning, so please reach out to someone you trust, or to your organization's employee "
                 "assistance program if you have one. If you're in danger, contact your local emergency services "
                 "now."),
}
# A redirect right after the same one: shorter, so the chat never repeats a sentence word for word.
REDIRECT_REPEAT_REPLY = 'I can only help with learning here. What skill would you like to build?'
# Said before the results when the learner asked for another provider's courses and named a topic to learn.
EXTERNAL_PROVIDER_NOTE = "I can only search your organization's catalog, so here's what it has."
# The follow-up heading under a redirect, and under a turn that found nothing: neither recommended anything.
REDIRECT_FOLLOWUP_QUESTION = 'Want a starting point?'
NO_RESULTS_FOLLOWUP_QUESTION = "Is there another topic you'd like to try?"
# Server-built results, when a model step can't be reached (``compose.server_output``): no model writes this text.
DEGRADED_REPLY = "I couldn't write a reply just now, so here are the closest matches from your organization's catalog."
DEGRADED_NO_RESULTS_REPLY = "I couldn't find matches just now. Try telling me again what you want to learn."
DEGRADED_TITLE = 'Closest matches from your catalog'
DEGRADED_DEK = 'Ranked by how closely they match what you asked for.'
DEGRADED_REASON = 'One of the closest matches in your catalog to what you asked for.'
OPENING_CHIPS = [
    'Prepare for a leadership role',
    'Learn AI for my job',
    'Build data skills',
    'Something quick, under 4 weeks',
]
FOLLOWUP_TEMPLATES = {
    'level': ('What level are you aiming for?', ['Introductory', 'Intermediate', 'Advanced']),
    'time': ('How much time can you give it?', ['Under 4 weeks', '1 to 3 months', 'Longer is fine']),
    'pacing': ('Do you prefer self-paced or instructor-led?', ['Self-paced', 'Instructor-led', 'Either works']),
    'type': ('Do you want single courses, executive education or a full program?',
             ['Courses', 'Executive education', 'Programs']),
    'open': ('What else would you like to learn?', OPENING_CHIPS),
}
# What makes an idea cover a fixed chip (lowercase phrases; a match drops the fixed chip). Kept narrow, so an
# idea about a topic ("deeper fermentation") doesn't stand in for a choice it doesn't make.
FIXED_CHIP_MATCHES = {
    'Introductory': ('introductory', 'beginner', 'basics', 'from scratch', 'new to'),
    'Intermediate': ('intermediate',),
    'Advanced': ('advanced', 'expert'),
    'Under 4 weeks': ('under 4 weeks', 'quick', 'short', 'shortest', 'fastest'),
    '1 to 3 months': ('month',),
    'Longer is fine': ('longer', 'in-depth', 'in depth'),
    'Self-paced': ('self-paced', 'self paced', 'own pace'),
    'Instructor-led': ('instructor', 'instructor-led', 'live session', 'live class', 'cohort', 'scheduled'),
    'Either works': ('either',),
    # Only phrases that choose the type: "the shortest course" is about length, not courses over programs.
    'Courses': ('individual course', 'single course', 'standalone course', 'just courses', 'only courses',
                'courses only', 'courses instead'),
    'Executive education': ('executive education', 'exec ed'),
    'Programs': ('program', 'certificate'),
}

# The model's own chip ideas per turn, shown after the fixed chips for the follow-up attribute.
MAX_IDEAS = 3

# Character caps (spec "LLM step contracts": the caps are authoritative).
CAPS = {
    'title': 80, 'dek': 400, 'section_dek': 200, 'why': 400, 'fit_tag': 24, 'fit_count': 3,
    'question': 200, 'suggestion': 48, 'session_title': 60,
    'insight_title': 60, 'insight_body': 600, 'plan_note': 80, 'plan_why': 160,
    'reason': 160, 'check': 40, 'check_count': 3, 'best_for': 60, 'caveat_title': 60, 'caveat_body': 300,
    # The intent step's contract: how many skills and roles, and how long its query and each list entry may be.
    'skills': 8, 'roles': 3, 'intent_query': 200, 'intent_list_item': 80, 'completed': 5,
}

ATTRIBUTES_TO_RETRIEVE = [
    'key', 'aggregation_key', 'content_type', 'learning_type', 'course_type', 'title', 'subtitle',
    'short_description', 'level_type', 'partners', 'authoring_organizations', 'card_image_url',
    'original_image_url', 'banner_image_url', 'skill_names', 'advertised_course_run', 'type',
    'course_keys', 'language',
]
