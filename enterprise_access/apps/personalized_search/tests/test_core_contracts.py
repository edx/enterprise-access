"""Tests for the core's contract modules: ports, errors and constants."""
# pylint: disable=unused-argument,missing-class-docstring,missing-function-docstring
from unittest import TestCase

from enterprise_access.apps.personalized_search.core import constants
from enterprise_access.apps.personalized_search.core.errors import (
    ModelConfigurationError,
    ModelError,
    SessionNotFound,
    TurnInProgress
)
from enterprise_access.apps.personalized_search.core.ports import CatalogSearch, ModelBackend, SessionStore, Usage


class UsageTests(TestCase):
    """``Usage`` prices a call only when it has prices and token counts."""

    def test_cost_splits_cached_and_uncached_input(self):
        usage = Usage(model='m', effort=None, input_tokens=1000, cached_input_tokens=400, output_tokens=200,
                      prices=(2.0, 0.5, 8.0))
        expected = (600 * 2.0 + 400 * 0.5 + 200 * 8.0) / 1_000_000
        self.assertAlmostEqual(usage.cost_usd(), expected)

    def test_cost_is_none_without_prices_or_counts(self):
        self.assertIsNone(Usage(model='m', effort=None, input_tokens=1, output_tokens=1).cost_usd())
        self.assertIsNone(Usage(model='m', effort=None, prices=(1.0, 1.0, 1.0), output_tokens=1).cost_usd())
        self.assertIsNone(Usage(model='m', effort=None, prices=(1.0, 1.0, 1.0), input_tokens=1).cost_usd())

    def test_as_dict_rounds_cost_and_keeps_none(self):
        priced = Usage(model='m', effort='low', input_tokens=10, output_tokens=5, prices=(1.0, 1.0, 1.0),
                       elapsed_ms=12, first_token_ms=3)
        self.assertEqual(priced.as_dict(), {
            'model': 'm', 'effort': 'low', 'input_tokens': 10, 'cached_input_tokens': None, 'output_tokens': 5,
            'reasoning_tokens': None, 'elapsed_ms': 12, 'first_token_ms': 3, 'cost_usd': 0.000015,
            'cache': None, 'tokens_saved': None,
        })
        self.assertIsNone(Usage(model='m', effort=None).as_dict()['cost_usd'])


class PortsTests(TestCase):
    """The ports are runtime-checkable, so any object with the methods satisfies them."""

    def test_objects_with_the_methods_satisfy_the_ports(self):
        class Backend:
            platform = 'fake'

            def models(self):
                return []

            def is_configured(self):
                return True

            def resolve_effort(self, model, effort):
                return None

            def complete_json(self, **kwargs):
                return '{}', Usage(model='m', effort=None)

            def stream_text(self, **kwargs):
                return iter(())

        class Catalog:
            def search(self, queries, *, scope, timeout_s):
                return []

        class Store:
            def load_or_create(self, **kwargs):
                return {}

            def lock(self, session):
                return None

            def unlock(self, session_uuid):
                return None

            def persist_turn(self, session, turn):
                return None

            def not_interested_ids(self, lms_user_id, enterprise_customer_uuid):
                return []

        self.assertIsInstance(Backend(), ModelBackend)
        self.assertIsInstance(Catalog(), CatalogSearch)
        self.assertIsInstance(Store(), SessionStore)

    def test_objects_missing_methods_do_not_satisfy_them(self):
        self.assertNotIsInstance(object(), ModelBackend)
        self.assertNotIsInstance(object(), CatalogSearch)
        self.assertNotIsInstance(object(), SessionStore)


class ErrorsTests(TestCase):
    """Callers catch the base classes."""

    def test_configuration_error_is_a_model_error(self):
        self.assertTrue(issubclass(ModelConfigurationError, ModelError))
        with self.assertRaises(ModelError):
            raise ModelConfigurationError('no key')

    def test_session_errors_are_distinct(self):
        self.assertFalse(issubclass(SessionNotFound, TurnInProgress))
        self.assertFalse(issubclass(TurnInProgress, SessionNotFound))


class ConstantsTests(TestCase):
    """The limits and copy agree with each other."""

    def test_candidate_caps_add_up_to_the_candidate_limit(self):
        self.assertEqual(sum(constants.CANDIDATE_CAPS.values()), constants.MAX_CANDIDATES)
        self.assertEqual(set(constants.CANDIDATE_CAPS), set(constants.ITEM_TYPES))

    def test_every_item_type_has_a_section_label(self):
        self.assertEqual(set(constants.SECTION_LABELS), set(constants.ITEM_TYPES))

    def test_follow_up_templates_have_a_question_and_three_suggestions(self):
        self.assertEqual(set(constants.FOLLOWUP_TEMPLATES), {'level', 'time', 'pacing', 'type', 'open'})
        for question, suggestions in constants.FOLLOWUP_TEMPLATES.values():
            self.assertLessEqual(len(question), constants.CAPS['question'])
            self.assertTrue(all(len(s) <= constants.CAPS['suggestion'] for s in suggestions))

    def test_per_turn_limits_are_ordered(self):
        self.assertLessEqual(constants.MAX_ITEMS_PER_SECTION, constants.MAX_ITEMS_PER_TURN)
        self.assertLess(constants.DEFAULT_INTENT_TIMEOUT_S + constants.DEFAULT_COMPOSE_READ_TIMEOUT_S,
                        constants.TURN_DEADLINE_S)
