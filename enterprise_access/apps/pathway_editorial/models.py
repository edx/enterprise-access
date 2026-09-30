"""
Models for pathway editorial rules.

Each row is one business decision about pathway content, editable in the Django admin
without a deploy. Edits overwrite the row in place and django-simple-history keeps every
prior revision. Rows are never deleted: switch ``is_active`` off instead, so the history
of why a rule existed stays next to the rule.

Nothing here holds user data. These are catalog-level rules keyed by course key, career
skill name and subject name.
"""
from django.core.exceptions import ValidationError
from django.db import models
from model_utils.models import TimeStampedModel
from simple_history.models import HistoricalRecords


class EditorialHistoryBase(models.Model):
    """
    Abstract base for this app's history tables.

    A history row is a revision of an editorial rule, so it holds the same catalog-level,
    non-user data as the rule itself. Declared as the ``HistoricalRecords`` base so the
    generated history models carry the PII annotation through their MRO.

    .. no_pii:
    """

    class Meta:
        abstract = True


class CourseRuleAction(models.TextChoices):
    """What a ``PathwayCourseRule`` does to its course."""
    EXCLUDE = 'exclude', 'Exclude'
    FLAGSHIP = 'flagship', 'Flagship'


class CourseLevel(models.TextChoices):
    """Pathway rungs, spelled as the catalog's ``level_type`` values."""
    INTRODUCTORY = 'Introductory', 'Introductory'
    INTERMEDIATE = 'Intermediate', 'Intermediate'
    ADVANCED = 'Advanced', 'Advanced'


def _clean_name_list(value, field_name: str) -> list[str]:
    """
    Validate a JSON list of names and return it stripped and de-duplicated.

    Order is kept, since an admin's ordering can carry meaning to the next reader, but
    blanks and repeats (case-insensitively) are dropped.
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError({field_name: f'{field_name} must be a JSON list of strings.'})
    cleaned, seen = [], set()
    for item in value:
        name = item.strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            cleaned.append(name)
    return cleaned


def _require_text(value: str, field_name: str) -> str:
    """Return ``value`` stripped, or raise if nothing is left."""
    text = (value or '').strip()
    if not text:
        raise ValidationError({field_name: f'{field_name} is required and cannot be blank.'})
    return text


class PathwayCourseRule(TimeStampedModel):
    """
    An editorial rule about one course: exclude it, or prefer it as a flagship.

    ``exclude`` removes the course from every pathway, before ranking gets a say. It is
    how a course built for one national market is kept out of English/US pathways while
    the search index carries no region field.

    ``flagship`` seats the course ahead of relevance order at ``level``, but only where it
    is already on topic: it must be in the career's candidate window at that level, and,
    when ``scope_skills`` is set, the career must carry at least one of those skills.

    At most one rule exists per course and action (enforced by a unique constraint).

    .. no_pii:
    """
    course_key = models.CharField(
        max_length=255,
        db_index=True,
        help_text='Course key exactly as the catalog spells it, e.g. "HarvardX+CS50P".',
    )
    action = models.CharField(
        max_length=16,
        choices=CourseRuleAction.choices,
        help_text='exclude: never show this course. flagship: prefer it where it is on topic.',
    )
    level = models.CharField(
        max_length=16,
        choices=CourseLevel.choices,
        blank=True,
        default='',
        help_text='Flagship only (required there): the rung the course is seated at. Blank for exclude.',
    )
    scope_skills = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Flagship only: JSON list of career skill names. The rule applies to a career carrying '
            'at least one of them. Empty applies it to every career.'
        ),
    )
    reason = models.TextField(
        help_text='Why this rule exists: who decided it, when, and on what evidence.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Untick to switch the rule off. Rules are deactivated, never deleted.',
    )
    history = HistoricalRecords(bases=[EditorialHistoryBase])

    class Meta:
        app_label = 'pathway_editorial'
        verbose_name = 'Pathway Course Rule'
        verbose_name_plural = 'Pathway Course Rules'
        constraints = [
            models.UniqueConstraint(
                fields=['course_key', 'action'],
                name='unique_pathway_course_rule_key_action',
            ),
        ]

    def __str__(self) -> str:
        return f'PathwayCourseRule({self.action} {self.course_key})'

    def clean(self) -> None:
        super().clean()
        self.course_key = _require_text(self.course_key, 'course_key')
        self.reason = _require_text(self.reason, 'reason')
        self.scope_skills = _clean_name_list(self.scope_skills, 'scope_skills')
        if self.action == CourseRuleAction.FLAGSHIP:
            if not self.level:
                raise ValidationError({'level': 'A flagship rule needs the level it is seated at.'})
        elif self.action == CourseRuleAction.EXCLUDE:
            if self.level:
                raise ValidationError({'level': 'An exclude rule applies at every level; leave level blank.'})
            if self.scope_skills:
                raise ValidationError(
                    {'scope_skills': 'An exclude rule applies to every career; leave scope_skills empty.'}
                )

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)


class PathwayPromotedTopic(TimeStampedModel):
    """
    A topic that pathway assembly may give a bounded number of seats to.

    A course belongs to the topic when one of its subjects is in ``subjects`` or its title
    contains one of ``title_terms`` as whole words. A skill tag alone does not make a
    course belong: tags are applied too loosely. ``skill_names`` lists the topic's own
    skills, which never count as evidence that a course is about a career's work.

    Belonging is not enough to be seated. The course must also rank within the first
    ``gate_top_k`` of its rung, share a skill with the career, and name one of those
    shared skills in its title or descriptions, so a generic course on the topic never
    takes a seat. ``max_per_pathway`` caps how many seats the topic gets, counting any
    flagship that already belongs to it.

    .. no_pii:
    """
    name = models.CharField(
        max_length=255,
        unique=True,
        help_text='Short name for the topic, e.g. "Artificial Intelligence".',
    )
    subjects = models.JSONField(
        default=list,
        blank=True,
        help_text='JSON list of catalog subject names that put a course in this topic.',
    )
    title_terms = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'JSON list of words or phrases that put a course in this topic when its title contains '
            'one as whole words, ignoring case, e.g. ["AI", "Generative", "Copilot"].'
        ),
    )
    skill_names = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "JSON list of the topic's own skill tags. They do not put a course in the topic; they are "
            "left out when checking that a course shares a skill with the career."
        ),
    )
    max_per_pathway = models.PositiveSmallIntegerField(
        default=1,
        help_text='Most seats one pathway gives this topic, flagships included.',
    )
    gate_top_k = models.PositiveSmallIntegerField(
        default=10,
        help_text='Only the first this-many candidates of a rung, in relevance order, are considered.',
    )
    reason = models.TextField(
        help_text='Why this topic is promoted: who decided it, when, and on what evidence.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Untick to switch the topic off. Topics are deactivated, never deleted.',
    )
    history = HistoricalRecords(bases=[EditorialHistoryBase])

    class Meta:
        app_label = 'pathway_editorial'
        verbose_name = 'Pathway Promoted Topic'
        verbose_name_plural = 'Pathway Promoted Topics'

    def __str__(self) -> str:
        return f'PathwayPromotedTopic({self.name})'

    def clean(self) -> None:
        super().clean()
        self.name = _require_text(self.name, 'name')
        self.reason = _require_text(self.reason, 'reason')
        self.subjects = _clean_name_list(self.subjects, 'subjects')
        self.title_terms = _clean_name_list(self.title_terms, 'title_terms')
        self.skill_names = _clean_name_list(self.skill_names, 'skill_names')
        if not self.subjects and not self.title_terms:
            raise ValidationError('A promoted topic needs at least one subject or title term, or it matches nothing.')

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)
