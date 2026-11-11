"""Service-owned records for reviewed execution plans."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from powdrr_service.planning.story_allocation import StoryPlanRef


@dataclass(frozen=True, slots=True)
class PlannedWorkItem:
    item_id: str
    title: str
    objective: str
    scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    workflow_directory: str | None
    workflow_id: str
    workflow_file: str | None = None
    implementation_steps: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = (".",)
    manual_prerequisite: Mapping[str, Any] | None = None
    inline_adjustments: tuple[Mapping[str, Any], ...] = ()
    github_issue_number: int | None = None
    code_evidence: tuple[Mapping[str, Any], ...] = ()
    story_refs: tuple[StoryPlanRef, ...] = ()
    story_validation: bool = False


@dataclass(frozen=True, slots=True)
class ReviewedExecutionPlan:
    plan_id: str
    items: tuple[PlannedWorkItem, ...]
    source_document: str | None = None
    planning: Mapping[str, Any] | None = None


__all__ = ["PlannedWorkItem", "ReviewedExecutionPlan"]
