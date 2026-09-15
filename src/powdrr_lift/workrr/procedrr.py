"""Workrr transport adapter for procedrr judge responses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as validate_json

from powdrr_lift.workrr.protocol import WorkflowLLMClient
from procedrr.fragments import apply_fragment_step_json, start_fragment


class ProcedrrResponseError(RuntimeError):
    """A procedrr judge could not produce a schema-valid response."""


class StructuredToolExecutor:
    """Convert agent tool exceptions into Workrr-visible structured results."""

    def __init__(self, executor: Any, *, available_paths: Any = None) -> None:
        self._executor = executor
        self._available_paths = available_paths

    def __call__(self, tool: str, parameters: Mapping[str, Any]) -> Any:
        try:
            if tool == "procedrr_fragment_start":
                return start_fragment(
                    name=str(parameters.get("name", "generated-fragment")),
                    available_bindings=_string_sequence(
                        parameters.get("available_bindings")
                    ),
                    allowed_tools=_string_sequence(parameters.get("allowed_tools")),
                    max_steps=int(parameters.get("max_steps", 64)),
                )
            if tool == "procedrr_fragment_apply_edit":
                state = parameters.get("state")
                step_json = parameters.get("step_json")
                if not isinstance(state, Mapping) or not isinstance(step_json, str):
                    raise ValueError(
                        "fragment apply requires a state mapping and step_json string"
                    )
                return apply_fragment_step_json(
                    state, step_json, evidence=parameters.get("evidence")
                )
            if tool == "edit":
                parameters = _materialize_line_edits(parameters, self._executor)
                edits = parameters.get("edits")
                if isinstance(edits, list) and any(
                    isinstance(item, Mapping)
                    and item.get("old_text") == item.get("new_text")
                    for item in edits
                ):
                    return {
                        "ok": False,
                        "error": {
                            "code": "no_op_edit",
                            "message": (
                                "edit replacement is identical to selected source lines"
                            ),
                            "tool": tool,
                            "retryable": True,
                        },
                    }
                if isinstance(edits, list) and any(
                    isinstance(item, Mapping)
                    and (
                        not item.get("old_text")
                        or item.get("old_text") == item.get("new_text")
                        or (
                            isinstance(item.get("start"), int)
                            and isinstance(item.get("end"), int)
                            and item["start"] == item["end"]
                            and not item.get("new_text")
                        )
                    )
                    for item in edits
                ):
                    return {
                        "ok": False,
                        "error": {
                            "code": "no_op_edit",
                            "message": "edit must change a non-empty source substring",
                            "tool": tool,
                            "retryable": True,
                        },
                    }
            return self._executor(tool, parameters)
        except FileNotFoundError as exc:
            path = parameters.get("file_path")
            details: dict[str, Any] = {
                "code": "file_not_found",
                "message": str(exc),
                "path": path,
                "retryable": True,
            }
            if callable(self._available_paths):
                details["available_paths"] = list(self._available_paths())
            return {"ok": False, "error": details}
        except Exception as exc:  # noqa: BLE001 - normalize tool boundary failures
            return {
                "ok": False,
                "error": {
                    "code": "tool_execution_failed",
                    "message": str(exc),
                    "tool": tool,
                    "retryable": False,
                },
            }


class WorkrrProcedrrClient:
    """Adapt Workrr's structured repair transport to the procedrr protocol."""

    def __init__(
        self,
        client: WorkflowLLMClient,
        *,
        skills_dir: Path,
        max_retries: int = 3,
    ) -> None:
        self._client = client
        self._skills_dir = skills_dir
        self._max_retries = max_retries

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if response_schema is None:
            raise ProcedrrResponseError("Procedrr judges require an output schema.")

        schema_example = json.dumps(
            _schema_example(response_schema), ensure_ascii=False, separators=(",", ":")
        )
        schema_contract = json.dumps(
            _schema_contract(response_schema),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        messages = [
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

        def parse(payload: dict[str, Any]) -> dict[str, Any]:
            try:
                validate_json(payload, response_schema)
            except JsonSchemaError as exc:
                raise RuntimeError(f"response_schema: {exc.message}") from exc
            return payload

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
                return parse(
                    cast(Any, self._client).complete_json(
                        messages, response_schema=response_schema
                    )
                )
            except (JsonSchemaError, RuntimeError, ValueError) as exc:
                last_error = str(exc)
        raise ProcedrrResponseError(
            "Workrr exhausted structured repair for a procedrr judge: " + last_error
        )


__all__ = [
    "ProcedrrResponseError",
    "StructuredToolExecutor",
    "WorkrrProcedrrClient",
]


def _string_sequence(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError("expected a list of strings")
    return list(value)


def _materialize_line_edits(
    parameters: Mapping[str, Any], executor: Any
) -> dict[str, Any]:
    edits = parameters.get("edits")
    file_path = parameters.get("file_path")
    if not isinstance(edits, list) or not isinstance(file_path, str):
        return dict(parameters)
    if not any(isinstance(item, Mapping) and "start" in item for item in edits):
        return dict(parameters)
    source = executor("read_document", {"file_path": file_path})
    if not isinstance(source, str):
        raise ValueError("line edit could not read the target source")
    lines = source.splitlines(keepends=True)
    materialized: list[dict[str, Any]] = []
    for item in edits:
        if not isinstance(item, Mapping):
            raise ValueError("line edit must be an object")
        start, end = item.get("start"), item.get("end")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not 1 <= start <= end <= len(lines)
        ):
            raise ValueError("line edit range is outside the source")
        materialized.append(
            {
                "old_text": "".join(lines[start - 1 : end]),
                "new_text": item.get("new_text"),
            }
        )
    result = dict(parameters)
    result["edits"] = materialized
    return result


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
