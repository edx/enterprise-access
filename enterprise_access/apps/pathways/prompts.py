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
