"""Tests for the bench round 1 seed migration."""
import importlib
import json
from pathlib import Path

from django.apps import apps as django_apps
from django.test import TestCase

from enterprise_access.apps.pathway_editorial.api import load_policy, plan_seats
from enterprise_access.apps.pathway_editorial.models import PathwayCourseRule, PathwayPromotedTopic

FIXTURES = Path(__file__).parent / 'fixtures'

seed = importlib.import_module('enterprise_access.apps.pathway_editorial.migrations.0002_seed_round1_rules')
title_terms_seed = importlib.import_module(
    'enterprise_access.apps.pathway_editorial.migrations.0003_promotedtopic_title_terms'
)

AI_SKILLS = {
    'Artificial Intelligence', 'Generative Artificial Intelligence', 'Prompt Engineering',
    'Large Language Modeling', 'Responsible AI', 'ChatGPT', 'Deep Learning',
}


class SeededContentTests(TestCase):
    """The test database is built by migrations, so the seed rows are already there."""

    def test_policy_holds_the_three_round_1_rules(self):
        policy = load_policy()

        self.assertEqual(policy.excluded_keys, frozenset({'State-Bank-of-India+SBSC0015x'}))
        self.assertEqual(len(policy.flagships), 1)
        flagship = policy.flagships[0]
        self.assertEqual((flagship.course_key, flagship.level), ('HarvardX+CS50P', 'Introductory'))
        self.assertEqual(set(flagship.scope_skills), set(seed.CS50P_SCOPE_SKILLS))
        self.assertEqual(len(policy.promoted), 1)
        topic = policy.promoted[0]
        self.assertEqual(topic.subjects, ('Artificial Intelligence',))
        self.assertEqual(set(topic.skill_names), AI_SKILLS)
        self.assertNotIn('Machine Learning', topic.skill_names)
        self.assertEqual((topic.max_per_pathway, topic.gate_top_k), (1, 10))

    def test_every_reason_cites_the_round(self):
        reasons = list(PathwayCourseRule.objects.values_list('reason', flat=True))
        reasons += list(PathwayPromotedTopic.objects.values_list('reason', flat=True))
        self.assertEqual(len(reasons), 3)
        for reason in reasons:
            self.assertIn('bench round 1, 2026-09-28', reason.lower())

    def test_seeded_rows_pass_model_validation(self):
        for row in [*PathwayCourseRule.objects.all(), *PathwayPromotedTopic.objects.all()]:
            row.full_clean()

    def test_cs50p_scope_reaches_programming_careers_only(self):
        careers = json.loads((FIXTURES / 'career_skills.json').read_text(encoding='utf-8'))['careers']
        window = [{'key': 'HarvardX+CS50P', 'level_type': 'Introductory', 'skill_names': []}]
        matched = {
            career for career, skills in careers.items()
            if plan_seats(ordered_candidates=window, shape=(1, 0, 0), career_skills=skills, policy=load_policy())
        }
        self.assertEqual(matched, {'Software Engineer', 'Application Developer'})
        self.assertEqual(
            set(careers) - matched, {'Project Manager', 'Product Manager', 'Data Analyst', 'Sales Manager'},
        )


class SeedFunctionTests(TestCase):
    """The forward and reverse functions, run against the live app registry."""

    def test_forward_is_idempotent(self):
        seed.seed_round1_rules(django_apps, None)
        seed.seed_round1_rules(django_apps, None)
        self.assertEqual(PathwayCourseRule.objects.count(), 2)
        self.assertEqual(PathwayPromotedTopic.objects.count(), 1)

    def test_forward_never_overwrites_an_admin_edit(self):
        rule = PathwayCourseRule.objects.get(course_key='HarvardX+CS50P', action='flagship')
        rule.scope_skills = ['Debugging']
        rule.save()
        topic = PathwayPromotedTopic.objects.get(name='Artificial Intelligence')
        topic.max_per_pathway = 2
        topic.save()

        seed.seed_round1_rules(django_apps, None)

        self.assertEqual(PathwayCourseRule.objects.get(pk=rule.pk).scope_skills, ['Debugging'])
        self.assertEqual(PathwayPromotedTopic.objects.get(pk=topic.pk).max_per_pathway, 2)

    def test_forward_recreates_missing_rows(self):
        PathwayCourseRule.objects.all().delete()
        PathwayPromotedTopic.objects.all().delete()
        seed.seed_round1_rules(django_apps, None)
        self.assertEqual(PathwayCourseRule.objects.count(), 2)
        self.assertEqual(PathwayPromotedTopic.objects.count(), 1)

    def test_reverse_removes_unedited_rows_and_keeps_edited_ones(self):
        edited = PathwayCourseRule.objects.get(action='exclude')
        edited.reason = edited.reason + ' Confirmed again in round 2.'
        edited.save()

        # Reverse order: 0003 first puts back 0002's wording, then 0002 recognises its rows.
        title_terms_seed.restore_seeded_reason(django_apps, None)
        seed.remove_round1_rules(django_apps, None)

        self.assertEqual(list(PathwayCourseRule.objects.values_list('pk', flat=True)), [edited.pk])
        self.assertFalse(PathwayPromotedTopic.objects.exists())

    def test_reverse_tolerates_missing_rows(self):
        PathwayCourseRule.objects.all().delete()
        PathwayPromotedTopic.objects.all().delete()
        seed.remove_round1_rules(django_apps, None)
        self.assertFalse(PathwayCourseRule.objects.exists())


class TitleTermsSeedTests(TestCase):
    """Migration 0003: title terms on the seeded AI topic, without clobbering admin edits."""

    def ai_topic(self):
        return PathwayPromotedTopic.objects.get(name=title_terms_seed.TOPIC_NAME)

    def test_migrated_database_has_the_terms_and_the_new_wording(self):
        topic = self.ai_topic()
        self.assertEqual(topic.title_terms, title_terms_seed.AI_TITLE_TERMS)
        self.assertNotIn('Machine Learning', topic.title_terms)
        self.assertEqual(topic.reason, title_terms_seed.AI_TOPIC_DEFAULTS['reason'])
        self.assertIn('bench round 1, 2026-09-28', topic.reason.lower())
        self.assertEqual(load_policy().promoted[0].title_terms, tuple(sorted(
            title_terms_seed.AI_TITLE_TERMS, key=lambda term: (term.casefold(), term),
        )))

    def test_fills_title_terms_only_while_empty(self):
        PathwayPromotedTopic.objects.filter(pk=self.ai_topic().pk).update(title_terms=[])
        title_terms_seed.fill_ai_title_terms(django_apps, None)
        self.assertEqual(self.ai_topic().title_terms, title_terms_seed.AI_TITLE_TERMS)

    def test_never_overwrites_an_admin_edit(self):
        topic = self.ai_topic()
        topic.title_terms = ['AI']
        topic.reason = 'Narrowed by the reviewer in round 2.'
        topic.max_per_pathway = 2
        topic.save()

        title_terms_seed.fill_ai_title_terms(django_apps, None)

        topic.refresh_from_db()
        self.assertEqual((topic.title_terms, topic.reason, topic.max_per_pathway),
                         (['AI'], 'Narrowed by the reviewer in round 2.', 2))

    def test_rewords_only_the_seeded_0002_reason(self):
        PathwayPromotedTopic.objects.filter(pk=self.ai_topic().pk).update(reason=title_terms_seed.SEEDED_0002_REASON)
        title_terms_seed.fill_ai_title_terms(django_apps, None)
        self.assertEqual(self.ai_topic().reason, title_terms_seed.AI_TOPIC_DEFAULTS['reason'])

    def test_creates_the_topic_when_missing(self):
        PathwayPromotedTopic.objects.all().delete()
        title_terms_seed.fill_ai_title_terms(django_apps, None)
        topic = self.ai_topic()
        self.assertEqual(topic.title_terms, title_terms_seed.AI_TITLE_TERMS)
        self.assertEqual(set(topic.skill_names), AI_SKILLS)
        topic.full_clean()

    def test_reverse_restores_0002_wording_only_on_an_untouched_row(self):
        title_terms_seed.restore_seeded_reason(django_apps, None)
        self.assertEqual(self.ai_topic().reason, title_terms_seed.SEEDED_0002_REASON)

        PathwayPromotedTopic.objects.filter(pk=self.ai_topic().pk).update(reason='Someone else wrote this.')
        title_terms_seed.restore_seeded_reason(django_apps, None)
        self.assertEqual(self.ai_topic().reason, 'Someone else wrote this.')

    def test_0002_reason_constant_matches_migration_0002(self):
        self.assertEqual(title_terms_seed.SEEDED_0002_REASON, seed.PROMOTED_TOPICS[0]['reason'])
