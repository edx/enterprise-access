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
    Validates and serializes the HTTP 200 response for the learning-intent endpoint.
    """
    skills_required = serializers.ListField(child=serializers.CharField(), required=False)
    skills_preferred = serializers.ListField(child=serializers.CharField(), required=False)
    condensed_algolia_query = serializers.CharField(required=False)
