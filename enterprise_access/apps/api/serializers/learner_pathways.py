"""
Request and response serializers for the Learner Pathways API.
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
    interested_industries = serializers.CharField(allow_blank=False)


class LearningIntentResponseSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Validates and serializes the HTTP 200 response for the learning-intent endpoint.
    """
    skills_required = serializers.ListField(child=serializers.CharField(), required=False)
    skills_preferred = serializers.ListField(child=serializers.CharField(), required=False)
    condensed_algolia_query = serializers.CharField(required=False)


class RecommendationFeedbackRequestSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Validates the request body for the recommendation-feedback endpoint.
    """
    selected_career = serializers.CharField(allow_blank=False)
    course_keys = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        allow_empty=False,
    )
    learner_profile = serializers.DictField(allow_empty=False)


class RecommendationFeedbackResponseSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Validates and serializes the HTTP 200 response for the recommendation-feedback endpoint.
    """
    reasons = serializers.DictField(child=serializers.CharField())


class CareerDiscoveryRequestSerializer(LearningIntentRequestSerializer):  # pylint: disable=abstract-method
    """
    Validates the request body for the career-discovery endpoint.

    Subclasses the learning-intent request rather than restating its four fields: the
    server-side pipeline sends the same intake to the same prompt, and the evaluation
    harness validates its personas against ``LearningIntentRequestSerializer`` directly.
    A divergence between the two contracts would only show up as unexplained differences
    between harness runs and live requests.
    """


class CareerCandidateSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializes one career candidate.

    ``external_id`` is the Lightcast job identifier and is the career's identity for
    every downstream call; ``name`` is for display only, because taxonomy names are
    neither unique nor stable.

    There is deliberately no match-percentage field. The client-side POC hardcoded 0.95
    on every card and the MFE has since removed it, on the grounds that no verified
    compatible domain value exists. Adding one here would reintroduce a fabricated number
    into the one place a consumer would trust it.
    """
    external_id = serializers.CharField()
    name = serializers.CharField()
    skills = serializers.ListField(child=serializers.CharField(), required=False)
    industries = serializers.ListField(child=serializers.CharField(), required=False)


class CareerDiscoveryResponseSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializes the HTTP 200 response for the career-discovery endpoint.

    ``workflow_uuid`` is returned so a caller can retrieve the full per-step trace --
    input, output, timing and failure -- for a run it has already made, without the
    endpoint having to embed any of it in the response.
    """
    workflow_uuid = serializers.UUIDField()
    careers = CareerCandidateSerializer(many=True)


class PathwayRequestSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Validates the request body for the pathway endpoint.

    Takes a career the client already chose, plus the skills that career carries. The
    learner's choice sits *between* career discovery and pathway assembly, so this is a
    second request rather than a continuation of the first -- and the client is the only
    party that knows which card was clicked.

    ``career_skills`` is required and non-empty because two thirds of Lightcast careers
    carry no skills at all, and a pathway built from no skills is a keyword search wearing
    a pathway's clothes. A caller holding a skill-less career should not reach here.
    """
    career_name = serializers.CharField(allow_blank=False)
    career_external_id = serializers.CharField(allow_blank=False)
    career_skills = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        allow_empty=False,
    )
    skills_required = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list,
    )
    skills_preferred = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list,
    )
    # Passed through to the existing ``recommendations_feedback`` prompt, which is what
    # generates the per-course rationale. Optional: a pathway without it is still a
    # pathway, just explained more generically.
    learner_profile = serializers.DictField(required=False, default=dict)


class PathwayCourseSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializes one course in a delivered pathway.

    Order is the pathway's order -- roughly easiest first. "Roughly" is honest: the
    catalog's ``level_type`` disagrees with course titles in 19-36% of cases, so the
    ordering combines it with title cues and is best-effort rather than guaranteed.
    """
    key = serializers.CharField()
    title = serializers.CharField()
    level_type = serializers.CharField(allow_blank=True)
    partner = serializers.CharField(allow_blank=True)
    rationale = serializers.CharField(allow_blank=True)


class PathwayResponseSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializes the HTTP 200 response for the pathway endpoint.

    ``unfilled_rungs`` is part of the contract rather than an internal detail: a pathway
    that could not reach an advanced course is materially different from one that did,
    and the catalog genuinely has skills with no advanced content. Telling the client
    lets it say so instead of implying a progression that is not there.
    """
    workflow_uuid = serializers.UUIDField()
    courses = PathwayCourseSerializer(many=True)
    unfilled_rungs = serializers.ListField(child=serializers.CharField(), required=False)
