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
from procedrr.editor import apply_json_edits
from procedrr.parser import (
    KNOWN_TOOLS,
    DocumentDiagnostic,
    validate_document,
    validate_single_decision,
)


class ProcedrrResponseError(RuntimeError):
    """A procedrr judge could not produce a schema-valid response."""


class ProcedrrFragmentError(RuntimeError):
    """A generated Procedrr fragment failed language or contract validation."""


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

    def complete_fragment(
        self,
        messages: list[dict[str, str]],
        *,
        allowed_tools: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Produce a validated JSON Procedrr fragment with pointer-based repair."""
        latest_fragment: dict[str, Any] | None = None
        response_schema = _fragment_or_edit_schema()
        language_contract = _fragment_language_contract(allowed_tools)
        fragment_messages = [
            {
                "role": "system",
                "content": (
                    "Return one Procedrr program as a JSON object with name and "
                    "steps. Do not return YAML, markdown, prose, comments, or code "
                    "fences. Every judge must make exactly one decision. If you are "
                    "correcting a previously rejected program, you may instead return "
                    "a JSON edit object whose edits use add, replace, or remove with "
                    "RFC 6901 JSON Pointer paths. The exact language contract is: "
                    + json.dumps(
                        language_contract, ensure_ascii=False, separators=(",", ":")
                    )
                ),
            },
            *messages,
        ]

        def parse(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal latest_fragment
            try:
                validate_json(payload, response_schema)
            except JsonSchemaError as exc:
                raise ProcedrrFragmentError(
                    _json_error("fragment_response_schema", exc.json_path, exc.message)
                ) from exc
            if "edits" in payload:
                if latest_fragment is None:
                    raise ProcedrrFragmentError(
                        _json_error(
                            "fragment_edit_without_base",
                            "/edits",
                            "JSON edits require a previously rejected fragment",
                        )
                    )
                try:
                    candidate = apply_json_edits(latest_fragment, payload["edits"])
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ProcedrrFragmentError(
                        _json_error("invalid_fragment_edit", "/edits", str(exc))
                    ) from exc
            else:
                candidate = dict(payload)
            latest_fragment = candidate
            diagnostics = [
                *validate_document(candidate),
                *validate_single_decision(candidate),
                *_tool_contract_diagnostics(candidate, allowed_tools),
            ]
            if diagnostics:
                raise ProcedrrFragmentError(
                    json.dumps(
                        {
                            "code": "invalid_procedrr_fragment",
                            "diagnostics": [item.to_data() for item in diagnostics],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            return candidate

        result = _complete_json_with_repair(
            self._client,
            fragment_messages,
            context="procedrr fragment",
            model="procedrr",
            parser=parse,
            repair_instructions=(
                "Correct every reported diagnostic. Use each diagnostic's "
                "json_pointer to target the invalid value. Return either the full "
                'corrected Procedrr JSON object or {"edits":[{"op":'
                '"replace","path":"/pointer","value":...}]}. Do not '
                "weaken guards, gates, schemas, or single-decision constraints."
            ),
            config=self._config,
            input_func=lambda: "abort",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            response_schema=response_schema,
        )
        if not isinstance(result, dict):
            raise ProcedrrResponseError(
                "Workrr exhausted structured repair for a procedrr fragment."
            )
        return result


__all__ = [
    "ProcedrrFragmentError",
    "ProcedrrResponseError",
    "StructuredToolExecutor",
    "WorkrrProcedrrClient",
]


def _fragment_or_edit_schema() -> dict[str, Any]:
    edit = {
        "type": "object",
        "required": ["op", "path"],
        "additionalProperties": False,
        "properties": {
            "op": {"type": "string", "enum": ["add", "replace", "remove"]},
            "path": {"type": "string", "pattern": "^/"},
            "value": {},
        },
    }
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["name", "steps"],
                "not": {"required": ["edits"]},
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "steps": {"type": "array", "minItems": 1},
                },
            },
            {
                "type": "object",
                "required": ["edits"],
                "additionalProperties": False,
                "properties": {
                    "edits": {"type": "array", "minItems": 1, "items": edit}
                },
            },
        ]
    }


def _fragment_language_contract(
    allowed_tools: set[str] | frozenset[str] | None,
) -> dict[str, Any]:
    return {
        "document": {
            "required": ["name", "steps"],
            "steps": "non-empty array of step objects",
        },
        "step": {
            "exactly_one_control": [
                "operation",
                "judge",
                "for_each",
                "worklist",
                "call",
                "terminal",
                "gate",
                "attempt",
                "repeat",
                "branch",
            ]
        },
        "operation": {
            "required": ["tool"],
            "optional": ["parameters", "command", "bind"],
            "allowed_tools": sorted(allowed_tools or KNOWN_TOOLS),
        },
        "judge": {
            "required": [
                "question",
                "subject",
                "prompt_system",
                "instructions",
                "context",
                "output",
                "validation",
            ],
            "output": {"required": ["name", "schema"]},
            "validation": {"kind": "json_schema"},
            "single_decision": (
                "output must describe one value; arrays of independently chosen "
                "objects are forbidden"
            ),
        },
        "bindings": "reference prior bindings as ${binding.path}",
        "example": {
            "name": "read-one-file",
            "inputs": [{"name": "file_path"}],
            "steps": [
                {
                    "operation": {
                        "tool": "read_document",
                        "parameters": {"file_path": "${file_path}"},
                        "bind": "source",
                    }
                },
                {"terminal": "succeeded"},
            ],
        },
    }


def _json_error(code: str, path: str, message: str) -> str:
    return json.dumps(
        {
            "code": code,
            "diagnostics": [
                {"code": code, "json_pointer": path or "/", "message": message}
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _tool_contract_diagnostics(
    document: Mapping[str, Any], allowed_tools: set[str] | frozenset[str] | None
) -> list[DocumentDiagnostic]:
    if allowed_tools is None:
        return []

    diagnostics: list[DocumentDiagnostic] = []

    def walk(steps: Any, path: str) -> None:
        if not isinstance(steps, list):
            return
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            step_path = f"{path}[{index}]"
            operation = step.get("operation")
            if isinstance(operation, Mapping):
                tool = operation.get("tool")
                if isinstance(tool, str) and tool not in allowed_tools:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.operation.tool",
                            f"tool {tool!r} is not allowed by the fragment contract",
                            code="tool_not_allowed",
                        )
                    )
            for control in ("for_each", "worklist", "call", "attempt", "repeat"):
                nested = step.get(control)
                if isinstance(nested, Mapping):
                    walk(
                        nested.get("body", nested.get("steps")),
                        f"{step_path}.{control}",
                    )
            branch = step.get("branch")
            if isinstance(branch, Mapping):
                cases = branch.get("cases")
                if isinstance(cases, Mapping):
                    for case, body in cases.items():
                        walk(body, f"{step_path}.branch.cases.{case}")
                walk(branch.get("default"), f"{step_path}.branch.default")

    walk(document.get("steps"), "steps")
    recoveries = document.get("recoveries")
    if isinstance(recoveries, Mapping):
        for name, recovery in recoveries.items():
            if isinstance(recovery, Mapping):
                walk(recovery.get("steps"), f"recoveries.{name}.steps")
    return diagnostics


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
