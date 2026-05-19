"""Factoryboy factories for the prompts app."""
from uuid import uuid4

import factory

from ..models import PROMPT_TYPE_LEARNER_INTENT, XpertLearnerPathwaysSystemPrompt


class XpertLearnerPathwaysSystemPromptFactory(factory.django.DjangoModelFactory):
    """Factory for ``XpertLearnerPathwaysSystemPrompt``."""

    class Meta:
        model = XpertLearnerPathwaysSystemPrompt

    uuid = factory.LazyFunction(uuid4)
    prompt_type = PROMPT_TYPE_LEARNER_INTENT
    system_prompt = 'You are a helpful assistant.'
    output_schema = None
