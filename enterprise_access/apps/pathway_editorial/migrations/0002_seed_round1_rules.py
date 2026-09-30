"""
Seed the editorial rules decided in bench round 1 (2026-09-28).

A product reviewer read generated pathways and made three business decisions:

* ``State-Bank-of-India+SBSC0015x`` was built for one national market and must not appear
  in English/US pathways. The search index has no region field, so this is an exclusion
  row until course-discovery's ``location_restriction`` is indexed.
* ``HarvardX+CS50P`` is the preferred introductory course for programming careers, used
  only where it is on topic.
* AI gets at most one seat per pathway, and only for an AI course about the career's
  work, never a generic one.

Properties this migration deliberately has, following ``prompts`` migration 0003:

* **Idempotent and never clobbers an admin edit.** ``get_or_create`` on each row's natural
  key, so a row someone already created or has since edited keeps their values.
* **Self-contained.** Values are inlined, not imported, because migrations are frozen
  history and must mean the same thing against future code.
* **Reversible without losing work.** Reversing deletes a row only while it still holds
  exactly the seeded values; an edited row is someone's content and is left in place.

The historical model from ``apps.get_model`` carries neither the ``full_clean()`` override
nor django-simple-history's signals, so no history row is written here. History for these
rules begins at their first admin edit.
"""
from django.db import migrations

ROUND_1 = 'Bench round 1, 2026-09-28 (product review of generated pathways)'

#: Career skill names that programming careers carry in the jobs index, checked with
#: ``pathway_eval.variant_collection.lookup_career`` on 2026-09-29. Software Engineer,
#: Application Developer, Software Developer, Python Developer, Java Developer and
#: Programmer Analyst carry at least one; Project Manager, Product Manager, Data Analyst
#: and Sales Manager carry none. "Python (Programming Language)" is deliberately absent:
#: Data Analyst carries it.
CS50P_SCOPE_SKILLS = [
    'Application Development',
    'Computer Programming',
    'Debugging',
    'Django (Web Framework)',
    'Flask (Web Framework)',
    'Object-Oriented Programming (OOP)',
    'Software Development',
    'Unit Testing',
]

AI_SKILL_NAMES = [
    'Artificial Intelligence',
    'ChatGPT',
    'Deep Learning',
    'Generative Artificial Intelligence',
    'Large Language Modeling',
    'Prompt Engineering',
    'Responsible AI',
]

COURSE_RULES = [
    {
        'course_key': 'State-Bank-of-India+SBSC0015x',
        'action': 'exclude',
        'level': '',
        'scope_skills': [],
        'reason': (
            f'{ROUND_1}: "Fundamentals of Consultative Sales" was built by State Bank of India '
            'for the Indian market and must not appear in English/US pathways. The search index '
            'has no region field; retire this row once course-discovery\'s location_restriction '
            'is indexed.'
        ),
        'is_active': True,
    },
    {
        'course_key': 'HarvardX+CS50P',
        'action': 'flagship',
        'level': 'Introductory',
        'scope_skills': CS50P_SCOPE_SKILLS,
        'reason': (
            f'{ROUND_1}: CS50\'s Introduction to Programming with Python is the preferred '
            'introductory course for programming careers. It is seated only where it is already '
            'in the career\'s candidate window at the Introductory level, and only for careers '
            'carrying one of scope_skills.'
        ),
        'is_active': True,
    },
]

PROMOTED_TOPICS = [
    {
        'name': 'Artificial Intelligence',
        'subjects': ['Artificial Intelligence'],
        'skill_names': AI_SKILL_NAMES,
        'max_per_pathway': 1,
        'gate_top_k': 10,
        'reason': (
            f'{ROUND_1}: promote at most one AI course per pathway, and only one about this '
            'career\'s work, never a generic AI course. A course counts as AI by the subject '
            'Artificial Intelligence or one of skill_names; Machine Learning alone does not count.'
        ),
        'is_active': True,
    },
]


def _is_unchanged(row, seeded: dict) -> bool:
    """True while ``row`` still holds exactly the seeded values."""
    return all(getattr(row, field) == value for field, value in seeded.items())


def seed_round1_rules(apps, schema_editor):
    """Create each seeded row if it is absent, leaving any existing row untouched."""
    rule_model = apps.get_model('pathway_editorial', 'PathwayCourseRule')
    topic_model = apps.get_model('pathway_editorial', 'PathwayPromotedTopic')
    for seeded in COURSE_RULES:
        defaults = {k: v for k, v in seeded.items() if k not in ('course_key', 'action')}
        rule_model.objects.get_or_create(
            course_key=seeded['course_key'], action=seeded['action'], defaults=defaults,
        )
    for seeded in PROMOTED_TOPICS:
        defaults = {k: v for k, v in seeded.items() if k != 'name'}
        topic_model.objects.get_or_create(name=seeded['name'], defaults=defaults)


def remove_round1_rules(apps, schema_editor):
    """Delete each seeded row on reverse, but only while it still holds the seeded values."""
    rule_model = apps.get_model('pathway_editorial', 'PathwayCourseRule')
    topic_model = apps.get_model('pathway_editorial', 'PathwayPromotedTopic')
    for seeded in COURSE_RULES:
        row = rule_model.objects.filter(course_key=seeded['course_key'], action=seeded['action']).first()
        if row is not None and _is_unchanged(row, seeded):
            row.delete()
    for seeded in PROMOTED_TOPICS:
        row = topic_model.objects.filter(name=seeded['name']).first()
        if row is not None and _is_unchanged(row, seeded):
            row.delete()


class Migration(migrations.Migration):
    """Data migration seeding the bench round 1 editorial rules."""

    dependencies = [
        ('pathway_editorial', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_round1_rules, remove_round1_rules),
    ]
