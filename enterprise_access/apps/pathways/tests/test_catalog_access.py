"""
Tests for how a catalog-reading step authorizes its search.

Two layers, deliberately:

* ``resolve_catalog_access`` is pure -- it takes a plain callable rather than a request --
  so its tests need no Django, no ``crum`` and no HTTP mocking at all.
* The step-level tests cover the edge where ``crum`` is actually reached for, including
  the two cases that matter operationally: a cross-tenant denial from enterprise-catalog,
  and the offline harness path where there is genuinely no request to vend from.
"""
from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import uuid4

import crum
import requests
from django.test import RequestFactory, TestCase

from enterprise_access.apps.api_client.algolia_client import SecuredAlgoliaKey
from enterprise_access.apps.api_client.enterprise_catalog_client import EnterpriseCatalogUserV1ApiClient
from enterprise_access.apps.pathways.catalog_access import CatalogAccess, resolve_catalog_access
from enterprise_access.apps.pathways.models import (
    PathwayAssemblyWorkflow,
    RetrieveCandidatesInput,
    RetrieveCandidatesStep,
    SnapshotCatalogFacetsInput,
    SnapshotCatalogFacetsOutput,
    SnapshotCatalogFacetsStep,
    TranslateToCatalogInput,
    TranslateToCatalogOutput,
    TranslateToCatalogStep
)

PATCH_SNAPSHOT = 'enterprise_access.apps.pathways.catalog_translation.snapshot_catalog_facets'
PATCH_REFINE = 'enterprise_access.apps.pathways.catalog_translation.refine_unmatched_skills'
PATCH_RETRIEVE = 'enterprise_access.apps.pathways.course_retrieval.retrieve_candidate_courses'

CUSTOMER_UUID = '417306cb-b24a-4d06-b83c-fb2a61d7fb96'

EMPTY_SNAPSHOT = {'skill_names': [], 'skills.name': [], 'subjects': [], 'truncated': []}


def a_key(api_key='secured-key'):
    """A secured key that is not expired, so the client would actually accept it."""
    return SecuredAlgoliaKey(
        api_key=api_key,
        valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def secured_key_payload(api_key='secured-key'):
    """The raw enterprise-catalog response shape, as ``from_response_payload`` reads it."""
    valid_until = datetime.now(timezone.utc) + timedelta(hours=1)
    return {'algolia': {'secured_api_key': api_key, 'valid_until': valid_until.isoformat()}}


def unrecovered_refinement(term='Underwater Basket Weaving'):
    """A refinement that found nothing -- the shape ``merge_refinement`` consumes."""
    return {'recovered': [], 'unresolved': [term], 'errors': []}


def empty_retrieval():
    return {
        'query': 'Welder', 'hit_count': 0, 'courses': [],
        'strict_filters_applied': [], 'strict_hit_count': 0,
        'strict_rungs_spanned': 0, 'broadened': False, 'zero_hits': True,
    }


class Accumulator:
    """Stands in for the workflow's dynamically-built accumulated-output object."""

    def __init__(self, **outputs):
        for key, value in outputs.items():
            setattr(self, key, value)


class TestResolveCatalogAccess(TestCase):
    """
    Tests for the pure resolution function.

    No request is constructed anywhere here -- that is the point of the callable seam.
    """

    def test_a_vended_key_is_returned(self):
        access = resolve_catalog_access(
            enterprise_customer_uuid=CUSTOMER_UUID,
            vend_secured_key=lambda uuid: a_key(),
        )

        self.assertEqual(access.secured_key.api_key, 'secured-key')
        self.assertFalse(access.allow_unscoped)

    def test_the_customer_uuid_reaches_the_vending_callable(self):
        seen = []

        resolve_catalog_access(
            enterprise_customer_uuid=CUSTOMER_UUID,
            vend_secured_key=lambda uuid: seen.append(uuid) or a_key(),
        )

        self.assertEqual(seen, [CUSTOMER_UUID])

    def test_no_vending_callable_falls_through_to_the_fallback(self):
        """
        The offline harness case: a customer scope is known, but there is no request to
        vend a per-user key from, so the explicit unscoped flag is what survives.
        """
        access = resolve_catalog_access(
            enterprise_customer_uuid=CUSTOMER_UUID,
            vend_secured_key=None,
            allow_unscoped_fallback=True,
        )

        self.assertEqual(access, CatalogAccess(secured_key=None, allow_unscoped=True))

    def test_no_customer_uuid_never_calls_the_vending_callable(self):
        def explode(_uuid):
            raise AssertionError('should not be called without a customer uuid')

        access = resolve_catalog_access(
            enterprise_customer_uuid='',
            vend_secured_key=explode,
            allow_unscoped_fallback=True,
        )

        self.assertEqual(access, CatalogAccess(secured_key=None, allow_unscoped=True))

    def test_no_customer_uuid_preserves_a_false_fallback(self):
        access = resolve_catalog_access(enterprise_customer_uuid=None, vend_secured_key=None)

        self.assertEqual(access, CatalogAccess(secured_key=None, allow_unscoped=False))

    def test_a_request_failure_falls_through_to_the_fallback(self):
        """
        A 403 from enterprise-catalog -- the cross-tenant denial -- arrives here as an
        ``HTTPError``. It must not become a fabricated success.
        """
        def denied(_uuid):
            raise requests.exceptions.HTTPError('403 Forbidden')

        access = resolve_catalog_access(
            enterprise_customer_uuid=CUSTOMER_UUID,
            vend_secured_key=denied,
            allow_unscoped_fallback=False,
        )

        self.assertEqual(access, CatalogAccess(secured_key=None, allow_unscoped=False))

    def test_a_value_error_falls_through_to_the_fallback(self):
        """A malformed uuid or an undecodable body, both of which surface as ``ValueError``."""
        def bad_value(_uuid):
            raise ValueError('not a uuid')

        access = resolve_catalog_access(
            enterprise_customer_uuid=CUSTOMER_UUID,
            vend_secured_key=bad_value,
            allow_unscoped_fallback=True,
        )

        self.assertEqual(access, CatalogAccess(secured_key=None, allow_unscoped=True))


class CatalogAccessStepTestMixin:
    """Shared request-context control for the step-level tests."""

    def use_request(self):
        """Install a real request in ``crum``, as the middleware would during a live call."""
        request = RequestFactory().post('/api/v1/learner-pathways/pathway/')
        crum.set_current_request(request)
        self.addCleanup(crum.set_current_request, None)
        return request


class TestSnapshotStepCatalogAccess(CatalogAccessStepTestMixin, TestCase):
    """
    ``SnapshotCatalogFacetsStep``'s credential resolution.
    """

    def _step(self, **kwargs):
        return SnapshotCatalogFacetsStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=SnapshotCatalogFacetsInput(**kwargs).to_dict(),
        )

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_SNAPSHOT, return_value=EMPTY_SNAPSHOT)
    def test_a_secured_key_is_vended_and_used(self, mock_snapshot, mock_vend):
        mock_vend.return_value = secured_key_payload()
        self.use_request()

        self._step(customer_uuid=CUSTOMER_UUID).execute()

        mock_vend.assert_called_once_with(enterprise_customer_uuid=CUSTOMER_UUID)
        self.assertEqual(mock_snapshot.call_args.kwargs['secured_key'].api_key, 'secured-key')
        self.assertFalse(mock_snapshot.call_args.kwargs['allow_unscoped'])

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_SNAPSHOT, return_value=EMPTY_SNAPSHOT)
    def test_a_cross_tenant_denial_never_becomes_a_scoped_search(self, mock_snapshot, mock_vend):
        """
        A user with no relationship to the requested enterprise is refused by
        enterprise-catalog. The step must not then search as though it had been granted
        that customer's scope -- it carries no key forward, leaving ``AlgoliaSearchClient``
        to refuse the search outright unless unscoped access is separately permitted.
        """
        mock_vend.side_effect = requests.exceptions.HTTPError('403 Forbidden')
        self.use_request()

        self._step(customer_uuid=CUSTOMER_UUID).execute()

        self.assertIsNone(mock_snapshot.call_args.kwargs['secured_key'])
        self.assertFalse(mock_snapshot.call_args.kwargs['allow_unscoped'])

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_SNAPSHOT, return_value=EMPTY_SNAPSHOT)
    def test_no_customer_uuid_does_not_attempt_to_vend(self, mock_snapshot, mock_vend):
        self.use_request()

        self._step().execute()

        mock_vend.assert_not_called()
        self.assertIsNone(mock_snapshot.call_args.kwargs['secured_key'])

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_SNAPSHOT, return_value=EMPTY_SNAPSHOT)
    def test_the_offline_harness_path_is_unchanged(self, mock_snapshot, mock_vend):
        """
        No request is installed -- ``crum`` genuinely returns ``None``, as it does under
        the evaluation harness's management command. Nothing is vended and the explicit
        unscoped flag survives untouched.
        """
        self.assertIsNone(crum.get_current_request())

        self._step(customer_uuid=CUSTOMER_UUID, allow_unscoped=True).execute()

        mock_vend.assert_not_called()
        self.assertIsNone(mock_snapshot.call_args.kwargs['secured_key'])
        self.assertTrue(mock_snapshot.call_args.kwargs['allow_unscoped'])


class TestTranslateStepCatalogAccess(CatalogAccessStepTestMixin, TestCase):
    """
    ``TranslateToCatalogStep``'s credential resolution, on its conditional refinement path.
    """

    def _step(self, **kwargs):
        kwargs.setdefault('career_skills', ['Underwater Basket Weaving'])
        return TranslateToCatalogStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=TranslateToCatalogInput(**kwargs).to_dict(),
        )

    def _accumulator(self):
        return Accumulator(
            snapshot_catalog_facets_output=SnapshotCatalogFacetsOutput(skill_names=['Python'])
        )

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_REFINE)
    def test_the_refinement_search_carries_a_secured_key(self, mock_refine, mock_vend):
        mock_refine.return_value = unrecovered_refinement()
        mock_vend.return_value = secured_key_payload()
        self.use_request()

        self._step(customer_uuid=CUSTOMER_UUID).execute(accumulated_output=self._accumulator())

        self.assertEqual(mock_refine.call_args.kwargs['secured_key'].api_key, 'secured-key')
        self.assertFalse(mock_refine.call_args.kwargs['allow_unscoped'])

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_REFINE)
    def test_a_cross_tenant_denial_never_becomes_a_scoped_refinement(self, mock_refine, mock_vend):
        mock_refine.return_value = unrecovered_refinement()
        mock_vend.side_effect = requests.exceptions.HTTPError('403 Forbidden')
        self.use_request()

        self._step(customer_uuid=CUSTOMER_UUID).execute(accumulated_output=self._accumulator())

        self.assertIsNone(mock_refine.call_args.kwargs['secured_key'])
        self.assertFalse(mock_refine.call_args.kwargs['allow_unscoped'])

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_REFINE)
    def test_nothing_is_vended_when_everything_already_resolves(self, mock_refine, mock_vend):
        """
        The refinement is the only catalog read this step makes, so the common path -- every
        term resolved against the snapshot -- must not spend a vend request.
        """
        self.use_request()

        self._step(career_skills=['Python'], customer_uuid=CUSTOMER_UUID).execute(
            accumulated_output=self._accumulator(),
        )

        mock_refine.assert_not_called()
        mock_vend.assert_not_called()

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_REFINE)
    def test_the_offline_harness_path_is_unchanged(self, mock_refine, mock_vend):
        mock_refine.return_value = unrecovered_refinement()
        self.assertIsNone(crum.get_current_request())

        self._step(customer_uuid=CUSTOMER_UUID, allow_unscoped=True).execute(
            accumulated_output=self._accumulator(),
        )

        mock_vend.assert_not_called()
        self.assertIsNone(mock_refine.call_args.kwargs['secured_key'])
        self.assertTrue(mock_refine.call_args.kwargs['allow_unscoped'])


class TestRetrieveStepCatalogAccess(CatalogAccessStepTestMixin, TestCase):
    """
    ``RetrieveCandidatesStep``'s credential resolution.
    """

    def _step(self, **kwargs):
        kwargs.setdefault('career_name', 'Welder')
        return RetrieveCandidatesStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=RetrieveCandidatesInput(**kwargs).to_dict(),
        )

    def _accumulator(self):
        return Accumulator(translate_to_catalog_output=TranslateToCatalogOutput(resolution_rate=1.0))

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_RETRIEVE, return_value=empty_retrieval())
    def test_a_secured_key_is_vended_and_used(self, mock_retrieve, mock_vend):
        mock_vend.return_value = secured_key_payload()
        self.use_request()

        self._step(customer_uuid=CUSTOMER_UUID).execute(accumulated_output=self._accumulator())

        self.assertEqual(mock_retrieve.call_args.kwargs['secured_key'].api_key, 'secured-key')
        self.assertFalse(mock_retrieve.call_args.kwargs['allow_unscoped'])
        # The customer scope itself is still passed: it is a facet filter, independent of
        # the credential the search is issued with.
        self.assertEqual(mock_retrieve.call_args.kwargs['customer_uuid'], CUSTOMER_UUID)

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_RETRIEVE, return_value=empty_retrieval())
    def test_a_cross_tenant_denial_never_becomes_a_scoped_retrieval(self, mock_retrieve, mock_vend):
        mock_vend.side_effect = requests.exceptions.HTTPError('403 Forbidden')
        self.use_request()

        self._step(customer_uuid=CUSTOMER_UUID).execute(accumulated_output=self._accumulator())

        self.assertIsNone(mock_retrieve.call_args.kwargs['secured_key'])
        self.assertFalse(mock_retrieve.call_args.kwargs['allow_unscoped'])

    @mock.patch.object(EnterpriseCatalogUserV1ApiClient, 'get_secured_algolia_api_key')
    @mock.patch(PATCH_RETRIEVE, return_value=empty_retrieval())
    def test_the_offline_harness_path_is_unchanged(self, mock_retrieve, mock_vend):
        self.assertIsNone(crum.get_current_request())

        self._step(customer_uuid=CUSTOMER_UUID, allow_unscoped=True).execute(
            accumulated_output=self._accumulator(),
        )

        mock_vend.assert_not_called()
        self.assertIsNone(mock_retrieve.call_args.kwargs['secured_key'])
        self.assertTrue(mock_retrieve.call_args.kwargs['allow_unscoped'])
        self.assertEqual(mock_retrieve.call_args.kwargs['customer_uuid'], CUSTOMER_UUID)


class TestPathwayWorkflowInputCarriesTheCustomerScope(TestCase):
    """
    ``generate_input_dict`` threads the customer scope to every step that reads the catalog.

    The secured key is deliberately absent: ``input_data`` is persisted and re-runnable, and
    a time-limited credential stored there would be replayed stale.
    """

    def test_every_catalog_reading_step_gets_the_customer_uuid(self):
        input_dict = PathwayAssemblyWorkflow.generate_input_dict(
            career_name='Welder', career_skills=['Welding'], customer_uuid=CUSTOMER_UUID,
        )

        for key in (
            SnapshotCatalogFacetsInput.KEY,
            TranslateToCatalogInput.KEY,
            RetrieveCandidatesInput.KEY,
        ):
            self.assertEqual(input_dict[key]['customer_uuid'], CUSTOMER_UUID)

    def test_no_secured_key_is_ever_persisted_into_the_input(self):
        input_dict = PathwayAssemblyWorkflow.generate_input_dict(
            career_name='Welder', career_skills=['Welding'], customer_uuid=CUSTOMER_UUID,
        )

        self.assertNotIn('secured_key', repr(input_dict))
