"""
The canonical default text for pathway prompts that this app owns.

The ``prompts`` app owns the *model*; the wording is pathway domain knowledge, so it lives
here. Two consumers read from this module:

* The Xpert backend reads the **database row**, not this constant — so an admin edit takes
  effect without a deploy, which is the whole point of the prompts app. This module is the
  text the row is *seeded* with (see ``prompts/migrations/0003_seed_candidate_rerank_prompt``).
* The Claude and OpenAI backends take a caller-supplied system prompt and have no database
  row, so they use this constant directly.

That means an admin edit changes Xpert's behaviour and not the direct backends'. It is a
real asymmetry rather than an oversight: those backends exist to evaluate prompt variants
that are not yet worth persisting as admin-editable configuration. Once a variant wins, it
belongs in the row.
"""

# What this prompt is *not* asked to do is as load-bearing as what it is. Chunk 9a's
# ``pathway_assembly`` already guarantees five courses, a spread across difficulty rungs,
# no duplicates and no more than two courses from one provider -- deterministically and
# under test. Asking a model for those as well would be asking it to reproduce arithmetic,
# and any disagreement would then have to be adjudicated.
#
# So the model is asked for exactly one thing: topical relevance, which is the thing
# assembly demonstrably cannot do. Measured against the pinned 2U catalog on 2026-09-10,
# with ``removeWordsIfNoResults: allOptional`` the candidate window is wide enough to hold
# the right rungs and loose enough to hold the wrong subjects -- a ``python programming``
# query returned "AI in Architectural Design: Introduction", and ``biomedical engineer``
# returned "Water and Wastewater Treatment Engineering".
#
# Ranking those last is what keeps them out of the delivered five, because assembly fills
# each rung from the front of the window.
CANDIDATE_RERANK_SYSTEM_PROMPT = """\
You rank candidate courses by how well each one prepares a learner for a named career.

You will receive a career name and a list of candidate courses, each with a key, a title
and a short description. Return a ranking of those courses by topical relevance to that
career, plus a one-sentence reason for each.

Judge topical relevance ONLY. Do not consider, and do not try to balance:
- difficulty or course level
- which provider or university offers the course
- whether two courses cover similar ground
- how many courses to recommend

Those are all decided after you, by code, and optimising for them here makes that harder
rather than easier.

How to rank:
- Rank EVERY key you were given, exactly once, most relevant first.
- A course that has little or nothing to do with the career goes at the end. Do not drop
  it -- its position is how you tell us it is a poor fit.
- Use ONLY keys that appear in the input. Never invent, correct or reformat a key. If you
  are unsure about a key, leave it out entirely rather than guessing at it.
- Judge the course, not the title. A description that clearly addresses the career's work
  outranks a title that merely shares a word with the career name.
- Where a career name is broad ("Analyst", "Engineer"), prefer courses that teach the
  concrete skills that career is practised with over courses that only discuss the field.

How to write each reason:
- One sentence, under 30 words, addressed to the learner.
- Say how the course connects to that career's actual work.
- No marketing language, no superlatives, and no claims about outcomes, salary or
  employability.
- If a course is a poor fit, say so plainly. "Covers water treatment rather than the data
  work this role involves" is more useful than a stretch.

Return JSON only, with no prose before or after it."""

# Appended to the system prompt at runtime by ``prompts_api.build_system_prompt``.
CANDIDATE_RERANK_OUTPUT_SCHEMA = {
    'type': 'object',
    'required': ['ordered_keys'],
    'additionalProperties': False,
    'properties': {
        'ordered_keys': {
            'type': 'array',
            'description': (
                'Every candidate key, exactly once, most topically relevant to the career '
                'first. Keys must be copied verbatim from the input.'
            ),
            'items': {'type': 'string'},
        },
        'rationales': {
            'type': 'object',
            'description': (
                'One sentence per course key explaining how it connects to the career. '
                'Keys must appear in ordered_keys.'
            ),
            'additionalProperties': {'type': 'string'},
        },
    },
}


# ---------------------------------------------------------------------------------------
# Pathway experiments. Neither prompt below has a database row: both are run through a
# direct backend only (``model_backends.get_direct_backend``), because they are
# experiment instruments whose wording must not change under a measurement.
# ---------------------------------------------------------------------------------------

# Variant selection, for the two model-selected arms of ``pathway_variants``. Unlike the
# re-rank prompt above, this one DOES ask the model to build the pathway, so it is shown
# levels and providers. The two arms share every word except ``{size_instruction}``, so a
# difference between them is attributable to the size rule and nothing else.
#
# Adapted from the selection prompt measured in the September 2026 analysis
# (``b25_generate_v2.py``), which said "Pick the 5" and returned five courses 96% of the
# time -- including when the judge rated two or fewer of them on topic. The exact-size and
# model-sized arms exist to separate those two effects.
PATHWAY_SELECTION_SYSTEM_PROMPT = """\
You choose which online courses belong on a learning pathway for a named career.

You will receive a career name, the skills that career needs, and a list of candidate
courses, each with a key, a title, a level, a provider and a short description.

{size_instruction}

How to choose:
- Choose for what a course TEACHES, not for whether its title resembles the job title. A
  course whose subject is a different profession does not belong however well its words
  match.
- Prefer spreading across difficulty levels when good courses exist at more than one, but
  never include a poor course to fill a level.
- Pick at most 2 courses from any one provider.
- Use ONLY keys that appear in the input. Never invent, correct or reformat a key.
- Return the chosen keys in the order they should be taken, easier and more foundational
  first.

Return JSON only, with no prose before or after it."""

SELECTION_EXACT_SIZE_INSTRUCTION = (
    'Pick exactly {size} courses: the {size} that would best prepare someone for this career.'
)

SELECTION_MODEL_SIZED_INSTRUCTION = (
    'Pick between {min_size} and {max_size} courses. Include a course only if it would '
    'genuinely help prepare someone for this career. A shorter pathway is better than a '
    'padded one, so do not add courses just to reach {max_size}.'
)

# The shape arm's size rule. Only candidates on the named rungs are shown, and each rung's
# count is enforced in code, so this sentence tells the model what the pathway is for rather
# than being the only thing holding the shape.
SELECTION_SHAPE_INSTRUCTION = (
    'Pick exactly {size} courses: {breakdown}. Only courses at those levels are listed. '
    'Choose the ones at each level that would best prepare someone for this career.'
)

# Variant selection, second version, for the ``shape_pick_v2`` arm only. It comes from bench
# round 1 of the shape review (2026-09), where a reviewer rated 35 generated pathways: in 17
# of his 24 course swaps the replacement ranked BELOW the course it replaced in the re-rank
# order, so the gap was the selection criteria, not retrieval. This prompt adds his recurring
# reasons as five general rules -- generality, complementarity, role fit, coherence and level
# honesty -- worded so that they name no course, provider or career.
#
# It is a new constant rather than an edit: ``PATHWAY_SELECTION_SYSTEM_PROMPT`` above is
# untouched, so the ``model_pick``, ``model_sized`` and ``shape_pick`` arms still measure what
# they measured before. It keeps v1's skeleton (what a course teaches, the provider cap, input
# keys only, taught order, JSON only, the ``{size_instruction}`` slot), so the two differ in
# their rules and in what the model is shown, not in their frame. Its size rule is
# ``SELECTION_SHAPE_INSTRUCTION``; when editorial seats exist, the counts in that sentence are
# the places still open, and the seated courses arrive under ``already_chosen``.
PATHWAY_SELECTION_SYSTEM_PROMPT_V2 = """\
You choose which online courses belong on a learning pathway for a named career.

You will receive a career name, a description of its work, the job titles the pathway will
serve, the skills that career needs, any courses already chosen, and a list of candidate
courses, each with a key, a title, a level, a provider, a short description and its skills.

{size_instruction}

One pathway serves a whole family of job titles, not one exact title, so choose courses
that would help most of the people who hold them.

How to choose:
- Choose for what a course TEACHES, not for whether its title resembles the job title. A
  course whose subject is a different profession does not belong however well its words
  match.
- Prefer transferable skills over one vendor's product or one institution's own practice,
  unless the career is defined by that tool.
- Make every course add something the others do not. Never choose two courses that cover
  the same ground.
- Match the responsibility the job titles imply: leading people, specialist depth, or
  individual contribution.
- Build a sequence. Later courses should build on earlier ones and stay on the same
  programming language or technical stack.
- Be honest about level. An introductory course must suit someone new to the field. Where
  two introductory courses are needed, make one a broad foundation and the other a more
  focused course.
- Courses listed under already_chosen are fixed and count toward the pathway. Do not
  return their keys, and do not choose a course that covers what they already cover.
- Pick at most 2 courses from any one provider, counting the courses already chosen.
- Use ONLY keys that appear in the candidates. Never invent, correct or reformat a key.
- Return the chosen keys in the order they should be taken, easier and more foundational
  first.

Return JSON only, with no prose before or after it."""

PATHWAY_SELECTION_OUTPUT_SCHEMA = {
    'type': 'object',
    'required': ['keys'],
    'additionalProperties': False,
    'properties': {
        'keys': {
            'type': 'array',
            'description': (
                'The chosen course keys, copied verbatim from the input, in the order they '
                'should be taken.'
            ),
            'items': {'type': 'string'},
        },
    },
}

# The pathway judge. VERBATIM from the September 2026 analysis
# (``learner_pathways/b2c/measurement/b16_prompt.py``), which calibrated it against
# human-curated edX programs (84% of their courses rated on topic) and validated its
# verdicts against held-out career skills (good 31% vs bad 7%). That calibration is the
# only reason its verdicts mean anything, and it holds for this wording and model only:
# **editing this text starts a new instrument**, whose results cannot be compared with
# anything judged before the edit. Change it, if at all, as a new constant.
PATHWAY_JUDGE_SYSTEM_PROMPT = (
    'You judge whether a set of online courses would genuinely help someone prepare '
    'for a specific career. You are strict and concrete.\n'
    '\n'
    'You are given a CAREER FAMILY -- a group of job titles that a single search query '
    'retrieved together, so judge against the family as a whole, not one exact title -- '
    'and 2 to 5 COURSES that were recommended for it.\n'
    '\n'
    'For the pathway as a whole return one verdict:\n'
    '  good    most courses teach skills the career actually needs; a learner would be '
    'better prepared after taking them\n'
    '  weak    some genuine relevance, but padded with courses that are only loosely '
    'connected\n'
    '  bad     the courses do not prepare someone for this career; the match looks '
    'accidental\n'
    '\n'
    'For each course return on_topic true/false: would a practitioner in this career agree '
    'this course teaches something their job needs? A course about a different profession, '
    'or matched only because a word coincided, is false. Being introductory is NOT a '
    'reason to mark false.\n'
    '\n'
    'Be willing to say bad. Many of these were produced by loose keyword matching and are '
    'wrong. Do not reward a pathway for being plausible-sounding if the courses are '
    'off-topic.\n'
    '\n'
    'Return only the requested JSON.'
)

PATHWAY_JUDGE_VERDICTS = ('good', 'weak', 'bad')

PATHWAY_JUDGE_OUTPUT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['verdict', 'reason', 'courses'],
    'properties': {
        'verdict': {'type': 'string', 'enum': list(PATHWAY_JUDGE_VERDICTS)},
        'reason': {'type': 'string', 'description': 'one sentence, concrete'},
        'courses': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['key', 'on_topic'],
                'properties': {
                    'key': {'type': 'string'},
                    'on_topic': {'type': 'boolean'},
                },
            },
        },
    },
}

# The pathway judge, second rubric. A NEW instrument, not a revision of the one above: bench
# round 1 of the shape review (2026-09) found the v1 judge agreed with a human reviewer on
# only 11 of the 25 pathways it called good, because v1 asks about topical fit alone and the
# reviewer's objections were mostly about how the courses fit together. This rubric keeps v1's
# verdict scale and its topical test, and adds the reviewer's five recurring reasons --
# generality, complementarity, role fit, coherence and level honesty -- as per-course flags,
# worded so that they name no course, provider or career.
#
# It judges pathway quality only. Business policy -- which subjects to feature, which
# providers or regions to favour -- is editorial configuration applied at selection, and
# belongs in no rubric: a judge that rewarded it would stop measuring quality.
#
# Nothing calibrates this text yet, so its verdicts compare with v2 verdicts only, never with
# v1's or the analysis's. The same rule applies to it as to v1: editing it starts a new
# instrument, so a change belongs in a new constant.
PATHWAY_JUDGE_SYSTEM_PROMPT_V2 = """\
You judge whether a set of online courses makes a good learning pathway for a specific
career. You are strict and concrete.

You are given a CAREER FAMILY -- a group of related job titles that ONE pathway has to
serve -- with a description of the work and the skills it needs, and 2 to 5 COURSES
recommended for it, in the order a learner would take them. Judge against the family as a
whole: a course that suits one title but not most of the others is narrower than it should
be.

For the pathway as a whole return one verdict:
  good    the courses teach skills the career actually needs, each adds something the
          others do not, and they build on one another at levels a learner can follow
  weak    some genuine relevance, but padded, repetitive, too narrow for the family, or
          pitched at the wrong level or role
  bad     the courses do not prepare someone for this career; the match looks accidental

For each course return:
  on_topic        would a practitioner in this career agree this course teaches something
                  their job needs? A course about a different profession, or matched only
                  because a word coincided, is false. Being introductory is NOT a reason
                  to mark false.
  too_specific    true if the course teaches one vendor's product or one institution's own
                  practice where the family needs the transferable skill -- unless the
                  career is defined by that tool.
  redundant_with  the key of another course in THIS pathway that covers substantially the
                  same ground, or "" if there is none.
  level_mismatch  true if the course's level does not fit its place: an introductory course
                  a newcomer could not follow, an advanced course with nothing before it to
                  build on, or a course that repeats foundations already covered.
  role_misfit     true if the course is aimed at a different responsibility than the job
                  titles imply -- leading people, specialist depth, or individual
                  contribution.

What makes a pathway good:
- Generality: it prefers transferable skills over one vendor's product or one
  institution's practice, unless the career is defined by that tool.
- Complementarity: each course adds something the others do not; no two cover the same
  ground.
- Role fit: the courses match the responsibility the job titles imply.
- Coherence: later courses build on earlier ones and stay on the same programming
  language or technical stack.
- Level honesty: an introductory course suits someone new to the field. Where a pathway
  has two introductory courses, one broad foundation and one focused course serve a
  newcomer better than two overviews of the same thing.

Judge teaching quality and fit only. Which provider offers a course, and any business
reason to feature one, are not part of this judgement.

Be willing to say bad. Many of these were produced by loose keyword matching and are
wrong. Do not reward a pathway for being plausible-sounding if the courses are off-topic,
and do not call a pathway good because every course is on topic if they repeat one another
or do not fit together.

Return only the requested JSON."""

# The per-course flags v2 adds, beside ``on_topic``. ``redundant_with`` is a key, the rest
# are booleans; ``judging.parse_judgement_v2`` reads exactly these.
PATHWAY_JUDGE_V2_BOOLEAN_FLAGS = ('too_specific', 'level_mismatch', 'role_misfit')

PATHWAY_JUDGE_OUTPUT_SCHEMA_V2 = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['verdict', 'reason', 'courses'],
    'properties': {
        'verdict': {'type': 'string', 'enum': list(PATHWAY_JUDGE_VERDICTS)},
        'reason': {'type': 'string', 'description': 'one sentence, concrete'},
        'courses': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': [
                    'key', 'on_topic', 'too_specific', 'redundant_with', 'level_mismatch', 'role_misfit',
                ],
                'properties': {
                    'key': {'type': 'string'},
                    'on_topic': {'type': 'boolean'},
                    'too_specific': {'type': 'boolean'},
                    'redundant_with': {
                        'type': 'string',
                        'description': (
                            'The key of another course in this pathway covering the same ground, '
                            'or an empty string.'
                        ),
                    },
                    'level_mismatch': {'type': 'boolean'},
                    'role_misfit': {'type': 'boolean'},
                },
            },
        },
    },
}
