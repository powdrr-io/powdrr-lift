from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from powdrr_lift.execution.runtime import ExecutionRuntime


@dataclass(frozen=True, slots=True)
class ProcessExecutionReference:
    """Definition identity and display steps for one Procedrr execution."""

    name: str
    path: Path
    steps: tuple[Mapping[str, Any], ...]


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
class _WorkflowExecutionState:
    """Mutable execution state shared by the workflow runtime and adapters.

    This state is deliberately independent of an LLM provider or chat UI. It
    is the durable boundary that lets action execution, validation, replay,
    and repair observe the same workflow facts.
    """

    selected_process: ProcessExecutionReference
    transcript: list[dict[str, str]]
    execution_events: list[dict[str, Any]]
    execution_context: list[str]
    step_index: int
    worktree_root: Path
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    root_process: ProcessExecutionReference | None = None
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
