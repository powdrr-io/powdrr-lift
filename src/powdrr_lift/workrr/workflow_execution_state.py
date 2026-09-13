from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.process.catalog import SkillCatalogEntry


@dataclass(slots=True)
class _ValidationObligation:
    obligation_id: str
    expected_action: Mapping[str, Any]
    source: Mapping[str, Any]
    epoch: int = 1
    status: str = "pending"
    attempts: int = 0
    last_result: Mapping[str, Any] | None = None
    last_issue_fingerprint: tuple[str, ...] | None = None
    issue_history: set[tuple[str, ...]] = field(default_factory=set)
    semantic_stalls: int = 0

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "expected_action": dict(self.expected_action),
            "source": dict(self.source),
            "epoch": self.epoch,
            "status": self.status,
            "attempts": self.attempts,
            "last_result": self.last_result,
            "last_issue_fingerprint": list(self.last_issue_fingerprint or ()),
            "semantic_stalls": self.semantic_stalls,
        }


@dataclass(slots=True)
class _ValidationGateState:
    step_index: int
    discovered: bool = False
    epoch: int = 0
    obligations: dict[str, _ValidationObligation] = field(default_factory=dict)
    correction_required: bool = False
    discovery_action: Mapping[str, Any] | None = None


@dataclass(slots=True)
class _WorkflowStepCheckpoint:
    identity: tuple[str, int]
    transcript: list[dict[str, str]]
    execution_events: list[dict[str, Any]]
    execution_context: list[str]
    handoff_records: dict[str, dict[str, Any]]
    durable_facts: dict[str, dict[str, Any]]
    current_file_path: Path | None
    validation_gates: dict[str, _ValidationGateState]
    worktree_files: dict[str, tuple[bool, bytes | None, int | None]]


@dataclass(slots=True)
class _WorkflowExecutionState:
    """Mutable execution state shared by the workflow runtime and adapters.

    This state is deliberately independent of an LLM provider or chat UI. It
    is the durable boundary that lets action execution, validation, replay,
    and repair observe the same workflow facts.
    """

    selected_skill: SkillCatalogEntry
    transcript: list[dict[str, str]]
    execution_events: list[dict[str, Any]]
    execution_context: list[str]
    step_index: int
    worktree_root: Path
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    root_skill: SkillCatalogEntry | None = None
    error_log_root: Path = Path(".")
    handoff_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    durable_facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_file_path: Path | None = None
    current_file_context_cache: dict[tuple[str, int, int], dict[str, Any]] = field(
        default_factory=dict
    )
    fuzzy_match_cache: dict[tuple[str, int, int | None], tuple[Path, ...]] = field(
        default_factory=dict
    )
    validation_gates: dict[str, _ValidationGateState] = field(default_factory=dict)
    step_checkpoint: _WorkflowStepCheckpoint | None = None
    stalled_step_context: list[dict[str, Any]] = field(default_factory=list)
    file_added_callback: Callable[[tuple[str, ...]], None] | None = None
    runtime: ExecutionRuntime | None = None


def _ensure_execution_runtime(state: _WorkflowExecutionState) -> ExecutionRuntime:
    """Give direct strategy helpers the same durable boundary as normal runs."""
    if state.runtime is None:
        state.runtime = ExecutionRuntime(
            "chat-helper-"
            + hashlib.sha256(str(state.worktree_root).encode()).hexdigest()[:24],
            profile_id="default",
            workflow_directory=state.worktree_root.parent / ".powdrr-execution",
            repo_root=state.worktree_root,
        )
    return state.runtime


def _git_changed_paths(worktree_root: Path) -> set[str]:
    if not (worktree_root / ".git").exists():
        return set()
    paths: set[str] = set()
    for command in (
        ["git", "diff", "--name-only", "-z", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    ):
        result = subprocess.run(
            command,
            cwd=worktree_root,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        output = result.stdout
        parts = output.split(b"\0") if isinstance(output, bytes) else output.split("\0")
        paths.update(
            os.fsdecode(path) if isinstance(path, bytes) else path
            for path in parts
            if path
        )
    return paths


def _is_git_tracked(worktree_root: Path, relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative_path],
        cwd=worktree_root,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _snapshot_worktree_files(
    worktree_root: Path,
) -> dict[str, tuple[bool, bytes | None, int | None]]:
    snapshot: dict[str, tuple[bool, bytes | None, int | None]] = {}
    for relative_path in _git_changed_paths(worktree_root):
        path = worktree_root / relative_path
        if path.is_file():
            stat = path.stat()
            snapshot[relative_path] = (
                _is_git_tracked(worktree_root, relative_path),
                path.read_bytes(),
                stat.st_mode & 0o777,
            )
        else:
            snapshot[relative_path] = (
                _is_git_tracked(worktree_root, relative_path),
                None,
                None,
            )
    return snapshot


def _remove_worktree_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _restore_step_worktree(
    worktree_root: Path,
    baseline: Mapping[str, tuple[bool, bytes | None, int | None]],
) -> None:
    current_paths = _git_changed_paths(worktree_root)
    for relative_path in sorted(current_paths | set(baseline)):
        path = worktree_root / relative_path
        snapshot = baseline.get(relative_path)
        if snapshot is None:
            if _is_git_tracked(worktree_root, relative_path):
                subprocess.run(
                    [
                        "git",
                        "restore",
                        "--source=HEAD",
                        "--staged",
                        "--worktree",
                        "--",
                        relative_path,
                    ],
                    cwd=worktree_root,
                    check=False,
                )
            else:
                _remove_worktree_path(path)
            continue

        tracked, content, mode = snapshot
        if tracked:
            subprocess.run(
                [
                    "git",
                    "restore",
                    "--source=HEAD",
                    "--staged",
                    "--worktree",
                    "--",
                    relative_path,
                ],
                cwd=worktree_root,
                check=False,
            )
        if content is None:
            _remove_worktree_path(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mode is not None:
            path.chmod(mode)


def _begin_step_checkpoint(
    state: _WorkflowExecutionState,
    *,
    skill: SkillCatalogEntry,
    step_index: int,
) -> None:
    state.step_checkpoint = _WorkflowStepCheckpoint(
        identity=(str(skill.path), step_index),
        transcript=list(state.transcript),
        execution_events=list(state.execution_events),
        execution_context=list(state.execution_context),
        handoff_records={
            name: dict(record) for name, record in state.handoff_records.items()
        },
        durable_facts={
            name: dict(record) for name, record in state.durable_facts.items()
        },
        current_file_path=state.current_file_path,
        validation_gates={
            gate_id: _ValidationGateState(
                step_index=gate_state.step_index,
                discovered=gate_state.discovered,
                epoch=gate_state.epoch,
                obligations={
                    obligation_id: _ValidationObligation(
                        obligation_id=obligation.obligation_id,
                        expected_action=dict(obligation.expected_action),
                        source=dict(obligation.source),
                        status=obligation.status,
                        epoch=obligation.epoch,
                        attempts=obligation.attempts,
                        last_result=obligation.last_result,
                    )
                    for obligation_id, obligation in gate_state.obligations.items()
                },
                correction_required=gate_state.correction_required,
                discovery_action=gate_state.discovery_action,
            )
            for gate_id, gate_state in state.validation_gates.items()
        },
        worktree_files=_snapshot_worktree_files(state.worktree_root),
    )


def _restore_step_checkpoint(state: _WorkflowExecutionState) -> None:
    checkpoint = state.step_checkpoint
    if checkpoint is None:
        return
    _restore_step_worktree(state.worktree_root, checkpoint.worktree_files)
    state.transcript = list(checkpoint.transcript)
    state.execution_events = list(checkpoint.execution_events)
    state.execution_context = list(checkpoint.execution_context)
    state.handoff_records = {
        name: dict(record) for name, record in checkpoint.handoff_records.items()
    }
    state.durable_facts = {
        name: dict(record) for name, record in checkpoint.durable_facts.items()
    }
    state.current_file_path = checkpoint.current_file_path
    state.validation_gates = checkpoint.validation_gates
    state.current_file_context_cache.clear()
    state.fuzzy_match_cache.clear()


def _record_durable_fact(
    state: _WorkflowExecutionState,
    value: str,
    *,
    kind: str,
    source: str,
) -> None:
    """Deduplicate durable decisions and corrections while retaining history."""
    normalized = " ".join(value.split())
    if not normalized:
        return
    key = f"{kind}:{normalized}"
    state.durable_facts.setdefault(
        key,
        {
            "value": normalized,
            "kind": kind,
            "source": source,
            "step_index": state.step_index,
        },
    )
    if normalized not in {" ".join(item.split()) for item in state.execution_context}:
        state.execution_context.append(normalized)
