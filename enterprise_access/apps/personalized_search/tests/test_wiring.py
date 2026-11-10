"""Tests for the composition root: settings become adapters, and steps forward the turn's limits."""
from unittest import mock

from django.test import TestCase, override_settings

from enterprise_access.apps.personalized_search import wiring
from enterprise_access.apps.personalized_search.core import constants

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

    def test_model_calls_go_through_the_registry_with_the_step_token_caps(self):
        registry = mock.Mock()
        steps = wiring.steps(registry=registry, catalog=mock.Mock(), store=mock.Mock())
        steps.intent(system='s', user='u', model='m', effort=None, timeout_s=3)
        self.assertEqual(registry.complete_json.call_args.kwargs['max_tokens'], constants.INTENT_MAX_TOKENS)
        usage = mock.Mock()
        steps.compose(system='s', user='u', model='m', effort='low', timeout_s=5, usage=usage)
        compose_kwargs = registry.stream_text.call_args.kwargs
        self.assertEqual((compose_kwargs['max_tokens'], compose_kwargs['timeout_s'], compose_kwargs['usage']),
                         (constants.COMPOSE_MAX_TOKENS, 5, usage))

    @override_settings(PERSONALIZED_SEARCH_CONTENT_LANGUAGES={'en': 'English', 'es': 'Spanish'})
    def test_content_language_follows_the_portal_locale_like_learner_pathways(self):
        cases = {'es': ('es', 'Spanish'), 'es-419': ('es', 'Spanish'), 'ES_mx': ('es', 'Spanish'),
                 'fr': ('en', 'English'), '': ('en', 'English'), None: ('en', 'English')}
        for locale, expected in cases.items():
            self.assertEqual(wiring.content_language(locale), expected, locale)

    @override_settings(PERSONALIZED_SEARCH_DEV_CATALOG_SCOPE={'filter': 'x:"y"'},
                       PERSONALIZED_SEARCH_CONTENT_LANGUAGES={'en': 'English', 'es': 'Spanish'})
    def test_the_catalog_scope_carries_the_content_language(self):
        scope = wiring.catalog_scope(request=None, enterprise_customer_uuid='e-1', locale='es')
        self.assertEqual((scope.locale, scope.language_name), ('es', 'Spanish'))

    def test_turn_context_uses_the_timeout_settings(self):
        intent, compose = wiring.default_models()
        with override_settings(PERSONALIZED_SEARCH_INTENT_TIMEOUT_S=4, PERSONALIZED_SEARCH_COMPOSE_READ_TIMEOUT_S=6):
            ctx = wiring.turn_context(session={}, message='m', intent_model=intent, compose_model=compose, scope=None)
        self.assertEqual((ctx.intent_timeout_s, ctx.compose_timeout_s), (4, 6))
        self.assertEqual(ctx.platform('gpt-4.1-mini'), 'openai')

    @override_settings(PERSONALIZED_SEARCH_SHORTLIST_SIZE=20, PERSONALIZED_SEARCH_SHORTLIST_SIZES={'gpt-6.1-terra': 40})
    def test_the_shortlist_size_follows_the_compose_model(self):
        intent, _ = wiring.default_models()

        def shortlist(model):
            return wiring.turn_context(session={}, message='m', intent_model=intent,
                                       compose_model=wiring.ModelChoice(model, 'low'), scope=None).shortlist
        self.assertEqual((shortlist('gpt-6.1-terra'), shortlist('gpt-6-luna')), (40, 20))

    def test_retrieve_forwards_the_shortlist_size(self):
        catalog = mock.Mock()
        catalog.search.return_value = [{'hits': []}] * 20
        steps = wiring.steps(catalog=catalog, store=mock.Mock())
        steps.retrieve({'query': 'q', 'item_types': ['course']}, mock.Mock(filter='f', course_language_filter='l',
                       program_language_filter='l', metadata_language_filter='m'), [], timeout_s=1, shortlist=40)
        self.assertEqual(catalog.search.call_args.args[0][0]['hitsPerPage'], 60)
