"""Provider retry behavior used by bounded planning questions."""

from __future__ import annotations

import inspect
import time
from collections.abc import Mapping
from typing import Any, Protocol, cast

from powdrr_service.providers.errors import ProviderClientError


class PlanningModelClient(Protocol):
    """Structured completion surface needed by planning questions."""

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class PlanningInferenceError(ProviderClientError):
    """A planning completion exhausted its provider retry budget."""


def complete_json_with_timeout_retry(
    client: PlanningModelClient,
    messages: list[dict[str, str]],
    *,
    model: str,
    stderr: Any,
    max_timeout_retries: int,
    timeout_backoff_seconds: float,
    response_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Request structured output and retry only transient provider failures."""
    retries = 0
    while True:
        try:
            return _complete_json(client, messages, response_schema=response_schema)
        except RuntimeError as error:
            if not _is_retryable(error):
                raise
            if retries >= max(0, max_timeout_retries):
                raise PlanningInferenceError(
                    f"LLM request failed after {retries} retries: {error}"
                ) from error
            retries += 1
            delay = timeout_backoff_seconds * (2 ** (retries - 1))
            reason = (
                "timed out"
                if _is_timeout(error)
                else "connection dropped"
                if not isinstance(getattr(error, "status_code", None), int)
                else "provider is overloaded"
            )
            print(
                f"LLM request {reason} for {model}; retrying in {delay:g} seconds "
                f"(retry {retries}/{max_timeout_retries}).",
                file=stderr,
                flush=True,
            )
            time.sleep(delay)


def _complete_json(
    client: PlanningModelClient,
    messages: list[dict[str, str]],
    *,
    response_schema: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if response_schema is None or not _supports_response_schema(client):
        return client.complete_json(messages)
    schema_client = cast(Any, client)
    return schema_client.complete_json(messages, response_schema=response_schema)


def _supports_response_schema(client: PlanningModelClient) -> bool:
    try:
        parameters = inspect.signature(client.complete_json).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "response_schema"
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _is_timeout(error: RuntimeError) -> bool:
    message = str(error).casefold()
    return "timed out" in message or "timeout" in message


def _is_retryable(error: RuntimeError) -> bool:
    status_code = getattr(error, "status_code", None)
    return (
        _is_timeout(error)
        or (
            isinstance(status_code, int)
            and (status_code == 429 or 500 <= status_code <= 599)
        )
        or any(
            phrase in str(error).casefold()
            for phrase in (
                "remote end closed connection",
                "remote disconnected",
                "connection reset",
                "connection aborted",
                "broken pipe",
                "temporary failure in name resolution",
                "name or service not known",
                "nodename nor servname provided",
                "getaddrinfo failed",
                "streaming response did not include any events",
                "streaming response content was empty",
                "response message content was empty",
            )
        )
    )
