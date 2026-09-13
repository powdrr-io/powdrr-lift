"""Parsing and structural validation for declarative procedrr documents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml


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
    steps = document.get("steps")
    if not isinstance(steps, list) or not steps:
        diagnostics.append(
            DocumentDiagnostic("steps", "steps must be a non-empty list")
        )
    else:
        _validate_steps(steps, "steps", diagnostics)
    return tuple(diagnostics)


def parse_and_validate(source: str) -> dict[str, Any]:
    document = parse_document(source)
    diagnostics = validate_document(document)
    if diagnostics:
        detail = "; ".join(f"{item.path}: {item.message}" for item in diagnostics)
        raise ParseError(detail)
    return document


def _validate_steps(
    steps: list[Any], path: str, diagnostics: list[DocumentDiagnostic]
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
            )
            if key in step
        }
        if len(controls) != 1:
            diagnostics.append(
                DocumentDiagnostic(step_path, "step must contain exactly one control")
            )
            continue
        control = next(iter(controls))
        if control in {"for_each", "worklist", "call"}:
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
                _validate_steps(nested, f"{step_path}.{control}.body", diagnostics)
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
        elif control == "judge":
            judge = step[control]
            if not isinstance(judge, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(f"{step_path}.judge", "judge must be a mapping")
                )
            else:
                for key in ("question", "subject", "output", "validator"):
                    if key not in judge:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.judge.{key}", "field is required"
                            )
                        )


__all__ = [
    "DocumentDiagnostic",
    "ParseError",
    "parse_and_validate",
    "parse_document",
    "validate_document",
]
