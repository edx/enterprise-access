"""Tests for the pathway editorial models."""
import ddt
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from enterprise_access.apps.pathway_editorial.models import (
    CourseLevel,
    CourseRuleAction,
    PathwayCourseRule,
    PathwayPromotedTopic
)


def course_rule(**overrides):
    """An unsaved rule with valid defaults."""
    values = {'course_key': 'HarvardX+CS50P', 'action': CourseRuleAction.FLAGSHIP,
              'level': CourseLevel.INTRODUCTORY, 'reason': 'round 1'}
    values.update(overrides)
    return PathwayCourseRule(**values)


def promoted_topic(**overrides):
    """An unsaved topic with valid defaults."""
    values = {'name': 'Artificial Intelligence', 'title_terms': ['AI'], 'skill_names': ['ChatGPT'], 'reason': 'round 1'}
    values.update(overrides)
    return PathwayPromotedTopic(**values)


@ddt.ddt
class PathwayCourseRuleTests(TestCase):
    """Tests for ``PathwayCourseRule``."""

    def setUp(self):
        super().setUp()
        PathwayCourseRule.objects.all().delete()   # the seed migration's rows

    def test_defaults(self):
        rule = course_rule(action=CourseRuleAction.EXCLUDE, level='')
        rule.save()
        rule.refresh_from_db()
        self.assertTrue(rule.is_active)
        self.assertEqual(rule.scope_skills, [])
        self.assertEqual(str(rule), 'PathwayCourseRule(exclude HarvardX+CS50P)')

    def test_flagship_requires_level(self):
        with self.assertRaises(ValidationError) as ctx:
            course_rule(level='').full_clean()
        self.assertIn('level', ctx.exception.message_dict)

    @ddt.data(*CourseLevel.values)
    def test_exclude_rejects_a_level(self, level):
        with self.assertRaises(ValidationError) as ctx:
            course_rule(action=CourseRuleAction.EXCLUDE, level=level).full_clean()
        self.assertIn('level', ctx.exception.message_dict)

    def test_exclude_rejects_scope_skills(self):
        with self.assertRaises(ValidationError) as ctx:
            course_rule(action=CourseRuleAction.EXCLUDE, level='', scope_skills=['Debugging']).full_clean()
        self.assertIn('scope_skills', ctx.exception.message_dict)

    @ddt.data('', '   ', '\n')
    def test_reason_is_required(self, reason):
        with self.assertRaises(ValidationError) as ctx:
            course_rule(reason=reason).full_clean()
        self.assertIn('reason', ctx.exception.message_dict)

    def test_whitespace_course_key_is_rejected_and_real_one_is_stripped(self):
        with self.assertRaises(ValidationError):
            course_rule(course_key='   ').full_clean()
        rule = course_rule(course_key='  HarvardX+CS50P  ')
        rule.full_clean()
        self.assertEqual(rule.course_key, 'HarvardX+CS50P')

    def test_unknown_level_or_action_is_rejected(self):
        with self.assertRaises(ValidationError):
            course_rule(level='Expert').full_clean()
        with self.assertRaises(ValidationError):
            course_rule(action='promote').full_clean()

    @ddt.data('Debugging', {'skill': 'Debugging'}, [1, 2], ['Debugging', None])
    def test_scope_skills_must_be_a_list_of_strings(self, scope):
        with self.assertRaises(ValidationError) as ctx:
            course_rule(scope_skills=scope).full_clean()
        self.assertIn('scope_skills', ctx.exception.message_dict)

    def test_scope_skills_are_stripped_and_deduplicated_in_order(self):
        rule = course_rule(scope_skills=[' Unit Testing ', 'Debugging', 'debugging', '  '])
        rule.full_clean()
        self.assertEqual(rule.scope_skills, ['Unit Testing', 'Debugging'])

    def test_save_runs_full_clean(self):
        with self.assertRaises(ValidationError):
            course_rule(level='').save()
        self.assertFalse(PathwayCourseRule.objects.exists())

    def test_one_rule_per_course_and_action(self):
        course_rule().save()
        with self.assertRaises(ValidationError):
            course_rule(level=CourseLevel.ADVANCED).save()
        # The database enforces it too, for writes that skip save().
        with self.assertRaises(IntegrityError), transaction.atomic():
            PathwayCourseRule.objects.bulk_create([course_rule(level=CourseLevel.ADVANCED)])

    def test_same_course_may_have_both_actions(self):
        course_rule().save()
        course_rule(action=CourseRuleAction.EXCLUDE, level='').save()
        self.assertEqual(PathwayCourseRule.objects.filter(course_key='HarvardX+CS50P').count(), 2)

    def test_every_change_is_kept_as_a_history_row(self):
        rule = course_rule()
        rule.save()
        rule.is_active = False
        rule.reason = 'switched off'
        rule.save()

        history = list(rule.history.all().order_by('history_id'))  # pylint: disable=no-member
        self.assertEqual([row.history_type for row in history], ['+', '~'])
        self.assertEqual(history[0].reason, 'round 1')
        self.assertTrue(history[0].is_active)
        self.assertFalse(history[1].is_active)


@ddt.ddt
class PathwayPromotedTopicTests(TestCase):
    """Tests for ``PathwayPromotedTopic``."""

    def setUp(self):
        super().setUp()
        PathwayPromotedTopic.objects.all().delete()   # the seed migration's rows

    def test_defaults(self):
        topic = promoted_topic()
        topic.save()
        topic.refresh_from_db()
        self.assertEqual((topic.max_per_pathway, topic.gate_top_k, topic.is_active), (1, 10, True))
        self.assertEqual(topic.subjects, [])
        self.assertEqual(topic.title_terms, ['AI'])
        self.assertEqual(str(topic), 'PathwayPromotedTopic(Artificial Intelligence)')

    def test_needs_a_subject_or_a_title_term(self):
        with self.assertRaises(ValidationError):
            promoted_topic(subjects=[], title_terms=[]).full_clean()
        # Skill names alone identify nothing: they only keep the topic's skills out of career overlap.
        with self.assertRaises(ValidationError):
            promoted_topic(subjects=[], title_terms=[], skill_names=['ChatGPT']).full_clean()
        promoted_topic(subjects=['Artificial Intelligence'], title_terms=[]).full_clean()
        promoted_topic(subjects=[], title_terms=['AI']).full_clean()

    def test_title_terms_are_stripped_and_deduplicated_in_order(self):
        topic = promoted_topic(title_terms=[' GenAI ', 'AI', 'genai', ''])
        topic.full_clean()
        self.assertEqual(topic.title_terms, ['GenAI', 'AI'])

    @ddt.data('subjects', 'title_terms', 'skill_names')
    def test_lists_must_hold_strings(self, field_name):
        with self.assertRaises(ValidationError) as ctx:
            promoted_topic(**{field_name: 'ChatGPT'}).full_clean()
        self.assertIn(field_name, ctx.exception.message_dict)

    @ddt.data('name', 'reason')
    def test_text_fields_are_required(self, field_name):
        with self.assertRaises(ValidationError) as ctx:
            promoted_topic(**{field_name: '  '}).full_clean()
        self.assertIn(field_name, ctx.exception.message_dict)

    def test_name_is_unique(self):
        promoted_topic().save()
        with self.assertRaises(ValidationError):
            promoted_topic(skill_names=['Deep Learning']).save()

    def test_negative_counts_are_rejected(self):
        with self.assertRaises(ValidationError):
            promoted_topic(max_per_pathway=-1).full_clean()

    def test_every_change_is_kept_as_a_history_row(self):
        topic = promoted_topic()
        topic.save()
        topic.gate_top_k = 5
        topic.save()
        history = topic.history.all().order_by('history_id')  # pylint: disable=no-member
        self.assertEqual([(row.history_type, row.gate_top_k) for row in history], [('+', 10), ('~', 5)])
