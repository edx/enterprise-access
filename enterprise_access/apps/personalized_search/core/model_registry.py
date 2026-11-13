"""
Routes a model id to the backend that serves it, so the pipeline never branches on model names.

Built by ``wiring.py`` from settings; another host registers its own backends.
"""
from typing import Iterator

from enterprise_access.apps.personalized_search.core.errors import ModelConfigurationError
from enterprise_access.apps.personalized_search.core.ports import ModelBackend, Usage


class ModelRegistry:
    """An ordered set of backends; the first that lists a model serves it."""

    def __init__(self, backends: list[ModelBackend] | None = None):
        self._backends: list[ModelBackend] = list(backends or [])

    def register(self, backend: ModelBackend) -> 'ModelRegistry':
        self._backends.append(backend)
        return self

    def backend_for(self, model: str) -> ModelBackend:
        """The backend serving ``model``. Raises ``ModelConfigurationError`` if none does."""
        for backend in self._backends:
            if model in backend.models():
                return backend
        raise ModelConfigurationError(f'Model {model!r} is not available.')

    def available_models(self) -> list[str]:
        """Every model a turn may use: the union of the backends' allowlists, in registration order."""
        out = []
        for backend in self._backends:
            out += [m for m in backend.models() if m not in out]
        return out

    def check_model(self, model: str) -> None:
        """Refuse models outside :meth:`available_models`."""
        if model not in self.available_models():
            raise ModelConfigurationError(f'Model {model!r} is not available.')

    def is_configured(self, model: str) -> bool:
        """Whether ``model``'s backend exists and can be called."""
        try:
            return self.backend_for(model).is_configured()
        except ModelConfigurationError:
            return False

    def platform_of(self, model: str) -> str:
        try:
            return self.backend_for(model).platform
        except ModelConfigurationError:
            return 'unknown'

    def resolve_effort(self, model: str, effort: str | None) -> str | None:
        return self.backend_for(model).resolve_effort(model, effort)

    def complete_json(self, *, model: str, **kwargs) -> tuple[str, Usage]:
        self.check_model(model)
        return self.backend_for(model).complete_json(model=model, **kwargs)

    def stream_text(self, *, model: str, **kwargs) -> Iterator[str]:
        """A generator, so a refused model raises on first read, inside the caller's error handling."""
        self.check_model(model)
        yield from self.backend_for(model).stream_text(model=model, **kwargs)
