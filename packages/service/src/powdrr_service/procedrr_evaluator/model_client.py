"""Service-owned structured model adapter for Procedrr judge responses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
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
_INTERACTION_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "procedrr_interaction_context", default=None
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


def _usage_fields(client: Any) -> dict[str, Any]:
    usage = getattr(client, "last_usage", None)
    if not isinstance(usage, Mapping):
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_amount": None,
            "cost_currency": None,
        }
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    return {
        "prompt_tokens": (
            prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else None
        ),
        "completion_tokens": (
            completion
            if isinstance(completion, int) and not isinstance(completion, bool)
            else None
        ),
        "cost_amount": (
            cost
            if isinstance(
                (cost := usage.get("cost_amount", usage.get("cost"))), (int, float)
            )
            and not isinstance(cost, bool)
            else None
        ),
        "cost_currency": (
            currency
            if isinstance(
                (currency := usage.get("cost_currency", usage.get("currency"))), str
            )
            else None
        ),
    }


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
        telemetry_role: str = "unknown",
        telemetry_context: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._process_directory = process_directory
        self._max_retries = max_retries
        self._provider_retry_attempts = provider_retry_attempts
        self._provider_retry_delay_seconds = provider_retry_delay_seconds
        self._replay_responses = dict(replay_responses or {})
        self._telemetry_sink = telemetry_sink
        self._telemetry_role = telemetry_role
        self._telemetry_context = dict(telemetry_context or {})

    @staticmethod
    def replay_key(messages: list[dict[str, str]]) -> str:
        """Return a stable, non-reversible key for one completed judge request."""
        canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def set_telemetry_sink(
        self, sink: Callable[[Mapping[str, Any]], None] | None
    ) -> None:
        """Set the run-owned sink after its durable artifact is initialized."""
        self._telemetry_sink = sink

    def is_replay_hit(self, messages: list[dict[str, str]]) -> bool:
        """Return whether this request is satisfied by the configured replay map."""
        return self.replay_key(messages) in self._replay_responses

    def _complete_from_provider(
        self,
        messages: list[dict[str, str]],
        response_schema: Mapping[str, Any],
        trace_id: str,
        *,
        interaction_id: str,
        prompt_fingerprint: str,
        retry_cause: str | None,
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
                    trace_id,
                    attempt,
                    started,
                    outcome="succeeded",
                    prompt_fingerprint=prompt_fingerprint,
                    interaction_id=interaction_id,
                    retry_cause=retry_cause,
                    **_usage_fields(self._client),
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
                    prompt_fingerprint=prompt_fingerprint,
                    interaction_id=interaction_id,
                    retry_cause=("provider_transport" if retryable else retry_cause),
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
        transport_kind: str = "single",
        batch_member_count: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_amount: int | float | None = None,
        cost_currency: str | None = None,
        prompt_fingerprint: str | None = None,
        interaction_id: str | None = None,
        retry_cause: str | None = None,
        interaction_ids: list[str] | None = None,
        interaction_contexts: list[Mapping[str, Any]] | None = None,
    ) -> None:
        if self._telemetry_sink is None:
            return
        client = self._client
        delegate = getattr(client, "_fallback", None)
        interaction_context = {
            **self._telemetry_context,
            **dict(_INTERACTION_CONTEXT.get() or {}),
        }
        self._telemetry_sink(
            {
                "record_type": "powdrr_service.procedrr.provider_attempt",
                "trace_id": trace_id,
                "interaction_id": interaction_id
                or interaction_context.get("interaction_id"),
                "interaction_ids": interaction_ids,
                "interactions": (
                    [dict(context) for context in interaction_contexts]
                    if interaction_contexts is not None
                    else None
                ),
                "caller": interaction_context.get("caller"),
                "gate_consumer": interaction_context.get("gate_consumer"),
                "judge_path": interaction_context.get("judge_path"),
                "replay_status": interaction_context.get("replay_status", "miss"),
                "evidence_fingerprint": interaction_context.get("evidence_fingerprint"),
                "prompt_builder_version": interaction_context.get(
                    "prompt_builder_version"
                ),
                "task_id": interaction_context.get("task_id"),
                "attempt_id": interaction_context.get("attempt_id"),
                "process_id": interaction_context.get("process_id"),
                "process_fingerprint": interaction_context.get("process_fingerprint"),
                "prompt_fingerprint": prompt_fingerprint,
                "retry_cause": retry_cause,
                "attempt": attempt + 1,
                "provider_client": type(client).__name__,
                "role": self._telemetry_role,
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
                "transport_kind": transport_kind,
                "batch_member_count": batch_member_count,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_amount": cost_amount,
                "cost_currency": cost_currency,
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
        contexts = (_INTERACTION_CONTEXT.get() or {}).get("batch_interactions", [])
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
                    outputs[index] = self._complete_batch_member(
                        requests[index][0], schema, index
                    )
            else:
                started = time.monotonic()
                batch_error: Exception | None = None
                try:
                    batch_outputs = batch_method(
                        [(messages, schema) for _, messages, schema, _ in pending]
                    )
                    if len(batch_outputs) != len(pending):
                        raise ProcedrrResponseError(
                            "Decision provider returned a mismatched batch length"
                        )
                    for (index, _, schema, _), output in zip(
                        pending, batch_outputs, strict=True
                    ):
                        validate_json(output, schema)
                        outputs[index] = output
                except Exception as error:
                    batch_error = error
                    # The batch boundary is an optimization. Preserve the
                    # established individual structured-repair path on error.
                    for index, _, schema, _ in pending:
                        outputs[index] = self._complete_batch_member(
                            requests[index][0], schema, index
                        )
                finally:
                    self._record_provider_attempt(
                        f"batch-{uuid.uuid4().hex}",
                        0,
                        started,
                        outcome="failed" if batch_error is not None else "succeeded",
                        error_type=(
                            type(batch_error).__name__ if batch_error else None
                        ),
                        error_module=(
                            type(batch_error).__module__ if batch_error else None
                        ),
                        error_message=(
                            _safe_provider_error(batch_error) if batch_error else None
                        ),
                        transport_kind="batch",
                        batch_member_count=len(pending),
                        interaction_ids=[
                            str(contexts[index].get("interaction_id"))
                            for index, *_ in pending
                            if index < len(contexts)
                            and contexts[index].get("interaction_id")
                        ],
                        interaction_contexts=[
                            contexts[index]
                            for index, *_ in pending
                            if index < len(contexts)
                        ],
                        **(_usage_fields(self._client) if batch_error is None else {}),
                    )
        return [output for output in outputs if output is not None]

    def _complete_batch_member(
        self,
        messages: list[dict[str, str]],
        schema: Mapping[str, Any],
        index: int,
    ) -> dict[str, Any]:
        contexts = (_INTERACTION_CONTEXT.get() or {}).get("batch_interactions", [])
        context = contexts[index] if index < len(contexts) else {}
        token = _INTERACTION_CONTEXT.set({**dict(context), "batch_interactions": []})
        try:
            return self.complete_json(messages, response_schema=schema)
        finally:
            _INTERACTION_CONTEXT.reset(token)

    def complete_json_traced(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any],
        telemetry_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Complete one judge request with caller identity for its attempt trace."""
        token = _INTERACTION_CONTEXT.set(dict(telemetry_context))
        try:
            return self.complete_json(messages, response_schema=response_schema)
        finally:
            _INTERACTION_CONTEXT.reset(token)

    def complete_json_batch_traced(
        self,
        requests: list[tuple[list[dict[str, str]], Mapping[str, Any]]],
        *,
        telemetry_contexts: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Complete a batch while preserving each member's logical identity."""
        token = _INTERACTION_CONTEXT.set(
            {"batch_interactions": [dict(item) for item in telemetry_contexts]}
        )
        try:
            return self.complete_json_batch(requests)
        finally:
            _INTERACTION_CONTEXT.reset(token)

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

        interaction_context = {
            **self._telemetry_context,
            **dict(_INTERACTION_CONTEXT.get() or {}),
        }
        interaction_id = str(
            interaction_context.get("interaction_id") or uuid.uuid4().hex
        )

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
                logical_request_id = uuid.uuid4().hex
                prompt_fingerprint = self.replay_key(messages)
                payload = self._complete_from_provider(
                    messages,
                    response_schema,
                    logical_request_id,
                    interaction_id=interaction_id,
                    prompt_fingerprint=prompt_fingerprint,
                    retry_cause=("invalid_response_correction" if attempt else None),
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
