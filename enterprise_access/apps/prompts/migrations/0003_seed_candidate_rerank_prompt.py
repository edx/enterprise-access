"""
Seed the ``candidate_rerank`` prompt row so a fresh environment has a working pipeline.

Without a row, ``XpertBackend`` raises ``ModelBackendConfigurationError``, re-ranking
degrades to retrieval order, and the pathway pipeline runs *without* its model step while
still returning a valid pathway. That failure is silent by design -- losing the ordering is
better than losing the recommendation -- which is exactly why it needs seeding rather than
a setup instruction someone can miss. A run in that state reads as "the model does not
help" when the model was never called.

Three properties this migration deliberately has:

* **Idempotent.** ``get_or_create`` on ``prompt_type``, so an environment where someone
  already authored the row by hand keeps their wording untouched.
* **Never clobbers an admin edit.** Same reason. The prompts app exists so wording can
  change without a deploy; a migration that overwrote it would defeat that.
* **Self-contained.** The text is inlined rather than imported from
  ``apps/pathways/prompts.py``. Migrations are frozen history and must keep working
  against future code, so importing a constant that will legitimately change would make
  this migration mean something different later. The live default lives in that module;
  this is the snapshot it was seeded from.

Because the historical model from ``apps.get_model`` carries neither the ``full_clean()``
override nor django-simple-history's signals, no history row is written here. History for
this prompt therefore begins at the first admin edit, which is the correct reading: nobody
authored this revision, it shipped as a default.
"""
from django.db import migrations

PROMPT_TYPE = 'candidate_rerank'

SYSTEM_PROMPT = """\
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

OUTPUT_SCHEMA = {
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

NOTES = (
    'Seeded by migration 0003 as a working default. Edit freely -- this app exists so the '
    'wording can change without a deploy, and every edit is preserved as a history row. '
    'Note the direct model backends (claude, openai) do not read this row; they use the '
    'constant in apps/pathways/prompts.py. See ADR 0037.'
)


def seed_candidate_rerank_prompt(apps, schema_editor):
    """Create the row if it is absent, leaving any existing row untouched."""
    prompt_model = apps.get_model('prompts', 'XpertLearnerPathwaysSystemPrompt')
    prompt_model.objects.get_or_create(
        prompt_type=PROMPT_TYPE,
        defaults={
            'system_prompt': SYSTEM_PROMPT,
            'output_schema': OUTPUT_SCHEMA,
            'notes': NOTES,
        },
    )


def remove_candidate_rerank_prompt(apps, schema_editor):
    """
    Remove the seeded row on reverse, but only if it is still the seeded text.

    A row someone has since edited is their content, not this migration's, so reversing
    must not delete it. Reversing then leaves the row in place, which is the safe
    asymmetry: an extra prompt row is harmless, a deleted one loses work.
    """
    prompt_model = apps.get_model('prompts', 'XpertLearnerPathwaysSystemPrompt')
    prompt_model.objects.filter(
        prompt_type=PROMPT_TYPE,
        system_prompt=SYSTEM_PROMPT,
    ).delete()


class Migration(migrations.Migration):
    """Data migration seeding the candidate re-rank prompt."""

    dependencies = [
        ('prompts', '0002_alter_historicalxpertlearnerpathwayssystemprompt_prompt_type_and_more'),
    ]

    operations = [
        migrations.RunPython(
            seed_candidate_rerank_prompt,
            remove_candidate_rerank_prompt,
        ),
    ]
