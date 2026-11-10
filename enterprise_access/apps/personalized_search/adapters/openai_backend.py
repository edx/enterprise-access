"""
OpenAI model backend for Personalized Search: one JSON completion (intent) and one text stream (compose).

Configuration comes in through the constructor (``wiring.py`` reads settings), so the backend works
outside Django. Rules from the model benchmark:

- every GPT-5 and o-series model rejects ``max_tokens``; ``max_completion_tokens`` works on all;
- ``gpt-5`` / ``-mini`` / ``-nano`` take ``reasoning_effort`` ``minimal`` (not ``none``) and
  otherwise spend the whole budget reasoning; 5.1 and later take ``none`` (not ``minimal``);
  4.x rejects the parameter;
- reasoning tokens count against ``max_completion_tokens``, so efforts above the lowest need headroom.

GPT-6 and the gpt-5.6 tiers (probed 2026-11-09 through Chat Completions) take ``none`` to ``xhigh``,
except the larger sol and astra tiers, whose lowest is ``low``; none of them takes ``minimal`` or ``max``.
"""
import re
import time
from typing import Callable, Iterator

from enterprise_access.apps.personalized_search.core.errors import ModelConfigurationError, ModelError
from enterprise_access.apps.personalized_search.core.ports import Usage

REASONING_HEADROOM_TOKENS = 3000
_FIRST_GEN_GPT5 = re.compile(r'^gpt-5(-mini|-nano)?(-\d{4}-\d{2}-\d{2})?$')
_TIERED = re.compile(r'^gpt-(6(\.\d+)?|5\.6)-(luna|terra|sol|astra)$')
# Efforts each tiered family accepts, lowest first.
TIER_EFFORTS = {
    'luna': ('none', 'low', 'medium', 'high', 'xhigh'),
    'terra': ('none', 'low', 'medium', 'high', 'xhigh'),
    'sol': ('low', 'medium', 'high', 'xhigh'),
    'astra': ('low', 'medium', 'high', 'xhigh'),
}


def resolve_effort(model: str, effort: str | None) -> str | None:
    """
    The ``reasoning_effort`` to send for ``model``, or None to omit it. None, ``none`` and ``minimal``
    mean the family's lowest; an effort a tiered family doesn't take raises ``ModelConfigurationError``.
    """
    if model.startswith('gpt-4'):
        return None
    tiered = _TIERED.match(model)
    if tiered:
        efforts = TIER_EFFORTS[tiered.group(3)]
        if effort in (None, '', 'none', 'minimal'):
            return efforts[0]
        if effort not in efforts:
            raise ModelConfigurationError(f'{model!r} does not take reasoning effort {effort!r}; use one of {efforts}.')
        return effort
    lowest = 'minimal' if _FIRST_GEN_GPT5.match(model) else 'none'
    if effort in (None, '', 'none', 'minimal'):
        return lowest
    return effort


def request_params(model: str, effort: str | None, max_tokens: int) -> dict:
    """Chat Completions parameters for a model family (see module docstring)."""
    resolved = resolve_effort(model, effort)
    params = {'model': model, 'max_completion_tokens': max_tokens}
    if resolved is not None:
        params['reasoning_effort'] = resolved
        if resolved not in ('none', 'minimal'):
            params['max_completion_tokens'] = max_tokens + REASONING_HEADROOM_TOKENS
    return params


def _fill_usage(usage: Usage, raw) -> None:
    """Copy token counts from an OpenAI usage object."""
    if raw is None:
        return
    usage.input_tokens = getattr(raw, 'prompt_tokens', None)
    usage.output_tokens = getattr(raw, 'completion_tokens', None)
    usage.cached_input_tokens = getattr(getattr(raw, 'prompt_tokens_details', None), 'cached_tokens', None)
    usage.reasoning_tokens = getattr(getattr(raw, 'completion_tokens_details', None), 'reasoning_tokens', None)


class OpenAIBackend:
    """
    ``ModelBackend`` for OpenAI Chat Completions. ``models`` maps each allowed model id to its
    (input, cached input, output) USD prices per million tokens; the keys are the allowlist.
    """

    platform = 'openai'

    def __init__(self, *, api_key: str, models: dict, client_factory: Callable | None = None):
        self._api_key = api_key
        self._models = dict(models)
        self._client_factory = client_factory

    def models(self) -> list[str]:
        """The allowlist: every model with a price entry."""
        return list(self._models)

    def prices(self, model: str) -> tuple | None:
        """(input, cached input, output) USD per million tokens, for the debug cost read-out."""
        return self._models.get(model)

    def is_configured(self) -> bool:
        """Whether an API key is set."""
        return bool(self._api_key)

    def resolve_effort(self, model: str, effort: str | None) -> str | None:
        """See :func:`resolve_effort`."""
        return resolve_effort(model, effort)

    def _client(self, timeout_s: float):
        """An OpenAI client with no retries (the turn deadline is the retry budget)."""
        if not self._api_key:
            raise ModelConfigurationError('OPENAI_API_KEY is not configured.')
        if self._client_factory:
            return self._client_factory(api_key=self._api_key, timeout=timeout_s, max_retries=0)
        import openai  # pylint: disable=import-outside-toplevel
        return openai.OpenAI(api_key=self._api_key, timeout=timeout_s, max_retries=0)

    def complete_json(self, *, model: str, effort: str | None, system: str, user: str,
                      max_tokens: int, timeout_s: float) -> tuple[str, Usage]:
        """One JSON-mode completion. The prompt must mention JSON (OpenAI rejects it otherwise)."""
        usage = Usage(model=model, effort=resolve_effort(model, effort), prices=self._models.get(model))
        client = self._client(timeout_s)
        started = time.monotonic()
        try:
            response = client.chat.completions.create(
                response_format={'type': 'json_object'},
                messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                **request_params(model, effort, max_tokens),
            )
        except Exception as exc:  # noqa: BLE001  # the SDK's message can echo the learner's text
            raise ModelError(f'OpenAI request failed ({type(exc).__name__}) for model {model!r}.') from exc
        usage.elapsed_ms = round((time.monotonic() - started) * 1000)
        _fill_usage(usage, response.usage)
        usage.extra['resolved_model'] = getattr(response, 'model', None)
        if not response.choices:
            raise ModelError(f'OpenAI returned no choices for model {model!r}.')
        usage.extra['finish_reason'] = getattr(response.choices[0], 'finish_reason', None)
        return response.choices[0].message.content or '', usage

    def stream_text(self, *, model: str, effort: str | None, system: str, user: str,
                    max_tokens: int, timeout_s: float, usage: Usage) -> Iterator[str]:
        """
        Stream a completion's text deltas. ``usage`` is filled in as the stream runs and ends.
        Closing the generator closes the HTTP response (Stop).
        """
        usage.prices = self._models.get(model)
        usage.effort = resolve_effort(model, effort)  # what is billed (sol and astra send low for None)
        client = self._client(timeout_s)
        started = time.monotonic()
        try:
            stream = client.chat.completions.create(
                stream=True,
                stream_options={'include_usage': True},
                messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                **request_params(model, effort, max_tokens),
            )
        except Exception as exc:  # noqa: BLE001
            raise ModelError(f'OpenAI request failed ({type(exc).__name__}) for model {model!r}.') from exc
        try:
            for chunk in stream:
                if getattr(chunk, 'usage', None):
                    _fill_usage(usage, chunk.usage)
                if not chunk.choices:
                    continue
                if getattr(chunk.choices[0], 'finish_reason', None):
                    # 'length' means the token budget cut the answer short (the last lines are lost).
                    usage.extra['finish_reason'] = chunk.choices[0].finish_reason
                delta = chunk.choices[0].delta.content
                if delta:
                    if usage.first_token_ms is None:
                        usage.first_token_ms = round((time.monotonic() - started) * 1000)
                    yield delta
        except Exception as exc:  # noqa: BLE001
            raise ModelError(f'OpenAI stream failed ({type(exc).__name__}) for model {model!r}.') from exc
        finally:
            usage.elapsed_ms = round((time.monotonic() - started) * 1000)
            stream.close()
