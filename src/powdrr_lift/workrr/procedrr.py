"""Workrr transport adapter for procedrr judge responses."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as validate_json

from powdrr_lift.workflow_chat_selection import WorkflowChatConfig
from powdrr_lift.workflow_chat_transport import _complete_json_with_repair
from powdrr_lift.workrr.protocol import WorkflowLLMClient


class ProcedrrResponseError(RuntimeError):
    """A procedrr judge could not produce a schema-valid response."""


class StructuredToolExecutor:
    """Convert agent tool exceptions into Workrr-visible structured results."""

    def __init__(self, executor: Any, *, available_paths: Any = None) -> None:
        self._executor = executor
        self._available_paths = available_paths

    def __call__(self, tool: str, parameters: Mapping[str, Any]) -> Any:
        try:
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
        self._config = WorkflowChatConfig(
            skills_dir=skills_dir,
            provider_retry_attempts=max_retries,
            provider_retry_delay_seconds=0,
        )

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
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a JSON-only function, not a conversational assistant. "
                    "Do not provide reasoning, analysis, prose, markdown, comments, "
                    "or code fences. Emit exactly one JSON object and stop. The object "
                    "must match this schema example shape: " + schema_example
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

        result = _complete_json_with_repair(
            self._client,
            messages,
            context="procedrr judge",
            model="procedrr",
            parser=parse,
            repair_instructions=(
                "Return only one JSON object matching the declared schema. "
                "Preserve the original objective and correct the specific "
                "schema error; do not repeat the invalid object."
            ),
            config=self._config,
            input_func=lambda: "abort",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            response_schema=response_schema,
        )
        if not isinstance(result, dict):
            raise ProcedrrResponseError(
                "Workrr exhausted structured repair for a procedrr judge."
            )
        return result


__all__ = [
    "ProcedrrResponseError",
    "StructuredToolExecutor",
    "WorkrrProcedrrClient",
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
    return ""
