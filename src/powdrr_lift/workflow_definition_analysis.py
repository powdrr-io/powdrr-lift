"""Deterministic quality checks and prompt snapshots for workflow definitions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core import (
    build_skill_validation_report,
    load_skill,
    resolve_repo_root,
)
from powdrr_lift.core.workflow_template_specification import (
    build_workflow_template_validation_report,
)

_PLACEHOLDER = re.compile(r"<([A-Za-z0-9_-]+)>")
_ACTION_START = re.compile(r'\{\s*"action"\s*:')


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionIssue:
    code: str
    message: str
    path: str
    severity: str = "error"

    def to_data(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionReport:
    definition: Path
    kind: str
    issues: tuple[WorkflowDefinitionIssue, ...]

    @property
    def validation_successful(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_data(self) -> dict[str, object]:
        return {
            "definition": str(self.definition),
            "kind": self.kind,
            "validation_successful": self.validation_successful,
            "issues": [issue.to_data() for issue in self.issues],
        }


def analyze_workflow_definition(path: Path) -> WorkflowDefinitionReport:
    """Validate a skill or workflow template and flag deterministic confusion risks."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        return WorkflowDefinitionReport(
            definition=path,
            kind="unknown",
            issues=(
                WorkflowDefinitionIssue(
                    code="definition_parse_error",
                    message=str(exc),
                    path=str(path),
                ),
            ),
        )
    if not isinstance(data, Mapping):
        return WorkflowDefinitionReport(
            definition=path,
            kind="unknown",
            issues=(
                WorkflowDefinitionIssue(
                    code="definition_not_mapping",
                    message="Definition must decode to an object.",
                    path=str(path),
                ),
            ),
        )
    base_issues: Sequence[Any] | None
    if "steps" in data:
        kind = "skill"
        base_issues = build_skill_validation_report(raw, source_path=path).issues
    elif "task_templates" in data:
        kind = "workflow_template"
        base_issues = build_workflow_template_validation_report(
            json.dumps(data), source_path=path
        ).issues
    else:
        kind = "unknown"
        base_issues = None
    issues: list[WorkflowDefinitionIssue] = []
    if base_issues is None:
        issues.append(
            WorkflowDefinitionIssue(
                code="unsupported_definition_kind",
                message="Definition must contain steps or task_templates.",
                path=str(path),
            )
        )
    else:
        issues.extend(
            WorkflowDefinitionIssue(issue.code, issue.message, issue.path or str(path))
            for issue in base_issues
        )
    step_key = "steps" if kind == "skill" else "task_templates"
    steps = data.get(step_key)
    if isinstance(steps, Sequence) and not isinstance(steps, (str, bytes)):
        declared = _declared_placeholders(data, steps)
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            step_path = f"{path}.{step_key}[{index}]"
            issues.extend(_validate_step_examples(step, step_path))
            if declared:
                issues.extend(_validate_step_placeholders(step, step_path, declared))
    return WorkflowDefinitionReport(path, kind, tuple(issues))


def render_skill_prompt_snapshots(
    definition_path: Path,
    *,
    output_dir: Path,
    repo_root: Path | None = None,
) -> tuple[Path, ...]:
    """Render normalized prompt contracts for every skill or template step."""
    from powdrr_lift.workflow_chat_agent import (
        SkillCatalogEntry,
        _build_step_execution_messages,
    )

    root = resolve_repo_root(repo_root)
    raw = yaml.safe_load(definition_path.read_text(encoding="utf-8"))
    if isinstance(raw, Mapping) and isinstance(raw.get("task_templates"), list):
        return _render_template_prompt_snapshots(
            definition_path, raw, output_dir=output_dir, repo_root=root
        )
    skill = load_skill(definition_path)
    entry = SkillCatalogEntry(definition_path, skill)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, step in enumerate(skill.steps):
        messages = _build_step_execution_messages(
            selected_skill=entry,
            current_step=step,
            current_step_index=index,
            transcript=[{"role": "user", "content": "<root-intent>"}],
            execution_events=[],
            execution_context=[],
            handoff_records={},
            durable_facts={},
            current_file_path=None,
            worktree_root=root,
            catalog=(entry,),
        )
        snapshot = _normalize_snapshot(
            {
                "schema_version": 1,
                "definition": _portable_path(definition_path, root),
                "skill": skill.name,
                "step_index": index,
                "step_id": step.id,
                "messages": messages,
            },
            root,
        )
        name = f"{index + 1:03d}-{step.id or 'step'}.json"
        output_path = output_dir / name
        output_path.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        paths.append(output_path)
    return tuple(paths)


def _render_template_prompt_snapshots(
    definition_path: Path,
    template: Mapping[str, Any],
    *,
    output_dir: Path,
    repo_root: Path,
) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    tasks = template["task_templates"]
    assert isinstance(tasks, list)
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            continue
        snapshot = _normalize_snapshot(
            {
                "schema_version": 1,
                "definition": _portable_path(definition_path, repo_root),
                "workflow_template": template.get("id"),
                "task_index": index,
                "description": task.get("description"),
                "step_type": task.get("step_type"),
                "input_state": task.get("input_state", {}),
                "pre_step": task.get("pre_step"),
                "details": task.get("details"),
                "output_state_type": task.get("output_state_type"),
            },
            repo_root,
        )
        name = f"{index + 1:03d}-{_snapshot_name(task.get('description'))}.json"
        output_path = output_dir / name
        output_path.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        paths.append(output_path)
    return tuple(paths)


def _snapshot_name(value: Any) -> str:
    text = value if isinstance(value, str) else "task"
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "task"


def _validate_step_examples(
    step: Mapping[str, Any], step_path: str
) -> list[WorkflowDefinitionIssue]:
    details = step.get("details")
    if not isinstance(details, str):
        return []
    issues: list[WorkflowDefinitionIssue] = []
    decoder = json.JSONDecoder()
    for match in _ACTION_START.finditer(details):
        try:
            action_data, _ = decoder.raw_decode(details[match.start() :])
        except json.JSONDecodeError as exc:
            issues.append(
                WorkflowDefinitionIssue(
                    "invalid_action_example_json",
                    f"Action example is not valid JSON: {exc.msg}.",
                    f"{step_path}.details",
                )
            )
            continue
        if not isinstance(action_data, dict):
            continue
        try:
            from powdrr_lift.workflow_chat_agent import _parse_action_response

            _parse_action_response(action_data)
        except RuntimeError as exc:
            issues.append(
                WorkflowDefinitionIssue(
                    "invalid_action_example",
                    f"Action example does not match the runtime action schema: {exc}",
                    f"{step_path}.details",
                )
            )
    return issues


def _declared_placeholders(
    definition: Mapping[str, Any], steps: Sequence[Any]
) -> set[str]:
    declared: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        for item in step.get("inputs", []):
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                declared.add(item["name"].replace("_", "-"))
                declared.add(item["name"])
            elif isinstance(item, str):
                declared.update(_PLACEHOLDER.findall(item))
        input_state = step.get("input_state")
        if isinstance(input_state, Mapping):
            for name, value in input_state.items():
                if isinstance(name, str):
                    declared.add(name)
                    declared.add(name.replace("_", "-"))
                if isinstance(value, str):
                    declared.update(_PLACEHOLDER.findall(value))
    for item in definition.get("inputs", []):
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            declared.add(item["name"])
            declared.add(item["name"].replace("_", "-"))
        elif isinstance(item, str):
            declared.update(_PLACEHOLDER.findall(item))
    return declared


def _validate_step_placeholders(
    step: Mapping[str, Any], step_path: str, declared: set[str]
) -> list[WorkflowDefinitionIssue]:
    issues: list[WorkflowDefinitionIssue] = []
    for field, text in _walk_strings(step):
        for name in _PLACEHOLDER.findall(text):
            if name in declared or re.fullmatch(r"upstream-task-\d+", name):
                continue
            issues.append(
                WorkflowDefinitionIssue(
                    "unbound_placeholder",
                    f"Placeholder <{name}> is not declared by this definition "
                    "input contract.",
                    f"{step_path}.{field}",
                )
            )
    return issues


def _walk_strings(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(prefix or "value", value)]
    if isinstance(value, Mapping):
        return [
            item
            for key, child in value.items()
            for item in _walk_strings(child, f"{prefix}.{key}" if prefix else str(key))
        ]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            item
            for index, child in enumerate(value)
            for item in _walk_strings(child, f"{prefix}[{index}]")
        ]
    return []


def _normalize_snapshot(value: Any, repo_root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(repo_root.resolve()), "<repo-root>")
    if isinstance(value, Mapping):
        return {
            key: _normalize_snapshot(item, repo_root) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_snapshot(item, repo_root) for item in value]
    return value


def _portable_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)
