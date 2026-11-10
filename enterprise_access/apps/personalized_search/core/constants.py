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
SESSION_LOCK_S = 20
SESSION_TTL_S = 24 * 60 * 60
INTENT_MAX_TOKENS = 400
COMPOSE_MAX_TOKENS = 1800  # room for the top pick and the comparison (KD-11)

# Prefilter caps (spec "Retrieval": top 20 with type caps).
CANDIDATE_CAPS = {'course': 10, 'executive_education': 5, 'program': 5}
MAX_CANDIDATES = 20
MAX_ITEMS_PER_SECTION = 3
MAX_ITEMS_PER_TURN = 12  # top pick + up to 3 compared + sections (KD-11)
MAX_COMPARE_OPTIONS = 3
RESERVE_PER_SECTION = 2  # next-best cards per section, to fill one the learner removes

PAGE_HEADER_LABEL = 'Your results'
SECTION_LABELS = {'course': 'Courses', 'executive_education': 'Executive education', 'program': 'Programs'}

REDIRECT_REPLY = (
    "I can help you find learning in your organization's catalog. What would you like to learn?"
)
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

# Character caps (spec "LLM step contracts": the caps are authoritative).
CAPS = {
    'title': 80, 'dek': 400, 'section_dek': 200, 'why': 400, 'fit_tag': 24, 'fit_count': 3,
    'question': 200, 'suggestion': 48, 'session_title': 60,
    'insight_title': 60, 'insight_body': 600, 'plan_note': 80,
    'reason': 160, 'check': 40, 'check_count': 3, 'best_for': 60, 'caveat_title': 60, 'caveat_body': 300,
}

ATTRIBUTES_TO_RETRIEVE = [
    'key', 'aggregation_key', 'content_type', 'learning_type', 'course_type', 'title', 'subtitle',
    'short_description', 'level_type', 'partners', 'authoring_organizations', 'card_image_url',
    'original_image_url', 'banner_image_url', 'skill_names', 'advertised_course_run', 'type',
    'course_keys', 'language',
]
