"""
Request and documentation-only response serializers for the Learner Pathways API.
"""
from rest_framework import serializers

LEARNER_PATHWAYS_API_TAG = 'Learner Pathways'


class LearningIntentRequestSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Validates the request body for the learning-intent endpoint.
    """
    selected_goals = serializers.CharField(allow_blank=False)
    free_text = serializers.CharField(allow_blank=False)
    known_context = serializers.CharField(allow_blank=False)


class LearningIntentResponseSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Documents the expected HTTP 200 response shape for the learning-intent endpoint.

    For OpenAPI schema generation only — never instantiated at runtime.
    """
    skills_required = serializers.ListField(child=serializers.CharField())
    skills_preferred = serializers.ListField(child=serializers.CharField())
    condensed_algolia_query = serializers.CharField()
