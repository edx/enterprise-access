"""Errors raised across the core's ports (model backends, catalog search and session stores)."""


class ModelError(Exception):
    """A model call failed. Carries the exception type and model only, never the request body."""


class ModelConfigurationError(ModelError):
    """A backend can't be used: no key or URL, a key or model the API refuses, or a model outside the allowlist."""


class ModelRejected(ModelError):
    """The model API refused the request itself (HTTP 400 or 422); sending it again won't help, so it isn't retried."""


def model_error_for_status(status: int | None, message: str) -> ModelError:
    """
    The port error for a failed model call with this HTTP status (None: no response). A key, permission or model the
    API won't accept (401, 403, 404) is a configuration error and a request it refuses (400, 422) is rejected; both
    end the turn. Anything else (timeouts, connection errors, 408, 409, 429, 5xx) may pass on a retry.
    """
    if status in (401, 403, 404):
        return ModelConfigurationError(message)
    if status in (400, 422):
        return ModelRejected(message)
    return ModelError(message)


class CatalogSearchError(Exception):
    """A catalog search failed or can't be made (transport, response or configuration). Never carries the query."""


class SessionNotFound(Exception):
    """No session with this uuid for this learner and enterprise."""


class TurnInProgress(Exception):
    """Another turn holds this session's lock."""
