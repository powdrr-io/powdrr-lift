"""Structured action values proposed by workflow agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class WorkflowEdit:
    """One line-based mutation in the shared workflow action contract."""

    kind: str
    start_line: int
    end_line: int | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowFileEdits:
    """Line-based mutations for one file in a shared edit action."""

    file_path: str
    edits: tuple[WorkflowEdit, ...]


@dataclass(frozen=True, slots=True)
class WorkflowYamlOperation:
    """One structural mutation in a YAML workflow action."""

    operation: str
    section: str | None = None
    item_id: str | None = None
    item_index: int | None = None
    path: tuple[str, ...] = field(default_factory=tuple)
    value: Any = None


@dataclass(frozen=True, slots=True)
class WorkflowAction:
    """The action schema parsed for chat and durable workflow agents."""

    kind: str
    tool: str | None = None
    skill_name: str | None = None
    step_id: str | None = None
    file_path: str | None = None
    destination_path: str | None = None
    file_operation: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    directory: str | None = None
    pattern: str | None = None
    recursive: bool = False
    text: str | None = None
    output_state: Any = None
    outputs: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    edits: tuple[WorkflowEdit, ...] = field(default_factory=tuple)
    file_edits: tuple[WorkflowFileEdits, ...] = field(default_factory=tuple)
    yaml_operations: tuple[WorkflowYamlOperation, ...] = field(default_factory=tuple)
    types: tuple[str, ...] = field(default_factory=tuple)
    feature_id: str | None = None
    keywords: tuple[str, ...] = field(default_factory=tuple)
    filters: dict[str, object] = field(default_factory=dict)
    decisions_and_context: str | None = None
    llm_type: str | None = None
    provider_role: Literal["normal", "adversarial"] | None = None
    clean: bool = False
    context: tuple[str, ...] = field(default_factory=tuple)
    human_input: dict[str, Any] | None = None
