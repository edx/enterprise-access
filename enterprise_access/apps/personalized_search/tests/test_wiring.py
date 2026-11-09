"""Tests for the composition root: settings become adapters, and steps forward the turn's limits."""
from unittest import mock

from django.test import TestCase, override_settings

from enterprise_access.apps.personalized_search import wiring

MODELS = {'gpt-4.1-mini': (0.4, 0.1, 1.6)}


@override_settings(OPENAI_API_KEY='k', PERSONALIZED_SEARCH_OPENAI_MODELS=MODELS)
class WiringTests(TestCase):
    """``wiring.steps`` and ``wiring.turn_context``."""

    def test_retrieve_forwards_the_capped_timeout(self):
        catalog = mock.Mock()
        catalog.search.return_value = [{'hits': []}]
        steps = wiring.steps(catalog=catalog, store=mock.Mock())
        steps.retrieve({'query': 'q', 'item_types': ['course']}, mock.Mock(filter='f'), [], timeout_s=0.75)
        self.assertEqual(catalog.search.call_args.kwargs['timeout_s'], 0.75)

    def test_turn_context_uses_the_timeout_settings(self):
        intent, compose = wiring.default_models()
        with override_settings(PERSONALIZED_SEARCH_INTENT_TIMEOUT_S=4, PERSONALIZED_SEARCH_COMPOSE_READ_TIMEOUT_S=6):
            ctx = wiring.turn_context(session={}, message='m', intent_model=intent, compose_model=compose, scope=None)
        self.assertEqual((ctx.intent_timeout_s, ctx.compose_timeout_s), (4, 6))
        self.assertEqual(ctx.platform('gpt-4.1-mini'), 'openai')
