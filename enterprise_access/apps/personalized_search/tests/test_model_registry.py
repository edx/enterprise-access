"""Tests for the model registry: models route to backends by allowlist, not by name checks."""
from unittest import TestCase

from enterprise_access.apps.personalized_search.core.errors import ModelConfigurationError
from enterprise_access.apps.personalized_search.core.model_registry import ModelRegistry
from enterprise_access.apps.personalized_search.core.ports import ModelBackend, Usage


class FakeBackend:
    """A ``ModelBackend`` for registry tests."""

    def __init__(self, platform, models, configured=True):
        self.platform, self._models, self._configured = platform, models, configured

    def models(self):
        return list(self._models)

    def is_configured(self):
        return self._configured

    def resolve_effort(self, model, effort):  # pylint: disable=unused-argument
        return effort

    def complete_json(self, *, model, **_):
        return '{}', Usage(model=model, effort=None)

    def stream_text(self, *, model, **_):  # pylint: disable=unused-argument
        yield f'from {self.platform}'


class ModelRegistryTests(TestCase):
    """Models route to the first backend that lists them; nothing branches on model names."""

    def setUp(self):
        super().setUp()
        self.registry = ModelRegistry([FakeBackend('openai', ['m1', 'm2'], configured=False),
                                       FakeBackend('other', ['m2', 'm3'])])

    def test_backends_satisfy_the_port(self):
        self.assertIsInstance(FakeBackend('x', []), ModelBackend)

    def test_routing_and_listing(self):
        self.assertEqual(self.registry.available_models(), ['m1', 'm2', 'm3'])
        self.assertEqual(self.registry.platform_of('m2'), 'openai')
        self.assertEqual(self.registry.platform_of('m9'), 'unknown')
        self.assertEqual(''.join(self.registry.stream_text(model='m3', usage=None)), 'from other')
        self.assertFalse(self.registry.is_configured('m1'))
        self.assertTrue(self.registry.is_configured('m3'))

    def test_unknown_model_raises_on_first_read_not_on_call(self):
        gen = self.registry.stream_text(model='m9')  # no error yet: the caller's try block owns it
        with self.assertRaises(ModelConfigurationError):
            next(gen)
        with self.assertRaises(ModelConfigurationError):
            self.registry.complete_json(model='m9')
