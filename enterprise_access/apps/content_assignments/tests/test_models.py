"""
Tests for the ``api.py`` module of the content_assignments app.
"""
import re
import uuid

from django.test import TestCase
from django.utils import timezone

from enterprise_access.apps.subsidy_access_policy.tests.factories import AssignedLearnerCreditAccessPolicyFactory

from ..constants import RETIRED_EMAIL_ADDRESS_FORMAT, AssignmentActions, AssignmentActorTypes, AssignmentSources
from ..models import AssignmentConfiguration, LearnerContentAssignmentAction
from .factories import LearnerContentAssignmentFactory


class TestAssignmentActions(TestCase):
    """
    Test functions around LearnerContentAssignmentActions.
    """

    @classmethod
    def setUpClass(cls):
        """
        Set up a single assignment record.
        """
        super().setUpClass()
        cls.assignment_configuration = AssignmentConfiguration.objects.create()
        cls.subsidy_access_policy = AssignedLearnerCreditAccessPolicyFactory.create(
            assignment_configuration=cls.assignment_configuration,
        )
        cls.assignment = LearnerContentAssignmentFactory.create(
            assignment_configuration=cls.assignment_configuration,
        )

    def tearDown(self):
        """
        Clear all actions after each test function.
        """
        super().tearDown()
        self.assignment.actions.all().delete()

    def test_get_set_linked_action(self):
        """
        Tests that we can idempotently get/set the linked action for an assignment.
        """
        # Start with no linked actions
        self.assertIsNone(self.assignment.get_last_successful_linked_action())

        # now create one
        linked_action = self.assignment.add_successful_linked_action()

        self.assertEqual(linked_action.action_type, AssignmentActions.LEARNER_LINKED)
        self.assertIsNone(linked_action.error_reason)
        self.assertAlmostEqual(
            timezone.now(),
            linked_action.completed_at,
            delta=timezone.timedelta(seconds=2),
        )

        # now if we fetch the linked action for this assignment, we'll
        # get the thing we just created
        self.assertEqual(
            self.assignment.get_last_successful_linked_action(),
            linked_action,
        )

        # ...and adding a linked action through this method will create a new action record
        linked_action_again = self.assignment.add_successful_linked_action()
        self.assertNotEqual(linked_action_again, linked_action)
        self.assertEqual(
            self.assignment.get_last_successful_linked_action(),
            linked_action_again,
        )

    def test_get_set_notified_action(self):
        """
        Tests that we can idempotently get/set the notified action for an assignment.
        """
        # Start with no notified actions
        self.assertIsNone(self.assignment.get_last_successful_notified_action())

        # now create one
        notified_action = self.assignment.add_successful_notified_action()

        self.assertEqual(notified_action.action_type, AssignmentActions.NOTIFIED)
        self.assertIsNone(notified_action.error_reason)
        self.assertAlmostEqual(
            timezone.now(),
            notified_action.completed_at,
            delta=timezone.timedelta(seconds=2),
        )

        # now if we fetch the notified action for this assignment, we'll
        # get the thing we just created
        self.assertEqual(
            self.assignment.get_last_successful_notified_action(),
            notified_action,
        )

        # ...and adding a notified action through this method creates a new action record
        notified_action_again = self.assignment.add_successful_notified_action()
        self.assertNotEqual(notified_action_again, notified_action)
        self.assertEqual(
            self.assignment.get_last_successful_notified_action(),
            notified_action_again,
        )

    def test_get_set_reminded_actions(self):
        """
        Tests that we can idempotently get/set the reminded action for an assignment.
        """
        # Start with no reminded actions
        self.assertIsNone(self.assignment.get_last_successful_reminded_action())

        # now create one
        reminded_action = self.assignment.add_successful_reminded_action()

        self.assertEqual(reminded_action.action_type, AssignmentActions.REMINDED)
        self.assertIsNone(reminded_action.error_reason)
        self.assertAlmostEqual(
            timezone.now(),
            reminded_action.completed_at,
            delta=timezone.timedelta(seconds=2),
        )

        # now if we fetch the reminded action for this assignment, we'll
        # get the thing we just created
        self.assertEqual(
            self.assignment.get_last_successful_reminded_action(),
            reminded_action,
        )

        # we can have multiple, successful reminded actions for our assignment
        reminded_action_again = self.assignment.add_successful_reminded_action()
        self.assertNotEqual(reminded_action_again.uuid, reminded_action.uuid)
        self.assertIsNone(reminded_action_again.error_reason)
        self.assertAlmostEqual(
            timezone.now(),
            reminded_action_again.completed_at,
            delta=timezone.timedelta(seconds=2),
        )

        # now `reminded_action_again` is the most recent reminded action
        self.assertEqual(
            self.assignment.get_last_successful_reminded_action(),
            reminded_action_again,
        )

    def test_clear_pii(self):
        """
        Tests that we can clear pii on an assignment.
        """
        self.assignment.learner_email = 'foo@bar.com'
        self.assignment.lms_user_id = 12345
        self.assignment.save()

        self.assignment.clear_pii()
        self.assignment.save()

        self.assignment.refresh_from_db()

        self.assertEqual(12345, self.assignment.lms_user_id)
        pattern = RETIRED_EMAIL_ADDRESS_FORMAT.format('[a-f0-9]{16}')
        self.assertIsNotNone(re.match(pattern, self.assignment.learner_email))

        for historical_record in self.assignment.history.all():
            self.assertIsNotNone(re.match(pattern, historical_record.learner_email))


class TestLearnerContentAssignmentActionAuditFields(TestCase):
    """
    Unit tests for ENT-12026 audit field extensions on LearnerContentAssignmentAction.
    Covers: nullable defaults, field persistence, enum choices, new action types, index declaration.
    """

    @classmethod
    def setUpTestData(cls):
        cls.assignment_configuration = AssignmentConfiguration.objects.create()
        cls.assignment = LearnerContentAssignmentFactory.create(
            assignment_configuration=cls.assignment_configuration,
        )

    def tearDown(self):
        super().tearDown()
        self.assignment.actions.all().delete()

    def _create_action(self, **kwargs):
        """Create an action with safe defaults; override via kwargs."""
        return self.assignment.actions.create(
            action_type=AssignmentActions.NOTIFIED,
            completed_at=timezone.now(),
            **kwargs,
        )

    # --- Nullable defaults ---

    def test_new_fields_are_null_by_default(self):
        """Every new audit field defaults to None when not supplied."""
        action = self._create_action()
        self.assertIsNone(action.actor_lms_user_id)
        self.assertIsNone(action.actor_type)
        self.assertIsNone(action.learner_lms_user_id)
        self.assertIsNone(action.learner_email)
        self.assertIsNone(action.learner_external_key)
        self.assertIsNone(action.source)
        self.assertIsNone(action.enterprise_customer_uuid)
        self.assertIsNone(action.metadata)

    # --- Persistence / round-trip ---

    def test_audit_fields_persist_and_round_trip(self):
        """All new fields are saved to and retrieved from the DB correctly."""
        customer_uuid = uuid.uuid4()
        action = self._create_action(
            actor_lms_user_id=42,
            actor_type=AssignmentActorTypes.ADMIN,
            learner_lms_user_id=99,
            learner_email='learner@example.com',
            learner_external_key='ext-key-001',
            source=AssignmentSources.ADMIN_UI_SINGLE,
            enterprise_customer_uuid=customer_uuid,
            metadata={'correlation_id': 'abc-123', 'batch_id': 'batch-456'},
        )
        action.refresh_from_db()
        self.assertEqual(action.actor_lms_user_id, 42)
        self.assertEqual(action.actor_type, AssignmentActorTypes.ADMIN)
        self.assertEqual(action.learner_lms_user_id, 99)
        self.assertEqual(action.learner_email, 'learner@example.com')
        self.assertEqual(action.learner_external_key, 'ext-key-001')
        self.assertEqual(action.source, AssignmentSources.ADMIN_UI_SINGLE)
        self.assertEqual(action.enterprise_customer_uuid, customer_uuid)
        self.assertEqual(action.metadata['correlation_id'], 'abc-123')
        self.assertEqual(action.metadata['batch_id'], 'batch-456')

    # --- Enum choices ---

    def test_all_actor_type_choices_are_accepted(self):
        """actor_type accepts every value in AssignmentActorTypes.CHOICES."""
        for value, _ in AssignmentActorTypes.CHOICES:
            with self.subTest(actor_type=value):
                action = self._create_action(actor_type=value)
                self.assertEqual(action.actor_type, value)

    def test_all_source_choices_are_accepted(self):
        """source accepts every value in AssignmentSources.CHOICES."""
        for value, _ in AssignmentSources.CHOICES:
            with self.subTest(source=value):
                action = self._create_action(source=value)
                self.assertEqual(action.source, value)

    # --- New action type enums ---

    def test_new_action_types_are_valid_choices(self):
        """ALLOCATED, REALLOCATED, APPROVED, ERRORED are accepted action_type values."""
        new_types = [
            AssignmentActions.ALLOCATED,
            AssignmentActions.REALLOCATED,
            AssignmentActions.APPROVED,
            AssignmentActions.ERRORED,
        ]
        for action_type in new_types:
            with self.subTest(action_type=action_type):
                action = self.assignment.actions.create(
                    action_type=action_type,
                    completed_at=timezone.now(),
                )
                self.assertEqual(action.action_type, action_type)

    # --- Index declaration ---

    def test_composite_indexes_declared_on_meta(self):
        """Both composite indexes are present in LearnerContentAssignmentAction._meta.indexes."""
        index_field_sets = [
            tuple(idx.fields)
            for idx in LearnerContentAssignmentAction._meta.indexes
        ]
        self.assertIn(('assignment', 'created'), index_field_sets)
        self.assertIn(('enterprise_customer_uuid', 'created'), index_field_sets)

    # --- Metadata JSON structure ---

    def test_metadata_accepts_all_documented_keys(self):
        """metadata JSONField persists all documented audit keys correctly."""
        payload = {
            'correlation_id': 'corr-001',
            'batch_id': 'batch-002',
            'request_id': 'req-003',
            'state_before': 'allocated',
            'state_after': 'errored',
            'error_code': 'STRIPE_TIMEOUT',
            'error_message': 'Stripe API timed out.',
            'idempotency_key': 'idem-key-xyz',
        }
        action = self._create_action(metadata=payload)
        action.refresh_from_db()
        self.assertEqual(action.metadata, payload)
