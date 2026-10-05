"""
Tests for the catalog-translation workflow steps.
"""
from unittest import mock
from uuid import uuid4

from django.test import TestCase

from enterprise_access.apps.pathways.models import (
    SnapshotCatalogFacetsInput,
    SnapshotCatalogFacetsOutput,
    SnapshotCatalogFacetsStep,
    TranslateToCatalogInput,
    TranslateToCatalogStep,
    TranslateToCatalogStepException
)

PATCH_SNAPSHOT = 'enterprise_access.apps.pathways.catalog_translation.snapshot_catalog_facets'
PATCH_REFINE = 'enterprise_access.apps.pathways.catalog_translation.refine_unmatched_skills'

SNAPSHOT_VALUES = [
    'Python (Programming Language)',
    'Microsoft Excel',
    'Data Analysis',
]


def make_snapshot_output(skill_names=None, truncated=None):
    return SnapshotCatalogFacetsOutput(
        skill_names=skill_names if skill_names is not None else list(SNAPSHOT_VALUES),
        subjects=['Computer Science'],
        truncated=truncated or [],
    )


class Accumulator:
    """Stands in for the workflow's dynamically-built accumulated-output object."""

    def __init__(self, **outputs):
        for key, value in outputs.items():
            setattr(self, key, value)


class TestSnapshotCatalogFacetsStep(TestCase):
    """
    Tests for ``SnapshotCatalogFacetsStep``.
    """

    def _step(self, allow_unscoped=False):
        return SnapshotCatalogFacetsStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=SnapshotCatalogFacetsInput(allow_unscoped=allow_unscoped).to_dict(),
        )

    @mock.patch(PATCH_SNAPSHOT)
    def test_both_skill_facets_are_merged_into_one_vocabulary(self, mock_snapshot):
        """
        The resolver only needs to know which values exist, and ``skill_names`` already
        wins a collision, so the two facets collapse to one list.
        """
        mock_snapshot.return_value = {
            'skill_names': ['Python (Programming Language)', 'Data Analysis'],
            'skills.name': ['Data Analysis', 'Communication'],
            'subjects': ['Computer Science'],
            'truncated': [],
        }

        output = self._step().execute()

        self.assertEqual(
            output.skill_names,
            ['Python (Programming Language)', 'Data Analysis', 'Communication'],
        )
        self.assertEqual(output.subjects, ['Computer Science'])

    @mock.patch(PATCH_SNAPSHOT)
    def test_truncation_is_persisted(self, mock_snapshot):
        """
        Whether the snapshot was complete changes how an unresolved term should be read,
        so it has to survive on the record.
        """
        mock_snapshot.return_value = {
            'skill_names': SNAPSHOT_VALUES, 'skills.name': [],
            'subjects': [], 'truncated': ['skill_names'],
        }

        step = self._step()
        step.execute()
        step.refresh_from_db()

        self.assertEqual(step.output_object.truncated, ['skill_names'])

    @mock.patch(PATCH_SNAPSHOT)
    def test_allow_unscoped_is_passed_through(self, mock_snapshot):
        mock_snapshot.return_value = {'skill_names': [], 'skills.name': [],
                                      'subjects': [], 'truncated': []}

        self._step(allow_unscoped=True).execute()

        self.assertTrue(mock_snapshot.call_args.kwargs['allow_unscoped'])

    @mock.patch(PATCH_SNAPSHOT)
    def test_output_round_trips_through_the_facet_snapshot_shape(self, mock_snapshot):
        mock_snapshot.return_value = {'skill_names': SNAPSHOT_VALUES, 'skills.name': [],
                                      'subjects': [], 'truncated': []}

        output = self._step().execute()

        self.assertEqual(output.as_facet_snapshot()['skill_names'], SNAPSHOT_VALUES)


class TestTranslateToCatalogStep(TestCase):
    """
    Tests for ``TranslateToCatalogStep``.
    """

    def _step(self, **input_kwargs):
        return TranslateToCatalogStep.objects.create(
            workflow_record_uuid=uuid4(),
            input_data=TranslateToCatalogInput(**input_kwargs).to_dict(),
        )

    def test_skills_resolve_to_catalog_values(self):
        step = self._step(career_skills=['Python'], skills_required=['Data Analysis'])

        output = step.execute(accumulated_output=Accumulator(
            snapshot_catalog_facets_output=make_snapshot_output(),
        ))

        resolved = {entry.catalog_value for entry in output.strict}
        self.assertIn('Python (Programming Language)', resolved)
        self.assertIn('Data Analysis', resolved)
        self.assertEqual(output.unresolved, [])
        self.assertFalse(output.refined)

    def test_refinement_is_skipped_when_everything_resolves(self):
        """Scenario: Refinement is skipped when unnecessary."""
        step = self._step(career_skills=['Python'])

        with mock.patch(PATCH_REFINE) as mock_refine:
            output = step.execute(accumulated_output=Accumulator(
                snapshot_catalog_facets_output=make_snapshot_output(),
            ))

        mock_refine.assert_not_called()
        self.assertFalse(output.refined)

    def test_refinement_runs_only_when_terms_remain_unresolved(self):
        step = self._step(career_skills=['Python', 'Welding'])

        with mock.patch(PATCH_REFINE) as mock_refine:
            mock_refine.return_value = {
                'recovered': [{'term': 'Welding', 'catalog_value': 'Welding',
                               'catalog_field': 'skill_names', 'match_type': 'exact'}],
                'unresolved': [],
                'errors': [],
            }
            output = step.execute(accumulated_output=Accumulator(
                snapshot_catalog_facets_output=make_snapshot_output(),
            ))

        mock_refine.assert_called_once()
        self.assertEqual(mock_refine.call_args.kwargs['unresolved'], ['Welding'])
        self.assertIn('Welding', {entry.catalog_value for entry in output.strict})
        # Recorded so the harness can count how often the capped snapshot was insufficient.
        self.assertTrue(output.refined)

    def test_unresolved_terms_survive_refinement_and_are_reported(self):
        step = self._step(career_skills=['Underwater Basket Weaving'])

        with mock.patch(PATCH_REFINE) as mock_refine:
            mock_refine.return_value = {
                'recovered': [], 'unresolved': ['Underwater Basket Weaving'], 'errors': [],
            }
            output = step.execute(accumulated_output=Accumulator(
                snapshot_catalog_facets_output=make_snapshot_output(),
            ))

        self.assertEqual(output.unresolved, ['Underwater Basket Weaving'])
        self.assertEqual(output.resolution_rate, 0.0)

    def test_duplicate_terms_across_sources_are_resolved_once(self):
        step = self._step(
            career_skills=['Python'],
            skills_required=['Python'],
            skills_preferred=['python'],
        )

        output = step.execute(accumulated_output=Accumulator(
            snapshot_catalog_facets_output=make_snapshot_output(),
        ))

        self.assertEqual(len(output.strict), 1)

    def test_missing_snapshot_fails_the_step_explicitly(self):
        """
        A missing snapshot must be a named failure, not an empty translation that reads
        as "this career has no catalog coverage".
        """
        step = self._step(career_skills=['Python'])

        with self.assertRaises(TranslateToCatalogStepException) as ctx:
            step.execute(accumulated_output=Accumulator())

        self.assertIn('facet snapshot', str(ctx.exception))
        step.refresh_from_db()
        self.assertIsNotNone(step.failed_at)
        self.assertIsNotNone(step.exception_message)

    def test_output_persists_and_round_trips(self):
        step = self._step(career_skills=['Python', 'Excel'])

        step.execute(accumulated_output=Accumulator(
            snapshot_catalog_facets_output=make_snapshot_output(),
        ))
        step.refresh_from_db()

        reloaded = step.output_object
        self.assertEqual(
            [entry.catalog_value for entry in reloaded.strict],
            ['Python (Programming Language)'],
        )
        self.assertEqual(
            [entry.catalog_value for entry in reloaded.boost], ['Microsoft Excel'],
        )
        self.assertEqual(reloaded.resolution_rate, 1.0)
