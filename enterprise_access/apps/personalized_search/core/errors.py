"""Errors raised across the core's ports (model backends, catalog search and session stores)."""


class ModelError(Exception):
    """A model call failed. Carries the exception type and model only, never the request body."""


class ModelConfigurationError(ModelError):
    """A backend can't be used: no key or URL, or a model outside the allowlist."""


class CatalogSearchError(Exception):
    """A catalog search failed or can't be made (transport, response or configuration). Never carries the query."""


class SessionNotFound(Exception):
    """No session with this uuid for this learner and enterprise."""


class TurnInProgress(Exception):
    """Another turn holds this session's lock."""
