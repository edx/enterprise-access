"""Tests for the prompts app models."""
import ddt
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from ..models import PROMPT_TYPE_LEARNER_INTENT, PROMPT_TYPE_RECOMMENDATIONS_FEEDBACK, XpertLearnerPathwaysSystemPrompt
from .factories import XpertLearnerPathwaysSystemPromptFactory


@ddt.ddt
class XpertLearnerPathwaysSystemPromptTests(TestCase):
    """Tests for ``XpertLearnerPathwaysSystemPrompt``."""

    def test_activate_sets_is_active_true(self):
        prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_LEARNER_INTENT,
        )
        self.assertFalse(prompt.is_active)

        prompt.activate()

        prompt.refresh_from_db()
        self.assertTrue(prompt.is_active)

    def test_activate_deactivates_previously_active_same_prompt_type(self):
        previously_active = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_LEARNER_INTENT,
            active=True,
        )
        new_prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_LEARNER_INTENT,
        )

        new_prompt.activate()

        previously_active.refresh_from_db()
        new_prompt.refresh_from_db()
        self.assertFalse(previously_active.is_active)
        self.assertTrue(new_prompt.is_active)

    def test_activate_does_not_deactivate_other_prompt_type(self):
        intent_active = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_LEARNER_INTENT,
            active=True,
        )
        feedback_prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_RECOMMENDATIONS_FEEDBACK,
        )

        feedback_prompt.activate()

        intent_active.refresh_from_db()
        feedback_prompt.refresh_from_db()
        self.assertTrue(intent_active.is_active)
        self.assertTrue(feedback_prompt.is_active)

    def test_get_active_returns_active_learner_intent(self):
        active = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_LEARNER_INTENT,
            active=True,
        )
        XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_LEARNER_INTENT,
        )

        result = XpertLearnerPathwaysSystemPrompt.get_active(PROMPT_TYPE_LEARNER_INTENT)

        self.assertEqual(result, active)

    def test_get_active_returns_active_recommendations_feedback(self):
        active = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_RECOMMENDATIONS_FEEDBACK,
            active=True,
        )

        result = XpertLearnerPathwaysSystemPrompt.get_active(
            PROMPT_TYPE_RECOMMENDATIONS_FEEDBACK,
        )

        self.assertEqual(result, active)

    @ddt.data(PROMPT_TYPE_LEARNER_INTENT, PROMPT_TYPE_RECOMMENDATIONS_FEEDBACK)
    def test_get_active_returns_none_when_no_active_row(self, prompt_type):
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=prompt_type)

        self.assertIsNone(XpertLearnerPathwaysSystemPrompt.get_active(prompt_type))

    def test_clean_rejects_empty_system_prompt(self):
        prompt = XpertLearnerPathwaysSystemPromptFactory.build(system_prompt='')

        with self.assertRaises(ValidationError) as ctx:
            prompt.clean()
        self.assertIn('system_prompt', ctx.exception.message_dict)

    @ddt.data('   ', '\n\n', '\t')
    def test_clean_rejects_whitespace_only_system_prompt(self, whitespace):
        prompt = XpertLearnerPathwaysSystemPromptFactory.build(system_prompt=whitespace)

        with self.assertRaises(ValidationError) as ctx:
            prompt.clean()
        self.assertIn('system_prompt', ctx.exception.message_dict)

    @ddt.data([1, 2], 'a string', 42, True)
    def test_clean_rejects_non_dict_output_schema(self, bad_schema):
        prompt = XpertLearnerPathwaysSystemPromptFactory.build(output_schema=bad_schema)

        with self.assertRaises(ValidationError) as ctx:
            prompt.clean()
        self.assertIn('output_schema', ctx.exception.message_dict)

    def test_clean_accepts_valid_dict_output_schema(self):
        prompt = XpertLearnerPathwaysSystemPromptFactory.build(
            output_schema={'type': 'object', 'properties': {}},
        )
        prompt.clean()  # should not raise

    def test_clean_accepts_none_output_schema(self):
        prompt = XpertLearnerPathwaysSystemPromptFactory.build(output_schema=None)
        prompt.clean()  # should not raise

    def test_clean_rejects_invalid_prompt_type(self):
        prompt = XpertLearnerPathwaysSystemPromptFactory.build(prompt_type='nonsense')

        with self.assertRaises(ValidationError) as ctx:
            prompt.clean()
        self.assertIn('prompt_type', ctx.exception.message_dict)

    def test_unique_constraint_blocks_two_active_same_prompt_type(self):
        XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_LEARNER_INTENT,
            active=True,
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                XpertLearnerPathwaysSystemPromptFactory(
                    prompt_type=PROMPT_TYPE_LEARNER_INTENT,
                    active=True,
                )

    def test_unique_constraint_allows_active_rows_for_different_prompt_types(self):
        XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_LEARNER_INTENT,
            active=True,
        )
        XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PROMPT_TYPE_RECOMMENDATIONS_FEEDBACK,
            active=True,
        )
        self.assertEqual(XpertLearnerPathwaysSystemPrompt.objects.filter(is_active=True).count(), 2)

    def test_unique_constraint_allows_multiple_inactive_rows_same_prompt_type(self):
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PROMPT_TYPE_LEARNER_INTENT)
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PROMPT_TYPE_LEARNER_INTENT)
        self.assertEqual(
            XpertLearnerPathwaysSystemPrompt.objects.filter(
                prompt_type=PROMPT_TYPE_LEARNER_INTENT,
            ).count(),
            2,
        )
