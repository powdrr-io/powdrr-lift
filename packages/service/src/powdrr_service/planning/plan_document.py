"""Parsing, validation, and serialization for reviewed execution plans."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from powdrr_service.planning.plan_models import PlannedWorkItem, ReviewedExecutionPlan

QUEUE_SCHEMA = "https://powdrr.io/schemas/github-issue-execution-plan-v1"
MANUAL_PREREQUISITE_TRIGGER = "after_implementation_before_validation"
PLAN_REPAIR_MARKER = "<!-- powdrr-plan-repair:v1 -->"
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")


class PlanDocumentError(ValueError):
    """A reviewed execution plan document is invalid."""


def parse_reviewed_plan_yaml(text: str) -> ReviewedExecutionPlan:
    """Parse and validate a GitHub issue execution plan document."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise PlanDocumentError(f"invalid plan YAML: {error}") from error
    if not isinstance(data, Mapping) or data.get("schema") != QUEUE_SCHEMA:
        raise PlanDocumentError(f"plan schema must be {QUEUE_SCHEMA!r}")
    allowed_fields = {"schema", "plan_id", "source_document", "items", "planning"}
    required_fields = {"schema", "plan_id", "items"}
    if not required_fields <= set(data) or set(data) - allowed_fields:
        raise PlanDocumentError("plan has unknown or missing top-level fields")
    plan_id = data.get("plan_id")
    source_document = data.get("source_document")
    if source_document is not None and (
        not isinstance(source_document, str)
        or not source_document.strip()
        or Path(source_document).is_absolute()
        or ".." in Path(source_document).parts
    ):
        raise PlanDocumentError(
            "source_document must be a repository-relative path without traversal"
        )
    raw_items = data.get("items")
    if not isinstance(plan_id, str) or not _safe_id(plan_id):
        raise PlanDocumentError("plan_id must be a stable safe identifier")
    if not isinstance(raw_items, list) or not raw_items:
        raise PlanDocumentError("plan items must be a non-empty list")
    items: list[PlannedWorkItem] = []
    for index, raw in enumerate(raw_items):
        field = f"items[{index}]"
        if not isinstance(raw, Mapping):
            raise PlanDocumentError(f"{field} must be a mapping")
        common_fields = {
            "id",
            "title",
            "objective",
            "scope",
            "acceptance_criteria",
            "dependencies",
            "workflow_id",
        }
        workflow_keys = set(raw) & {"workflow_directory", "workflow_file"}
        optional_keys = set(raw) - common_fields - workflow_keys
        if (
            not common_fields <= set(raw)
            or len(workflow_keys) != 1
            or not optional_keys
            <= {"implementation_steps", "allowed_paths", "manual_prerequisite"}
        ):
            raise PlanDocumentError(f"{field} has unknown or missing fields")
        item_id = raw.get("id")
        workflow_dir = raw.get("workflow_directory")
        workflow_file = raw.get("workflow_file")
        workflow_id = raw.get("workflow_id")
        if not isinstance(item_id, str) or not _safe_id(item_id):
            raise PlanDocumentError(f"{field}.id must be a stable safe identifier")
        workflow_reference = (
            workflow_file if workflow_file is not None else workflow_dir
        )
        if not isinstance(workflow_reference, str) or not workflow_reference:
            raise PlanDocumentError(f"{field}.workflow_file is required")
        if (
            Path(workflow_reference).is_absolute()
            or ".." in Path(workflow_reference).parts
        ):
            raise PlanDocumentError(
                f"{field}.workflow_file must stay in the repository"
            )
        if not isinstance(workflow_id, str) or not _safe_id(workflow_id):
            raise PlanDocumentError(
                f"{field}.workflow_id must be a stable safe identifier"
            )
        item = PlannedWorkItem(
            item_id=item_id,
            title=_required_text(raw, "title", field),
            objective=_required_text(raw, "objective", field),
            scope=_string_list(raw, "scope", field),
            acceptance_criteria=_string_list(raw, "acceptance_criteria", field),
            dependencies=_string_list(raw, "dependencies", field, allow_empty=True),
            workflow_directory=workflow_dir if isinstance(workflow_dir, str) else None,
            workflow_id=workflow_id,
            workflow_file=workflow_file if isinstance(workflow_file, str) else None,
            implementation_steps=(
                _string_list(raw, "implementation_steps", field)
                if "implementation_steps" in raw
                else ()
            ),
            allowed_paths=(
                _repository_path_list(raw, field) if "allowed_paths" in raw else (".",)
            ),
            manual_prerequisite=(
                parse_manual_prerequisite(raw["manual_prerequisite"], field)
                if "manual_prerequisite" in raw
                else None
            ),
        )
        if not item.scope or not item.acceptance_criteria:
            raise PlanDocumentError(
                f"{field} must declare scope and acceptance criteria"
            )
        if "implementation_steps" in raw and (
            len(item.scope) > 5 or not 1 <= len(item.implementation_steps) <= 8
        ):
            raise PlanDocumentError(
                f"{field} exceeds the bounded scope or implementation-step limits"
            )
        items.append(item)
    ids = [item.item_id for item in items]
    if len(set(ids)) != len(ids):
        raise PlanDocumentError("plan item IDs must be unique")
    known = set(ids)
    for item in items:
        if (
            len(set(item.dependencies)) != len(item.dependencies)
            or item.item_id in item.dependencies
            or not set(item.dependencies) <= known
        ):
            raise PlanDocumentError(
                f"item {item.item_id!r} has a self or unknown dependency"
            )
    if _has_dependency_cycle(items):
        raise PlanDocumentError("plan dependencies must be acyclic")
    planning = data.get("planning")
    if planning is not None:
        if not isinstance(planning, Mapping):
            raise PlanDocumentError("planning metadata must be a mapping")
        try:
            from powdrr_service.planning.compiler import PlanningManifest

            PlanningManifest.from_data(planning)
        except (ValueError, TypeError) as error:
            raise PlanDocumentError(f"invalid planning metadata: {error}") from error
    return ReviewedExecutionPlan(
        plan_id, tuple(items), source_document, dict(planning) if planning else None
    )


def serialize_reviewed_plan_yaml(plan: ReviewedExecutionPlan) -> str:
    """Deterministically serialize a typed execution plan, including metadata."""
    data: dict[str, Any] = {"schema": QUEUE_SCHEMA, "plan_id": plan.plan_id}
    if plan.source_document is not None:
        data["source_document"] = plan.source_document
    items = []
    for item in plan.items:
        record: dict[str, Any] = {
            "id": item.item_id,
            "title": item.title,
            "objective": item.objective,
            "scope": list(item.scope),
            "acceptance_criteria": list(item.acceptance_criteria),
            "dependencies": list(item.dependencies),
            "workflow_file": item.workflow_file or item.workflow_directory,
            "workflow_id": item.workflow_id,
        }
        if item.implementation_steps:
            record["implementation_steps"] = list(item.implementation_steps)
        if item.allowed_paths != (".",):
            record["allowed_paths"] = list(item.allowed_paths)
        if item.manual_prerequisite is not None:
            record["manual_prerequisite"] = dict(item.manual_prerequisite)
        items.append(record)
    data["items"] = items
    if plan.planning is not None:
        from powdrr_service.planning.compiler import PlanningManifest

        data["planning"] = PlanningManifest.from_data(plan.planning).to_data()
    return yaml.safe_dump(
        data, allow_unicode=True, default_flow_style=False, sort_keys=False
    )


def render_plan_repair_metadata(
    *, plan_id: str, plan_path: str, repair: Mapping[str, Any]
) -> str:
    """Render the authoritative repair manifest embedded in an amendment PR."""
    payload = {
        "schema": "https://powdrr.io/schemas/github-issue-plan-repair-v1",
        "plan_id": plan_id,
        "plan_path": plan_path,
        "repair": dict(repair),
    }
    return (
        f"{PLAN_REPAIR_MARKER}\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)}\n"
        "```"
    )


def parse_manual_prerequisite(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "title",
        "instructions",
        "verification",
        "trigger",
        "unblocks",
    }:
        raise PlanDocumentError(
            f"{field}.manual_prerequisite must contain exactly title, instructions, "
            "verification, trigger, and unblocks"
        )
    title = value.get("title")
    instructions = value.get("instructions")
    verification = value.get("verification")
    trigger = value.get("trigger")
    unblocks = value.get("unblocks")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
        raise PlanDocumentError(
            f"{field}.manual_prerequisite.title must be non-empty text of at most "
            "160 characters"
        )
    if (
        not isinstance(instructions, list)
        or not instructions
        or not all(isinstance(item, str) and item.strip() for item in instructions)
    ):
        raise PlanDocumentError(
            f"{field}.manual_prerequisite.instructions must be a non-empty list of "
            "non-empty strings"
        )
    if not isinstance(verification, str) or not verification.strip():
        raise PlanDocumentError(
            f"{field}.manual_prerequisite.verification must be non-empty text"
        )
    if trigger != MANUAL_PREREQUISITE_TRIGGER:
        raise PlanDocumentError(
            f"{field}.manual_prerequisite.trigger must be "
            f"{MANUAL_PREREQUISITE_TRIGGER!r}"
        )
    if not isinstance(unblocks, str) or not unblocks.strip():
        raise PlanDocumentError(
            f"{field}.manual_prerequisite.unblocks must be non-empty text"
        )
    return {
        "title": title.strip(),
        "instructions": [item.strip() for item in instructions],
        "verification": verification.strip(),
        "trigger": trigger,
        "unblocks": unblocks.strip(),
    }


def _required_text(raw: Mapping[str, Any], key: str, field: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlanDocumentError(f"{field}.{key} must be non-empty text")
    return value.strip()


def _string_list(
    raw: Mapping[str, Any], key: str, field: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    value = raw.get(key, [] if allow_empty else None)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PlanDocumentError(f"{field}.{key} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _repository_path_list(raw: Mapping[str, Any], field: str) -> tuple[str, ...]:
    paths = _string_list(raw, "allowed_paths", field)
    if (
        not paths
        or len(set(paths)) != len(paths)
        or any(Path(path).is_absolute() or ".." in Path(path).parts for path in paths)
    ):
        raise PlanDocumentError(
            f"{field}.allowed_paths must contain unique repository-relative paths"
        )
    return paths


def _has_dependency_cycle(items: Sequence[PlannedWorkItem]) -> bool:
    dependencies = {item.item_id: set(item.dependencies) for item in items}
    active: set[str] = set()
    done: set[str] = set()

    def visit(item_id: str) -> bool:
        if item_id in active:
            return True
        if item_id in done:
            return False
        active.add(item_id)
        if any(visit(dep) for dep in dependencies[item_id]):
            return True
        active.remove(item_id)
        done.add(item_id)
        return False

    return any(visit(item_id) for item_id in dependencies)


def _safe_id(value: str) -> bool:
    return bool(_SAFE_ID.fullmatch(value)) and ".." not in value.split("/")


__all__ = [
    "MANUAL_PREREQUISITE_TRIGGER",
    "PLAN_REPAIR_MARKER",
    "PlanDocumentError",
    "QUEUE_SCHEMA",
    "parse_manual_prerequisite",
    "parse_reviewed_plan_yaml",
    "render_plan_repair_metadata",
    "serialize_reviewed_plan_yaml",
]
