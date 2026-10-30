"""Service-owned plan workflow descriptor model and validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_service.planning.plan_document import (
    PlanDocumentError,
    parse_manual_prerequisite,
)

PLAN_WORKFLOW_SCHEMA = "https://powdrr.io/schemas/plan-workflow-instance-v2"
PLAN_WORKFLOW_PROCESS = "execute-proposed-pr-workflow"
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")


class PlanWorkflowInstanceError(ValueError):
    """A generated plan workflow descriptor is invalid."""


@dataclass(frozen=True, slots=True)
class PlanWorkflowInstance:
    """A compact reference to a Procedrr process and its task inputs."""

    workflow_id: str
    process_id: str
    dependencies: tuple[str, ...]
    inputs: dict[str, Any]


def parse_plan_workflow_instance_yaml(text: str) -> PlanWorkflowInstance:
    """Parse and validate a generated plan workflow descriptor from YAML."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise PlanWorkflowInstanceError(
            f"workflow definition is invalid YAML: {error}"
        ) from error
    if not isinstance(data, Mapping) or set(data) != {
        "schema",
        "workflow_id",
        "process_id",
        "dependencies",
        "inputs",
    }:
        raise PlanWorkflowInstanceError(
            "workflow definition has unknown or missing fields"
        )
    if data.get("schema") != PLAN_WORKFLOW_SCHEMA:
        raise PlanWorkflowInstanceError("workflow definition has an unsupported schema")
    workflow_id = data.get("workflow_id")
    process_id = data.get("process_id")
    raw_dependencies = data.get("dependencies")
    inputs = data.get("inputs")
    if not isinstance(workflow_id, str) or not _SAFE_ID.fullmatch(workflow_id):
        raise PlanWorkflowInstanceError("workflow_id must be a stable safe identifier")
    if process_id != PLAN_WORKFLOW_PROCESS:
        raise PlanWorkflowInstanceError(
            f"unsupported plan workflow process: {process_id!r}"
        )
    if (
        not isinstance(raw_dependencies, list)
        or not all(
            isinstance(dependency, str) and _SAFE_ID.fullmatch(dependency)
            for dependency in raw_dependencies
        )
        or len(set(raw_dependencies)) != len(raw_dependencies)
        or workflow_id in raw_dependencies
    ):
        raise PlanWorkflowInstanceError(
            "dependencies must be unique workflow IDs without self-dependencies"
        )
    if not isinstance(inputs, Mapping) or set(inputs) != {
        "work_item_name",
        "proposed_pr_id",
        "feature_description",
        "proposed_pr_context",
    }:
        raise PlanWorkflowInstanceError(
            "workflow process inputs have unknown or missing fields"
        )
    if inputs.get("proposed_pr_id") != workflow_id:
        raise PlanWorkflowInstanceError("proposed_pr_id must match workflow_id")
    if (
        not isinstance(inputs.get("work_item_name"), str)
        or not inputs["work_item_name"].strip()
    ):
        raise PlanWorkflowInstanceError("work_item_name must not be empty")
    if (
        not isinstance(inputs.get("feature_description"), str)
        or not inputs["feature_description"].strip()
    ):
        raise PlanWorkflowInstanceError("feature_description must not be empty")
    context = inputs.get("proposed_pr_context")
    if not isinstance(context, Mapping):
        raise PlanWorkflowInstanceError("proposed_pr_context must be an object")
    for field in ("plan_id", "item_id", "title", "objective", "verification_command"):
        value = context.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PlanWorkflowInstanceError(
                f"proposed_pr_context.{field} must not be empty"
            )
    for field in ("structrr_proposal_file", "structrr_item_id"):
        value = context.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PlanWorkflowInstanceError(
                f"proposed_pr_context.{field} must not be empty; execute-proposed-pr "
                "requires a reviewed Structrr diff"
            )
    if context.get("require_structrr_proposal") is not True:
        raise PlanWorkflowInstanceError(
            "proposed_pr_context.require_structrr_proposal must be true"
        )
    if "manual_prerequisite" in context:
        try:
            parse_manual_prerequisite(
                context["manual_prerequisite"], "proposed_pr_context"
            )
        except PlanDocumentError as error:
            raise PlanWorkflowInstanceError(str(error)) from error
    proposal_path = Path(str(context["structrr_proposal_file"]))
    if proposal_path.is_absolute() or ".." in proposal_path.parts:
        raise PlanWorkflowInstanceError(
            "proposed_pr_context.structrr_proposal_file must be repository-relative"
        )
    if context["structrr_item_id"] != context["item_id"]:
        raise PlanWorkflowInstanceError(
            "proposed_pr_context.structrr_item_id must match item_id"
        )
    for field in ("scope", "acceptance_criteria", "dependencies"):
        value = context.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise PlanWorkflowInstanceError(
                f"proposed_pr_context.{field} must be a string list"
            )
    if "allowed_paths" in context:
        value = context["allowed_paths"]
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(path, str) and path.strip() for path in value)
            or len(set(value)) != len(value)
            or any(
                Path(path).is_absolute() or ".." in Path(path).parts
                for path in value
                if isinstance(path, str)
            )
        ):
            raise PlanWorkflowInstanceError(
                "proposed_pr_context.allowed_paths must be unique "
                "repository-relative paths"
            )
    if "implementation_steps" in context:
        value = context["implementation_steps"]
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise PlanWorkflowInstanceError(
                "proposed_pr_context.implementation_steps must be a string list"
            )
    return PlanWorkflowInstance(
        workflow_id,
        process_id,
        tuple(raw_dependencies),
        dict(inputs),
    )


__all__ = [
    "PLAN_WORKFLOW_PROCESS",
    "PLAN_WORKFLOW_SCHEMA",
    "PlanWorkflowInstance",
    "PlanWorkflowInstanceError",
    "parse_plan_workflow_instance_yaml",
]
