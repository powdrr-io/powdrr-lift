"""Service-owned structured model adapter for Procedrr judge responses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as validate_json

from powdrr_service.procedrr_evaluator.evaluator import WorkflowLLMClient

_PROVIDER_CREDENTIAL_RE = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?key|secret|password|token|authorization)"
    r"\s*[:=]\s*[\"']?)[^\s,}\"']+"
)
_PROVIDER_TOKEN_RE = re.compile(
    r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._-]{20,}|sk-[A-Za-z0-9_-]{20,})"
)


def _safe_provider_error(error: Exception) -> str:
    """Return bounded provider diagnostics with common credential forms removed."""
    message = " ".join(str(error).split())
    for name, secret in os.environ.items():
        if secret and any(
            marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        ):
            message = message.replace(secret, "[redacted]")
    message = _PROVIDER_CREDENTIAL_RE.sub(r"\1[redacted]", message)
    message = _PROVIDER_TOKEN_RE.sub("[redacted]", message)
    return message[:500]


class ProcedrrResponseError(RuntimeError):
    """A procedrr judge could not produce a schema-valid response."""


class ProcedrrModelClient:
    """Adapt Workrr's structured repair transport to the procedrr protocol."""

    def __init__(
        self,
        client: WorkflowLLMClient,
        *,
        process_directory: Path,
        max_retries: int = 3,
        provider_retry_attempts: int = 5,
        provider_retry_delay_seconds: float = 1.0,
        replay_responses: Mapping[str, Mapping[str, Any]] | None = None,
        telemetry_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._client = client
        self._process_directory = process_directory
        self._max_retries = max_retries
        self._provider_retry_attempts = provider_retry_attempts
        self._provider_retry_delay_seconds = provider_retry_delay_seconds
        self._replay_responses = dict(replay_responses or {})
        self._telemetry_sink = telemetry_sink

    @staticmethod
    def replay_key(messages: list[dict[str, str]]) -> str:
        """Return a stable, non-reversible key for one completed judge request."""
        canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _complete_from_provider(
        self,
        messages: list[dict[str, str]],
        response_schema: Mapping[str, Any],
        trace_id: str,
    ) -> dict[str, Any]:
        for attempt in range(self._provider_retry_attempts + 1):
            started = time.monotonic()
            try:
                try:
                    result = cast(Any, self._client).complete_json(
                        messages, response_schema=response_schema
                    )
                except TypeError as error:
                    if "response_schema" not in str(error):
                        raise
                    result = self._client.complete_json(messages)
                except RuntimeError as error:
                    if not _is_unsupported_schema_keyword(error):
                        raise
                    # Keep the full schema for local validation in parse();
                    # retry only the provider transport without constrained
                    # decoding when its grammar backend rejects a standard
                    # JSON Schema keyword.
                    result = self._client.complete_json(messages)
                self._record_provider_attempt(
                    trace_id, attempt, started, outcome="succeeded"
                )
                return result
            except Exception as exc:  # noqa: BLE001 - classify provider failures below
                retryable = _is_retryable_provider_failure(exc)
                will_retry = retryable and attempt < self._provider_retry_attempts
                self._record_provider_attempt(
                    trace_id,
                    attempt,
                    started,
                    outcome="retrying" if will_retry else "failed",
                    error_type=type(exc).__name__,
                    error_module=type(exc).__module__,
                    error_message=_safe_provider_error(exc),
                    error_status_code=(
                        getattr(exc, "status_code", None)
                        if type(getattr(exc, "status_code", None)) is int
                        else None
                    ),
                    retryable=retryable,
                )
                if not retryable:
                    raise
                if not will_retry:
                    raise
                time.sleep(self._provider_retry_delay_seconds * (2**attempt))
        raise AssertionError("provider retry loop exited without a result")

    def _record_provider_attempt(
        self,
        trace_id: str,
        attempt: int,
        started: float,
        *,
        outcome: str,
        error_type: str | None = None,
        error_module: str | None = None,
        error_message: str | None = None,
        error_status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        if self._telemetry_sink is None:
            return
        client = self._client
        delegate = getattr(client, "_fallback", None)
        self._telemetry_sink(
            {
                "record_type": "powdrr_service.procedrr.provider_attempt",
                "trace_id": trace_id,
                "attempt": attempt + 1,
                "provider_client": type(client).__name__,
                "model": getattr(client, "model_name", None)
                or getattr(client, "_model", None),
                "provider_delegate": (
                    type(delegate).__name__ if delegate is not None else None
                ),
                "delegate_model": (
                    getattr(delegate, "model_name", None)
                    or getattr(delegate, "_model", None)
                    if delegate is not None
                    else None
                ),
                "duration_ms": round((time.monotonic() - started) * 1000),
                "outcome": outcome,
                "error_type": error_type,
                "error_module": error_module,
                "error_message": error_message,
                "error_status_code": error_status_code,
                "retryable": retryable,
            }
        )

    @staticmethod
    def _structured_messages(
        messages: list[dict[str, str]], response_schema: Mapping[str, Any]
    ) -> list[dict[str, str]]:
        schema_example = json.dumps(
            _schema_example(response_schema), ensure_ascii=False, separators=(",", ":")
        )
        schema_contract = json.dumps(
            _schema_contract(response_schema),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return [
            {
                "role": "system",
                "content": (
                    "You are a JSON-only function, not a conversational assistant. "
                    "Do not provide reasoning, analysis, prose, markdown, comments, "
                    "or code fences. Emit exactly one JSON object and stop. The object "
                    "must satisfy this response contract: "
                    + schema_contract
                    + " Valid example: "
                    + schema_example
                ),
            },
            *messages,
        ]

    def complete_json_batch(
        self,
        requests: list[tuple[list[dict[str, str]], Mapping[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Complete a declared group of judge requests while preserving replay."""
        if not requests:
            return []
        replay_keys = [self.replay_key(messages) for messages, _ in requests]
        outputs: list[dict[str, Any] | None] = [None] * len(requests)
        pending: list[tuple[int, list[dict[str, str]], Mapping[str, Any], str]] = []
        for index, ((messages, schema), replay_key) in enumerate(
            zip(requests, replay_keys, strict=True)
        ):
            replay = self._replay_responses.get(replay_key)
            if replay is not None:
                validate_json(replay, schema)
                outputs[index] = dict(replay)
            else:
                pending.append(
                    (
                        index,
                        self._structured_messages(messages, schema),
                        schema,
                        replay_key,
                    )
                )
        if pending:
            batch_method = getattr(self._client, "complete_json_batch", None)
            if not callable(batch_method):
                for index, _, schema, _ in pending:
                    outputs[index] = self.complete_json(
                        requests[index][0], response_schema=schema
                    )
            else:
                started = time.monotonic()
                try:
                    batch_outputs = batch_method(
                        [(messages, schema) for _, messages, schema, _ in pending]
                    )
                    if len(batch_outputs) != len(pending):
                        raise ProcedrrResponseError(
                            "JEV returned a different number of results than requests"
                        )
                    for (index, _, schema, _), output in zip(
                        pending, batch_outputs, strict=True
                    ):
                        validate_json(output, schema)
                        outputs[index] = output
                except Exception:
                    # The batch boundary is an optimization. Preserve the
                    # established individual structured-repair path on error.
                    for index, _, schema, _ in pending:
                        outputs[index] = self.complete_json(
                            requests[index][0], response_schema=schema
                        )
                finally:
                    for _, _, _, trace_id in pending:
                        self._record_provider_attempt(
                            trace_id,
                            0,
                            started,
                            outcome="succeeded"
                            if all(output is not None for output in outputs)
                            else "failed",
                        )
        return [output for output in outputs if output is not None]

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if response_schema is None:
            raise ProcedrrResponseError("Procedrr judges require an output schema.")

        replay_key = self.replay_key(messages)

        messages = self._structured_messages(messages, response_schema)

        def parse(payload: dict[str, Any]) -> dict[str, Any]:
            validate_json(payload, response_schema)
            return payload

        replay = self._replay_responses.get(replay_key)
        if replay is not None:
            return parse(dict(replay))

        last_error = ""
        for attempt in range(self._max_retries + 1):
            if attempt:
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "The previous response was invalid: "
                            + last_error
                            + " Return one corrected JSON object only."
                        ),
                    },
                ]
            try:
                payload = self._complete_from_provider(
                    messages, response_schema, replay_key
                )
                return parse(payload)
            except JsonSchemaError as exc:
                last_error = str(exc)
            except Exception as exc:
                if not _is_invalid_json_response(exc):
                    raise
                last_error = str(exc)
        raise ProcedrrResponseError(
            "Workrr exhausted structured repair for a procedrr judge: " + last_error
        )

    @property
    def supports_batch(self) -> bool:
        """Whether the wrapped provider has a native batch completion API."""
        return callable(getattr(self._client, "complete_json_batch", None))


def _is_retryable_provider_failure(error: Exception) -> bool:
    """Identify transient provider failures that warrant backoff and retry."""

    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or 500 <= status_code <= 599
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    message = str(error).casefold()
    return any(
        phrase in message
        for phrase in (
            "connection refused",
            "connection reset",
            "remote end closed connection",
            "remote disconnected",
            "timed out",
            "timeout",
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname provided",
            "getaddrinfo failed",
            "streaming response did not include any events",
            "streaming response content was empty",
            "streaming response ended before a completion marker",
            "response message content was empty",
        )
    )


def _is_invalid_json_response(error: Exception) -> bool:
    """Recognize parse failures that can be repaired against the same schema."""
    if getattr(error, "error_code", None) == "invalid_json_response":
        return True
    message = str(error).casefold()
    return (
        "not valid json" in message
        or "invalid json" in message
        or "must be a json object" in message
    )


def _is_unsupported_schema_keyword(error: Exception) -> bool:
    """Recognize providers that reject a JSON Schema keyword in grammar mode."""
    return "grammar error: unimplemented keys:" in str(error).casefold()


__all__ = [
    "ProcedrrResponseError",
    "ProcedrrModelClient",
]


def _schema_example(schema: Mapping[str, Any]) -> Any:
    """Build a minimal concrete example to anchor weak JSON-mode providers."""
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            return {
                str(name): _schema_example(value)
                for name, value in properties.items()
                if isinstance(value, Mapping)
            }
        return {}
    if schema_type == "array":
        items = schema.get("items")
        return [_schema_example(items)] if isinstance(items, Mapping) else []
    if "enum" in schema and isinstance(schema["enum"], list):
        return schema["enum"][0] if schema["enum"] else None
    if schema_type == "boolean":
        return False
    if schema_type == "integer" or schema_type == "number":
        return 0
    if schema_type == "string" and schema.get("pattern") == r"^\{.*\}$":
        return "{}"
    if schema_type == "string" and schema.get("minLength", 0):
        return "value"
    return ""


def _schema_contract(schema: Mapping[str, Any]) -> Any:
    """Keep response-shape rules useful in a compact natural-language prompt."""
    contract: dict[str, Any] = {}
    for key in (
        "type",
        "required",
        "additionalProperties",
        "enum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
    ):
        if key in schema:
            contract[key] = schema[key]
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        contract["properties"] = {
            str(name): _schema_contract(value)
            for name, value in properties.items()
            if isinstance(value, Mapping)
        }
    items = schema.get("items")
    if isinstance(items, Mapping):
        contract["items"] = _schema_contract(items)
    return contract
