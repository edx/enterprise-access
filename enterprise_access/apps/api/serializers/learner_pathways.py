"""
Request and response serializers for the Learner Pathways API.
"""
from rest_framework import serializers

from enterprise_access.apps.pathways.pathway_variants import MAX_PATHWAY_SIZE, MIN_PATHWAY_SIZE, VARIANT_STRATEGIES

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

    # Pathway experiments, off unless the ``learner_pathways_pathway_experiments`` switch is
    # on (the view rejects them otherwise): the model-selected variants and the judge each
    # cost extra paid model calls, on a learner-facing endpoint. None of them changes the
    # delivered ``courses``.
    variant_sizes = serializers.ListField(
        child=serializers.IntegerField(min_value=MIN_PATHWAY_SIZE, max_value=MAX_PATHWAY_SIZE),
        required=False, default=list,
        help_text='Build pathway variants of these sizes (2-5) beside the delivered pathway.',
    )
    variant_strategies = serializers.ListField(
        child=serializers.ChoiceField(choices=VARIANT_STRATEGIES),
        required=False, default=list,
        help_text='How to build the variants. Sizes alone default to ranked_cut; '
                  'strategies alone default to every size from 2 to 5.',
    )
    judge = serializers.BooleanField(
        required=False, default=False,
        help_text='Score the delivered pathway and every variant with the model judge.',
    )

    def requests_experiments(self) -> bool:
        """Whether the validated request asks for any experiment field."""
        data = self.validated_data
        return bool(data.get('variant_sizes') or data.get('variant_strategies') or data.get('judge'))


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


class PathwayJudgementSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializes one model-judge result.

    ``verdict`` is ``good``, ``weak`` or ``bad``, or blank with ``error`` set when the
    judgement failed. ``same_as`` names an earlier pathway with the identical courses whose
    verdict was reused.
    """
    label = serializers.CharField()
    verdict = serializers.CharField(allow_blank=True)
    reason = serializers.CharField(allow_blank=True)
    on_topic = serializers.DictField(child=serializers.BooleanField())
    n_on_topic = serializers.IntegerField()
    n_courses = serializers.IntegerField()
    same_as = serializers.CharField(allow_blank=True)
    error = serializers.CharField(allow_blank=True)
    model = serializers.CharField(allow_blank=True)


class PathwayVariantSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializes one pathway size variant: an experiment, never the delivered pathway.

    ``requested_size`` is null for ``model_sized``, where the model chose the length.
    ``complete`` is false when a variant came back short of its size -- it is never padded.
    """
    label = serializers.CharField()
    strategy = serializers.CharField()
    requested_size = serializers.IntegerField(allow_null=True)
    courses = PathwayCourseSerializer(many=True)
    complete = serializers.BooleanField()
    level_mix = serializers.DictField(child=serializers.IntegerField())
    violations = serializers.ListField(child=serializers.CharField())
    dropped = serializers.DictField(child=serializers.IntegerField())
    fabricated_keys = serializers.ListField(child=serializers.CharField())
    error = serializers.CharField(allow_blank=True)
    judgement = PathwayJudgementSerializer(allow_null=True)


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
    # Present only when the request asked for experiments, so the default response is
    # unchanged for every existing client.
    variants = PathwayVariantSerializer(many=True, required=False)
    judgement = PathwayJudgementSerializer(required=False)
