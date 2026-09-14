"""Parsing and structural validation for declarative procedrr documents."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import yaml

KNOWN_TOOLS = frozenset(
    {
        "gather_context",
        "internal",
        "edit",
        "yaml_edit",
        "read_document",
        "invoke_tool",
        "list_files",
    }
)
KNOWN_VALIDATORS = frozenset({"json_schema"})
_BINDING = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


@dataclass(frozen=True, slots=True)
class DocumentDiagnostic:
    path: str
    message: str


class ParseError(ValueError):
    """The source is not a valid procedrr document."""


def parse_document(source: str) -> dict[str, Any]:
    """Parse YAML and reject non-mapping documents."""
    try:
        document = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ParseError(str(exc)) from exc
    if not isinstance(document, Mapping):
        raise ParseError("a procedrr document must be a mapping")
    return dict(document)


def validate_document(document: Mapping[str, Any]) -> tuple[DocumentDiagnostic, ...]:
    diagnostics: list[DocumentDiagnostic] = []
    if not isinstance(document.get("name"), str) or not document["name"].strip():
        diagnostics.append(
            DocumentDiagnostic("name", "name must be a non-empty string")
        )
    recoveries = document.get("recoveries", {})
    recovery_names = set(recoveries) if isinstance(recoveries, Mapping) else set()
    steps = document.get("steps")
    if not isinstance(steps, list) or not steps:
        diagnostics.append(
            DocumentDiagnostic("steps", "steps must be a non-empty list")
        )
    else:
        initial = {
            item["name"]
            for item in document.get("inputs", [])
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        _validate_steps(steps, "steps", diagnostics, initial, recovery_names)
    if recoveries is not None and not isinstance(recoveries, Mapping):
        diagnostics.append(
            DocumentDiagnostic("recoveries", "recoveries must be a mapping")
        )
    elif isinstance(recoveries, Mapping):
        for name, recovery in recoveries.items():
            if not isinstance(name, str) or not isinstance(recovery, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(
                        "recoveries", "each recovery must be a named mapping"
                    )
                )
            elif not isinstance(recovery.get("steps"), list):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"recoveries.{name}.steps", "recovery steps must be a list"
                    )
                )
            else:
                _validate_steps(
                    recovery["steps"],
                    f"recoveries.{name}.steps",
                    diagnostics,
                    {"failure"},
                    recovery_names,
                )
    return tuple(diagnostics)


def parse_and_validate(source: str) -> dict[str, Any]:
    document = parse_document(source)
    diagnostics = validate_document(document)
    if diagnostics:
        detail = "; ".join(f"{item.path}: {item.message}" for item in diagnostics)
        raise ParseError(detail)
    return document


def _validate_steps(
    steps: list[Any],
    path: str,
    diagnostics: list[DocumentDiagnostic],
    bindings: set[str],
    recovery_names: set[Any] | None = None,
) -> None:
    for index, step in enumerate(steps):
        step_path = f"{path}[{index}]"
        if not isinstance(step, Mapping):
            diagnostics.append(DocumentDiagnostic(step_path, "step must be a mapping"))
            continue
        controls = {
            key
            for key in (
                "operation",
                "judge",
                "for_each",
                "worklist",
                "call",
                "terminal",
                "gate",
                "attempt",
            )
            if key in step
        }
        if len(controls) != 1:
            diagnostics.append(
                DocumentDiagnostic(step_path, "step must contain exactly one control")
            )
            continue
        control = next(iter(controls))
        if control == "attempt":
            value = step[control]
            if not isinstance(value, Mapping) or not isinstance(
                value.get("body"), list
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.body", "attempt body must be a list"
                    )
                )
            if not isinstance(value, Mapping) or not isinstance(value.get("id"), str):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.id", "attempt id must be a string"
                    )
                )
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("max_attempts"), int)
                or value["max_attempts"] <= 0
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.max_attempts",
                        "attempt max_attempts must be positive",
                    )
                )
            failure = value.get("on_failure") if isinstance(value, Mapping) else None
            if (
                not isinstance(failure, Mapping)
                or not isinstance(failure.get("recovery"), str)
                or not isinstance(failure.get("resume"), str)
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.on_failure",
                        "attempt requires recovery and resume",
                    )
                )
            elif isinstance(value, Mapping) and failure["resume"] != value.get("id"):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.on_failure.resume",
                        "resume must match the attempt id",
                    )
                )
            elif failure["recovery"] not in (recovery_names or set()):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.on_failure.recovery",
                        f"unknown recovery: {failure['recovery']}",
                    )
                )
            nested = value.get("body") if isinstance(value, Mapping) else None
            if isinstance(nested, list):
                _validate_steps(
                    nested,
                    f"{step_path}.attempt.body",
                    diagnostics,
                    set(bindings) | {"failure"},
                    recovery_names,
                )
        elif control in {"for_each", "worklist", "call"}:
            value = step[control]
            nested = (
                value.get("steps", value.get("body"))
                if isinstance(value, Mapping)
                else None
            )
            if not isinstance(value, Mapping) or not isinstance(nested, list):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.{control}", "nested steps must be a list"
                    )
                )
            else:
                local = set(bindings)
                for key in ("item_binding", "item"):
                    if isinstance(value.get(key), str):
                        local.add(value[key])
                if control == "for_each" and "collect" not in value:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.for_each.collect",
                            "data-driven body outputs require an explicit collect "
                            "binding",
                        )
                    )
                collect = value.get("collect")
                if isinstance(collect, str):
                    bindings.add(collect)
                elif isinstance(collect, Mapping) and isinstance(
                    collect.get("binding"), str
                ):
                    bindings.add(collect["binding"])
                _validate_steps(
                    nested,
                    f"{step_path}.{control}.body",
                    diagnostics,
                    local,
                    recovery_names,
                )
                bindings.update(local)
        elif control == "operation":
            operation = step[control]
            if not isinstance(operation, Mapping) or not isinstance(
                operation.get("name", operation.get("tool")), str
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.operation", "operation name is required"
                    )
                )
            elif "tool" not in operation:
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.operation.tool",
                        "a concrete tool reference is required",
                    )
                )
            elif operation.get("tool") not in KNOWN_TOOLS:
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.operation.tool",
                        f"unknown tool: {operation.get('tool')}",
                    )
                )
            elif operation.get("tool") == "gather_context":
                parameters = operation.get("parameters")
                if (
                    not isinstance(parameters, Mapping)
                    or not isinstance(parameters.get("types"), list)
                    or not parameters["types"]
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.operation.parameters",
                            "gather_context requires a non-empty types list",
                        )
                    )
            elif operation.get("tool") == "internal":
                command = operation.get("command")
                if command is None and isinstance(operation.get("parameters"), Mapping):
                    command = operation["parameters"].get("command")
                if (
                    not isinstance(command, list)
                    or not command
                    or not all(isinstance(part, str) for part in command)
                ) and not (
                    isinstance(command, str) and _BINDING.fullmatch(command) is not None
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.operation.command",
                            "internal requires a non-empty string command list",
                        )
                    )
            if isinstance(operation, Mapping):
                references = list(_template_references(operation))
                if isinstance(operation.get("source"), list):
                    references.extend(
                        item for item in operation["source"] if isinstance(item, str)
                    )
                for reference in references:
                    root = reference.split(".", 1)[0]
                    if root not in bindings:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.operation",
                                f"unknown binding: {reference}",
                            )
                        )
            if isinstance(operation, Mapping) and isinstance(
                operation.get("bind"), str
            ):
                bindings.add(operation["bind"])
        elif control == "gate":
            gate = step[control]
            if not isinstance(gate, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(f"{step_path}.gate", "gate must be a mapping")
                )
            else:
                subject = gate.get("subject")
                gate_root: str | None = (
                    subject.split(".", 1)[0] if isinstance(subject, str) else None
                )
                if not isinstance(subject, str) or gate_root not in bindings:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.gate.subject", f"unknown binding: {subject}"
                        )
                    )
                if "equals" not in gate or "on_failure" not in gate:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.gate", "equals and on_failure are required"
                        )
                    )
                failure = gate.get("on_failure")
                if isinstance(failure, Mapping):
                    retry = failure.get("retry")
                    if (
                        not isinstance(retry, Mapping)
                        or not isinstance(retry.get("max_attempts"), int)
                        or retry["max_attempts"] <= 0
                    ):
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.gate.on_failure",
                                "retry requires a positive max_attempts",
                            )
                        )
                    elif retry.get("on_exhausted") not in {"failed", "blocked"}:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.gate.on_failure.retry.on_exhausted",
                                "retry requires failed or blocked exhaustion",
                            )
                        )
        elif control == "judge":
            judge = step[control]
            if not isinstance(judge, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(f"{step_path}.judge", "judge must be a mapping")
                )
            else:
                for key in (
                    "question",
                    "subject",
                    "prompt_system",
                    "instructions",
                    "context",
                    "output",
                ):
                    if key not in judge:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.judge.{key}", "field is required"
                            )
                        )
                context = judge.get("context")
                if isinstance(context, list):
                    for binding in context:
                        if binding not in bindings:
                            diagnostics.append(
                                DocumentDiagnostic(
                                    f"{step_path}.judge.context",
                                    f"unknown binding: {binding}",
                                )
                            )
                if "validation" not in judge and "validator" not in judge:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.validation",
                            "a validation reference is required",
                        )
                    )
                output = judge.get("output")
                if not isinstance(output, Mapping) or not isinstance(
                    output.get("schema"), Mapping
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.output.schema",
                            "an inline output schema is required",
                        )
                    )
                validation = judge.get("validation", judge.get("validator"))
                kind = (
                    validation.get("kind")
                    if isinstance(validation, Mapping)
                    else validation
                )
                if kind not in KNOWN_VALIDATORS:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.validation",
                            f"unknown validator: {kind}",
                        )
                    )
                if isinstance(output, Mapping) and isinstance(output.get("name"), str):
                    bindings.add(output["name"])


def _template_references(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield from (match.group(1) for match in _BINDING.finditer(value))
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _template_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _template_references(child)


__all__ = [
    "DocumentDiagnostic",
    "ParseError",
    "parse_and_validate",
    "parse_document",
    "validate_document",
]
