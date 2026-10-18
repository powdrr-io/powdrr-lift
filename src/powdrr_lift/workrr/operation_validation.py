"""Input validation shared by authorized Workrr operation adapters."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core.execution_state import ExecutionArtifact
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.workrr.llm import PowdrrExecutionError

_INTERNAL_BINARY = "powdrr-lift"


class StructuredDocumentError(PowdrrExecutionError):
    """Raised when an edited JSON or YAML document is malformed."""


def worktree_relative_path(path: Path, worktree_root: Path) -> str:
    try:
        return path.resolve().relative_to(worktree_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def validate_internal_command(command: object) -> None:
    command_items = command_items_for_validation(command)
    if command_items[0] != _INTERNAL_BINARY:
        raise PowdrrExecutionError(
            "The internal tool may invoke only the powdrr-lift binary."
        )


def validate_structured_document_text(path: Path, text: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise StructuredDocumentError(
                f"Edited JSON file {path} is invalid at line {exc.lineno}, "
                f"column {exc.colno}: {exc.msg}. Correct the JSON before continuing."
            ) from exc
    elif suffix in {".yaml", ".yml"}:
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            problem_mark = getattr(exc, "problem_mark", None)
            location = (
                f"line {problem_mark.line + 1}, column {problem_mark.column + 1}"
                if problem_mark is not None
                else "an unknown location"
            )
            problem = getattr(exc, "problem", None) or str(exc)
            raise StructuredDocumentError(
                f"Edited YAML file {path} is invalid at {location}: {problem}. "
                "Correct the YAML before continuing."
            ) from exc


def command_items_for_validation(command: object) -> list[str]:
    if isinstance(command, str):
        try:
            command_items = shlex.split(command)
        except ValueError as exc:
            raise PowdrrExecutionError(
                "Workflow tool command is not valid shell syntax."
            ) from exc
    elif isinstance(command, Sequence) and not isinstance(
        command, (str, bytes, bytearray)
    ):
        command_items = list(command)
    else:
        raise PowdrrExecutionError("Workflow tool action must include a command.")
    if not command_items or any(
        not isinstance(item, str) or not item for item in command_items
    ):
        raise PowdrrExecutionError(
            "Workflow tool action command must contain non-empty strings."
        )
    return command_items


def runtime_readiness_report(runtime: ExecutionRuntime) -> dict[str, Any]:
    report = runtime.publish_readiness(required_artifact_types=())
    return {
        "ready": report.ready,
        "reasons": list(report.reasons),
        "satisfied_requirements": list(getattr(report, "satisfied_requirements", ())),
    }


def record_runtime_readiness_from_pre_step(
    step: Any,
    runtime: ExecutionRuntime,
) -> None:
    outputs = getattr(step, "outputs", ())
    if not any(output.name == "readiness_report" for output in outputs):
        return
    report_data = runtime_readiness_report(runtime)
    if not report_data["ready"]:
        return
    content_ref = (
        "readiness:"
        + hashlib.sha256(
            json.dumps(report_data, sort_keys=True).encode("utf-8")
        ).hexdigest()
    )
    runtime.record_artifact(
        ExecutionArtifact(
            artifact_id="readiness-report-" + content_ref[-16:],
            artifact_type="readiness_report",
            schema_version="readiness-report-v1",
            owner_persona_id="code_reviewer",
            content_ref=content_ref,
            accepted=True,
        )
    )
