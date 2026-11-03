"""Local structured tool dispatch for the combined application runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from powdrr_service.procedrr.fragments import apply_fragment_step_json, start_fragment


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
                # Accept the explicit model-facing operation name and normalize
                # it to the underlying edit tool contract.
                if parameters.get("operation") == "replace_lines":
                    parameters = {
                        "file_path": parameters.get("file_path"),
                        "edits": [
                            {
                                "start": parameters.get("start_line"),
                                "end": parameters.get(
                                    "end_line", parameters.get("start_line")
                                ),
                                "new_text": parameters.get("replacement"),
                            }
                        ],
                    }
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
