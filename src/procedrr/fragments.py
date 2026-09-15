"""Validated incremental construction of executable Procedrr fragments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from procedrr.editor import apply_json_edits
from procedrr.parser import (
    DocumentDiagnostic,
    validate_document,
    validate_single_decision,
)


def start_fragment(
    *,
    name: str,
    available_bindings: Sequence[str] = (),
    allowed_tools: Sequence[str] = (),
    max_steps: int = 64,
) -> dict[str, Any]:
    """Create the initial state consumed by the generate-fragment process."""
    if not name.strip():
        raise ValueError("fragment name must not be empty")
    if max_steps <= 0:
        raise ValueError("fragment max_steps must be positive")
    roots = sorted({item.split(".", 1)[0] for item in available_bindings})
    return {
        "fragment": {
            "name": name,
            "inputs": [{"name": item} for item in roots],
            "steps": [],
        },
        "allowed_tools": sorted(set(allowed_tools)),
        "max_steps": max_steps,
        "done": False,
        "accepted": True,
        "diagnostic": {
            "code": "next_step_required",
            "json_pointer": "/steps/-",
            "message": "Append exactly one Procedrr step.",
        },
        "rejected_fingerprints": [],
        "accepted_fingerprints": [],
    }


def apply_fragment_edit(
    state: Mapping[str, Any],
    edit: Mapping[str, Any],
    *,
    evidence: Any = None,
) -> dict[str, Any]:
    """Apply one candidate step, retaining the last valid draft on rejection."""
    fragment = state.get("fragment")
    if not isinstance(fragment, Mapping):
        raise ValueError("fragment state does not contain a fragment")
    rejected = list(state.get("rejected_fingerprints", []))
    fingerprint = _fingerprint(edit)
    if fingerprint in rejected:
        return _rejected(
            state,
            rejected,
            fingerprint,
            "repeated_fragment_edit",
            "/",
            "This exact rejected edit was already proposed; make a different decision.",
        )
    if edit.get("op") != "add" or edit.get("path") != "/steps/-":
        return _rejected(
            state,
            rejected,
            fingerprint,
            "fragment_edit_scope",
            "/",
            "Each decision must append exactly one step at /steps/-.",
        )
    value = edit.get("value")
    if not isinstance(value, Mapping) or len(value) != 1:
        return _rejected(
            state,
            rejected,
            fingerprint,
            "fragment_step_shape",
            "/value",
            "The appended value must be one step with exactly one control.",
        )
    accepted = list(state.get("accepted_fingerprints", []))
    value_fingerprint = _fingerprint(value)
    if value_fingerprint in accepted:
        return _rejected(
            state,
            rejected,
            fingerprint,
            "repeated_fragment_step",
            "/value",
            "This exact step was already accepted; append a different step "
            "that advances the fragment.",
        )
    steps = fragment.get("steps")
    maximum = state.get("max_steps")
    if not isinstance(steps, list) or not isinstance(maximum, int):
        raise ValueError("fragment state is malformed")
    if len(steps) >= maximum:
        return _rejected(
            state,
            rejected,
            fingerprint,
            "fragment_step_limit",
            "/steps",
            f"The fragment cannot exceed {maximum} steps.",
        )
    candidate = apply_json_edits(fragment, [edit])
    diagnostics = [
        *validate_document(candidate),
        *validate_single_decision(candidate),
        *_tool_diagnostics(candidate, set(state.get("allowed_tools", []))),
        *_operation_contract_diagnostics(candidate, evidence=evidence),
    ]
    if diagnostics:
        first = diagnostics[0]
        message = first.message
        if first.code == "ungrounded_edit":
            message += "; copy old_text exactly from this evidence excerpt: " + repr(
                _evidence_text(evidence)[:1000]
            )
        return _rejected(
            state,
            rejected,
            fingerprint,
            first.code,
            first.json_pointer,
            message,
        )
    terminal = value.get("terminal")
    done = terminal == "succeeded"
    result = dict(state)
    result.update(
        {
            "fragment": candidate,
            "done": done,
            "accepted": True,
            "diagnostic": {
                "code": "fragment_complete" if done else "next_step_required",
                "json_pointer": "/steps/-",
                "message": (
                    "The fragment is complete."
                    if done
                    else "Append the next single Procedrr step."
                ),
            },
            "accepted_fingerprints": [*accepted, value_fingerprint],
        }
    )
    return result


def apply_fragment_step_json(
    state: Mapping[str, Any], step_json: str, *, evidence: Any = None
) -> dict[str, Any]:
    """Parse one JSON-encoded step and append it through the checked editor."""
    try:
        step = json.loads(step_json)
    except json.JSONDecodeError as exc:
        return _rejected(
            state,
            list(state.get("rejected_fingerprints", [])),
            _fingerprint(step_json),
            "invalid_step_json",
            "/step_json",
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
        )
    return apply_fragment_edit(
        state,
        {"op": "add", "path": "/steps/-", "value": step},
        evidence=evidence,
    )


def _rejected(
    state: Mapping[str, Any],
    rejected: list[str],
    fingerprint: str,
    code: str,
    pointer: str,
    message: str,
) -> dict[str, Any]:
    result = dict(state)
    if fingerprint not in rejected:
        rejected.append(fingerprint)
    result.update(
        {
            "done": False,
            "accepted": False,
            "diagnostic": {
                "code": code,
                "json_pointer": pointer,
                "message": message,
                "rejected_edit_fingerprint": fingerprint,
            },
            "rejected_fingerprints": rejected,
        }
    )
    return result


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _tool_diagnostics(
    document: Mapping[str, Any], allowed_tools: set[str]
) -> list[DocumentDiagnostic]:
    if not allowed_tools:
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
                cases = branch.get("cases", {})
                if isinstance(cases, Mapping):
                    for case, body in cases.items():
                        walk(body, f"{step_path}.branch.cases.{case}")
                walk(branch.get("default"), f"{step_path}.branch.default")

    walk(document.get("steps"), "steps")
    return diagnostics


def _operation_contract_diagnostics(
    document: Mapping[str, Any],
    *,
    evidence: Any,
) -> list[DocumentDiagnostic]:
    diagnostics: list[DocumentDiagnostic] = []
    steps = document.get("steps")
    if not isinstance(steps, list):
        return diagnostics
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        operation = step.get("operation")
        if not isinstance(operation, Mapping):
            continue
        tool = operation.get("tool")
        parameters = operation.get("parameters")
        path = f"steps[{index}].operation.parameters"
        if tool == "read_document" and (
            not isinstance(parameters, Mapping)
            or not isinstance(parameters.get("file_path"), str)
            or not parameters["file_path"].strip()
        ):
            diagnostics.append(
                DocumentDiagnostic(
                    path,
                    "read_document requires one non-empty file_path",
                    code="invalid_operation_contract",
                )
            )
        if tool == "edit":
            edits = parameters.get("edits") if isinstance(parameters, Mapping) else None
            valid = (
                isinstance(parameters, Mapping)
                and isinstance(parameters.get("file_path"), str)
                and bool(parameters["file_path"].strip())
                and isinstance(edits, list)
                and len(edits) == 1
                and isinstance(edits[0], Mapping)
                and isinstance(edits[0].get("old_text"), str)
                and bool(edits[0]["old_text"])
                and isinstance(edits[0].get("new_text"), str)
                and edits[0]["new_text"] != edits[0]["old_text"]
            )
            if not valid:
                diagnostics.append(
                    DocumentDiagnostic(
                        path,
                        "edit requires one file and exactly one non-empty, changing "
                        "old_text/new_text edit",
                        code="invalid_operation_contract",
                    )
                )
            else:
                assert isinstance(edits, list)
                first_edit = edits[0]
                assert isinstance(first_edit, Mapping)
                old_text = first_edit.get("old_text")
                assert isinstance(old_text, str)
                if "${" not in old_text and old_text not in _evidence_text(evidence):
                    diagnostics.append(
                        DocumentDiagnostic(
                            path,
                            "edit old_text does not occur in the supplied source "
                            "context",
                            code="ungrounded_edit",
                        )
                    )
        if tool == "internal":
            command = (
                parameters.get("command") if isinstance(parameters, Mapping) else None
            )
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(part, str) and part for part in command)
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        path,
                        "internal requires a non-empty command array",
                        code="invalid_operation_contract",
                    )
                )
    return diagnostics


def _evidence_text(evidence: Any) -> str:
    if evidence is None:
        return ""
    if isinstance(evidence, str):
        return evidence
    if isinstance(evidence, Mapping):
        return "\n".join(_evidence_text(value) for value in evidence.values())
    if isinstance(evidence, (list, tuple)):
        return "\n".join(_evidence_text(value) for value in evidence)
    return str(evidence)


__all__ = ["apply_fragment_edit", "apply_fragment_step_json", "start_fragment"]
