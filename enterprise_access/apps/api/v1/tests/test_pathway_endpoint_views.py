"""
Tests for the pathway assembly endpoint.

HTTP-layer behaviour only: the feature flag, validation, permissions, error mapping and
response shape. Assembly and query-construction behaviour is tested in
``enterprise_access.apps.pathways.tests``.

The distinction this file cares about most is 200-with-no-courses versus 500. "We could
not build a pathway for this career" and "something broke" lead to different client
behaviour, and the catalog genuinely contains careers with no matching courses.
"""
import uuid
from unittest import mock

import ddt
from django.core.cache import cache as django_cache
from django.test import TestCase
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from edx_toggles.toggles.testutils import override_waffle_switch
from rest_framework import permissions, status
from rest_framework.reverse import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from enterprise_access.apps.api.v1.views.pathways import PathwayViewSet
from enterprise_access.apps.api_client.algolia_client import AlgoliaSearchError
from enterprise_access.apps.core.constants import LEARNER_PATHWAYS_LEARNER_ROLE, SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.core.models import EnterpriseAccessFeatureRole, EnterpriseAccessRoleAssignment
from enterprise_access.apps.core.tests.factories import UserFactory
from enterprise_access.apps.pathways.models import AssemblePathwayStep, PathwayAssemblyWorkflow
from enterprise_access.apps.prompts.api import PromptError
from enterprise_access.toggles import LEARNER_PATHWAYS_SERVER_PIPELINE
from test_utils import APITest

PATCH_SNAPSHOT = 'enterprise_access.apps.pathways.catalog_translation.snapshot_catalog_facets'
PATCH_RETRIEVE = 'enterprise_access.apps.pathways.course_retrieval.retrieve_candidate_courses'
PATCH_RERANK = 'enterprise_access.apps.pathways.reranking.rerank_candidates'
# Patched because the payload's skills deliberately do not all resolve against the
# snapshot, which is what makes the conditional refinement pass fire. Without this the
# tests would reach a real Algolia client.
PATCH_REFINE = 'enterprise_access.apps.pathways.catalog_translation.refine_unmatched_skills'
PATCH_ENRICH = 'enterprise_access.apps.pathways.models.pathways_api.enrich_rationales'

_PATHWAY_URL_NAME = 'api:v1:pathway-pathway'

_VALID_PAYLOAD = {
    'career_name': 'Welder',
    'career_external_id': 'ETE78CD2CDFFFAC66B',
    'career_skills': ['Welding', 'Blueprint Reading'],
    'skills_required': ['Welding'],
    'skills_preferred': ['Metallurgy'],
}


def course_hit(key, *, level='Introductory', partner='edX'):
    return {
        'key': key,
        'title': f'Course {key}',
        'short_description': 'short',
        'full_description': 'long',
        'level_type': level,
        'partners': [{'name': partner}],
        'language': 'English',
    }


def spanning_hits():
    """Six candidates able to fill a 2/2/1 quota across four providers."""
    return [
        course_hit('A+1', partner='P1'),
        course_hit('A+2', partner='P1'),
        course_hit('A+3', partner='P2'),
        course_hit('B+1', level='Intermediate', partner='P3'),
        course_hit('B+2', level='Intermediate', partner='P4'),
        course_hit('C+1', level='Advanced', partner='P4'),
    ]


def retrieval_result(courses=None, **overrides):
    hits = spanning_hits() if courses is None else courses
    return {
        'query': 'Welder Welding',
        'hit_count': len(hits),
        'courses': hits,
        'strict_filters_applied': ['Welding'],
        'strict_hit_count': len(hits),
        'strict_rungs_spanned': len({h['level_type'] for h in hits}),
        'broadened': False,
        'zero_hits': not hits,
        **overrides,
    }


class PathwayAPITestMixin:
    """Shared set-up: patched externals and an authorized learner."""

    def setUp(self):
        super().setUp()
        self.addCleanup(django_cache.clear)
        self.url = reverse(_PATHWAY_URL_NAME)

        self.snapshot_patcher = mock.patch(PATCH_SNAPSHOT, return_value={
            'skill_names': ['Welding'], 'skills.name': [], 'subjects': [], 'truncated': [],
        })
        self.mock_snapshot = self.snapshot_patcher.start()
        self.addCleanup(self.snapshot_patcher.stop)

        self.refine_patcher = mock.patch(PATCH_REFINE, return_value={
            'recovered': [], 'unresolved': ['Blueprint Reading', 'Metallurgy'], 'errors': [],
        })
        self.mock_refine = self.refine_patcher.start()
        self.addCleanup(self.refine_patcher.stop)

        self.retrieve_patcher = mock.patch(PATCH_RETRIEVE, return_value=retrieval_result())
        self.mock_retrieve = self.retrieve_patcher.start()
        self.addCleanup(self.retrieve_patcher.stop)

        self.enrich_patcher = mock.patch(PATCH_ENRICH, return_value={
            'reasons': {}, 'prompt_revision': '',
        })
        self.mock_enrich = self.enrich_patcher.start()
        self.addCleanup(self.enrich_patcher.stop)

        self.rerank_patcher = mock.patch(PATCH_RERANK, return_value={
            'ordered_keys': [], 'rationales': {}, 'fabricated_keys': [],
            'prompt_revision': '', 'trace': {},
        })
        self.mock_rerank = self.rerank_patcher.start()
        self.addCleanup(self.rerank_patcher.stop)

        self.authenticate_as_enterprise_learner()

    def authenticate_as_enterprise_learner(self):
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])

    def post_pathway(self, payload=None):
        body = _VALID_PAYLOAD if payload is None else payload
        return self.client.post(self.url, data=body, format='json')


@override_waffle_switch(LEARNER_PATHWAYS_SERVER_PIPELINE, True)
class TestPathwaySuccess(PathwayAPITestMixin, APITest):
    """Tests for a successful pathway request."""

    def test_a_selected_career_returns_five_ordered_courses(self):
        """Scenario: A pathway is returned end to end."""
        response = self.post_pathway()

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['courses']) == 5

    def test_courses_carry_the_display_fields_a_client_needs(self):
        response = self.post_pathway()

        course = response.data['courses'][0]
        for name in ('key', 'title', 'level_type', 'partner', 'rationale'):
            assert name in course

    def test_rationales_reach_the_response(self):
        self.mock_enrich.return_value = {
            'reasons': {'A+1': 'a solid starting point'}, 'prompt_revision': '4',
        }

        response = self.post_pathway()

        rationales = {c['key']: c['rationale'] for c in response.data['courses']}
        self.assertEqual(rationales.get('A+1'), 'a solid starting point')

    def test_a_failed_enrichment_still_returns_the_pathway(self):
        """Losing the explanations is a far smaller loss than losing the recommendation."""
        self.mock_enrich.side_effect = PromptError('no prompt configured')

        response = self.post_pathway()

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['courses']) == 5

    def test_the_response_carries_the_trace_handle(self):
        response = self.post_pathway()

        assert PathwayAssemblyWorkflow.objects.filter(
            uuid=response.data['workflow_uuid'],
        ).exists()

    def test_unfilled_rungs_are_reported_to_the_client(self):
        """
        A pathway that could not reach an advanced course differs materially from one
        that did, and some skills genuinely have no advanced content.
        """
        self.mock_retrieve.return_value = retrieval_result(
            [course_hit(f'A+{i}', partner=f'P{i}') for i in range(6)],
        )

        response = self.post_pathway()

        assert response.status_code == status.HTTP_200_OK
        assert 'Intermediate' in response.data['unfilled_rungs']
        assert 'Advanced' in response.data['unfilled_rungs']

    def test_every_execution_leaves_an_inspectable_trace(self):
        response = self.post_pathway()

        record = AssemblePathwayStep.objects.filter(
            workflow_record_uuid=response.data['workflow_uuid'],
        ).first()
        assert record is not None
        assert record.output_data
        assert record.succeeded_at is not None

    def test_explicit_db_role_assignment_is_allowed(self):
        self.client.logout()
        self.client.cookies.clear()
        user = UserFactory(is_active=True)
        role, _ = EnterpriseAccessFeatureRole.objects.get_or_create(name=LEARNER_PATHWAYS_LEARNER_ROLE)
        EnterpriseAccessRoleAssignment.objects.create(
            user=user, role=role, enterprise_customer_uuid=uuid.uuid4(),
        )
        self.client.force_authenticate(user=user)

        assert self.post_pathway().status_code == status.HTTP_200_OK


@override_waffle_switch(LEARNER_PATHWAYS_SERVER_PIPELINE, True)
class TestPathwayNoCoverage(PathwayAPITestMixin, APITest):
    """A career with no catalog coverage is an answer, not an error."""

    def test_no_candidates_returns_200_with_no_courses(self):
        self.mock_retrieve.return_value = retrieval_result([], broadened=True)

        response = self.post_pathway()

        assert response.status_code == status.HTTP_200_OK
        assert response.data['courses'] == []

    def test_too_few_candidates_returns_no_courses_rather_than_a_short_pathway(self):
        """Never pad, and never return four courses as though that were a pathway."""
        self.mock_retrieve.return_value = retrieval_result([
            course_hit('A+1'), course_hit('B+1', level='Intermediate'),
        ])

        response = self.post_pathway()

        assert response.status_code == status.HTTP_200_OK
        assert response.data['courses'] == []

    def test_a_skill_less_career_is_rejected_at_validation(self):
        """
        Two thirds of Lightcast careers carry no skills, and a pathway built from none is
        a keyword search wearing a pathway's clothes.
        """
        response = self.post_pathway({**_VALID_PAYLOAD, 'career_skills': []})

        assert response.status_code == status.HTTP_400_BAD_REQUEST


@ddt.ddt
@override_waffle_switch(LEARNER_PATHWAYS_SERVER_PIPELINE, True)
class TestPathwayAuthorization(PathwayAPITestMixin, APITest):
    """Authorization and validation tests."""

    def test_unauthenticated_caller_is_rejected(self):
        self.client.logout()
        self.client.cookies.clear()

        assert self.post_pathway().status_code in (
            status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN,
        )

    def test_authenticated_non_enterprise_learner_is_rejected(self):
        self.client.logout()
        self.client.cookies.clear()
        self.client.force_authenticate(user=UserFactory(is_active=True))

        assert self.post_pathway().status_code == status.HTTP_403_FORBIDDEN

    @ddt.data(
        {},
        {'career_name': 'Welder'},
        {'career_name': '', 'career_external_id': 'X', 'career_skills': ['Welding']},
        {'career_name': 'Welder', 'career_external_id': '', 'career_skills': ['Welding']},
        {'career_name': 'Welder', 'career_external_id': 'X', 'career_skills': ['']},
    )
    def test_invalid_payload_is_rejected(self, payload):
        assert self.post_pathway(payload).status_code == status.HTTP_400_BAD_REQUEST

    def test_optional_skill_lists_may_be_omitted(self):
        response = self.post_pathway({
            'career_name': 'Welder',
            'career_external_id': 'ETE78CD2CDFFFAC66B',
            'career_skills': ['Welding'],
        })

        assert response.status_code == status.HTTP_200_OK

    def test_get_is_rejected(self):
        assert self.client.get(self.url).status_code == status.HTTP_405_METHOD_NOT_ALLOWED


@override_waffle_switch(LEARNER_PATHWAYS_SERVER_PIPELINE, True)
class TestPathwayFailures(PathwayAPITestMixin, APITest):
    """A broken dependency is a 500, and never a silently empty pathway."""

    def test_an_algolia_failure_returns_500_without_partial_results(self):
        self.mock_snapshot.side_effect = AlgoliaSearchError('boom')

        response = self.post_pathway()

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert 'courses' not in response.data

    def test_a_retrieval_failure_returns_500_rather_than_no_coverage(self):
        """
        The distinction that matters: a transport failure must not be reported as "this
        career has no courses", which is what an empty 200 would say.
        """
        self.mock_retrieve.side_effect = AlgoliaSearchError('boom')

        response = self.post_pathway()

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


class TestPathwayFeatureFlag(PathwayAPITestMixin, APITest):
    """The endpoint 404s while the pipeline is disabled."""

    @override_waffle_switch(LEARNER_PATHWAYS_SERVER_PIPELINE, False)
    def test_disabled_pipeline_returns_404(self):
        assert self.post_pathway().status_code == status.HTTP_404_NOT_FOUND

    @override_waffle_switch(LEARNER_PATHWAYS_SERVER_PIPELINE, False)
    def test_disabled_pipeline_returns_404_for_an_invalid_payload_too(self):
        """A disabled endpoint must be indistinguishable from one that does not exist."""
        assert self.post_pathway({}).status_code == status.HTTP_404_NOT_FOUND

    @override_waffle_switch(LEARNER_PATHWAYS_SERVER_PIPELINE, True)
    def test_enabled_pipeline_serves_the_endpoint(self):
        assert self.post_pathway().status_code == status.HTTP_200_OK


class TestPathwayRouteConfig(TestCase):
    """Route-level configuration for the pathway action."""

    def test_url_reverses_under_learner_pathways(self):
        assert reverse(_PATHWAY_URL_NAME).endswith('/learner-pathways/pathway/')

    def test_route_does_not_shadow_the_other_pathway_endpoints(self):
        assert reverse('api:v1:career-discovery-careers') != reverse(_PATHWAY_URL_NAME)
        assert reverse('api:v1:learner-pathways-learning-intent') != reverse(_PATHWAY_URL_NAME)

    def test_post_is_routed(self):
        response = APIClient().post(reverse(_PATHWAY_URL_NAME), data={}, format='json')
        assert response.status_code != status.HTTP_405_METHOD_NOT_ALLOWED

    def test_action_configuration(self):
        # pylint: disable=no-member  # DRF @action adds .kwargs at decoration time.
        action_kwargs = PathwayViewSet.pathway.kwargs
        assert JwtAuthentication in action_kwargs['authentication_classes']
        assert permissions.IsAuthenticated in action_kwargs['permission_classes']
        assert ScopedRateThrottle in action_kwargs['throttle_classes']
        assert action_kwargs['throttle_scope'] == 'learner_pathways_pathway'

    def test_no_class_level_throttle_classes(self):
        assert 'throttle_classes' not in PathwayViewSet.__dict__
        assert PathwayViewSet.throttle_scope is None
