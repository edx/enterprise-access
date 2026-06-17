"""Tests for the prompts app models."""
import ddt
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from ..models import PromptType, XpertLearnerPathwaysSystemPrompt
from .factories import XpertLearnerPathwaysSystemPromptFactory


@ddt.ddt
class XpertLearnerPathwaysSystemPromptTests(TestCase):
    """Tests for ``XpertLearnerPathwaysSystemPrompt``."""

    @ddt.data(PromptType.LEARNER_INTENT, PromptType.RECOMMENDATIONS_FEEDBACK)
    def test_get_current_returns_row_for_prompt_type(self, prompt_type):
        prompt = XpertLearnerPathwaysSystemPromptFactory(prompt_type=prompt_type)

        self.assertEqual(
            XpertLearnerPathwaysSystemPrompt.get_current(prompt_type),
            prompt,
        )

    @ddt.data(PromptType.LEARNER_INTENT, PromptType.RECOMMENDATIONS_FEEDBACK)
    def test_get_current_returns_none_when_no_row_exists_for_prompt_type(self, prompt_type):
        self.assertIsNone(XpertLearnerPathwaysSystemPrompt.get_current(prompt_type))

    def test_get_current_is_scoped_per_prompt_type(self):
        intent = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.LEARNER_INTENT,
        )
        feedback = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.RECOMMENDATIONS_FEEDBACK,
        )

        self.assertEqual(
            XpertLearnerPathwaysSystemPrompt.get_current(PromptType.LEARNER_INTENT),
            intent,
        )
        self.assertEqual(
            XpertLearnerPathwaysSystemPrompt.get_current(PromptType.RECOMMENDATIONS_FEEDBACK),
            feedback,
        )

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

    def test_save_rejects_invalid_data_via_full_clean(self):
        with self.assertRaises(ValidationError) as ctx:
            XpertLearnerPathwaysSystemPromptFactory(system_prompt='')
        self.assertIn('system_prompt', ctx.exception.message_dict)

    def test_save_rejects_duplicate_prompt_type_via_full_clean(self):
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)

        with self.assertRaises(ValidationError):
            XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)

    def test_db_unique_constraint_blocks_two_rows_same_prompt_type(self):
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)
        # bulk_create bypasses save()/full_clean(), exercising the DB constraint directly.
        with transaction.atomic(), self.assertRaises(IntegrityError):
            XpertLearnerPathwaysSystemPrompt.objects.bulk_create([
                XpertLearnerPathwaysSystemPrompt(
                    prompt_type=PromptType.LEARNER_INTENT,
                    system_prompt='You are a helpful assistant.',
                ),
            ])

    def test_unique_constraint_allows_one_row_per_prompt_type(self):
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.LEARNER_INTENT)
        XpertLearnerPathwaysSystemPromptFactory(prompt_type=PromptType.RECOMMENDATIONS_FEEDBACK)

        self.assertEqual(XpertLearnerPathwaysSystemPrompt.objects.count(), 2)

    def test_edits_preserve_history_via_simple_history(self):
        prompt = XpertLearnerPathwaysSystemPromptFactory(
            prompt_type=PromptType.LEARNER_INTENT,
            system_prompt='Initial draft.',
        )
        prompt.system_prompt = 'Revised wording.'
        prompt.save()

        history = list(prompt.history.order_by('history_date'))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].system_prompt, 'Initial draft.')
        self.assertEqual(history[-1].system_prompt, 'Revised wording.')
        # Latest historical row mirrors the current persisted state.
        self.assertEqual(
            XpertLearnerPathwaysSystemPrompt.objects.get(pk=prompt.pk).system_prompt,
            history[-1].system_prompt,
        )
