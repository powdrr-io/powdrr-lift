"""Agent-backed prompt snapshot rendering for process definitions.

The process compiler validates definitions without loading the agent runtime.
This adapter is the deliberate integration point for rendering those
definitions with the production workflow prompt builder.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core.repo import resolve_repo_root
from powdrr_lift.process.catalog import SkillCatalogEntry
from powdrr_lift.process.model import load_skill
from powdrr_lift.workflow_chat_agent import _build_step_execution_messages


def render_skill_prompt_snapshots(
    definition_path: Path,
    *,
    output_dir: Path,
    repo_root: Path | None = None,
) -> tuple[Path, ...]:
    """Render normalized prompt contracts for every skill or template step."""
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
