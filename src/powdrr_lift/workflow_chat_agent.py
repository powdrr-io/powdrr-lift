from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, TextIO, cast

import laga
import yaml

try:
    import readline
    import termios
    import tty
except ImportError:  # pragma: no cover - only used on non-POSIX platforms
    readline = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from powdrr_lift.agent.exchanges import (
    ExchangeRecordingClient,
    normalize_cache_usage,
)
from powdrr_lift.agent.provider_config import (
    DEFAULT_MODEL,
    ZAI_LLM_MAPPINGS,
    LLMModelLimits,
    LLMModelMapping,
    LLMProviderRole,
    LLMProviderRoles,
    provider_definition,
    provider_supports_llm_mappings,
)
from powdrr_lift.agent.providers import (
    LOCAL_MODEL_PATTERN,
    LocalModelRuntimeError,
    ProviderCredentials,
    _EmptyProviderResponseError,
    _estimate_message_tokens,
    _ModelUnavailableError,
    _SemanticRepairExhaustedError,
    auto_provider_candidates,
    available_provider_names,
    backup_model_for,
    build_provider_client,
    initial_model_for_provider,
    long_context_backup_for,
    provider_model_limits,
    resolve_llm_mapping,
    resolve_local_model_path,
    resolve_provider_credentials,
    resolve_provider_roles,
)
from powdrr_lift.basedpyright_tools import (
    BASEDPYRIGHT_STRUCTURE_TOOL,
    BASEDPYRIGHT_SYMBOL_TOOL,
    is_basedpyright_tool,
)
from powdrr_lift.builtin_tool_help import (
    BUILTIN_TOOL_NAMES,
    builtin_tool_help,
)
from powdrr_lift.core import (
    Skill,
    SkillToolInvocation,
    architecture_specification_default_output_path,
    build_skill_directory_validation_report,
    codebase_state_default_output_path,
    current_state_specification_default_output_path,
    feature_pr_specification_default_output_path,
    implementation_specification_default_output_path,
    load_skills,
    pr_specification_default_output_path,
    resolve_repo_root,
    system_map_specification_default_output_path,
    system_specification_default_output_path,
)
from powdrr_lift.core.delivery_profile import PhaseType, load_delivery_profile
from powdrr_lift.core.execution_state import ExecutionArtifact
from powdrr_lift.core.pr_specification import (
    build_authoritative_effect_handoff,
    compile_split_pr_specification,
)
from powdrr_lift.core.python_tool_commands import (
    dependency_backed_command_variants,
    missing_executable_output,
)
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    normalize_context_type,
    render_gather_context_report,
)
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)
from powdrr_lift.execution.builtin_tools import (
    invoke_basedpyright_capability,
    invoke_deferred_edit_capability,
    invoke_file_mutation,
    invoke_fuzzy_match_capability,
    invoke_intrinsic_capability,
    invoke_repository_read,
    invoke_shell_capability,
)
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.file_management import manage_worktree_file
from powdrr_lift.fuzzy_match import fuzzy_match_json
from powdrr_lift.intrinsic_edit import (
    APPLY_EDIT_TOOL,
    VALIDATE_EDIT_TOOL,
)
from powdrr_lift.intrinsic_enrich import ENRICH_TOOL
from powdrr_lift.intrinsic_git_gh import (
    GH_TOOL,
    GIT_TOOL,
    execute_intrinsic_git_gh_tool,  # noqa: F401 - compatibility monkeypatch seam
    intrinsic_command,
)
from powdrr_lift.pr_workflow_record import (
    is_pull_request_create_command,
    pull_request_number,
    record_pull_request_workflow,
)
from powdrr_lift.workflow_error_logging import record_workflow_llm_error
from powdrr_lift.workflow_llm import (
    PowdrrExecutionError,
    ProgressDecision,
    RepairContext,
    RepairDirective,
    RepairExhaustionReport,
    RepairPromptManifest,
    RepairStage,
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionProgressStrategy,
    WorkflowActionRequest,
    WorkflowExecutionStrategy,
    WorkflowLLMClient,
    WorkflowLLMExecutionAborted,
    WorkflowStepRunner,
    assert_material_repair_prompt,
    build_clean_room_action_parameters_prompt,
    build_clean_room_action_selection_prompt,
    build_clean_room_repair_prompt,
    build_repair_prompt_manifest,
    complete_two_pass_action,
    constrain_action_response_schema,
    prompt_size_breakdown,
    prune_execution_events,
    workflow_action_failure_signature,
    workflow_action_summary,
)
from powdrr_lift.workflow_llm import (
    WorkflowAction as SkillChatAction,
)
from powdrr_lift.workflow_llm import (
    WorkflowEdit as SkillChatEdit,
)
from powdrr_lift.workflow_llm import (
    WorkflowFileEdits as SkillChatFileEdits,
)
from powdrr_lift.workflow_llm import (
    WorkflowYamlOperation as SkillChatYamlOperation,
)
from powdrr_lift.workflow_llm import (
    complete_json as _request_json,
)
from powdrr_lift.workflow_llm import (
    workflow_action_signature as _shared_workflow_action_signature,
)
from powdrr_lift.workflow_observer import (
    ObserverActionRecommendation,
    ObserverDecision,
    ObserverExecutionContext,
    ShadowWorkflowObserver,
    compact_observer_mapping,
    observer_action_matches,
)
from powdrr_lift.workflow_replay import (
    WORKFLOW_REPLAY_PROMPT_BUILDER_VERSION,
    build_workflow_replay_state,
    definition_content_sha256,
)
from powdrr_lift.workflow_step_behavior import behavior_for_step

_WORKFLOW_FILE_ADDED_EVENT_PREFIX = "[powdrr-file-added] "

_MAX_EMPTY_QUESTION_REPROMPTS = 3
_LOCAL_MODEL_REPOSITORY = "Qwen/Qwen2.5-Coder-14B-Instruct-GGUF"
_DEFAULT_LOCAL_MODEL_CONTEXT = 24576
_LOCAL_MODEL_CONTEXT_ENV = "POWDRR_LOCAL_MODEL_CONTEXT"
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 3
_CONTEXT_SAFETY_MARGIN_TOKENS = 1024
_MAX_DOCUMENT_CONTEXT_LINES = 2000
_ENABLE_LLM_EXCHANGE_LOGGING = False
_MAX_PROMPT_TRANSCRIPT_ENTRIES = 12
_MAX_PROMPT_TRANSCRIPT_CHARS = 12000
_MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS = 8000
_MAX_PROMPT_FILE_LINES = 200
_MAX_PROMPT_FILE_CHARS = 16000
_MAX_PROMPT_STEP_CONTEXT_ENTRIES = 24
_MAX_PROMPT_STEP_CONTEXT_CHARS = 16000
_MAX_STREAM_CHUNKS = 4096
_MAX_STREAM_CONTENT_CHARS = 131072
_MAX_REPEATED_REPAIR_ATTEMPTS = 5
_WORKFLOW_CONTEXT_PATH = Path(".powdrr") / "workflow-context.json"
_INTERNAL_TOOL = "internal"
_INTERNAL_BINARY = "powdrr-lift"


WorkflowActionParser = Callable[
    [dict[str, Any], str | None, str | None], "SkillChatAction"
]


_INTERACTION_STYLE_GUIDANCE: dict[str, str] = {
    "engineering": (
        "Be concise and implementation-oriented. State assumptions, choose the "
        "smallest scoped change, preserve existing contracts, and verify changes "
        "with relevant tests or commands."
    ),
    "observational_review": (
        "Inspect evidence before making recommendations. Separate observations, "
        "inferences, risks, and recommendations. Cite concrete files, lines, tests, "
        "or command results. Do not edit unless the current step authorizes edits."
    ),
    "devils_advocate": (
        "Intentionally challenge the proposed change. Look for hidden assumptions, "
        "regressions, missing requirements, and simpler alternatives. Treat the "
        "change as untrusted until evidence supports it. Label counterarguments as "
        "risks or objections, and do not edit unless the current step authorizes it."
    ),
}


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    path: Path
    skill: Skill


@dataclass(frozen=True, slots=True)
class SkillChatConfig:
    skills_dir: Path
    repo_root: Path | None = None
    output_dir: Path | None = None
    provider: str = "auto"
    normal_provider: str | None = None
    adversarial_provider: str | None = None
    model: str = DEFAULT_MODEL
    llm_mappings: tuple[tuple[str, LLMModelMapping], ...] = ()
    api_key: str | None = None
    base_url: str | None = None
    max_turns: int = 8
    max_stalled_roundtrips: int = 3
    provider_retry_attempts: int = 3
    provider_retry_delay_seconds: float = 30.0
    verbose: bool = False
    execution_id: str | None = None

    @property
    def templates_dir(self) -> Path:
        return self.skills_dir


@dataclass(frozen=True, slots=True)
class SkillChatResult:
    selected_skill_path: Path
    summary_path: Path


@dataclass(frozen=True, slots=True)
class SkillChatSelection:
    selected_skill_path: Path
    selected_skill_reason: str
    next_question: str | None = None
    ready_to_execute: bool = False
    llm_type: str | None = None

    @property
    def selected_template_path(self) -> Path:
        return self.selected_skill_path

    @property
    def selected_template_reason(self) -> str:
        return self.selected_skill_reason

    @property
    def ready_to_generate(self) -> bool:
        return self.ready_to_execute


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    worktree_root: Path
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    skill_name: str | None = None
    request: str | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "worktree_root": str(self.worktree_root),
            "branch_name": self.branch_name,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "skill_name": self.skill_name,
            "request": self.request,
        }


WorkflowTemplateCatalogEntry = SkillCatalogEntry
WorkflowChatConfig = SkillChatConfig
WorkflowChatResult = SkillChatResult
WorkflowChatSelection = SkillChatSelection


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


@dataclass(slots=True)
class _ChatActionProgressStrategy(WorkflowActionProgressStrategy[SkillChatAction]):
    """Keep chat-only transcript and status context outside the shared engine."""

    state: _WorkflowExecutionState

    def material_state(self, action: SkillChatAction) -> object:
        return _workflow_action_material_state(action, self.state)

    def record_no_progress(
        self,
        action: SkillChatAction,
        observation: WorkflowActionObservation,
    ) -> None:
        _ = action
        assert observation.correction is not None
        self.state.transcript.append(
            {"role": "user", "content": observation.correction}
        )
        _record_durable_fact(
            self.state,
            observation.correction,
            kind="correction",
            source="no_progress",
        )


@dataclass(slots=True)
class _ChatWorkflowExecutionStrategy(WorkflowExecutionStrategy):
    """Interactive adapter around the shared action-roundtrip driver."""

    config: WorkflowChatConfig
    selection: SkillChatSelection
    catalog: tuple[SkillCatalogEntry, ...]
    workflow_context: WorkflowContext | None
    state: _WorkflowExecutionState
    progress: _WorkflowProgressDisplay
    input_func: Callable[[], str]
    stdout: TextIO
    stderr: TextIO
    client_for_model: Callable[[str, str], WorkflowLLMClient]
    provider_roles: LLMProviderRoles
    provider_role: LLMProviderRole
    current_model: str
    provider: str
    driver: WorkflowStepRunner
    skill_stack: list[_SkillExecutionFrame] = field(default_factory=list)
    completed_dependencies: set[tuple[str, int, str]] = field(default_factory=set)
    last_failed_action: SkillChatAction | None = None
    last_validation_error: str | None = None
    failure_kind: str = "validation_error"
    current_step: Any = None
    current_step_index: int = 0
    inherited_interaction_style: str | None = None
    observer_intervention: str | None = None
    observer_allowed_action: ObserverActionRecommendation | None = None
    observer_rejected_action_signature: str | None = None
    clean_room_repair_pending: bool = False
    clean_room_repair_used: bool = False
    repair_prompt_manifest: RepairPromptManifest | None = None
    terminalized: bool = False
    terminal_exit_code: int | None = None

    @property
    def selected_skill(self) -> SkillCatalogEntry:
        return self.state.selected_skill

    def _parent_progress(self) -> tuple[SkillCatalogEntry | None, int | None]:
        if not self.skill_stack:
            return None, None
        frame = self.skill_stack[-1]
        return frame.parent_skill, frame.parent_step_index

    def _restore_parent(self) -> None:
        frame = self.skill_stack.pop()
        child_handoff_records = {
            name: dict(record) for name, record in self.state.handoff_records.items()
        }
        if frame.dependency_key is not None:
            self.completed_dependencies.add(frame.dependency_key)
        self.state.selected_skill = frame.parent_skill
        self.state.step_index = frame.resume_step_index
        if frame.clean_context:
            self.state.transcript = list(frame.parent_transcript or ())
            self.state.execution_events = list(frame.parent_execution_events or ())
            self.state.execution_context = list(frame.parent_execution_context or ())
            self.state.current_file_path = frame.parent_current_file_path
            if frame.parent_handoff_records is None:
                self.state.handoff_records = {}
            else:
                self.state.handoff_records = {
                    name: dict(record) for name, record in frame.parent_handoff_records
                }
            self.state.durable_facts = {
                name: dict(record)
                for name, record in (frame.parent_durable_facts or ())
            }
            for child_name, parent_name, schema in frame.output_bindings:
                record = child_handoff_records.get(child_name)
                if record is None or "value" not in record:
                    raise PowdrrExecutionError(
                        f"Nested skill did not produce required output {child_name!r}."
                    )
                if schema is not None:
                    schema_error = _json_schema_error(
                        record["value"], schema, path=f"output {child_name}"
                    )
                    if schema_error is not None:
                        raise PowdrrExecutionError(schema_error)
                mapped = dict(record)
                mapped["name"] = parent_name
                mapped["produced_by"] = {
                    "step_index": frame.parent_step_index,
                    "action": "uses_skill",
                    "skill": frame.child_skill_name,
                    "output": child_name,
                }
                self.state.handoff_records[parent_name] = mapped
        self.current_model = frame.parent_model
        self.provider = frame.parent_provider
        self.provider_role = frame.parent_provider_role
        self.inherited_interaction_style = frame.parent_interaction_style

    def _push_skill(
        self,
        nested_skill: SkillCatalogEntry,
        *,
        resume_step_index: int,
        dependency_key: tuple[str, int, str] | None = None,
        clean_context: bool = False,
        initial_handoff_records: Mapping[str, Mapping[str, Any]] | None = None,
        output_bindings: tuple[tuple[str, str, Mapping[str, Any] | None], ...] = (),
    ) -> None:
        parent_interaction_style = _effective_interaction_style(
            self.selected_skill,
            self.current_step,
            self.inherited_interaction_style,
        )
        _push_nested_skill(
            self.skill_stack,
            current_skill=self.selected_skill,
            nested_skill=nested_skill,
            parent_step_index=self.current_step_index,
            resume_step_index=resume_step_index,
            dependency_key=dependency_key,
            parent_model=self.current_model,
            parent_provider=self.provider,
            parent_provider_role=self.provider_role,
            parent_interaction_style=parent_interaction_style,
            clean_context=clean_context,
            parent_transcript=tuple(self.state.transcript) if clean_context else None,
            parent_execution_events=(
                tuple(self.state.execution_events) if clean_context else None
            ),
            parent_execution_context=(
                tuple(self.state.execution_context) if clean_context else None
            ),
            parent_current_file_path=(
                self.state.current_file_path if clean_context else None
            ),
            parent_handoff_records=(
                tuple(self.state.handoff_records.items()) if clean_context else None
            ),
            parent_durable_facts=(
                tuple(self.state.durable_facts.items()) if clean_context else None
            ),
            output_bindings=output_bindings,
        )
        self.state.selected_skill = nested_skill
        self.state.step_index = 0
        if initial_handoff_records is not None:
            self.state.handoff_records = {
                name: dict(record) for name, record in initial_handoff_records.items()
            }
        self.inherited_interaction_style = parent_interaction_style

    def next_request(self) -> WorkflowActionRequest | None:
        if self.terminalized:
            return None
        while True:
            if self.state.step_index >= len(self.selected_skill.skill.steps):
                if not self.skill_stack:
                    return None
                self._restore_parent()
                continue
            self.provider = self.provider_roles.provider_for(self.provider_role)
            self.current_model = (
                provider_definition(self.provider).forced_model or self.current_model
            )
            self.current_step_index = self.state.step_index
            self.current_step = self.selected_skill.skill.steps[self.current_step_index]
            step_behavior = behavior_for_step(self.current_step)
            if self.state.runtime is not None:
                self.state.runtime.install_step_scope(
                    step_behavior.runtime_actions(self.current_step.actions),
                    enforce_empty=getattr(self.current_step, "actions_declared", False),
                )
                if self.observer_allowed_action is not None:
                    self.state.runtime.allow_observer_action(
                        asdict(self.observer_allowed_action)
                    )
            deterministic_uses_skill = getattr(self.current_step, "uses_skill", None)
            dependency_name = (
                deterministic_uses_skill.skill
                if deterministic_uses_skill is not None
                else None
            )
            if dependency_name is not None:
                nested_skill = _find_skill_by_name(self.catalog, dependency_name)
                nested_role: LLMProviderRole = (
                    self.provider_role
                    if nested_skill.skill.adversarial is None
                    else ("adversarial" if nested_skill.skill.adversarial else "normal")
                )
                initial_handoff_records: dict[str, dict[str, Any]] | None = None
                output_bindings: tuple[
                    tuple[str, str, Mapping[str, Any] | None], ...
                ] = ()
                clean_context = False
                if deterministic_uses_skill is not None:
                    clean_context = bool(
                        deterministic_uses_skill.inputs
                        or deterministic_uses_skill.outputs
                    )
                    initial_handoff_records = {} if clean_context else None
                    for binding in deterministic_uses_skill.inputs:
                        source = self.state.handoff_records.get(binding.ref)
                        if source is None or "value" not in source:
                            raise PowdrrExecutionError(
                                f"uses_skill input {binding.ref!r} is not available."
                            )
                        if binding.schema is not None:
                            schema_error = _json_schema_error(
                                source["value"],
                                binding.schema,
                                path=f"input {binding.name}",
                            )
                            if schema_error is not None:
                                raise PowdrrExecutionError(schema_error)
                        if initial_handoff_records is None:
                            raise PowdrrExecutionError(
                                "uses_skill inputs require an isolated handoff context."
                            )
                        initial_handoff_records[binding.name] = {
                            **dict(source),
                            "name": binding.name,
                            "produced_by": {
                                "step_index": self.current_step_index,
                                "action": "uses_skill",
                                "input": binding.ref,
                            },
                        }
                    output_bindings = tuple(
                        (binding.name, binding.ref, binding.schema)
                        for binding in deterministic_uses_skill.outputs
                    )
                self._push_skill(
                    nested_skill,
                    resume_step_index=self.current_step_index + 1,
                    dependency_key=(
                        str(self.selected_skill.path),
                        self.current_step_index,
                        dependency_name,
                    ),
                    clean_context=clean_context,
                    initial_handoff_records=initial_handoff_records,
                    output_bindings=output_bindings,
                )
                self.state.audit_events.append(
                    {
                        "kind": "invoke_skill",
                        "skill": dependency_name,
                        "step_index": self.current_step_index,
                        "source": (
                            "uses_skill"
                            if deterministic_uses_skill is not None
                            else "uses_skill"
                        ),
                    }
                )
                self.provider_role = nested_role
                continue
            checkpoint_identity = (
                str(self.selected_skill.path),
                self.current_step_index,
            )
            if (
                self.state.step_checkpoint is None
                or self.state.step_checkpoint.identity != checkpoint_identity
            ):
                self.state.stalled_step_context = []
                self.clean_room_repair_used = False
                self.repair_prompt_manifest = None
                _begin_step_checkpoint(
                    self.state,
                    skill=self.selected_skill,
                    step_index=self.current_step_index,
                )
            if step_behavior.runs_gate:
                if self.state.runtime is not None:
                    self.state.runtime.install_step_scope(frozenset())
                try:
                    passed = _run_gate(
                        self.current_step,
                        skill_name=self.selected_skill.skill.name,
                        worktree_root=self.state.worktree_root,
                        execution_events=self.state.execution_events,
                        execution_context=self.state.execution_context,
                        handoff_records=self.state.handoff_records,
                        step_index=self.state.step_index,
                        workflow_context=self.workflow_context,
                        stdout=self.stdout,
                        stderr=self.stderr,
                        verbose=self.config.verbose,
                        runtime=self.state.runtime,
                    )
                finally:
                    if self.state.runtime is not None:
                        self.state.runtime.install_step_scope(
                            step_behavior.runtime_actions(self.current_step.actions),
                            enforce_empty=self.current_step.actions_declared,
                        )
                if passed:
                    success_step_id = self.current_step.gate.success_goto_step
                    if success_step_id is None:
                        self.state.step_index += 1
                    else:
                        target_index = _step_index_by_id(
                            self.selected_skill, success_step_id
                        )
                        self.state.step_index = target_index
                        self.state.execution_events.append(
                            {
                                "kind": "goto_step",
                                "step_id": success_step_id,
                                "target_step_index": target_index,
                                "source": "gate_success",
                                "step_index": self.current_step_index,
                            }
                        )
                else:
                    target_index = _step_index_by_id(
                        self.selected_skill, self.current_step.gate.goto_step
                    )
                    _invalidate_deterministic_pre_step(
                        self.state.execution_events,
                        skill_name=self.selected_skill.skill.name,
                        step_index=target_index,
                    )
                    self.state.step_index = target_index
                    self.state.execution_events.append(
                        {
                            "kind": "goto_step",
                            "step_id": self.current_step.gate.goto_step,
                            "target_step_index": target_index,
                            "source": "gate",
                            "step_index": self.current_step_index,
                        }
                    )
                continue
            if self.current_step.pre_step is not None:
                # A deterministic pre-step is engine-owned work declared by
                # the step, not a model-proposed action. Temporarily remove
                # the model action contract while executing it, then restore
                # the contract before constructing the LLM prompt.
                if self.state.runtime is not None:
                    self.state.runtime.install_step_scope(frozenset())
                try:
                    _run_deterministic_pre_step(
                        self.current_step,
                        skill_name=self.selected_skill.skill.name,
                        worktree_root=self.state.worktree_root,
                        execution_events=self.state.execution_events,
                        execution_context=self.state.execution_context,
                        handoff_records=self.state.handoff_records,
                        step_index=self.state.step_index,
                        workflow_context=self.workflow_context,
                        stdout=self.stdout,
                        stderr=self.stderr,
                        verbose=self.config.verbose,
                        runtime=self.state.runtime,
                    )
                finally:
                    if self.state.runtime is not None:
                        self.state.runtime.install_step_scope(
                            step_behavior.runtime_actions(self.current_step.actions),
                            enforce_empty=self.current_step.actions_declared,
                        )
                pre_step_event = _latest_deterministic_pre_step(
                    self.state.execution_events,
                    skill_name=self.selected_skill.skill.name,
                    step_index=self.state.step_index,
                )
                pre_step_template = (
                    pre_step_event.get("template")
                    if pre_step_event is not None
                    else None
                )
                generated_file_path = _resolve_generated_file_path_from_command(
                    pre_step_template.get("command")
                    if isinstance(pre_step_template, Mapping)
                    else None,
                    worktree_root=self.state.worktree_root,
                )
                if generated_file_path is not None:
                    self.state.current_file_path = generated_file_path
                if step_behavior.auto_advance_after_pre_step(
                    completion_satisfied=_predicated_step_complete(
                        self.current_step, self.state
                    )
                ):
                    _advance_predicated_step(self.state, self.current_step)
                    continue
                if not step_behavior.invokes_llm:
                    self.state.step_index += 1
                    continue
            step_mapping = (
                resolve_llm_mapping(
                    self.current_step.llm_type or self.selection.llm_type,
                    mappings=_active_llm_mappings(
                        self.config, self.provider_roles, self.provider_role
                    ),
                    provider=self.provider,
                )
                if provider_supports_llm_mappings(self.provider)
                else None
            )
            if step_mapping is None:
                step_mapping = LLMModelMapping(
                    self.current_model, provider=self.provider
                )
            self.current_model = step_mapping.model
            self.provider = _resolve_provider(
                self.config.provider,
                self.current_model,
                mapping=step_mapping,
            )
            parent_skill, parent_step_index = self._parent_progress()
            self.progress.update(
                self.selected_skill,
                current_step_index=self.current_step_index,
                status=f"waiting for {self.current_model} LLM response...",
                parent_skill=parent_skill,
                parent_step_index=parent_step_index,
            )
            response_schema = _step_action_response_schema(
                self.current_step,
                execution_events=self.state.execution_events,
                step_index=self.state.step_index,
            )
            response_parser = partial(
                _parse_action_response_with_schema, schema=response_schema
            )
            request_action = None
            if self.clean_room_repair_pending:
                if self.clean_room_repair_used:
                    self.clean_room_repair_pending = False
                    repair_attempts = tuple(
                        event.get("prompt_manifest", {})
                        for event in self.state.execution_events
                        if event.get("kind") == "repair_attempt"
                        and event.get("step_index") == self.current_step_index
                    )
                    exhaustion_report = RepairExhaustionReport(
                        boundary_id=(
                            f"{self.selected_skill.path}:{self.current_step_index}"
                        ),
                        objective=self.current_step.description,
                        final_state={
                            "step_index": self.current_step_index,
                            "current_file": (
                                str(self.state.current_file_path)
                                if self.state.current_file_path is not None
                                else None
                            ),
                        },
                        failures=tuple(self.state.stalled_step_context),
                        prompt_manifests=repair_attempts,
                        rejected_strategies=tuple(self.state.stalled_step_context),
                        allowed_actions=tuple(
                            _declared_action_names(self.current_step)
                        ),
                        reason=(
                            "The clean-room repair budget for this step was consumed."
                        ),
                    )
                    self.state.execution_events.append(
                        {
                            "kind": "repair_exhausted",
                            "stage": "clean_room",
                            "step_index": self.current_step_index,
                            "reason": (
                                "The clean-room repair budget for this step was "
                                "already consumed."
                            ),
                            "report": exhaustion_report.to_data(),
                        }
                    )
                    raise PowdrrExecutionError(
                        "Workflow repair exhausted after one clean-room attempt.",
                        error_code="semantic_repair_exhausted",
                        remediation=(
                            "Review rejected strategies and provide a deterministic "
                            "action or revise the step contract."
                        ),
                    )
                self.clean_room_repair_pending = False
                self.clean_room_repair_used = True
                recovery_context = {
                    "objective": self.current_step.description,
                    "step": _current_step_contract(
                        self.current_step,
                        execution_events=self.state.execution_events,
                        step_index=self.state.step_index,
                    ),
                    "current_file": (
                        str(self.state.current_file_path)
                        if self.state.current_file_path is not None
                        else None
                    ),
                    "rejected_strategies": self.state.stalled_step_context,
                    "allowed_actions": list(_declared_action_names(self.current_step)),
                    "repair_context": self.repair_context().to_data(),
                }
                context_json = json.dumps(
                    recovery_context, ensure_ascii=False, separators=(",", ":")
                )
                selection_messages, selection_schema, manifest = (
                    build_clean_room_action_selection_prompt(
                        context=context_json,
                        error_message=(
                            self.last_validation_error
                            or "The previous strategy made no material progress."
                        ),
                        allowed_actions=_declared_action_names(self.current_step),
                        model=self.current_model,
                    )
                )
                parameter_context = context_json
                parameter_error = (
                    self.last_validation_error
                    or "The previous strategy made no material progress."
                )

                def parameter_messages_for(
                    selected_action: str,
                    context: str = parameter_context,
                    error: str = parameter_error,
                    schema: Mapping[str, Any] = response_schema,
                    model: str = self.current_model,
                ) -> list[dict[str, str]]:
                    parameter_messages, _parameter_manifest = (
                        build_clean_room_action_parameters_prompt(
                            context=context,
                            error_message=error,
                            selected_action=selected_action,
                            response_schema=constrain_action_response_schema(
                                schema, selected_action
                            ),
                            model=model,
                        )
                    )
                    return parameter_messages

                def parameter_schema_for(
                    selected_action: str, schema: Mapping[str, Any] = response_schema
                ) -> Mapping[str, Any]:
                    return constrain_action_response_schema(schema, selected_action)

                messages = selection_messages
                # The manifest records the first, deliberately constrained pass;
                # the parameter pass is recorded when the request is executed.
                if self.repair_prompt_manifest is None:
                    raise RuntimeError(
                        "Clean-room repair requested without a prior prompt manifest."
                    )
                assert_material_repair_prompt(self.repair_prompt_manifest, manifest)
                self.state.execution_events.append(
                    {
                        "kind": "repair_attempt",
                        "stage": "clean_room_action_selection",
                        "step_index": self.current_step_index,
                        "prompt_manifest": manifest.to_data(),
                    }
                )
                self.repair_prompt_manifest = manifest
                request_action = partial(
                    self._request_two_pass_action,
                    selection_messages,
                    selection_schema=selection_schema,
                    parameter_messages_for=parameter_messages_for,
                    parameter_schema_for=parameter_schema_for,
                    parser=response_parser,
                    allowed_actions=_declared_action_names(self.current_step),
                    fallback_mapping=step_mapping.backup_model,
                )
            else:
                messages = _build_step_execution_messages(
                    selected_skill=self.selected_skill,
                    current_step=self.current_step,
                    current_step_index=self.current_step_index,
                    transcript=self.state.transcript,
                    execution_events=self.state.execution_events,
                    execution_context=self.state.execution_context,
                    handoff_records=self.state.handoff_records,
                    durable_facts=self.state.durable_facts,
                    current_file_path=self.state.current_file_path,
                    worktree_root=self.state.worktree_root,
                    catalog=self.catalog,
                    workflow_context=self.workflow_context,
                    current_file_context_cache=self.state.current_file_context_cache,
                    validation_gate=_validation_gate_prompt_data(self.state),
                    stalled_step_context=self.state.stalled_step_context,
                    inherited_interaction_style=self.inherited_interaction_style,
                    observer_intervention=self.observer_intervention,
                    runtime_prompt_context=(
                        self.driver.runtime.prompt_context()
                        if self.driver.runtime is not None
                        else None
                    ),
                    failed_action=self.last_failed_action,
                    failure_reason=self.last_validation_error,
                )
                self.repair_prompt_manifest = build_repair_prompt_manifest(
                    messages,
                    profile="normal_full_context",
                    source_sections=(
                        "step",
                        "transcript",
                        "execution_events",
                        "execution_context",
                        "handoffs",
                        "durable_facts",
                        "current_file",
                        "validation_gate",
                    ),
                    history_policy="full",
                    allowed_actions=_declared_action_names(self.current_step),
                    response_schema=response_schema,
                    reasoning_mode="direct_action",
                    model=self.current_model,
                )
                request_action = partial(
                    self._request_action,
                    messages,
                    response_schema=response_schema,
                    parser=response_parser,
                )
            return WorkflowActionRequest(
                client=self.client_for_model(self.current_model, self.provider),
                messages=messages,
                parser=response_parser,
                model=self.current_model,
                stderr=self.stderr,
                max_timeout_retries=0,
                timeout_backoff_seconds=0,
                response_schema=response_schema,
                request_action=request_action,
            )

    def _request_two_pass_action(
        self,
        selection_messages: list[dict[str, str]],
        *,
        selection_schema: Mapping[str, Any],
        parameter_messages_for: Callable[[str], list[dict[str, str]]],
        parameter_schema_for: Callable[[str], Mapping[str, Any]],
        parser: Callable[[dict[str, Any]], SkillChatAction],
        allowed_actions: Sequence[str],
        fallback_mapping: LLMModelMapping | None,
    ) -> SkillChatAction:
        fallback_client = (
            self.client_for_model(fallback_mapping.model, fallback_mapping.provider)
            if fallback_mapping is not None
            else None
        )
        return complete_two_pass_action(
            self.client_for_model(self.current_model, self.provider),
            selection_messages=selection_messages,
            selection_schema=selection_schema,
            parameter_messages_for=parameter_messages_for,
            parameter_schema_for=parameter_schema_for,
            parser=parser,
            allowed_actions=allowed_actions,
            model=self.current_model,
            stderr=self.stderr,
            max_timeout_retries=0,
            timeout_backoff_seconds=0,
            fallback_client=fallback_client,
            fallback_model=(
                fallback_mapping.model if fallback_mapping is not None else None
            ),
        )

    def _request_action(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
        parser: Callable[[dict[str, Any]], SkillChatAction] | None = None,
    ) -> SkillChatAction:
        action, self.current_model, self.provider = _complete_json_with_model_fallback(
            client_for=self.client_for_model,
            messages=messages,
            parser=parser or _parse_action_response,
            context=(
                f"workflow execution for step {self.current_step_index + 1}/"
                f"{len(self.selected_skill.skill.steps)}"
            ),
            model=self.current_model,
            repair_instructions=_action_repair_prompt(
                self.selected_skill,
                current_step=self.current_step,
                execution_events=self.state.execution_events,
                step_index=self.state.step_index,
                failed_action=self.last_failed_action,
                validation_error=self.last_validation_error,
            ),
            config=self.config,
            input_func=self.input_func,
            stdout=self.stdout,
            stderr=self.stderr,
            provider=self.provider,
            error_recorder=self._record_repair_error,
            model_mappings=tuple(ZAI_LLM_MAPPINGS.items())
            + tuple(
                _active_llm_mappings(
                    self.config, self.provider_roles, self.provider_role
                )
            ),
            empty_response_fallback_payload={"action": "next_step"},
            response_schema=response_schema,
        )
        if action is None:
            raise WorkflowLLMExecutionAborted(1)
        return action

    def material_state(self, action: SkillChatAction) -> object:
        return _workflow_action_material_state(action, self.state)

    def _llm_error_context(self) -> dict[str, Any]:
        step = self.current_step
        current_step_events = [
            event
            for event in self.state.execution_events
            if event.get("step_index") == self.current_step_index
        ]
        recent_events = prune_execution_events(
            current_step_events[-6:],
            include_results=False,
        )
        last_successful_action = next(
            (
                event
                for event in reversed(recent_events)
                if event.get("kind")
                not in {"validation_error", "action_error", "deterministic_pre_step"}
            ),
            None,
        )
        context: dict[str, Any] = {
            "skill": {
                "name": self.selected_skill.skill.name,
                "path": str(self.selected_skill.path),
                "content_sha256": definition_content_sha256(self.selected_skill.path),
                "step_index": self.current_step_index,
                "step_id": getattr(step, "id", None),
                "description": getattr(step, "description", None),
                "details": getattr(step, "details", None),
            },
            "worktree_root": str(self.state.worktree_root),
            "provider": self.provider,
            "model": self.current_model,
            "current_step_contract": _current_step_contract(
                step,
                execution_events=self.state.execution_events,
                step_index=self.state.step_index,
            ),
            "recent_current_step_events": recent_events,
            "last_successful_action": last_successful_action,
            "stalled_step_context": list(self.state.stalled_step_context),
            "current_step_error_count": sum(
                event.get("kind") in {"validation_error", "action_error"}
                for event in current_step_events
            ),
            "prompt_builder_version": WORKFLOW_REPLAY_PROMPT_BUILDER_VERSION,
            "replay_state": build_workflow_replay_state(
                transcript=self.state.transcript,
                execution_events=self.state.execution_events,
                execution_context=self.state.execution_context,
                handoff_records=self.state.handoff_records,
                durable_facts=self.state.durable_facts,
                current_file_path=self.state.current_file_path,
                worktree_root=self.state.worktree_root,
                validation_gate=_validation_gate_prompt_data(self.state),
                stalled_step_context=self.state.stalled_step_context,
            ),
        }
        if self.workflow_context is not None:
            context["workflow"] = self.workflow_context.to_data()
        validation_gate = _validation_gate_prompt_data(self.state)
        if validation_gate is not None:
            context["validation_gate"] = validation_gate
        return context

    def _record_repair_error(
        self,
        error: RuntimeError,
        payload: dict[str, Any] | None,
    ) -> None:
        """Record failures handled inside the automatic response-repair loop."""
        record_workflow_llm_error(
            self.state.error_log_root,
            execution_mode="execute_selected_skill",
            phase="llm_output_repair",
            error=error,
            context=self._llm_error_context(),
            llm_output=payload,
            guidance=_action_repair_prompt(
                self.selected_skill,
                current_step=self.current_step,
                execution_events=self.state.execution_events,
                step_index=self.state.step_index,
                failed_action=self.last_failed_action,
                validation_error=str(error),
            ),
        )

    def report_roundtrip(self, roundtrip: int, action: SkillChatAction) -> None:
        if self.config.verbose:
            print(
                f"Workflow chat LLM action:\n{_workflow_action_signature(action)}",
                file=self.stderr,
                flush=True,
            )
        parent_skill, parent_step_index = self._parent_progress()
        self.progress.update(
            self.selected_skill,
            current_step_index=self.current_step_index,
            status=(f"roundtrip {roundtrip}: {workflow_action_summary(action)}"),
            parent_skill=parent_skill,
            parent_step_index=parent_step_index,
        )

    def record_no_progress(
        self,
        action: SkillChatAction,
        observation: WorkflowActionObservation,
    ) -> None:
        _ChatActionProgressStrategy(self.state).record_no_progress(action, observation)

    def record_repair_directive(self, directive: RepairDirective) -> None:
        """Persist the shared runner's stage decision at the chat boundary."""
        self.state.execution_events.append(
            {
                "kind": "repair_attempt",
                "stage": directive.stage.value,
                "attempt": directive.attempt,
                "reason": directive.reason,
                "allowed_actions": list(directive.allowed_actions),
                "prompt_profile": directive.prompt_profile,
                "model_policy": directive.model_policy,
                "failure_class": directive.failure_class.value,
                "error_code": directive.error_code,
                "target_signature": directive.target_signature,
                "material_state_fingerprint": directive.material_state_fingerprint,
                "rejected_strategy_signatures": list(
                    directive.rejected_strategy_signatures
                ),
                "repair_context": self.repair_context().to_data(),
                "step_index": self.current_step_index,
            }
        )
        if self.terminalized:
            return
        if directive.stage in {RepairStage.HUMAN_HANDOFF, RepairStage.EXHAUSTED}:
            report = RepairExhaustionReport(
                boundary_id=(
                    f"skill:{self.selected_skill.path}:{self.current_step_index}"
                ),
                objective=str(
                    getattr(self.current_step, "description", None)
                    or getattr(self.current_step, "id", "current step")
                ),
                final_state={
                    "step_index": self.current_step_index,
                    "current_file_path": self.state.current_file_path,
                },
                failures=tuple(
                    event
                    for event in self.state.execution_events
                    if event.get("kind")
                    in {"validation_error", "action_error", "tool_error", "no_progress"}
                ),
                prompt_manifests=tuple(
                    event.get("prompt_manifest", {})
                    for event in self.state.execution_events
                    if event.get("kind") == "repair_attempt"
                ),
                rejected_strategies=(),
                allowed_actions=tuple(
                    self.state.runtime.allowed_actions() or ()
                    if self.state.runtime is not None
                    else ()
                ),
                reason=directive.reason,
            )
            self.state.execution_events.append(
                {
                    "kind": "repair_exhausted",
                    "stage": directive.stage.value,
                    "report": report.to_data(),
                }
            )
            print(
                "Workflow stopped: semantic repair needs human review or was "
                "exhausted. See the repair_exhausted event for the report.",
                file=self.stderr,
            )
            self.terminalized = True
            self.terminal_exit_code = 1

    def repair_context(self) -> RepairContext:
        material_state = {
            "skill": self.selected_skill.path,
            "step_index": self.current_step_index,
            "current_file_path": self.state.current_file_path,
            "handoff_records": self.state.handoff_records,
            "durable_facts": self.state.durable_facts,
        }
        fingerprint = hashlib.sha256(
            json.dumps(material_state, sort_keys=True, default=str).encode()
        ).hexdigest()
        rejected = tuple(
            signature
            for event in self.state.execution_events
            if event.get("kind") == "repair_attempt"
            for signature in (
                [event["target_signature"]] if event.get("target_signature") else []
            )
        )
        return RepairContext(
            execution_id=str(self.state.selected_skill.path),
            boundary_id=f"{self.selected_skill.path}:{self.current_step_index}",
            objective=str(
                getattr(self.current_step, "description", None)
                or getattr(self.current_step, "id", "current step")
            ),
            deterministic_state=material_state,
            allowed_actions=tuple(self.state.runtime.allowed_actions() or ())
            if self.state.runtime is not None
            else (),
            rejected_strategies=rejected,
            material_state_fingerprint=fingerprint,
        )

    def execute_action(self, action: SkillChatAction) -> WorkflowActionOutcome:
        try:
            return self._execute_action(action)
        except PowdrrExecutionError:
            raise
        except (RuntimeError, ValueError) as exc:
            raise PowdrrExecutionError(str(exc), cause_error=exc) from exc

    def _execute_action(self, action: SkillChatAction) -> WorkflowActionOutcome:
        step_behavior = behavior_for_step(self.current_step)
        action_signature = _workflow_action_signature(action)
        observer_action_authorized = observer_action_matches(
            action, self.observer_allowed_action
        )
        if action_signature == self.observer_rejected_action_signature:
            raise PowdrrExecutionError(
                "The observer rejected this exact action after it failed to "
                "make progress. Choose a materially different action."
            )
        action_failure_signature = workflow_action_failure_signature(
            action,
            signature=_workflow_action_signature,
        )
        if any(
            record.get("action_signature") == action_failure_signature
            for record in self.state.stalled_step_context
        ):
            raise PowdrrExecutionError(
                "This action was already identified as stalled for the current "
                "step. Choose a materially different action; changing only "
                "decisions_and_context is not sufficient."
            )
        if action.llm_type is not None and provider_supports_llm_mappings(
            self.provider
        ):
            mapping = resolve_llm_mapping(
                action.llm_type,
                mappings=_active_llm_mappings(
                    self.config, self.provider_roles, self.provider_role
                ),
                provider=self.provider,
            )
            assert mapping is not None
            self.current_model = mapping.model
            self.provider = mapping.provider
        if action.kind == "invoke_skill":
            if action.skill_name is None:
                raise PowdrrExecutionError(
                    "invoke_skill action must include a skill name."
                )
            nested_skill = _find_skill_by_name(self.catalog, action.skill_name)
            context = list(action.context)
            if action.decisions_and_context is not None:
                context.append(action.decisions_and_context)
            nested_role: LLMProviderRole = (
                (
                    self.provider_role
                    if nested_skill.skill.adversarial is None
                    else ("adversarial" if nested_skill.skill.adversarial else "normal")
                )
                if action.provider_role is None
                else action.provider_role
            )
            self._push_skill(
                nested_skill,
                resume_step_index=(
                    self.current_step_index
                    if observer_action_authorized
                    else self.current_step_index + 1
                ),
                clean_context=action.clean,
            )
            self.state.execution_events.append(
                {
                    "kind": action.kind,
                    "skill": action.skill_name,
                    "step_index": self.current_step_index,
                }
            )
            self.state.audit_events.append(
                {
                    "kind": action.kind,
                    "skill": action.skill_name,
                    "step_index": self.current_step_index,
                    "clean": action.clean,
                }
            )
            if action.clean:
                self.state.transcript = []
                self.state.execution_events = []
                self.state.execution_context = context
                self.state.handoff_records = {}
                self.state.durable_facts = {}
                self.state.current_file_path = None
            else:
                for context_value in context:
                    _record_durable_fact(
                        self.state,
                        context_value,
                        kind="decision",
                        source="invoke_skill",
                    )
            self.provider_role = nested_role
            return WorkflowActionOutcome()
        if action.kind == "goto_step":
            target_index = _step_index_by_id(self.selected_skill, action.step_id)
            _reset_split_pr_handoffs_for_repair(
                self.state,
                skill_name=self.selected_skill.skill.name,
                target_step_id=action.step_id,
            )
            self.state.step_index = target_index
            if action.decisions_and_context:
                self.state.execution_context.append(action.decisions_and_context)
            self.state.execution_events.append(
                {
                    "kind": action.kind,
                    "step_id": action.step_id,
                    "target_step_index": target_index,
                    "decisions_and_context": action.decisions_and_context,
                    "step_index": self.current_step_index,
                }
            )
            return WorkflowActionOutcome()
        if action.kind == "complete" and self.skill_stack:
            self._restore_parent()
            return WorkflowActionOutcome()
        status = _workflow_action_progress_status(action)
        if status is not None:
            parent_skill, parent_step_index = self._parent_progress()
            self.progress.update(
                self.selected_skill,
                current_step_index=self.state.step_index,
                status=status,
                parent_skill=parent_skill,
                parent_step_index=parent_step_index,
            )
        self.failure_kind = "validation_error"
        _validate_coding_loop_action(
            self.current_step,
            self.state.execution_events,
            action_kind=action.kind,
            step_index=self.state.step_index,
            worktree_root=self.state.worktree_root,
        )
        _validate_workflow_step_transition(
            action,
            self.current_step,
            self.state.execution_events,
            self.state.step_index,
            state=self.state,
        )
        if _validation_gate_enabled(self.current_step):
            _validate_dynamic_validation_gate_action(
                action,
                self.state,
                self.current_step,
            )
        else:
            _validate_workflow_action_for_step(
                action,
                self.current_step,
                execution_events=self.state.execution_events,
                step_index=self.state.step_index,
                observer_allowed_action=self.observer_allowed_action,
            )
        _validate_workflow_action_outputs(action, self.current_step)
        if observer_action_authorized:
            if self.driver.runtime is not None:
                self.driver.runtime.consume_observer_action(action)
            self.observer_allowed_action = None
            self.observer_rejected_action_signature = None
            self.observer_intervention = None
        _materialize_split_pr_specification(action, self.state, self.current_step)
        _record_workflow_action_outputs(action, self.state, self.current_step)
        _record_runtime_readiness_artifact(action, self.state, self.current_step)
        if action.kind == "next_step":
            next_step = (
                self.selected_skill.skill.steps[self.state.step_index + 1]
                if self.state.step_index + 1 < len(self.selected_skill.skill.steps)
                else None
            )
            _validate_workflow_handoff(
                self.current_step,
                next_step,
                self.state.handoff_records,
                current_step_index=self.state.step_index,
            )
        self.failure_kind = "action_error"
        handler = _workflow_action_handlers().get(action.kind)
        if handler is None:
            raise PowdrrExecutionError(
                f"Unsupported workflow action kind: {action.kind!r}"
            )
        should_continue = handler(
            action,
            self.state,
            self.stdout,
            self.stderr,
            self.input_func,
            self.config,
        )
        _register_validation_gate_discovery(action, self.state)
        _record_dynamic_validation_result(action, self.state)
        verification = (
            _run_coding_loop_verification(
                self.current_step,
                worktree_root=self.state.worktree_root,
                stdout=self.stdout,
                stderr=self.stderr,
                verbose=self.config.verbose,
                runtime=self.driver.runtime,
            )
            if action.kind in {"edit", "yaml_edit", "file_management", "delete_file"}
            else None
        )
        if verification is not None:
            self.state.execution_events.append(
                {
                    "kind": "coding_loop_verification",
                    "step_index": self.state.step_index,
                    **verification,
                }
            )
        _reset_validation_gate_after_correction(action, self.state)
        if step_behavior.auto_advance_after_action(
            completion_satisfied=_predicated_step_complete(
                self.current_step, self.state
            )
        ):
            _advance_predicated_step(self.state, self.current_step)
        self.last_failed_action = None
        self.last_validation_error = None
        return WorkflowActionOutcome(continue_running=should_continue)

    def record_response_error(
        self,
        error: RuntimeError,
        payload: dict[str, Any] | None,
    ) -> None:
        record_workflow_llm_error(
            self.state.error_log_root,
            execution_mode="execute_selected_skill",
            phase="llm_output_parse",
            error=error,
            context=self._llm_error_context(),
            llm_output=payload,
            guidance=_action_repair_prompt(
                self.selected_skill,
                current_step=self.current_step,
                execution_events=self.state.execution_events,
                step_index=self.state.step_index,
                failed_action=self.last_failed_action,
                validation_error=str(error),
            ),
        )
        raise error

    def record_action_error(self, action: SkillChatAction, error: Exception) -> None:
        underlying_error = _underlying_execution_error(error)
        if (
            _validation_gate_enabled(self.current_step)
            and action.kind == "invoke_tool"
            and not isinstance(underlying_error, _WorkflowToolValidationError)
        ):
            _validation_gate_state(
                self.state, self.current_step
            ).correction_required = True
        signature = _workflow_action_signature(action)
        feedback = _workflow_edit_failure_feedback(
            action,
            error,
            _current_file_context(
                self.state.worktree_root, self.state.current_file_path
            ),
        )
        print(feedback, file=self.stderr)
        _write_agent_error(
            self.state.worktree_root,
            feedback + _rejected_edit_guidance(action),
        )
        record_workflow_llm_error(
            self.state.error_log_root,
            execution_mode="execute_selected_skill",
            phase="action_validation_or_execution",
            error=error,
            context=self._llm_error_context(),
            attempted_action=json.loads(signature),
            guidance=feedback + _rejected_edit_guidance(action),
        )
        self.last_failed_action = action
        self.state.transcript.extend(
            [
                {"role": "assistant", "content": signature},
                {"role": "user", "content": feedback},
            ]
        )
        _record_durable_fact(
            self.state,
            feedback,
            kind="correction",
            source="action_error",
        )
        validator_data = (
            validation_error_to_data(underlying_error.validation_error)
            if isinstance(underlying_error, _WorkflowToolValidationError)
            else None
        )
        validation_result = {
            self.failure_kind: {
                "action": json.loads(signature),
                "message": str(error),
                "corrective_instructions": feedback,
                **({"validator": validator_data} if validator_data else {}),
            }
        }
        if self.failure_kind == "validation_error":
            self.state.transcript.append(
                {"role": "user", "content": json.dumps(validation_result)}
            )
        self.state.execution_events.append(
            {
                "kind": self.failure_kind,
                "action_kind": action.kind,
                "error": str(error),
                "result": validation_result,
                "step_index": self.state.step_index,
            }
        )
        self.last_validation_error = (
            validator_data["message"] if validator_data is not None else str(error)
        )

    def action_failure_exit_code(self, action: SkillChatAction) -> int:
        _ = action
        print("Workflow stopped after repeated action failures.", file=self.stderr)
        return 1

    def apply_observer_decision(
        self,
        decision: ObserverDecision,
        action: SkillChatAction,
        observation: WorkflowActionObservation | None,
    ) -> bool:
        """Apply Phase 2 coaching without bypassing deterministic skill rules."""
        if decision.verdict in {"continue", "request_human"}:
            return False
        guidance = [f"Reason: {decision.reason}", *decision.guidance]
        if decision.expected_progress:
            guidance.append(f"Evidence expected: {decision.expected_progress}")
        recommended_action = decision.target_action
        if recommended_action is None and decision.target_step_id:
            recommended_action = ObserverActionRecommendation(
                kind="goto_step", step_id=decision.target_step_id
            )
        if recommended_action is None and decision.target_skill_name:
            recommended_action = ObserverActionRecommendation(
                kind="invoke_skill", skill_name=decision.target_skill_name
            )
        if recommended_action:
            self.observer_allowed_action = recommended_action
            if self.driver.runtime is not None:
                self.driver.runtime.allow_observer_action(asdict(recommended_action))
            guidance.append(
                f"Observer recommends the {recommended_action.kind!r} action; "
                "choose it "
                "directly if it is the appropriate next action."
            )
        self.observer_intervention = "Observer intervention\n" + "\n".join(
            f"- {item}" for item in guidance
        )
        if decision.target_step_id:
            self.observer_intervention += (
                "\n- Follow this advice with goto_step "
                f"step_id={decision.target_step_id!r}."
            )
        if decision.target_skill_name:
            self.observer_intervention += (
                "\n- Follow this advice with invoke_skill "
                f"skill={decision.target_skill_name!r}; the current skill resumes "
                "afterward."
            )
        self.observer_rejected_action_signature = _workflow_action_signature(action)
        if decision.verdict == "redirect" and decision.target_step_id:
            try:
                _step_index_by_id(self.selected_skill, decision.target_step_id)
            except RuntimeError as error:
                self.observer_intervention += f"\n- Redirect ignored: {error}"
            else:
                target_index = _step_index_by_id(
                    self.selected_skill, decision.target_step_id
                )
                if target_index >= self.current_step_index:
                    self.observer_intervention += (
                        "\n- Redirect ignored: target step is not prior to the "
                        "current step."
                    )
        if decision.verdict == "redirect" and decision.target_skill_name:
            try:
                _find_skill_by_name(self.catalog, decision.target_skill_name)
            except RuntimeError as error:
                self.observer_intervention += f"\n- Skill redirect ignored: {error}"
        self.state.execution_events.append(
            {
                "kind": "observer_intervention",
                "verdict": decision.verdict,
                "reason": decision.reason,
                "target_action": (
                    asdict(recommended_action) if recommended_action else None
                ),
                "target_step_id": decision.target_step_id,
                "target_skill_name": decision.target_skill_name,
                "action": json.loads(_workflow_action_signature(action)),
                "material_progress": (
                    observation.made_progress if observation is not None else None
                ),
            }
        )
        if self.driver.runtime is not None:
            self.driver.runtime.record_observer_decision(
                verdict=decision.verdict,
                reason=decision.reason,
                action_kind=str(getattr(action, "kind", "unknown")),
                action_signature=_workflow_action_signature(action),
                material_progress=(
                    observation.made_progress if observation is not None else None
                ),
                target_action=(
                    json.dumps(asdict(recommended_action), sort_keys=True)
                    if recommended_action
                    else None
                ),
                target_step_id=decision.target_step_id,
                target_skill_name=decision.target_skill_name,
            )
        return observation is None

    def clear_observer_intervention(self) -> None:
        self.observer_intervention = None
        self.observer_allowed_action = None
        self.observer_rejected_action_signature = None

    def _retry_stalled_step(
        self,
        action: SkillChatAction,
        observation: WorkflowActionObservation,
    ) -> WorkflowActionOutcome:
        _restore_step_checkpoint(self.state)
        retry_number = len(self.state.stalled_step_context) + 1
        stall_record = {
            "retry_number": retry_number,
            "action_signature": workflow_action_failure_signature(
                action,
                signature=_workflow_action_signature,
            ),
            "stalled_action": json.loads(observation.signature),
            "reason": (
                observation.correction
                or "The action repeated without changing workflow state."
            ),
        }
        self.state.stalled_step_context.append(stall_record)
        self.clean_room_repair_pending = True
        self.state.execution_events.append(
            {
                "kind": "stalled_step_retry",
                "step_index": self.state.step_index,
                "retry_number": retry_number,
                "stalled_action": stall_record["stalled_action"],
                "reason": stall_record["reason"],
            }
        )
        self.last_failed_action = None
        self.last_validation_error = None
        self.driver.action_engine.reset_progress()
        parent_skill, parent_step_index = self._parent_progress()
        status = (
            f"Retrying step after stall (attempt {retry_number + 1}); "
            "previous step actions were discarded"
        )
        self.progress.update(
            self.selected_skill,
            current_step_index=self.state.step_index,
            status=status,
            parent_skill=parent_skill,
            parent_step_index=parent_step_index,
        )
        print(status, file=self.stderr)
        _ = action
        return WorkflowActionOutcome()

    def observe_outcome(
        self,
        action: SkillChatAction,
        observation: WorkflowActionObservation,
        outcome: WorkflowActionOutcome,
    ) -> WorkflowActionOutcome:
        if action.kind == "invoke_tool" and _validation_gate_enabled(self.current_step):
            gate_state = _validation_gate_state(self.state, self.current_step)
            actual = {
                "kind": action.kind,
                "tool": action.tool,
                "parameters": dict(action.parameters),
            }
            stalled_obligation = next(
                (
                    item
                    for item in gate_state.obligations.values()
                    if _validation_actions_match(item.expected_action, actual)
                    and item.semantic_stalls >= 2
                ),
                None,
            )
            if stalled_obligation is not None:
                warning = (
                    "Workflow stopped: validation obligation "
                    f"{stalled_obligation.obligation_id!r} repeated or worsened "
                    "the semantic validation state twice. The repair loop is "
                    "cycling; inspect the recorded issue fingerprints and choose "
                    "a different correction."
                )
                print(warning, file=self.stderr)
                _write_agent_error(self.state.worktree_root, warning)
                return WorkflowActionOutcome(exit_code=1)
        if not observation.made_progress:
            if observation.decision == ProgressDecision.THRESHOLD:
                return self._retry_stalled_step(action, observation)
        if self.state.step_index != self.current_step_index:
            self.driver.action_engine.reset_progress()
        if not outcome.continue_running:
            parent_skill, parent_step_index = self._parent_progress()
            self.progress.update(
                self.selected_skill,
                current_step_index=len(self.selected_skill.skill.steps),
                status=f"{self.selected_skill.skill.name} skill completed",
                parent_skill=parent_skill,
                parent_step_index=parent_step_index,
            )
        return outcome

    def exhausted_roundtrips_exit_code(self) -> int:
        return 2

    def coding_loop_exhausted(self, limit: int) -> int:
        message = (
            f"Coding loop for step {self.current_step.id!r} stopped after "
            f"{limit} model iterations without a completion transition."
        )
        self.state.execution_events.append(
            {
                "kind": "coding_loop_exhausted",
                "step_id": self.current_step.id,
                "max_iterations": limit,
                "message": message,
            }
        )
        print(message, file=self.stderr, flush=True)
        return 2


@dataclass(frozen=True, slots=True)
class _SkillExecutionFrame:
    parent_skill: SkillCatalogEntry
    parent_step_index: int
    resume_step_index: int
    dependency_key: tuple[str, int, str] | None = None
    parent_model: str = ""
    parent_provider: str = ""
    parent_provider_role: LLMProviderRole = "normal"
    parent_interaction_style: str | None = None
    clean_context: bool = False
    parent_transcript: tuple[dict[str, str], ...] | None = None
    parent_execution_events: tuple[dict[str, Any], ...] | None = None
    parent_execution_context: tuple[str, ...] | None = None
    parent_current_file_path: Path | None = None
    parent_handoff_records: tuple[tuple[str, dict[str, Any]], ...] | None = None
    parent_durable_facts: tuple[tuple[str, dict[str, Any]], ...] | None = None
    output_bindings: tuple[tuple[str, str, Mapping[str, Any] | None], ...] = ()
    child_skill_name: str = ""


def _maybe_record_llm_exchanges(
    client: WorkflowLLMClient,
    repo_root: Path,
) -> WorkflowLLMClient:
    """Apply exchange recording only while the hardcoded diagnostic flag is enabled."""
    if not _ENABLE_LLM_EXCHANGE_LOGGING:
        return client
    return ExchangeRecordingClient(
        client,
        repo_root,
        request_json=_request_json,
    )


# Compatibility names for existing scenario and unit-test seams.
_LLMExchangeRecordingClient = ExchangeRecordingClient
_normalize_cache_usage = normalize_cache_usage


class _WorkflowEditRangeError(PowdrrExecutionError):
    """Raised when a line-based edit falls outside the current file."""


class _WorkflowStructuredDocumentError(PowdrrExecutionError):
    """Raised when an edit produces invalid structured document text."""


class _WorkflowYamlEditError(PowdrrExecutionError):
    """Raised when a structural YAML edit cannot be applied safely."""


class _WorkflowToolValidationError(PowdrrExecutionError):
    def __init__(self, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(validation_error.message)


class _WorkflowProgressDisplay:
    def __init__(
        self,
        stream: TextIO,
        on_update: Callable[..., None] | None = None,
    ) -> None:
        self._stream = stream
        self._on_update = on_update
        self._dynamic = stream.isatty()
        self._rendered_line_count = 0
        self._last_step_index: int | None = None

    def update(
        self,
        skill: SkillCatalogEntry,
        *,
        current_step_index: int,
        status: str,
        parent_skill: SkillCatalogEntry | None = None,
        parent_step_index: int | None = None,
    ) -> None:
        if self._on_update is not None:
            if len(inspect.signature(self._on_update).parameters) >= 5:
                self._on_update(
                    skill,
                    current_step_index,
                    status,
                    parent_skill,
                    parent_step_index,
                )
            else:
                self._on_update(skill, current_step_index, status)
            self._last_step_index = current_step_index
            return
        if not self._dynamic and self._last_step_index == current_step_index:
            print(f"[workflow] {status}", file=self._stream, flush=True)
            return

        lines = ["Workflow progress:"]
        if parent_skill is not None and parent_step_index is not None:
            lines.append(
                f"  ▶ {parent_step_index + 1}. "
                f"{parent_skill.skill.steps[parent_step_index].description}"
            )
            lines.append("  -------")
        for step_index, step in enumerate(skill.skill.steps):
            if step_index < current_step_index:
                marker = "✓"
            elif step_index == current_step_index:
                marker = "▶"
            else:
                marker = "·"
            lines.append(f"  {marker} {step_index + 1}. {step.description}")
        lines.append(f"Status: {status}")

        if self._dynamic and self._rendered_line_count:
            self._stream.write(f"\033[{self._rendered_line_count}A")
        for line in lines:
            if self._dynamic:
                self._stream.write(f"\033[2K{line}\n")
            else:
                self._stream.write(f"{line}\n")
        self._stream.flush()
        self._rendered_line_count = len(lines)
        self._last_step_index = current_step_index


def run_workflow_chat(
    config: WorkflowChatConfig,
    *,
    input_func: Callable[[], str] = input,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    progress_callback: Callable[
        [SkillCatalogEntry, int, str, SkillCatalogEntry | None, int | None], None
    ]
    | None = None,
    file_added_callback: Callable[[tuple[str, ...]], None] | None = None,
) -> int:
    configured_repo_root = resolve_repo_root(config.repo_root)
    error_log_root = _resolve_project_root(configured_repo_root, configured_repo_root)
    project_root = configured_repo_root
    workflow_context = _load_workflow_context(project_root)
    skills_dir = config.skills_dir
    if not skills_dir.is_absolute():
        skills_dir = configured_repo_root / skills_dir

    catalog = _load_skill_catalog(skills_dir, stderr=stderr)
    if not catalog:
        print(f"No skills found in {skills_dir}.", file=stderr)
        return 1

    provider_roles = resolve_provider_roles(
        config.provider,
        normal_provider=config.normal_provider,
        adversarial_provider=config.adversarial_provider,
    )
    provider_role: LLMProviderRole = "normal"
    provider = provider_roles.provider_for(provider_role)
    current_model = initial_model_for_provider(provider, config.model)
    credentials = resolve_provider_credentials(
        provider, config.api_key, config.base_url
    )
    clients: dict[tuple[str, str], WorkflowLLMClient] = {}

    def client_for(
        selected_provider: str,
        selected_credentials: ProviderCredentials,
        selected_model: str,
    ) -> WorkflowLLMClient:
        key = (selected_provider, selected_model)
        if key not in clients:
            clients[key] = _maybe_record_llm_exchanges(
                _build_chat_client(
                    selected_credentials,
                    model=selected_model,
                    model_cache_dir=project_root / ".powdrr" / "models",
                    progress_stream=stderr,
                ),
                project_root,
            )
        return clients[key]

    def client_for_model(
        selected_model: str, selected_provider: str
    ) -> WorkflowLLMClient:
        selected_credentials = resolve_provider_credentials(
            selected_provider,
            config.api_key,
            config.base_url,
        )
        return client_for(selected_provider, selected_credentials, selected_model)

    print(
        f"Using {credentials.provider} credentials from {credentials.source} "
        f"with base URL from {credentials.base_url_source}: {credentials.base_url}",
        file=stderr,
    )
    if config.provider == "auto" and provider_roles.adversarial is None:
        print(
            "WARNING: only one LLM provider is configured; reviews might be "
            "limited because adversarial work will use the normal provider.",
            file=stderr,
        )
    _verbose_print(
        stderr,
        config.verbose,
        f"Loaded {len(catalog)} skill(s) from {skills_dir}",
    )
    _verbose_print(stderr, config.verbose, f"Selected provider: {provider}")
    _verbose_print(stderr, config.verbose, f"Selected model: {current_model}")

    user_request = _prompt_user(
        "What do you want to do? ",
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )
    transcript: list[dict[str, str]] = [{"role": "user", "content": user_request}]
    _verbose_print(stderr, config.verbose, f"Initial user request: {user_request}")
    selected_skill: SkillCatalogEntry | None = None
    selection: SkillChatSelection | None = None
    skill_announced = False

    def record_selection_error(
        error: RuntimeError,
        payload: dict[str, Any] | None,
    ) -> None:
        record_workflow_llm_error(
            error_log_root,
            execution_mode="select_skill",
            phase="llm_output_parse",
            error=error,
            context={
                "request": user_request,
                "skills_dir": str(skills_dir),
                "available_skills": [
                    {
                        "name": entry.skill.name,
                        "path": str(entry.path),
                    }
                    for entry in catalog
                ],
                "provider": provider,
                "model": current_model,
                "workflow": (
                    workflow_context.to_data() if workflow_context is not None else None
                ),
            },
            llm_output=payload,
            guidance=_selection_repair_prompt(catalog),
        )

    for _turn in range(config.max_turns):
        _verbose_print(stderr, config.verbose, f"Starting selection turn {_turn + 1}")
        selection, current_model, provider = _complete_json_with_model_fallback(
            client_for=client_for_model,
            messages=_build_selection_messages(
                catalog,
                transcript,
                configured_repo_root,
                workflow_context,
            ),
            parser=lambda payload: _parse_selection_response(payload, catalog),
            context="skill selection",
            model=current_model,
            repair_instructions=_selection_repair_prompt(catalog),
            config=config,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
            provider=provider,
            error_recorder=record_selection_error,
            model_mappings=_active_llm_mappings(config, provider_roles, provider_role),
        )
        if selection is None:
            return 1
        _verbose_print(
            stderr,
            config.verbose,
            (
                "Selection result: "
                f"skill={selection.selected_skill_path}, "
                f"ready_to_execute={selection.ready_to_execute}"
            ),
        )
        selected_skill = _find_catalog_entry(catalog, selection.selected_skill_path)
        selection_mapping = (
            resolve_llm_mapping(
                selection.llm_type,
                mappings=_active_llm_mappings(config, provider_roles, provider_role),
                provider=provider,
            )
            if provider_supports_llm_mappings(provider)
            else None
        )
        if selection_mapping is not None:
            current_model = selection_mapping.model
            provider = _resolve_provider(
                config.provider,
                current_model,
                mapping=selection_mapping,
            )
        credentials = resolve_provider_credentials(
            provider, config.api_key, config.base_url
        )
        if not skill_announced:
            print(f"Matched skill: {selected_skill.skill.name}", file=stdout)
            skill_announced = True
        if selection.ready_to_execute and selection.next_question is None:
            break

        if selection.next_question is None:
            break

        print(selection.next_question, file=stdout)
        answer = _prompt_user(
            "> ",
            input_func=input_func,
            stdout=stdout,
            status_stream=stderr,
        )
        _verbose_print(stderr, config.verbose, f"Follow-up answer: {answer}")
        transcript.append({"role": "assistant", "content": selection.next_question})
        transcript.append({"role": "user", "content": answer})
    else:
        print(
            "Reached the maximum number of skill chat turns without selecting a skill.",
            file=stderr,
        )
        return 1

    if selected_skill is None or selection is None:
        print("Could not select a skill.", file=stderr)
        return 1

    provider_role = (
        "adversarial" if selected_skill.skill.adversarial is True else "normal"
    )

    worktree_root = _resolve_worktree_for_request(
        configured_repo_root,
        request=user_request,
        selected_skill=selected_skill,
        context=workflow_context,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
        verbose=config.verbose,
    )
    repo_root = worktree_root
    project_root = _resolve_project_root(configured_repo_root, worktree_root)
    output_dir = config.output_dir
    if output_dir is not None and not output_dir.is_absolute():
        output_dir = repo_root / output_dir

    progress = _WorkflowProgressDisplay(stderr, on_update=progress_callback)
    root_skill = selected_skill
    execution_id = config.execution_id or (
        "chat-"
        + hashlib.sha256(
            f"{selection.selected_skill_path}:{user_request}".encode()
        ).hexdigest()[:24]
    )
    delivery_profile = (
        load_delivery_profile(
            repo_root / "delivery-profiles/default-software-delivery.yaml"
        )
        if (repo_root / "delivery-profiles/default-software-delivery.yaml").is_file()
        else None
    )
    runtime = ExecutionRuntime(
        execution_id,
        profile_id=(
            delivery_profile.profile_id
            if delivery_profile is not None
            else selected_skill.skill.name
        ),
        workflow_directory=project_root / ".powdrr",
        repo_root=repo_root,
        profile=delivery_profile,
    )
    runtime.capture_explicit_guidance(
        user_request,
        source_ref=f"{execution_id}:user-request",
    )
    if delivery_profile is not None:
        assignment = next(
            item
            for item in delivery_profile.phases
            if item.phase_type is PhaseType.INTAKE
        )
        available_actions = runtime.available_adapter_actions()
        runtime.persona_packet(
            delivery_profile,
            run_id=execution_id,
            phase_type=PhaseType.INTAKE,
            phase_actions=available_actions,
            persona_actions={assignment.persona_id: available_actions},
            allowed_effects=frozenset(),
        )
    execution_state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        root_skill=root_skill,
        transcript=transcript,
        execution_events=[],
        audit_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=worktree_root,
        error_log_root=project_root,
        handoff_records=_workflow_context_handoff_records(workflow_context),
        file_added_callback=file_added_callback,
        runtime=runtime,
    )
    driver = WorkflowStepRunner(
        max_stalled_roundtrips=config.max_stalled_roundtrips,
        runtime=runtime,
        actor_id="workflow-chat-agent",
    )
    execution_strategy = _ChatWorkflowExecutionStrategy(
        config=config,
        selection=selection,
        catalog=catalog,
        workflow_context=workflow_context,
        state=execution_state,
        progress=progress,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
        client_for_model=client_for_model,
        provider_roles=provider_roles,
        provider_role=provider_role,
        current_model=current_model,
        provider=provider,
        driver=driver,
    )
    observer_provider = provider_roles.provider_for(provider_role)
    observer_mapping = (
        resolve_llm_mapping(
            "high_reasoning",
            mappings=_active_llm_mappings(config, provider_roles, provider_role),
            provider=observer_provider,
        )
        if provider_supports_llm_mappings(observer_provider)
        else None
    )
    if observer_mapping is not None:

        def observer_context() -> ObserverExecutionContext:
            step = execution_strategy.current_step
            validation_state = _validation_gate_prompt_data(execution_state) or {}
            current_step_events = [
                event
                for event in execution_state.execution_events
                if event.get("step_index") == execution_strategy.current_step_index
            ]
            validation_state = {
                "gate": validation_state,
                "issue_count": sum(
                    event.get("kind") in {"validation_error", "action_error"}
                    for event in current_step_events
                ),
            }
            root_request = next(
                (
                    item["content"]
                    for item in execution_state.transcript
                    if item.get("role") == "user"
                ),
                user_request,
            )
            return ObserverExecutionContext(
                execution_mode="execute_selected_skill",
                root_intent=root_request,
                skill_or_workflow=execution_strategy.selected_skill.skill.name,
                current_step_id=str(
                    getattr(step, "id", None)
                    or f"step-{execution_strategy.current_step_index + 1}"
                ),
                current_step_intent=str(
                    getattr(step, "description", None)
                    or getattr(step, "details", None)
                    or "Execute the current skill step."
                ),
                skill_definition=execution_strategy.selected_skill.skill.to_data(),
                error_state={
                    "recent_errors": [
                        event
                        for event in execution_state.execution_events
                        if event.get("kind") in {"validation_error", "action_error"}
                    ][-8:],
                    "error_count": sum(
                        event.get("kind") in {"validation_error", "action_error"}
                        for event in execution_state.execution_events
                    ),
                },
                validation_state=validation_state,
                handoff_state=compact_observer_mapping(execution_state.handoff_records),
            )

        observer_credentials = resolve_provider_credentials(
            observer_mapping.provider,
            config.api_key,
            config.base_url,
        )
        observer_client = _maybe_record_llm_exchanges(
            _build_chat_client(
                observer_credentials,
                model=observer_mapping.model,
                model_cache_dir=project_root / ".powdrr" / "models",
                progress_stream=stderr,
            ),
            project_root,
        )
        driver.observer = ShadowWorkflowObserver(
            client=observer_client,
            model=observer_mapping.model,
            provider=observer_mapping.provider,
            worktree_root=worktree_root,
            log_root=project_root,
            context_provider=observer_context,
        )
    exit_code = driver.run(
        execution_strategy,
        # Skill selection and execution have different turn budgets. Keep a
        # bounded execution ceiling so malformed provider responses cannot
        # spin forever, while allowing multi-phase skills to finish.
        max_roundtrips=max(config.max_turns, 128),
        signature=_workflow_action_signature,
    )
    if exit_code != 0:
        return exit_code
    selected_skill = execution_strategy.selected_skill
    progress.update(
        root_skill,
        current_step_index=len(root_skill.skill.steps),
        status=f"{root_skill.skill.name} skill completed",
    )

    summary = _build_skill_execution_summary(
        root_skill,
        selection,
        execution_state.transcript,
        execution_state.execution_events,
    )
    _verbose_print(
        stderr,
        config.verbose,
        f"Prepared execution summary for {selected_skill.skill.name}",
    )

    output_dir = (
        output_dir
        if output_dir is not None
        else Path(tempfile.mkdtemp(prefix="powdrr-lift-skill-chat-"))
    )
    _verbose_print(stderr, config.verbose, f"Writing skill summary to {output_dir}")
    summary_path = _write_skill_summary(summary, output_dir)
    _verbose_print(
        stderr,
        config.verbose,
        f"Summary written to {summary_path}",
    )
    _persist_workflow_context(
        project_root,
        worktree_root,
        skill_name=root_skill.skill.name,
        request=user_request,
    )

    if config.output_dir is None:
        print(
            json.dumps(
                {
                    "selected_skill_file": str(selected_skill.path),
                    "summary_path": str(summary_path),
                    "summary": summary,
                },
                indent=2,
                ensure_ascii=False,
            ),
            file=stdout,
        )
    else:
        print(f"Wrote skill execution summary to {summary_path}", file=stdout)

    return 0


def _load_skill_catalog(
    skills_dir: Path,
    *,
    stderr: TextIO,
) -> tuple[SkillCatalogEntry, ...]:
    resolved_dir = skills_dir.expanduser().resolve()
    if not resolved_dir.exists():
        print(f"Skill directory does not exist: {resolved_dir}", file=stderr)
        return ()
    if not resolved_dir.is_dir():
        print(f"Skill path is not a directory: {resolved_dir}", file=stderr)
        return ()

    report = build_skill_directory_validation_report(resolved_dir)
    if not report.validation_successful:
        for issue in report.issues:
            print(f"{issue.path}: {issue.code}: {issue.message}", file=stderr)
        return ()

    skill_paths = tuple(
        skill_path
        for pattern in ("*.yaml", "*.yml", "*.json")
        for skill_path in sorted(resolved_dir.glob(pattern))
        if skill_path.is_file()
    )
    skills = load_skills(resolved_dir)
    entries = tuple(
        SkillCatalogEntry(path=skill_path, skill=skill)
        for skill_path, skill in zip(skill_paths, skills, strict=False)
    )

    return entries


def _load_workflow_template_catalog(
    templates_dir: Path,
    *,
    stderr: TextIO,
) -> tuple[SkillCatalogEntry, ...]:
    return _load_skill_catalog(templates_dir, stderr=stderr)


def _build_selection_messages(
    catalog: Sequence[SkillCatalogEntry],
    transcript: Sequence[dict[str, str]],
    worktree_root: Path,
    workflow_context: WorkflowContext | None = None,
) -> list[dict[str, str]]:
    available_work_items = _available_work_item_names(worktree_root)
    return [
        {
            "role": "system",
            "content": _selection_system_prompt(),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "skills": [_catalog_entry_to_data(entry) for entry in catalog],
                    "conversation": list(transcript),
                    "previous_workflow_context": (
                        workflow_context.to_data() if workflow_context else None
                    ),
                    "work_item_context": {
                        "available": list(available_work_items),
                        "matches": list(
                            _match_work_item_names(
                                transcript,
                                available_work_items,
                            )
                        ),
                        "documents": {
                            work_item_name: list(
                                _available_work_item_documents(
                                    worktree_root,
                                    work_item_name,
                                )
                            )
                            for work_item_name in available_work_items
                        },
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def _write_skill_summary(summary: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "skill-execution.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary_path


def _parse_selection_response(
    payload: dict[str, Any],
    catalog: Sequence[SkillCatalogEntry],
) -> SkillChatSelection:
    selected_skill_path_value = payload.get("selected_skill_path")
    if not isinstance(selected_skill_path_value, str) or not selected_skill_path_value:
        raise PowdrrExecutionError(
            "Skill selection response must include selected_skill_path."
        )
    selected_skill_path = _resolve_skill_path(selected_skill_path_value, catalog)
    selected_skill_reason = payload.get("selected_skill_reason")
    if not isinstance(selected_skill_reason, str) or not selected_skill_reason:
        raise PowdrrExecutionError(
            "Skill selection response must include selected_skill_reason."
        )
    next_question = payload.get("next_question")
    if next_question is not None and not isinstance(next_question, str):
        raise PowdrrExecutionError(
            "Skill selection response next_question must be a string."
        )
    if next_question is not None:
        next_question = _validate_user_question(
            next_question,
            field_name="Skill selection response next_question",
        )
    ready_to_execute_value = payload.get("ready_to_execute")
    if not isinstance(ready_to_execute_value, bool):
        raise PowdrrExecutionError(
            "Skill selection response ready_to_execute must be a boolean."
        )
    ready_to_execute = ready_to_execute_value
    if ready_to_execute and next_question is not None:
        raise PowdrrExecutionError(
            "Skill selection response must not include next_question when ready."
        )
    if not ready_to_execute and next_question is None:
        raise PowdrrExecutionError(
            "Skill selection response must include next_question when not ready."
        )
    llm_type = _optional_llm_type(payload.get("llm_type"))
    return SkillChatSelection(
        selected_skill_path=selected_skill_path,
        selected_skill_reason=selected_skill_reason,
        next_question=next_question,
        ready_to_execute=ready_to_execute,
        llm_type=llm_type,
    )


def _validate_user_question(value: str, *, field_name: str) -> str:
    normalized_value = value.strip()
    if (
        not normalized_value
        or not re.search(r"[A-Za-z]", normalized_value)
        or not normalized_value.endswith("?")
    ):
        raise PowdrrExecutionError(
            f"{field_name} must be a non-empty, properly formed English question."
        )
    return normalized_value


def _resolve_skill_path(
    skill_path_value: str,
    catalog: Sequence[SkillCatalogEntry],
) -> Path:
    normalized_value = _normalize_skill_path_value(skill_path_value)
    for entry in catalog:
        entry_value = str(entry.path)
        entry_value_no_suffix = _path_without_suffix(entry_value)
        if (
            skill_path_value == entry_value
            or skill_path_value == entry.path.name
            or skill_path_value == entry.path.stem
            or normalized_value == _normalize_skill_path_value(entry_value)
            or normalized_value == _normalize_skill_path_value(entry.path.name)
            or normalized_value == _normalize_skill_path_value(entry.path.stem)
            or _path_without_suffix(skill_path_value) == entry_value_no_suffix
        ):
            return entry.path
    raise PowdrrExecutionError(
        f"Skill selection response referenced unknown skill {skill_path_value!r}."
    )


def _resolve_template_path(
    template_path_value: str,
    catalog: Sequence[SkillCatalogEntry],
) -> Path:
    return _resolve_skill_path(template_path_value, catalog)


def _available_work_item_names(worktree_root: Path) -> tuple[str, ...]:
    specifications_root = worktree_root / "docs" / "proposals"
    if not specifications_root.is_dir():
        return ()
    return tuple(
        sorted(
            path.name
            for path in specifications_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
    )


def _available_work_item_documents(
    worktree_root: Path,
    work_item_name: str,
) -> tuple[str, ...]:
    work_item_root = worktree_root / "docs" / "proposals" / work_item_name
    if not work_item_root.is_dir():
        return ()
    return tuple(
        sorted(
            str(path.relative_to(worktree_root))
            for path in work_item_root.rglob("*")
            if path.is_file()
        )
    )


def _match_work_item_names(
    transcript: Sequence[dict[str, str]],
    work_item_names: Sequence[str],
) -> tuple[str, ...]:
    request_text = " ".join(
        message.get("content", "")
        for message in transcript
        if message.get("role") == "user"
    )
    request_tokens = _work_item_name_tokens(request_text)
    matches: list[str] = []
    for work_item_name in work_item_names:
        name_tokens = _work_item_name_tokens(work_item_name)
        if not name_tokens:
            continue
        token_count = len(name_tokens)
        contiguous_match = any(
            request_tokens[index : index + token_count] == name_tokens
            for index in range(len(request_tokens) - token_count + 1)
        )
        if contiguous_match or (
            token_count > 1 and all(token in request_tokens for token in name_tokens)
        ):
            matches.append(work_item_name)
    return tuple(matches)


def _work_item_name_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _normalize_skill_path_value(value: str) -> str:
    return value.strip().rstrip(".").rstrip()


def _path_without_suffix(value: str) -> str:
    return str(Path(value.rstrip(".")).with_suffix(""))


def _verbose_print(stderr: TextIO, verbose: bool, message: str) -> None:
    if verbose:
        print(f"[verbose] {message}", file=stderr)


def _write_agent_error(repo_root: Path, message: str) -> None:
    """Persist the latest failure context without hiding the original error."""
    try:
        (repo_root / "agent_error.txt").write_text(
            message.rstrip() + "\n", encoding="utf-8"
        )
    except OSError:
        return


def _verbose_json(
    stderr: TextIO,
    verbose: bool,
    label: str,
    value: object,
) -> None:
    if not verbose:
        return
    serialized = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    for line_number, line in enumerate(serialized.splitlines()):
        prefix = f"{label}: " if line_number == 0 else "  "
        _verbose_print(stderr, verbose, f"{prefix}{line}")


def _workflow_context_path(project_root: Path) -> Path:
    return project_root / _WORKFLOW_CONTEXT_PATH


def _workflow_context_runtime(
    project_root: Path,
    worktree_root: Path,
) -> ExecutionRuntime:
    """Create the durable runtime used by worktree-context bookkeeping."""
    return ExecutionRuntime(
        "workflow-context",
        profile_id="workflow-context",
        workflow_directory=project_root / ".powdrr" / "context-execution",
        repo_root=worktree_root,
    )


def _load_workflow_context(project_root: Path) -> WorkflowContext | None:
    path = _workflow_context_path(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    worktree_value = payload.get("worktree_root")
    if not isinstance(worktree_value, str) or not worktree_value:
        return None
    pr_number = payload.get("pr_number")
    if not isinstance(pr_number, int):
        pr_number = None
    return WorkflowContext(
        worktree_root=Path(worktree_value).expanduser().resolve(),
        branch_name=payload.get("branch_name")
        if isinstance(payload.get("branch_name"), str)
        else None,
        pr_number=pr_number,
        pr_url=payload.get("pr_url")
        if isinstance(payload.get("pr_url"), str)
        else None,
        skill_name=payload.get("skill_name")
        if isinstance(payload.get("skill_name"), str)
        else None,
        request=payload.get("request")
        if isinstance(payload.get("request"), str)
        else None,
    )


def _persist_workflow_context(
    project_root: Path,
    worktree_root: Path,
    *,
    skill_name: str,
    request: str,
) -> None:
    if not (worktree_root / ".git").exists():
        return
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    try:
        runtime = _workflow_context_runtime(project_root, worktree_root)
        with runtime.without_action_contract():
            branch_result = invoke_intrinsic_capability(
                GIT_TOOL,
                {"operation": "branch_current"},
                worktree_root=worktree_root,
                runtime=runtime,
            )
            branch_name = str(branch_result.get("stdout", "")).strip() or None
            pr_result = invoke_intrinsic_capability(
                GH_TOOL,
                {"operation": "pr_view", "json_fields": ["number", "url"]},
                worktree_root=worktree_root,
                runtime=runtime,
            )
            if int(pr_result.get("returncode", 1)) == 0:
                pr_payload = json.loads(str(pr_result.get("stdout", "")))
                if isinstance(pr_payload, dict):
                    value = pr_payload.get("number")
                    pr_number = value if isinstance(value, int) else None
                    url = pr_payload.get("url")
                    pr_url = url if isinstance(url, str) else None
    except (OSError, json.JSONDecodeError, PowdrrExecutionError):
        pass
    context = WorkflowContext(
        worktree_root=worktree_root,
        branch_name=branch_name,
        pr_number=pr_number,
        pr_url=pr_url,
        skill_name=skill_name,
        request=request,
    )
    path = _workflow_context_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context.to_data(), indent=2) + "\n", encoding="utf-8")


def _can_reuse_workflow_context(context: WorkflowContext | None) -> bool:
    return bool(
        context
        and context.worktree_root.exists()
        and _is_dedicated_worktree(context.worktree_root)
    )


def _workflow_context_pr_is_closed(context: WorkflowContext | None) -> bool:
    """Return whether the saved workflow PR has been closed or merged.

    A failed GitHub lookup is intentionally treated as unknown so users can
    still choose whether to reuse an otherwise valid worktree.
    """
    if (
        context is None
        or not _can_reuse_workflow_context(context)
        or context.pr_number is None
    ):
        return False
    try:
        project_root = context.worktree_root
        runtime = _workflow_context_runtime(project_root, context.worktree_root)
        with runtime.without_action_contract():
            result = invoke_intrinsic_capability(
                GH_TOOL,
                {
                    "operation": "pr_view",
                    "pr_reference": str(context.pr_number),
                    "json_fields": ["state"],
                },
                worktree_root=context.worktree_root,
                runtime=runtime,
            )
        if int(result.get("returncode", 1)) != 0:
            return False
        payload = json.loads(str(result.get("stdout", "")))
    except (OSError, json.JSONDecodeError, PowdrrExecutionError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("state") in {"CLOSED", "MERGED"}


def _worktree_reuse_decision(
    request: str,
    selected_skill: SkillCatalogEntry,
    context: WorkflowContext | None,
) -> bool | None:
    if not _can_reuse_workflow_context(context):
        return False
    normalized = request.casefold()
    if any(
        phrase in normalized
        for phrase in ("new worktree", "new branch", "new feature", "start over")
    ):
        return False
    if selected_skill.skill.name in {"handle-ad-hoc", "address-review-comments"}:
        return True
    if any(
        phrase in normalized
        for phrase in (
            "reuse",
            "same worktree",
            "same branch",
            "previous skill",
            "the pr",
            "pull request",
            "review comment",
            "pr comment",
            "continue",
        )
    ):
        return True
    return None


def _resolve_worktree_for_request(
    configured_repo_root: Path,
    *,
    request: str,
    selected_skill: SkillCatalogEntry,
    context: WorkflowContext | None,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
) -> Path:
    if _is_dedicated_worktree(configured_repo_root):
        return configured_repo_root
    if _workflow_context_pr_is_closed(context):
        assert context is not None
        _verbose_print(
            stderr,
            verbose,
            f"Previous workflow pull request #{context.pr_number} is closed; "
            "creating a new worktree",
        )
        return _resolve_worktree_context(
            configured_repo_root,
            stderr=stderr,
            verbose=verbose,
        )
    decision = _worktree_reuse_decision(request, selected_skill, context)
    if decision is None:
        answer = _prompt_user(
            "A previous workflow worktree is available. Do you want to reuse it? ",
            input_func=input_func,
            stdout=stdout,
            status_stream=stderr,
        )
        decision = answer.casefold() in {"y", "yes", "reuse", "same", "continue"}
    if decision and context is not None:
        _verbose_print(
            stderr,
            verbose,
            f"Reusing previous workflow worktree at {context.worktree_root}",
        )
        return context.worktree_root
    return _resolve_worktree_context(
        configured_repo_root,
        stderr=stderr,
        verbose=verbose,
    )


def _resolve_worktree_context(
    repo_root: Path | None,
    *,
    stderr: TextIO,
    verbose: bool,
) -> Path:
    resolved_repo_root = resolve_repo_root(repo_root)
    if _is_dedicated_worktree(resolved_repo_root):
        _verbose_print(
            stderr,
            verbose,
            f"Using existing worktree context at {resolved_repo_root}",
        )
        return resolved_repo_root

    branch_name = _generate_worktree_branch_name()
    script_path = resolved_repo_root / "scripts" / "create-worktree.sh"
    if not script_path.is_file():
        raise PowdrrExecutionError(
            f"Could not find the worktree creation script at {script_path}."
        )

    _verbose_print(
        stderr,
        verbose,
        f"Creating dedicated worktree with branch {branch_name}",
    )
    process = subprocess.run(
        ["bash", str(script_path), branch_name],
        check=True,
        capture_output=True,
        text=True,
        cwd=resolved_repo_root,
    )
    worktree_path = Path(process.stdout.strip().splitlines()[-1]).expanduser().resolve()
    if not worktree_path.exists():
        raise PowdrrExecutionError(
            f"Worktree creation script did not return an existing path: {worktree_path}"
        )
    _verbose_print(stderr, verbose, f"Using dedicated worktree at {worktree_path}")
    return worktree_path


def _resolve_project_root(configured_repo_root: Path, worktree_root: Path) -> Path:
    """Return the primary checkout root used for shared local model storage."""
    if not _is_dedicated_worktree(configured_repo_root):
        return configured_repo_root
    worktree_parts = worktree_root.parts
    worktree_marker = ".worktrees"
    if worktree_marker not in worktree_parts:
        raise PowdrrExecutionError(
            f"Could not determine project root for worktree {worktree_root}."
        )
    marker_index = worktree_parts.index(worktree_marker)
    if marker_index == 0:
        raise PowdrrExecutionError(
            f"Could not determine project root for worktree {worktree_root}."
        )
    return Path(*worktree_parts[:marker_index])


def _is_dedicated_worktree(repo_root: Path) -> bool:
    # Git worktrees created outside the repository's conventional .worktrees
    # directory (for example, the feature-run harness's temporary worktree)
    # still have a file .git marker. Treat them as dedicated so untracked
    # harness fixtures remain visible instead of creating a second worktree
    # from HEAD and silently dropping those fixtures.
    return ".worktrees" in repo_root.parts or (repo_root / ".git").is_file()


def _generate_worktree_branch_name() -> str:
    return f"workflow-chat-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"


def _find_catalog_entry(
    catalog: Sequence[SkillCatalogEntry],
    template_path: Path,
) -> SkillCatalogEntry:
    for entry in catalog:
        if entry.path == template_path:
            return entry
    raise PowdrrExecutionError(f"Could not find skill {template_path}.")


def _find_skill_by_name(
    catalog: Sequence[SkillCatalogEntry],
    skill_name: str,
) -> SkillCatalogEntry:
    normalized_name = skill_name.strip().casefold()
    for entry in catalog:
        if entry.skill.name.casefold() == normalized_name:
            return entry
    raise PowdrrExecutionError(f"Could not find referenced skill {skill_name!r}.")


def _push_nested_skill(
    stack: list[_SkillExecutionFrame],
    *,
    current_skill: SkillCatalogEntry,
    nested_skill: SkillCatalogEntry,
    parent_step_index: int,
    resume_step_index: int,
    dependency_key: tuple[str, int, str] | None = None,
    parent_model: str = "",
    parent_provider: str = "",
    parent_provider_role: LLMProviderRole = "normal",
    parent_interaction_style: str | None = None,
    clean_context: bool = False,
    parent_transcript: tuple[dict[str, str], ...] | None = None,
    parent_execution_events: tuple[dict[str, Any], ...] | None = None,
    parent_execution_context: tuple[str, ...] | None = None,
    parent_current_file_path: Path | None = None,
    parent_handoff_records: tuple[tuple[str, dict[str, Any]], ...] | None = None,
    parent_durable_facts: tuple[tuple[str, dict[str, Any]], ...] | None = None,
    output_bindings: tuple[tuple[str, str, Mapping[str, Any] | None], ...] = (),
) -> None:
    active_skill_paths = {str(frame.parent_skill.path) for frame in stack}
    active_skill_paths.add(str(current_skill.path))
    if str(nested_skill.path) in active_skill_paths:
        raise PowdrrExecutionError(
            f"Recursive skill invocation is not allowed: {nested_skill.skill.name!r}."
        )
    stack.append(
        _SkillExecutionFrame(
            parent_skill=current_skill,
            parent_step_index=parent_step_index,
            resume_step_index=resume_step_index,
            dependency_key=dependency_key,
            parent_model=parent_model,
            parent_provider=parent_provider,
            parent_provider_role=parent_provider_role,
            parent_interaction_style=parent_interaction_style,
            clean_context=clean_context,
            parent_transcript=parent_transcript,
            parent_execution_events=parent_execution_events,
            parent_execution_context=parent_execution_context,
            parent_current_file_path=parent_current_file_path,
            parent_handoff_records=parent_handoff_records,
            parent_durable_facts=parent_durable_facts,
            output_bindings=output_bindings,
            child_skill_name=nested_skill.skill.name,
        )
    )


def _active_llm_mappings(
    config: SkillChatConfig,
    provider_roles: LLMProviderRoles,
    role: LLMProviderRole,
) -> tuple[tuple[str, LLMModelMapping], ...]:
    """Return mappings for a role without exposing provider details to callers."""
    provider = provider_roles.provider_for(role)
    mappings = tuple(provider_definition(provider).llm_mappings.items())
    if role == "normal":
        mappings += config.llm_mappings
    return mappings


def _catalog_entry_to_data(entry: SkillCatalogEntry) -> dict[str, Any]:
    return {
        "file": str(entry.path),
        "name": entry.skill.name,
        "adversarial": entry.skill.adversarial,
        "interaction_style": entry.skill.interaction_style,
        "when_to_use": list(entry.skill.when_to_use),
        "steps": [_skill_step_to_data(step) for step in entry.skill.steps],
    }


def _selected_skill_prompt_data(entry: SkillCatalogEntry) -> dict[str, Any]:
    """Return only skill identity; the active step carries execution details."""
    return {
        "file": entry.path.name,
        "name": entry.skill.name,
        "adversarial": entry.skill.adversarial,
        "interaction_style": entry.skill.interaction_style,
    }


def _effective_interaction_style(
    selected_skill: SkillCatalogEntry,
    current_step: Any,
    inherited_style: str | None = None,
) -> str | None:
    return (
        getattr(current_step, "interaction_style", None)
        or selected_skill.skill.interaction_style
        or inherited_style
    )


def _interaction_style_prompt(style: str | None) -> str:
    if style is None:
        return ""
    guidance = _INTERACTION_STYLE_GUIDANCE[style]
    return (
        "Interaction style: "
        f"{style}.\n"
        "Style guidance: "
        f"{guidance}\n"
        "This guidance changes reasoning posture and communication only; the "
        "current step contract, allowed actions, gates, and validation rules remain "
        "authoritative.\n"
    )


def _step_needs_prompt_catalog(step: Any, capability: str) -> bool:
    configured_catalogs = getattr(step, "prompt_catalogs", ())
    if capability not in {"context_types", "skills", "actions"}:
        raise ValueError(f"Unknown prompt catalog capability: {capability}")
    return capability in configured_catalogs


def _workflow_context_prompt_data(
    workflow_context: WorkflowContext | None,
) -> dict[str, object] | None:
    if workflow_context is None:
        return None
    data = {
        "branch_name": workflow_context.branch_name,
        "pr_number": workflow_context.pr_number,
        "pr_url": workflow_context.pr_url,
        "skill_name": workflow_context.skill_name,
        "request": workflow_context.request,
    }
    return {key: value for key, value in data.items() if value is not None}


def _selection_system_prompt() -> str:
    return (
        "Task: route the user's request to the best available skill. Read the "
        "catalog, conversation, and work-item context in the user message. "
        "Decide whether the request is sufficiently specified to begin that "
        "skill.\n"
        "Choose exactly one outcome:\n"
        "1. Ready: use this when one skill clearly matches and the available "
        "context is sufficient to start it. Set ready_to_execute to true and "
        "next_question to null.\n"
        "2. Need-information: use this when the skill is identifiable but a "
        "specific missing user decision or fact prevents starting. Set "
        "ready_to_execute to false and put exactly one concise question in "
        "next_question. Ask only for information not already present in the "
        "conversation or work-item context.\n"
        "3. Continue-clarification: use this only when the request is still "
        "ambiguous enough that the best skill cannot be selected. Set "
        "ready_to_execute to false and put exactly one concise question in "
        "next_question.\n"
        "Response: return exactly one JSON object with the keys "
        "selected_skill_path, selected_skill_reason, next_question, and "
        "ready_to_execute; llm_type is optional. For a ready response, "
        "next_question must be null and ready_to_execute must be true. For "
        "either clarification outcome, next_question must be a question and "
        "ready_to_execute must be false.\n"
        "A user question must be a properly formed English question: it must "
        "contain meaningful words, cannot be empty or only whitespace, and "
        "must end with a question mark. Never return whitespace or an "
        "instruction as next_question.\n"
        "llm_type describes the capability needed for the next roundtrip; use "
        "high_reasoning, standard_reasoning, simple_task, fast_iteration, "
        "long_context, or vision.\n"
        "selected_skill_path must match one of the catalog entries.\n"
        "Use the skill when_to_use and step descriptions to decide.\n"
        "When previous_workflow_context is present, treat it as the last skill's "
        "worktree and pull-request context. Select handle-ad-hoc for a small "
        "follow-up that does not match a more specific skill. Select "
        "address-review-comments for requests to check or fix pull-request "
        "comments. Follow-up requests about that worktree, branch, or PR should "
        "continue there. If the request could reasonably be either a continuation "
        "or a new task, ask exactly whether the user wants to reuse the previous "
        "worktree or start a new one.\n"
        "The user may refer to an existing work item using natural language. "
        "Before asking whether approved specification documents exist, inspect "
        "work_item_context. When matches contains a reasonable canonical name "
        "and its documents list is non-empty, reuse that exact name and existing "
        "documents; do not ask the user to confirm that they exist.\n"
        "Do not output markdown."
    )


def _build_skill_execution_summary(
    selected_skill: SkillCatalogEntry,
    selection: SkillChatSelection,
    transcript: Sequence[dict[str, str]],
    execution_events: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "selected_skill_file": str(selected_skill.path),
        "selected_skill_name": selected_skill.skill.name,
        "selected_skill_reason": selection.selected_skill_reason,
        "conversation": list(transcript),
        "execution_events": list(execution_events),
        "skill": selected_skill.skill.to_data(),
    }


def _execution_events_for_prompt(
    execution_events: Sequence[dict[str, Any]],
    current_step_index: int | None = None,
) -> list[dict[str, Any]]:
    """Return the event metadata needed for the next action decision.

    Event results are retained in the full execution summary, but are also
    copied into the transcript or execution context as they are produced.
    Sending both copies on every roundtrip needlessly grows prompts and makes
    large tool results increasingly expensive to serialize. Keep the prompt
    event stream as metadata while leaving the complete event stream intact
    for persistence and diagnostics.
    """
    events = (
        [
            event
            for event in execution_events
            if event.get("step_index") == current_step_index
        ]
        if current_step_index is not None
        else execution_events
    )
    return [
        {key: value for key, value in event.items() if key != "decisions_and_context"}
        for event in prune_execution_events(events, include_results=False)
    ]


def _successful_document_reads_for_prompt(
    execution_events: Sequence[Mapping[str, Any]],
    current_step_index: int | None = None,
) -> list[dict[str, Any]]:
    """Expose successful reads as durable repair context.

    Compact event metadata intentionally omits results. Repairs still need to
    know which documents already supplied context so they do not spend a retry
    rereading the same file instead of correcting the failed capability call.
    """
    reads: list[dict[str, Any]] = []
    for event in execution_events:
        if event.get("kind") != "read_document":
            continue
        if current_step_index is not None and event.get("step_index") not in {
            None,
            current_step_index,
        }:
            continue
        result = event.get("result")
        if not isinstance(result, Mapping):
            continue
        path = result.get("path")
        if not isinstance(path, str) or not path:
            continue
        reads.append(
            {
                "path": path,
                "requested_start_line": result.get("requested_start_line"),
                "requested_end_line": result.get("requested_end_line"),
                "returned_end_line": result.get("end_line"),
            }
        )
    return reads


def _latest_execution_event_for_prompt(
    execution_events: Sequence[dict[str, Any]],
    current_step_index: int | None = None,
) -> dict[str, Any] | None:
    """Retain the latest result separately from the compact event metadata."""
    if current_step_index is not None:
        execution_events = [
            event
            for event in execution_events
            if event.get("step_index") == current_step_index
        ]
    if not execution_events:
        return None
    latest = prune_execution_events(execution_events[-1:], include_results=True)
    if not latest:
        return None
    return {
        key: value for key, value in latest[0].items() if key != "decisions_and_context"
    }


def _sanitize_prior_step_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Keep prior-step progress while hiding reusable command payloads."""
    hidden_keys = {
        "command",
        "parameters",
        "file_edits",
        "edits",
        "operations",
        "result",
        "template",
    }
    return {key: value for key, value in event.items() if key not in hidden_keys}


_PROMPT_OBSERVATION_RESULT_KEYS = {
    "tool_result",
    "edit_result",
    "yaml_edit_result",
    "document_context",
}


def _is_prompt_observation_message(message: Mapping[str, str]) -> bool:
    """Identify action/result transcript entries represented by event state."""
    content = message.get("content", "")
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(decoded, Mapping):
        return False
    if message.get("role") == "assistant":
        return isinstance(decoded.get("action", decoded.get("kind")), str)
    return bool(_PROMPT_OBSERVATION_RESULT_KEYS.intersection(decoded))


def _prompt_transcript(
    transcript: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """Keep recurring prompts bounded while retaining the complete transcript."""
    conversational = [
        message for message in transcript if not _is_prompt_observation_message(message)
    ]
    if len(conversational) <= _MAX_PROMPT_TRANSCRIPT_ENTRIES:
        return conversational

    first = {
        **conversational[0],
        "content": _truncate_prompt_content(conversational[0].get("content", "")),
    }
    recent = [
        {
            **message,
            "content": _truncate_prompt_content(message.get("content", "")),
        }
        for message in conversational[-(_MAX_PROMPT_TRANSCRIPT_ENTRIES - 2) :]
    ]
    omitted = {
        "role": "user",
        "content": "[Earlier workflow transcript omitted from this prompt; "
        "full history remains in the execution summary.]",
    }
    compacted = [first, omitted, *recent]
    while (
        len(compacted) > 3
        and sum(len(message.get("content", "")) for message in compacted)
        > _MAX_PROMPT_TRANSCRIPT_CHARS
    ):
        compacted.pop(2)
    return compacted


def _prompt_step_context(
    execution_context: Sequence[str],
    durable_facts: Mapping[str, Mapping[str, Any]] | None = None,
    execution_events: Sequence[Mapping[str, Any]] = (),
    current_step_index: int | None = None,
) -> list[str]:
    """Bound recurring step context while retaining the newest handoff facts."""
    result_prefixes = (
        "Gathered context:\n",
        "Deterministic pre-step gather_context result:\n",
        "Document context: ",
        "Gate failed: ",
    )
    keep_current_gather = any(
        event.get("kind") == "gather_context"
        and event.get("step_index") == current_step_index
        for event in execution_events
    )
    execution_context = [
        value
        for value in execution_context
        if keep_current_gather
        and value.startswith("Gathered context:\n")
        or not value.startswith(result_prefixes)
    ]
    fact_values = {
        str(record.get("value"))
        for record in (durable_facts or {}).values()
        if record.get("value") is not None
    }
    execution_context = [
        value
        for value in execution_context
        if " ".join(value.split()) not in fact_values
    ]
    if len(execution_context) <= _MAX_PROMPT_STEP_CONTEXT_ENTRIES:
        recent = list(execution_context)
    else:
        recent = list(execution_context[-_MAX_PROMPT_STEP_CONTEXT_ENTRIES:])
    while (
        len(recent) > 1
        and sum(len(value) for value in recent) > _MAX_PROMPT_STEP_CONTEXT_CHARS
    ):
        recent.pop(0)
    return recent


def _prompt_durable_facts(
    durable_facts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return deduplicated durable facts in a compact, stable prompt shape."""
    facts = list(durable_facts.values())[-_MAX_PROMPT_STEP_CONTEXT_ENTRIES:]
    return [dict(fact) for fact in facts]


def _truncate_prompt_content(content: str) -> str:
    if len(content) <= _MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS:
        return content
    half_limit = _MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS // 2
    return (
        content[:half_limit]
        + "\n... [prompt transcript message truncated] ...\n"
        + content[-half_limit:]
    )


_PRE_STEP_PLACEHOLDER = re.compile(r"<([^<>]+)>")


def _latest_deterministic_pre_step(
    execution_events: Sequence[Mapping[str, Any]],
    *,
    skill_name: str,
    step_index: int,
) -> Mapping[str, Any] | None:
    for event in reversed(execution_events):
        if (
            event.get("kind") == "deterministic_pre_step"
            and event.get("skill_name") == skill_name
            and event.get("step_index") == step_index
        ):
            return event
    return None


def _invalidate_deterministic_pre_step(
    execution_events: list[dict[str, Any]],
    *,
    skill_name: str,
    step_index: int,
) -> None:
    execution_events[:] = [
        event
        for event in execution_events
        if not (
            event.get("kind") == "deterministic_pre_step"
            and event.get("skill_name") == skill_name
            and event.get("step_index") == step_index
        )
    ]


def _pre_step_context_values(
    handoff_records: Mapping[str, Mapping[str, Any]],
    workflow_context: WorkflowContext | None,
    execution_events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    values: dict[str, Any] = {}

    def add(name: object, value: object, *, include_none: bool = False) -> None:
        if not isinstance(name, str) or (value is None and not include_none):
            return
        normalized = re.sub(r"[-\s]+", "_", name.strip().lower())
        if normalized:
            values[normalized] = value

    if workflow_context is not None:
        for name, value in workflow_context.to_data().items():
            add(name, value)
    for name, record in handoff_records.items():
        if isinstance(record, Mapping):
            add(name, record.get("value"), include_none="value" in record)
    for event in execution_events:
        parameters = event.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        command = parameters.get("command")
        if not isinstance(command, Sequence) or isinstance(
            command, (str, bytes, bytearray)
        ):
            continue
        command_items = [item for item in command if isinstance(item, str)]
        if "--work-item-name" in command_items:
            option_index = command_items.index("--work-item-name")
            if option_index + 1 < len(command_items):
                add("work-item-name", command_items[option_index + 1])
        for item in command_items:
            proposal_prefix = "docs/proposals/"
            if item.startswith(proposal_prefix):
                proposal_name = item[len(proposal_prefix) :].split("/", 1)[0]
                add("work-item-name", proposal_name)
    return values


def _resolve_pre_step_template(
    value: Any,
    context_values: Mapping[str, Any],
) -> Any:
    if isinstance(value, str):
        exact = _PRE_STEP_PLACEHOLDER.fullmatch(value.strip())
        if exact is not None:
            key = re.sub(r"[-\s]+", "_", exact.group(1).strip().lower())
            if key not in context_values:
                raise PowdrrExecutionError(
                    f"Deterministic pre-step placeholder <{exact.group(1)}> "
                    "is not present in the input context."
                )
            return context_values[key]

        def replace_match(match: re.Match[str]) -> str:
            key = re.sub(r"[-\s]+", "_", match.group(1).strip().lower())
            if key not in context_values:
                raise PowdrrExecutionError(
                    f"Deterministic pre-step placeholder <{match.group(1)}> "
                    "is not present in the input context."
                )
            replacement = context_values[key]
            if isinstance(replacement, (Mapping, Sequence)) and not isinstance(
                replacement, (str, bytes, bytearray)
            ):
                raise PowdrrExecutionError(
                    f"Deterministic pre-step placeholder <{match.group(1)}> "
                    "must be the complete template value when its context value "
                    "is structured."
                )
            return str(replacement)

        return _PRE_STEP_PLACEHOLDER.sub(replace_match, value)
    if isinstance(value, Mapping):
        return {
            key: _resolve_pre_step_template(item, context_values)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_resolve_pre_step_template(item, context_values) for item in value]
    return value


def _wire_previous_tool_output(
    parameters: dict[str, Any],
    execution_events: Sequence[Mapping[str, Any]],
    handoff_records: Mapping[str, Mapping[str, Any]],
) -> None:
    reference = parameters.get("tool_output")
    if isinstance(reference, Mapping) and reference.get("source") == "handoff":
        name = reference.get("name")
        record = handoff_records.get(name) if isinstance(name, str) else None
        if record is None or "value" not in record:
            raise PowdrrExecutionError(
                "enrich tool_output handoff source requires a prior named output."
            )
        parameters["tool_output"] = record["value"]
        return
    if reference != {"source": "previous_tool_output"}:
        return
    previous = next(
        (
            event.get("result")
            for event in reversed(execution_events)
            if event.get("kind") == "deterministic_pre_step"
            and isinstance(event.get("result"), Mapping)
        ),
        None,
    )
    if previous is None:
        raise PowdrrExecutionError(
            "enrich tool_output source previous_tool_output requires a prior "
            "deterministic tool result."
        )
    parameters["tool_output"] = previous


def _run_deterministic_pre_step(
    step: Any,
    *,
    skill_name: str,
    worktree_root: Path,
    execution_events: list[dict[str, Any]],
    execution_context: list[str],
    handoff_records: Mapping[str, Mapping[str, Any]],
    step_index: int,
    workflow_context: WorkflowContext | None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    verbose: bool = False,
    force: bool = False,
    runtime: ExecutionRuntime | None = None,
) -> None:
    if runtime is None:
        runtime = ExecutionRuntime(
            "chat-pre-step-"
            + hashlib.sha256(str(worktree_root).encode()).hexdigest()[:24],
            profile_id="default",
            workflow_directory=worktree_root.parent / ".powdrr-execution",
            repo_root=worktree_root,
        )
    if not force and _latest_deterministic_pre_step(
        execution_events,
        skill_name=skill_name,
        step_index=step_index,
    ):
        return
    pre_step = step.pre_step
    if pre_step is None:
        raise PowdrrExecutionError("invoke_tool steps require a pre_step.")
    template = _resolve_pre_step_template(
        pre_step.template,
        _pre_step_context_values(
            handoff_records,
            workflow_context,
            execution_events=execution_events,
        ),
    )
    if not isinstance(template, Mapping):
        raise PowdrrExecutionError(
            "Deterministic pre-step template must resolve to an object."
        )
    if pre_step.action == "invoke_tool":
        tool = template.get("tool")
        if not isinstance(tool, str):
            raise PowdrrExecutionError("Invoke tool pre-step template requires a tool.")
        parameters = dict(template)
        parameters.pop("tool", None)
        if tool == ENRICH_TOOL:
            parameters.pop("tool", None)
            _wire_previous_tool_output(parameters, execution_events, handoff_records)
            result = invoke_intrinsic_capability(
                ENRICH_TOOL, parameters, worktree_root=worktree_root, runtime=runtime
            )
        elif tool == VALIDATE_EDIT_TOOL:
            parameters.pop("tool", None)
            result = invoke_deferred_edit_capability(
                VALIDATE_EDIT_TOOL,
                parameters,
                worktree_root=worktree_root,
                runtime=runtime,
            )
        elif tool == APPLY_EDIT_TOOL:
            parameters.pop("tool", None)
            result = invoke_deferred_edit_capability(
                APPLY_EDIT_TOOL,
                parameters,
                worktree_root=worktree_root,
                runtime=runtime,
            )
        elif tool == "fuzzy-match":
            result = invoke_fuzzy_match_capability(
                parameters,
                worktree_root=worktree_root,
                runtime=runtime,
            )
        elif tool == _INTERNAL_TOOL and _is_authoritative_effect_command(
            parameters.get("command")
        ):
            command = _command_items_for_validation(parameters.get("command"))
            work_item_name = _extract_command_option(command, "--work-item-name")
            if work_item_name is None:
                raise PowdrrExecutionError(
                    "authoritative-pr-effects requires --work-item-name."
                )
            result = invoke_repository_read(
                "authoritative_pr_effects",
                {"work_item_name": work_item_name},
                worktree_root=worktree_root,
                executor=lambda _arguments: build_authoritative_effect_handoff(
                    work_item_name=work_item_name,
                    repo_root=worktree_root,
                ),
                runtime=runtime,
            )
        elif tool in {"shell", _INTERNAL_TOOL}:
            if tool == _INTERNAL_TOOL and parameters.get("help") is not True:
                _validate_internal_command(parameters.get("command"))
            result = invoke_shell_capability(
                {**parameters, "_tool_name": tool},
                worktree_root=worktree_root,
                executor=lambda invocation: _execute_shell_tool(
                    dict(invocation),
                    worktree_root=worktree_root,
                    stdout=stdout,
                    stderr=stderr,
                    verbose=verbose,
                    announce=False,
                ),
                runtime=runtime,
            )
        elif tool in {GIT_TOOL, GH_TOOL}:
            result = invoke_intrinsic_capability(
                tool, parameters, worktree_root=worktree_root, runtime=runtime
            )
        elif is_basedpyright_tool(tool):
            result = invoke_basedpyright_capability(
                tool,
                parameters,
                worktree_root=worktree_root,
                runtime=runtime,
            )
        else:
            raise PowdrrExecutionError(
                f"Unsupported invoke_tool pre-step tool: {tool!r}"
            )
        event = {
            "kind": "deterministic_pre_step",
            "skill_name": skill_name,
            "step_type": step.step_type,
            "action": pre_step.action,
            "tool": tool,
            "template": template,
            "result": result,
            "step_index": step_index,
        }
        execution_events.append(event)
        _record_runtime_readiness_from_pre_step(step, runtime)
        if isinstance(handoff_records, dict):
            for output in step.outputs:
                output_value = (
                    _runtime_readiness_report(runtime)
                    if output.name == "readiness_report"
                    else result
                )
                handoff_records[output.name] = {
                    "name": output.name,
                    "type": output.type,
                    "value": output_value,
                    "produced_by": {
                        "step_index": step_index,
                        "action": "deterministic_pre_step",
                    },
                    "scope": output.scope,
                }
        execution_context.append(
            "Deterministic invoke_tool result:\n"
            + json.dumps(result, ensure_ascii=False)
        )
        return
    if pre_step.action != "gather_context":
        raise PowdrrExecutionError(
            "invoke_tool pre-steps must use gather_context or invoke_tool."
        )
    raw_types = template.get("types")
    if (
        not isinstance(raw_types, Sequence)
        or isinstance(raw_types, (str, bytes, bytearray))
        or not raw_types
    ):
        raise PowdrrExecutionError(
            "Deterministic gather_context template requires types."
        )
    feature_id = template.get("feature_id")
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise PowdrrExecutionError(
            "Deterministic gather_context template requires feature_id."
        )
    keywords = template.get("keywords")
    filters = template.get("filters")
    gathered_context = invoke_repository_read(
        "gather_context",
        dict(template),
        worktree_root=worktree_root,
        executor=lambda _arguments: gather_specification_context(
            worktree_root,
            types=[str(value) for value in raw_types],
            keywords=(
                [str(value) for value in keywords]
                if isinstance(keywords, Sequence)
                and not isinstance(keywords, (str, bytes, bytearray))
                else None
            ),
            filters=dict(filters) if isinstance(filters, Mapping) else None,
            feature_id=feature_id,
        ),
        runtime=runtime,
    )
    result = json.loads(render_gather_context_report(gathered_context))
    event = {
        "kind": "deterministic_pre_step",
        "skill_name": skill_name,
        "step_type": step.step_type,
        "action": pre_step.action,
        "template": template,
        "result": result,
        "step_index": step_index,
    }
    execution_events.append(event)
    if isinstance(handoff_records, dict):
        for output in step.outputs:
            if output.required_for_next_step:
                handoff_records[output.name] = {
                    "name": output.name,
                    "type": output.type,
                    "value": result,
                    "produced_by": {
                        "step_index": step_index,
                        "action": "deterministic_pre_step",
                    },
                    "scope": output.scope,
                }
    execution_context.append(
        "Deterministic pre-step gather_context result:\n"
        + json.dumps(result, ensure_ascii=False)
    )


def _is_authoritative_effect_command(command: object) -> bool:
    command_items = _command_items(command)
    return len(command_items) >= 2 and command_items[:2] == [
        "powdrr-lift",
        "authoritative-pr-effects",
    ]


def _gate_outcome_matches(
    result: Mapping[str, Any], outcome: Mapping[str, Any]
) -> bool:
    value: Any = result
    for component in str(outcome["path"]).split("."):
        if not isinstance(value, Mapping) or component not in value:
            return False
        value = value[component]
    return value == outcome["equals"]


def _run_gate(
    step: Any,
    *,
    skill_name: str,
    worktree_root: Path,
    execution_events: list[dict[str, Any]],
    execution_context: list[str],
    handoff_records: Mapping[str, Mapping[str, Any]],
    step_index: int,
    workflow_context: WorkflowContext | None,
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
    runtime: ExecutionRuntime | None = None,
) -> bool:
    if step.gate is None or step.pre_step is None:
        raise PowdrrExecutionError(
            "gate steps require gate and invoke_tool pre_step settings."
        )
    before = len(execution_events)
    _run_deterministic_pre_step(
        step,
        skill_name=skill_name,
        worktree_root=worktree_root,
        execution_events=execution_events,
        execution_context=execution_context,
        handoff_records=handoff_records,
        step_index=step_index,
        workflow_context=workflow_context,
        stdout=stdout,
        stderr=stderr,
        verbose=verbose,
        force=True,
        runtime=runtime,
    )
    event = execution_events[-1] if len(execution_events) > before else None
    if not isinstance(event, Mapping) or not isinstance(event.get("result"), Mapping):
        raise PowdrrExecutionError("Gate tool did not produce a structured result.")
    passed = _gate_outcome_matches(event["result"], step.gate.outcome)
    gate_event = {
        "kind": "gate",
        "skill_name": skill_name,
        "step_index": step_index,
        "passed": passed,
        "outcome": dict(step.gate.outcome),
        "result": event["result"],
    }
    execution_events.append(gate_event)
    step_id = getattr(step, "id", None) or f"step-{step_index + 1}"
    status = "passed" if passed else "failed"
    print(
        f"Workflow gate evaluation ({skill_name}/{step_id}): {status}\n"
        "Workflow gate result: "
        + json.dumps(event["result"], ensure_ascii=False, sort_keys=True),
        file=stderr,
        flush=True,
    )
    if not passed:
        execution_context.append(
            f"Gate failed: {json.dumps(event['result'], ensure_ascii=False)}. "
            + step.gate.retry_context
        )
    return passed


def _build_step_execution_messages(
    *,
    selected_skill: SkillCatalogEntry,
    current_step: Any,
    current_step_index: int,
    transcript: Sequence[dict[str, str]],
    execution_events: Sequence[dict[str, Any]],
    execution_context: Sequence[str],
    handoff_records: Mapping[str, Mapping[str, Any]] | None = None,
    durable_facts: Mapping[str, Mapping[str, Any]] | None = None,
    current_file_path: Path | None,
    worktree_root: Path,
    catalog: Sequence[SkillCatalogEntry],
    workflow_context: WorkflowContext | None = None,
    current_file_context_cache: dict[tuple[str, int, int], dict[str, Any]]
    | None = None,
    validation_gate: Mapping[str, Any] | None = None,
    stalled_step_context: Sequence[Mapping[str, Any]] = (),
    inherited_interaction_style: str | None = None,
    observer_intervention: str | None = None,
    runtime_prompt_context: Mapping[str, Any] | None = None,
    failed_action: SkillChatAction | None = None,
    failure_reason: str | None = None,
) -> list[dict[str, str]]:
    current_file_context = _current_file_context(
        worktree_root,
        current_file_path,
        cache=current_file_context_cache,
    )
    interaction_style = _effective_interaction_style(
        selected_skill,
        current_step,
        inherited_interaction_style,
    )
    available_work_items = _available_work_item_names(worktree_root)
    available_tools = sorted(
        {
            invocation.tool
            for invocation in current_step.tool_invocations
            if invocation.tool != "ref"
        }
    )
    tool_descriptions = {
        "shell": (
            "Execute a shell command in the current worktree. Commands run with "
            "the worktree as cwd; any explicit cwd must remain inside it. Set "
            "parameters.help=true for the tool's conventional --help guidance."
        ),
        _INTERNAL_TOOL: (
            "Execute a powdrr-lift CLI command. This tool is always available, "
            "but its command must invoke only the powdrr-lift binary and runs "
            "with the current worktree as cwd. Set parameters.help=true for the "
            "tool's conventional --help guidance and detailed examples."
            "detailed usage and examples."
        ),
        GIT_TOOL: (
            "Intrinsic Git tool; supports status, add, and move only. Example: "
            '{"action":"invoke_tool","tool":"git","parameters":'
            '{"operation":"status"}}. Set parameters.help=true for the tool\'s '
            "conventional --help guidance and detailed examples."
            "usage and examples."
        ),
        GH_TOOL: (
            "Intrinsic GitHub tool for pull-request creation, inspection, and "
            "inline review comments. "
            'Example: {"action":"invoke_tool","tool":"gh",'
            '"parameters":{"operation":"pr_view","pr_reference":"394"}}. '
            'Inline comment example: {"action":"invoke_tool","tool":"gh",'
            '"parameters":{"operation":"pr_review_comment",'
            '"repository":"owner/repo","pr_reference":"394",'
            '"body":"Finding","commit_id":"sha",'
            '"path":"docs/design.yaml","line":12,"side":"RIGHT"}}.'
            " Set parameters.help=true for the tool's conventional --help "
            "guidance and detailed examples."
        ),
        "fuzzy-match": (
            "Search worktree paths with find-like filters and fuzzy name matching. "
            "Set parameters.help=true for the tool's conventional --help "
            "guidance and detailed examples."
        ),
        BASEDPYRIGHT_SYMBOL_TOOL: (
            "Find Python symbols by name across the worktree. Set "
            "parameters.help=true for the tool's conventional --help guidance "
            "and detailed examples."
        ),
        BASEDPYRIGHT_STRUCTURE_TOOL: (
            "Discover the classes, functions, methods, and variables in a Python "
            "file. Set parameters.help=true for the tool's conventional --help "
            "guidance and detailed examples."
        ),
        ENRICH_TOOL: (
            "Convert a deterministic tool output into structured data. "
            "Use format pytest and pass the complete tool result as tool_output."
        ),
        VALIDATE_EDIT_TOOL: (
            "Validate a deferred edit without changing files. Pass the complete "
            "edit action in parameters.edit."
        ),
        APPLY_EDIT_TOOL: (
            "Apply a previously validated deferred edit. Pass the complete edit "
            "action in parameters.edit."
        ),
    }
    prompt_data: dict[str, Any] = {
        "execution_mode": "execute_selected_skill",
        "current_step_index": current_step_index,
        "current_step_count": len(selected_skill.skill.steps),
        "current_step": _skill_step_to_data(current_step),
        "handoff_inputs": _workflow_handoff_inputs(
            current_step,
            handoff_records or {},
        ),
        # Cross-step values must travel through declared handoff inputs. The
        # prompt helper retains explicit invocation context while removing
        # implicit tool and document results.
        "step_context": _prompt_step_context(
            execution_context,
            durable_facts,
            execution_events,
            current_step_index,
        ),
        "durable_facts": _prompt_durable_facts(durable_facts or {}),
        "available_tools": [
            {
                "name": tool,
                "description": tool_descriptions.get(tool, tool),
            }
            for tool in available_tools
        ],
        "worktree_root": ".",
        "previous_workflow_context": _workflow_context_prompt_data(workflow_context),
        "work_item_context": {
            "available": list(available_work_items),
            "matches": list(
                _match_work_item_names(
                    transcript,
                    available_work_items,
                )
            ),
        },
        "selected_skill": _selected_skill_prompt_data(selected_skill),
        "transcript": _prompt_transcript(transcript),
        "execution_events": _execution_events_for_prompt(
            execution_events,
            current_step_index,
        ),
        "latest_action": _latest_execution_event_for_prompt(
            execution_events,
            current_step_index,
        ),
        "successful_document_reads": _successful_document_reads_for_prompt(
            execution_events, current_step_index
        ),
        "stalled_step_context": [dict(item) for item in stalled_step_context],
        "current_file": current_file_context,
    }
    if observer_intervention is not None:
        prompt_data["observer_intervention"] = observer_intervention
    if runtime_prompt_context is not None:
        prompt_data["runtime_state"] = dict(runtime_prompt_context)
    if _step_needs_prompt_catalog(current_step, "context_types"):
        prompt_data["available_context_types"] = [
            {
                "name": context_type,
                "when_to_use": description,
            }
            for context_type, description in _context_type_catalog()
        ]
    if _step_needs_prompt_catalog(current_step, "skills"):
        prompt_data["available_skills"] = [
            {
                "name": entry.skill.name,
                "path": entry.path.name,
                "adversarial": entry.skill.adversarial,
            }
            for entry in catalog
        ]
    prompt_data["available_actions"] = [
        name
        for name, _instructions in _step_actions(
            current_step,
            execution_events=execution_events,
            step_index=current_step_index,
        )
    ]
    if failed_action is not None:
        successful_reads = _successful_document_reads_for_prompt(
            execution_events, current_step_index
        )
        prompt_data["recovery_required"] = {
            "rejected_action": json.loads(_workflow_action_signature(failed_action)),
            "reason": failure_reason
            or "The previous action was rejected by the workflow contract.",
            "must_choose_different_action": True,
            "allowed_actions": prompt_data["available_actions"],
            "successful_document_reads": successful_reads,
            "instruction": (
                "Do not repeat the rejected action, even with different prose. "
                "Choose one materially different action from allowed_actions, "
                "or return prompt_user if no allowed action can safely resolve "
                "the reported issue."
            ),
        }
        if successful_reads:
            prompt_data["recovery_required"]["instruction"] += (
                " These documents were already read successfully; do not reread "
                "them unless the failed action specifically requires changed file "
                "contents: "
                + ", ".join(str(item["path"]) for item in successful_reads)
                + "."
            )
    if "edit" in prompt_data["available_actions"]:
        prompt_data["edit_contract"] = (
            "For edit, return exactly one JSON object with action=edit, a string "
            "file_path, and a non-empty edits array. Each edit must be an object "
            "with kind add, remove, or replace; replace requires positive integer "
            "start_line and end_line plus a string text. Do not use yaml_edit, "
            "file_edits, operations, or a nested parameters object."
        )
    required_output_names = [
        output.name for output in getattr(current_step, "outputs", ())
    ]
    if required_output_names:
        prompt_data["required_output_names"] = required_output_names
        prompt_data["output_contract"] = (
            "When choosing next_step, include outputs with exactly these names: "
            + ", ".join(required_output_names)
            + ". Every edit output must be present even when no changes are needed; "
            'use {"added":[],"deleted":[]} for no changes.'
        )
    if validation_gate is not None:
        prompt_data["validation_gate"] = dict(validation_gate)
    pre_step_event = _latest_deterministic_pre_step(
        execution_events,
        skill_name=selected_skill.skill.name,
        step_index=current_step_index,
    )
    if pre_step_event is not None:
        bounded_pre_step_event = prune_execution_events(
            [pre_step_event], include_results=True
        )[0]
        prompt_data["deterministic_context"] = {
            "source": bounded_pre_step_event["action"],
            "scope": bounded_pre_step_event["template"],
            "result": bounded_pre_step_event["result"],
        }
    return [
        {
            "role": "system",
            "content": _modular_action_system_prompt(
                current_step,
                interaction_style=interaction_style,
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                prompt_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def _action_system_prompt(*, current_step: Any | None = None) -> str:
    predicated_step = (
        current_step is not None and behavior_for_step(current_step).is_predicated
    )
    completion_guidance = (
        "- predicated completion: never return next_step. Choose one declared "
        "work action and include completed handoff values in the top-level "
        "outputs object, for example "
        '{"action":"emit_outputs","outputs":{"result":{}}}. '
        "The runtime advances automatically as soon as every required output "
        "is present; if more work is needed, omit that output and continue.\n"
        if predicated_step
        else "- next_step: choose this when the current step is complete and the next "
        "skill step should receive the accumulated context.\n"
    )
    context_completion_guidance = (
        ""
        if predicated_step
        else (
            "After gathering context, include the relevant findings in "
            "decisions_and_context and report next_step when the current step is "
            "complete; do not leave the gathered result only in the tool history.\n"
        )
    )
    goto_next_action_guidance = (
        ""
        if predicated_step
        else (
            "goto_step takes a step_id matching an id on a step in the current "
            "skill; next_step has no action-specific fields; "
        )
    )
    next_step_example = (
        ""
        if predicated_step
        else (
            '{"action":"next_step","decisions_and_context":"...",'
            '"llm_type":"standard_reasoning"}\n'
        )
    )
    required_action_guidance = ""
    if predicated_step:
        requirements = tuple(
            getattr(getattr(current_step, "completion", None), "required_actions", ())
        )
        if requirements:
            obligation_lines = []
            for requirement in requirements:
                cardinality = (
                    f" exactly {requirement.exactly} time(s)"
                    if requirement.exactly is not None
                    else " at least once"
                )
                parameters = (
                    " with parameters "
                    + json.dumps(requirement.parameters, sort_keys=True)
                    if requirement.parameters is not None
                    else ""
                )
                target = (
                    f" for every target from {requirement.targets_from}"
                    if requirement.targets_from is not None
                    else ""
                )
                obligation_lines.append(
                    f"  - {requirement.action}{cardinality}{parameters}{target}"
                )
            required_action_guidance = (
                "Before emit_outputs, complete every required action obligation "
                "below. Do not emit outputs early; the runtime will reject them.\n"
                + "\n".join(obligation_lines)
                + "\n"
            )
    if current_step is None or _step_needs_prompt_catalog(
        current_step, "context_types"
    ):
        context_type_lines = "\n".join(
            f"- {name}: {description}" for name, description in _context_type_catalog()
        )
    else:
        context_type_lines = (
            "Context-type descriptions are omitted because this step does not "
            "request context."
        )
    return (
        "Task: execute the current checked-in skill step using the current step, "
        "prior step context, transcript, execution events, available tools, and "
        "latest_action result, and current file context in the user message. "
        "Choose the single next action "
        "that makes the most progress without asking for information already "
        "available.\n"
        "If stalled_step_context is non-empty, the current step is a fresh retry "
        "after its prior actions were discarded. Treat each recorded stalled "
        "action and its reason as a hard constraint: do not return the same action "
        "again, even with different narrative context. Choose a materially "
        "different action or a different valid route through the current step.\n"
        "When current_step.uses_skill is present, that skill runs automatically "
        "in the same worktree before you continue the current step. Use invoke_skill "
        "only for an additional listed skill that the current step discovers it "
        "needs.\n"
        "Choose exactly one outcome and use it for the following reason:\n"
        "- gather_context: choose this when checked-in specifications or other "
        "repository context must be discovered before deciding or acting.\n"
        + context_completion_guidance
        + "When a feature's proposal must be scoped, pass its feature_id to "
        "gather_context. It includes current specifications and only YAML files "
        "under docs/proposals/<feature_id>. Do not use fuzzy-match to locate the "
        "feature proposal or substitute another feature's proposal.\n"
        "- prompt_user: choose this only when a specific human decision or fact "
        "is genuinely required to continue; ask exactly one clear question.\n"
        "- edit: choose this when the current file context is sufficient and the "
        "next action is a line-based file change.\n"
        "- file_management: choose this to move or rename one existing "
        "regular file. Paths must be relative to the current worktree, must not "
        "contain '..', and move/rename requires destination_path.\n"
        "- delete_file: choose this to delete one existing regular file; provide "
        "only its relative file_path.\n"
        "- invoke_skill: choose this when a listed skill should run as a nested "
        "workflow before continuing. It inherits the current context and LLM "
        "provider role by default, including the current skill's adversarial "
        "role. Pass the current decisions and context to the nested skill; "
        'set provider_role="adversarial" to '
        "run this skill and its descendants with the adversarial provider, or "
        'provider_role="normal" to return to the normal provider. '
        "its descendants. Set clean=true only when the skill must receive only the "
        "explicit context list (and decisions_and_context) and must not return "
        "its gathered context to the caller.\n"
        "- goto_step: choose this when the current step explicitly says to repeat "
        "work. Set step_id to the labeled target step and include the progress "
        "that proves why another iteration is needed. The target must be a prior "
        "step in this skill; never jump to the current or a later step. Use it "
        "until the current step's stated completion condition is satisfied. Never "
        "use an unknown step_id or jump without making progress.\n"
        "- invoke_tool: choose this only when the current step's explicitly "
        "listed tool_invocations support the tool needed for the next action.\n"
        "- read_document: choose this when you know the document path but need "
        "specific lines from that document before deciding the next action. "
        "- list_files: choose this when you need to discover exact files in a "
        "directory; provide directory, optional glob pattern, and recursive. "
        "Request only the smallest useful contiguous range. If read_document "
        "reports that a file does not exist, do not retry that same path. Use "
        "list_files on the existing parent directory and then read one exact "
        "returned path; if the error lists candidate files, choose only one of "
        "those exact paths. Never synthesize a filename from a task id, template "
        "id, package name, or related name.\n"
        + completion_guidance
        + required_action_guidance
        + "- complete: choose this when the skill has finished and no more action "
        "is required. Every later gate in this skill must already have passed; "
        "you cannot complete while a gate remains further ahead.\n"
        + "If the observer intervention recommends an action, treat that action as "
        "allowed for this step and choose it directly when appropriate.\n"
        "When the current step declares outputs, provide the completed values "
        "in an outputs object using exactly those declared names. A later step "
        "receives only validated handoff inputs; do not rely on hidden transcript "
        "history.\n"
        "Response: return exactly one JSON object with a top-level action field, "
        "matching exactly one of these outcome shapes. Include "
        "decisions_and_context when there is information "
        "a later step needs. Include llm_type only when the next roundtrip needs "
        "a different capability; otherwise use null or omit it.\n"
        "Response field requirements by outcome: gather_context requires a non-"
        "empty types array and may include keywords and filters mappings; "
        "prompt_user requires "
        "text containing exactly one clear English question ending in '?'; edit "
        "requires either file_path plus a non-empty edits array or a non-empty "
        "file_edits array, with each edit using add, remove, or replace and valid "
        "line numbers. Edit line numbers are 1-based: start_line and end_line "
        "must be positive integers (1 or greater), never 0; end_line must be "
        "greater than or equal to start_line. Prefer yaml_edit for .yaml or .yml "
        "files, but use edit as "
        "a fallback when a structural operation cannot express the repair. "
        "file_management requires operation (move or rename) and "
        "file_path; move and rename also require destination_path.\n"
        "invoke_tool requires a tool listed in the current step's "
        "tool_invocations. Shell and internal require parameters.command as a "
        "non-empty string or string array. The intrinsic git and gh tools use "
        "parameters.operation and never accept a shell command array. "
        "Every builtin tool accepts parameters.help = true without its "
        "normal command arguments; use it to discover that tool's parameters, "
        "examples, and when to use it. A help response is informational and does "
        "not satisfy a required successful tool invocation. "
        "For git use a registered operation such as status, add, commit, or push; "
        "for gh use only pr_view, pr_diff, pr_checks, pr_create, pr_edit, "
        "pr_comments, or pr_review_comment. "
        "For pr_create and pr_edit, provide only title and body. The runtime "
        "determines the repository, current branch, base branch, and edit target; "
        "never provide pr_reference, head, or base for those operations. "
        "basedpyright-symbol uses operation=resolve_symbol with parameters.query "
        "and optional parameters.limit; basedpyright-structure uses "
        "operation=inspect_structure with parameters.path; yaml_edit requires "
        "a .yaml or .yml file_path and a non-empty operations array; invoke_skill "
        "takes "
        "a skill name from available_skills; "
        + goto_next_action_guidance
        + "read_document requires file_path, non-negative "
        "start_line and end_line for a range of at most 2000 lines. Line 0 means "
        "the beginning of the document, and an end_line beyond EOF is clamped; "
        "complete may include a human-readable text; any action may include an "
        "outputs object when the current step declares outputs.\n"
        '{"action":"gather_context","feature_id":"display-related-photos",'
        '"types":["requirements"],"keywords":["photo"],"filters":{"entity_type":["Service"]},'
        '"decisions_and_context":"...","llm_type":"simple_task"}\n'
        '{"action":"prompt_user","text":"...","decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        '{"action":"edit","file_path":"src/example.py",'
        '"edits":[{"kind":"replace","start_line":1,"end_line":2,'
        '"text":"..."}],"decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        '{"action":"file_management","operation":"rename",'
        '"file_path":"src/old_name.py","destination_path":"src/new_name.py",'
        '"decisions_and_context":"Renamed the file."}\n'
        "For edits across multiple files, use one edit action with "
        '"file_edits":[{"file_path":"...","edits":[...]}].\n'
        '{"action":"yaml_edit","file_path":"docs/proposals/example/implementation-specification.yaml",'
        '"operations":[{"op":"upsert_item","section":"features",'
        '"id":"feature-capture","value":{"action":"added",'
        '"description":"Capture interactions",'
        '"functional_requirements":["Store input and output"]}}],'
        '"decisions_and_context":"...","llm_type":"standard_reasoning"}\n'
        '{"action":"invoke_tool","tool":"shell","parameters":{"command":["..."],"cwd":"...","env":{...}},"decisions_and_context":"...",'
        '"llm_type":"simple_task"}\n'
        '{"action":"invoke_skill","skill":"bootstrap-code-structure",'
        '"decisions_and_context":"...","llm_type":"standard_reasoning"}\n'
        '{"action":"invoke_skill","skill":"adversarial-review",'
        '"provider_role":"adversarial","clean":true,'
        '"context":["Review only this diff."],"decisions_and_context":"..."}\n'
        '{"action":"goto_step","step_id":"process-next-item",'
        '"decisions_and_context":"More items remain; continue with the next item."}\n'
        '{"action":"read_document","file_path":"docs/proposals/example/system-specification.yaml",'
        '"start_line":1,"end_line":80,"decisions_and_context":"...",'
        '"llm_type":"long_context"}\n'
        + next_step_example
        + '{"action":"complete","text":"...","decisions_and_context":"...",'
        '"llm_type":"high_reasoning"}\n'
        "Use gather_context when you need to discover information already "
        "specified in checked-in specs before deciding the next action.\n"
        "Use gather_context to discover what requirements are already "
        "specified, find related entities, inspect approach notes, or gather "
        "current features, decisions, risks, or proposed PRs.\n"
        "The supported context types are:\n"
        f"{context_type_lines}\n"
        "Use keywords to narrow results to items that mention one or more "
        "words. Use filters for exact field matching, such as "
        '{"entity_type":["Tool"],"labels":["python"]}.\n'
        "Do not use filters.work_item_name. Work-item scope comes from the "
        "document path and the current work-item context; gather_context "
        "already searches the relevant local and checked-in documents. Use "
        "keywords or item fields to narrow results within that scope.\n"
        "Use prompt_user only when you need more information to continue "
        "executing the current step.\n"
        "When work_item_context contains matches, treat those names as the "
        "canonical existing work items. Normalize case, spaces, underscores, "
        "and hyphens when matching the user's wording, reuse the exact "
        "canonical name, and do not ask the user to repeat it. Only ask for "
        "a work-item name when no available work item is a reasonable match "
        "and a new item is genuinely required.\n"
        "For start-implementing-feature, a unique normalized match under "
        "docs/proposals or docs/current establishes the canonical feature name: "
        "use the matched directory basename, even when the user's wording uses "
        "a nearby singular/plural or hyphenation variant. The execution workflow "
        "directory is deterministic: docs/workflows/<canonical-feature-name>. "
        "If it does not exist yet, report it as missing so instantiate-workflow "
        "can create it; never ask the user to choose a workflow directory or path.\n"
        "Do not ask for information already present in the transcript or "
        "execution context. Every prompt_user action must include a concise, "
        "properly formed English question in text. The question must contain "
        "meaningful words, cannot be empty or only whitespace, and must end "
        "with a question mark; never return an instruction or placeholder.\n"
        "Use edit when you know the current file should be changed and you "
        "have enough context to describe line-based removals, additions, or "
        "replacements.\n"
        "Prefer yaml_edit for YAML specification files. It preserves section keys "
        "and edits list items structurally: upsert_item uses section, id, and a "
        "complete value mapping; remove_item uses section and id, or section and "
        "index for a validator-reported boilerplate list entry; set_value uses "
        "a mapping-key path and value; remove_key deletes an exact mapping-key "
        "path. Never use set_value to delete a key or represent deletion with null. "
        "Use edit as a fallback when direct textual "
        "repair is necessary, and validate the resulting YAML afterward. Try to "
        "combine multiple independent edits "
        "to the same YAML file into one yaml_edit operations array. If yaml_edit "
        "reports a usage error, "
        "follow its corrective instructions and retry with the corrected shape.\n"
        "For YAML or JSON edits, preserve the surrounding document structure. "
        "When replacing a list item, start at the list item rather than its "
        "mapping key (for example, preserve `entities:` above `- id: ...`). "
        "For prose values containing embedded double quotes, colons, or other "
        "YAML-sensitive punctuation, use a single-quoted scalar or a `>-` "
        "block scalar; never place unescaped double quotes inside a double-"
        "quoted YAML value. "
        "After composing all line edits, ensure the complete resulting document "
        "remains valid before returning the action.\n"
        "When edit is available, current_file includes the file path and the "
        "current contents when the file is small enough to fit this prompt. "
        "For an omitted large file, use read_document to inspect the exact "
        "range before editing.\n"
        "Use invoke_skill for a listed nested skill; it runs in the same worktree "
        "and returns here when complete. Use invoke_tool for shell commands, "
        "or use the always-available intrinsic git and gh tools for repository "
        "state/staging/moves and pull-request creation/inspection. Examples: "
        '{"action":"invoke_tool","tool":"git","parameters":{"operation":"status"}} '
        "and "
        '{"action":"invoke_tool","tool":"gh","parameters":'
        '{"operation":"pr_view","pr_reference":"394"}}. '
        "fuzzy-match searches, or basedpyright "
        "symbol and structure queries.\n"
        "If unsure how to use any listed builtin tool, first invoke it with "
        'parameters {"help":true} (the tool\'s conventional --help option) '
        "and use the returned guidance.\n"
        "Use goto_step only with an id declared on a step in the current skill. "
        "The target step becomes current and receives accumulated context; the "
        "jump must identify the remaining item or changed condition requiring "
        "another pass.\n"
        "When a tool result reports validation failure, a non-zero validation "
        "status, or structured validation errors with corrective_action, do "
        "not invoke the same validation command again unchanged. First use the "
        "reported corrective_action to edit the affected document or gather the "
        "missing context; rerun validation only after a corrective action has "
        "changed or clarified the input.\n"
        "Use read_document instead of requesting or embedding an entire large "
        "document when only a section is needed. The returned line-numbered "
        "excerpt will be included in the next roundtrip context.\n"
        "The fuzzy-match tool executes in Python and returns structured JSON. "
        "Its command array starts with fuzzy-match followed by a search root and "
        "supports -name/-iname, -path/-ipath, -type f|d, -maxdepth, -mindepth, "
        "-threshold, and -print. Use -name for the natural-language query; it is "
        "fuzzy matched rather than treated as an exact glob.\n"
        "Before asking whether existing proposed PR specifications should be "
        "used, invoke fuzzy-match in the current feature specification directory "
        "with a query such as 'proposed PR specification'. Ask only after the "
        "tool result establishes whether matching files exist.\n"
        "For start-implementing-feature, the workflow template path is known and "
        "fixed: templates/execute-proposed-pr.yaml. Use it directly when invoking "
        "instantiate-workflow and never ask the user to supply or choose that path.\n"
        "A missing execute workflow is expected during start-implementing-feature: "
        "this skill creates it. If fuzzy-match finds no matching workflow, invoke "
        "instantiate-workflow immediately rather than asking the user for one.\n"
        "When the current step includes tool_invocations, choose one of those "
        "structured invocations and fill in its parameters unless the task "
        "description explicitly says otherwise. When it does not, "
        "do not return invoke_tool.\n"
        "Never return next_step or complete from a step with tool_invocations "
        "until you have invoked a declared tool for that step and received a "
        "successful result. A prose summary of the intended command is not a "
        "tool invocation; emit invoke_tool and wait for its result.\n"
        "Use next_step when the current step is complete and the next step "
        "should receive the accumulated context.\n"
        "When a step declares tool_invocations, next_step and complete are "
        "invalid until a declared tool has been invoked successfully for that "
        "step.\n"
        "Use complete when the skill is finished.\n"
        "For invoke_tool steps with a deterministic pre-step, the pre-step already "
        "ran. The deterministic_context field in the step prompt contains its "
        "result; do not invoke the pre-step again. Use the result and current step "
        "details before choosing next_step or complete.\n"
        "Always include decisions_and_context with the concise information "
        "future steps will need. Keep it to one short sentence explaining why "
        "you chose this action or what it enables next; it is shown in progress "
        "status after every roundtrip.\n"
        "Always include llm_type to select the model for the next roundtrip. "
        "Use high_reasoning for architecture, difficult reasoning, and final "
        "review; standard_reasoning for normal implementation; simple_task "
        "for mechanical work; fast_iteration for quick feedback; long_context "
        "for large specifications; and vision for image-oriented tasks.\n"
        "Do not output markdown."
    )


def _modular_action_system_prompt(
    current_step: Any,
    *,
    interaction_style: str | None = None,
) -> str:
    """Build a compact action prompt with explicitly selected guidance sections."""
    step_actions = _step_actions(current_step)
    action_names = {name for name, _ in step_actions}
    include_context = _step_needs_prompt_catalog(current_step, "context_types")
    include_skills = _step_needs_prompt_catalog(current_step, "skills")
    action_lines = "\n".join(
        f"- {name}: {instructions}" for name, instructions in step_actions
    )
    prompt = (
        "Task: execute the supplied details using the handoff inputs, latest action "
        "result, and available actions. Choose exactly one action.\n"
        "Available actions for this step (and only this step; choose exactly one):\n"
        + action_lines
        + "\nThe current-step contract below is authoritative. Do not use action "
        "instructions or action names from any previous step.\n"
        "next_step is always allowed and is listed with its default completion "
        "behavior below. Required outputs add exact handoff requirements.\n" + "\n"
        "If the current-step contract lists required outputs, the advancing action "
        "must also include an outputs object containing every required output under "
        "its exact declared name. A statement in decisions_and_context is not an "
        "output. For example, a step requiring work_item_name must return: "
        '{"action":"next_step","outputs":{"work_item_name":"interaction-file-log"},'
        '"decisions_and_context":"Captured the feature name."}.\n'
        "The outputs object is scoped to the current step only: never copy an "
        "output name produced by a previous step, and never include a name that "
        "is absent from the current-step contract.\n"
        "Return exactly one JSON object with a top-level action field. The action "
        "field is the discriminator. Include "
        "decisions_and_context when a later step needs it, and include outputs "
        "using the declared names. A completed step is represented as: "
        '{"action":"next_step","decisions_and_context":"The current step "'
        '"is complete."}.\n'
        "Use the field names required by the selected action and do not combine "
        "actions.\n"
        "If the user payload contains recovery_required, it is authoritative: the "
        "previous action was rejected. Do not repeat its action or parameters. "
        "Return one materially different action from allowed_actions, or use "
        "prompt_user when no safe contract-valid action is available.\n"
    )
    prompt += _interaction_style_prompt(interaction_style)
    if "invoke_tool" in action_names:
        prompt += (
            "A declared internal command is represented as: "
            '{"action":"invoke_tool","tool":"internal","parameters":{"command":'
            '["powdrr-lift","system-specification","--work-item-name",'
            '"example-feature"]},"decisions_and_context":"Generated the '
            'system template."}.\n'
        )
    if "prompt_user" in action_names:
        prompt += (
            "prompt_user requires the question in the text field. Example: "
            '{"action":"prompt_user","text":"What specific success criteria '
            'should this feature meet?","decisions_and_context":"More information '
            'is required before continuing."}.\n'
        )
    if "file_management" in action_names:
        prompt += (
            "file_management uses operation move or rename plus a relative "
            "file_path; move and rename also require destination_path. Never use '..' "
            "or absolute paths.\n"
        )
    if "delete_file" in action_names:
        prompt += (
            "delete_file uses only a relative file_path. Never use '..' or "
            "absolute paths.\n"
        )
    if "goto_step" in action_names:
        prompt += "Use goto_step only with a declared prior step id.\n"
    if include_context:
        context_type_lines = "\n".join(
            f"- {name}: {description}" for name, description in _context_type_catalog()
        )
        prompt += (
            "Context guidance: use gather_context to discover checked-in specs. For a "
            "feature proposal, pass the exact feature_id and do not use fuzzy-match to "
            "substitute another proposal. After gathering, put relevant findings in "
            "decisions_and_context and report next_step when complete. Use keywords to "
            "narrow results and filters for exact fields; never use "
            "filters.work_item_name. Supported context types:\n"
            f"{context_type_lines}\n"
            "Use the exact token entity-relationships when requesting entity "
            "relationships; do not abbreviate it as relationships. Architecture "
            "context is requested with entities, entity-relationships, invariants, "
            "and guidance; do not use architecture as a context type.\n"
            'Example: {"action":"gather_context","feature_id":"display-related-photos",'
            '"types":["requirements"],"keywords":["photo"]}.\n'
        )
    if include_skills:
        prompt += (
            "Nested-skill guidance: use invoke_skill only for a listed skill. It "
            "inherits the current provider role by default; set provider_role to "
            "adversarial or normal when needed. Set clean=true only when the nested "
            "skill should receive only explicit context.\n"
            'Example: {"action":"invoke_skill","skill":"adversarial-review",'
            '"provider_role":"adversarial","clean":true}.\n'
        )
    nested_skill = getattr(current_step, "uses_skill", None)
    if nested_skill is not None:
        prompt += (
            "This step delegates to a nested skill; use invoke_skill, "
            "not invoke_tool or an internal CLI command. The only listed nested "
            f"skill for this step is {json.dumps(nested_skill.skill)}. For "
            "example: "
            '{"action":"invoke_skill","skill":'
            f"{json.dumps(nested_skill.skill)}"
            ',"decisions_and_context":"The nested skill should perform its '
            'declared work."}.\n'
        )
    if getattr(current_step, "pre_step", None) is not None:
        prompt += (
            "Deterministic context: the resolved pre_step template has already run. "
            "The deterministic_context field is the context for this step; do not "
            "invoke the pre-step again. invoke_tool is not allowed in this step. "
        )
        if "complete" in action_names:
            prompt += (
                "Use the result and choose next_step; choose complete only when the "
                "skill itself is finished.\n"
            )
        else:
            prompt += (
                "Use the result and choose next_step when this step is finished.\n"
            )
    if _validation_gate_enabled(current_step):
        prompt += (
            "This step has a runtime validation gate. Run every discovered obligation "
            "using the exact action in validation_gate. You cannot choose next_step, "
            + ("goto_step, " if "goto_step" in action_names else "")
            + ("or complete " if "complete" in action_names else "")
            + "until every obligation passes in the current "
            "epoch. If any obligation fails, apply its corrective action; the runtime "
            "will reset the epoch and require every obligation to run again.\n"
            + (
                "gather_context remains allowed while repairing a failed obligation. "
                "Use it when the latest validator result reports a missing or unknown "
                "id; "
                "use the validator's suggested context types and keywords, then apply "
                "the correction.\n"
                if "gather_context" in action_names
                else ""
            )
            + "Validation repair protocol: a failed result is a diagnosis, not "
            "permission to repeat the same edit. First inspect the exact current "
            "file and full validator result, map each issue to its reported path, "
            "and apply a structural correction at that path. Never repeat an "
            "operation or semantically equivalent operation that produced the same "
            "issue fingerprint. Preserve fields not named by an issue, combine "
            "independent fixes in one yaml_edit, and wait for the deterministic "
            "obligation rerun before claiming progress. If the latest issue state "
            "is unchanged or worse, change the target or repair strategy; do not keep "
            "retrying the same action.\n"
        )
    coding_loop = getattr(current_step, "coding_loop", None)
    if coding_loop is not None:
        verification = json.dumps(
            [item.to_data() for item in coding_loop.verification], ensure_ascii=False
        )
        stopping = json.dumps(list(coding_loop.stopping_conditions), ensure_ascii=False)
        prompt += (
            "Coding-loop protocol: work toward the declared goal "
            f"{coding_loop.goal!r}, inspect the "
            "current implementation, make the smallest justified edits, and run "
            f"each declared verification item {verification}. Stop only when the "
            f"declared stopping conditions {stopping} are satisfied. This loop is "
            f"bounded to {coding_loop.max_iterations} model iterations; use the "
            "latest verification result as evidence and repair failures before "
            "choosing next_step. Do not claim verification passed without a tool "
            "result. If no verification result exists yet, invoke the declared "
            "command first so you can observe the baseline, including failures. "
            "When the latest verification result says all checks passed, choose "
            "next_step immediately; do not make another edit, reread files, or "
            "rerun an already-passing check. "
            "After a failed verification, prioritize an edit or a targeted test "
            "that addresses the reported failure; do not repeat the same "
            "read_document action unless the failure identifies information that "
            "the prior read did not contain. "
            "For shell/process tools, use an argv array and omit cwd or use a "
            "path relative to the active worktree; do not use shell operators, "
            "absolute paths, or commands that assume an unverified filename. "
            "Use the returned error/output to choose the next edit, then let the "
            "automatic coding-loop verification run after each edit.\n"
        )
    if "goto_step" in action_names or "complete" in action_names:
        prompt += (
            "Transition rules are enforced by the runtime: "
            + (
                "goto_step may target only a prior step in this skill, never the "
                "current or a later step. "
                if "goto_step" in action_names
                else ""
            )
            + (
                "complete is invalid while any gate remains later in this skill."
                if "complete" in action_names
                else ""
            )
            + "\n"
        )
    if "read_document" in action_names:
        prompt += (
            "read_document guidance: use the smallest useful range of a large "
            "document; it requires file_path, non-negative start_line and end_line "
            "for at most 2000 lines, with line 0 meaning the beginning and an "
            "end_line past EOF clamped.\n"
        )
    if "edit" in action_names:
        prompt += (
            "edit guidance: use valid line edits; prefer yaml_edit for YAML only "
            "when yaml_edit is also listed, but edit is allowed as a fallback when "
            "structural operations cannot express the repair.\n"
        )
    if "yaml_edit" in action_names:
        prompt += (
            "yaml_edit guidance: combine independent corrections in one operations "
            "array and preserve document structure. A set_value path is a JSON array "
            'of mapping keys, such as ["title"] or ["metadata","owner"], never a '
            "JSON pointer such as /title. For list sections, use one upsert_item per "
            "item with section, id, and a complete value mapping. Return yaml_edit "
            'directly with action="yaml_edit"; never wrap it in invoke_tool. '
            "Before proposing yaml_edit, verify that file_path exists using the "
            "declared read/list action or a generator result; yaml_edit cannot create "
            "a missing document, and inventing a filename is invalid.\n"
        )
    if current_step.tool_invocations and "invoke_tool" in action_names:
        prompt += (
            "Tool guidance: invoke one of the declared tool_invocations successfully "
            "before next_step or complete. A prose summary is not a tool invocation.\n"
        )
    if (
        not behavior_for_step(current_step).invokes_llm
        and getattr(current_step, "pre_step", None) is None
    ):
        prompt += (
            "This is an atomic invoke_tool step. Invoke its single declared tool, "
            "then choose next_step after the successful result; do not perform "
            "additional reasoning or edits in this step.\n"
        )
    prompt += (
        "The worktree is the command root; use relative paths. Do not output markdown."
    )
    return prompt


def _context_type_catalog() -> tuple[tuple[str, str], ...]:
    return (
        ("requirements", "discover what requirements are already specified"),
        ("approach", "discover the existing approach or solution shape"),
        ("entities", "discover the domain entities already described"),
        (
            "entity-relationships",
            "discover how entities are already related",
        ),
        ("invariants", "discover the rules that must always remain true"),
        ("guidance", "discover implementation guidance or cautions"),
        ("features", "discover the features already recorded or in scope"),
        (
            "human-decisions",
            "discover human decisions that must be preserved",
        ),
        ("intent", "discover the problem, goal, or reasoning already stated"),
        ("intents", "discover current-state intent records"),
        (
            "acceptance_criteria",
            "discover acceptance criteria already written down",
        ),
        ("expected_tests", "discover expected tests already listed"),
        ("required_test_cases", "discover required test cases already listed"),
        ("expected_outcomes", "discover expected outcomes already stated"),
        ("non_goals", "discover what is explicitly out of scope"),
        ("risks", "discover open risks or concerns"),
        ("decisions", "discover recorded decisions or tradeoffs"),
        ("proposed_prs", "discover proposed PR records and their status"),
        ("modules", "discover project modules and their locations"),
        ("tools", "discover project tools and validation commands"),
    )


def _workflow_action_handlers() -> dict[
    str,
    Callable[
        [
            SkillChatAction,
            _WorkflowExecutionState,
            TextIO,
            TextIO,
            Callable[[], str],
            WorkflowChatConfig,
        ],
        bool,
    ],
]:
    return {
        "complete": _handle_workflow_action_complete,
        "edit": _handle_workflow_action_edit,
        "yaml_edit": _handle_workflow_action_yaml_edit,
        "file_management": _handle_workflow_action_file_management,
        "delete_file": _handle_workflow_action_file_management,
        "read_document": _handle_workflow_action_read_document,
        "list_files": _handle_workflow_action_list_files,
        "next_step": _handle_workflow_action_next_step,
        "emit_outputs": _handle_workflow_action_emit_outputs,
        "prompt_user": _handle_workflow_action_prompt_user,
        "invoke_tool": _handle_workflow_action_invoke_tool,
        "gather_context": _handle_workflow_action_gather_context,
    }


def _current_file_contents(state: _WorkflowExecutionState) -> str | None:
    if state.current_file_path is None or not state.current_file_path.exists():
        return None
    return state.current_file_path.read_text(encoding="utf-8")


def _material_file_contents(path: Path) -> str | None:
    """Return semantic file contents for progress comparisons.

    YAML comments, key ordering, and whitespace are not workflow progress.
    Invalid YAML falls back to raw text so a repair can still make the file
    parseable.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.suffix.casefold() not in {".yaml", ".yml"}:
        return raw
    try:
        return json.dumps(yaml.safe_load(raw), sort_keys=True, default=str)
    except yaml.YAMLError:
        return raw


def _last_user_message(state: _WorkflowExecutionState) -> str | None:
    if not state.transcript or state.transcript[-1]["role"] != "user":
        return None
    return state.transcript[-1]["content"]


def _workflow_action_material_state(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
) -> object:
    """Return only state that proves this action changed the workflow.

    Tool transcript entries are observations, not progress.  Counting them made
    an unchanged tool call appear productive forever and let the prompt grow
    without bound.  This mirrors durable task execution's edit-only material
    state snapshot while retaining the genuinely interactive user response.
    """
    if action.kind in {"edit", "yaml_edit"}:
        file_paths = (
            tuple(group.file_path for group in action.file_edits)
            if action.file_edits
            else ((action.file_path,) if action.file_path is not None else ())
        )
        return tuple(
            (
                file_path,
                (
                    _material_file_contents(target_path)
                    if (
                        target_path := _resolve_worktree_file_path(
                            file_path,
                            state.worktree_root,
                        )
                    ).exists()
                    else None
                ),
            )
            for file_path in file_paths
        )
    if action.kind in {"file_management", "delete_file"}:
        return (action.file_operation, action.file_path, action.destination_path)
    if action.kind == "prompt_user":
        return _last_user_message(state)
    if action.kind == "goto_step":
        return (action.step_id, state.step_index)
    return None


def _step_index_by_id(skill: SkillCatalogEntry, step_id: str | None) -> int:
    if step_id is None:
        raise PowdrrExecutionError("Workflow goto_step action must include step_id.")
    for index, step in enumerate(skill.skill.steps):
        if step.id == step_id:
            return index
    raise PowdrrExecutionError(
        f"Workflow goto_step target {step_id!r} is not declared in skill "
        f"{skill.skill.name!r}."
    )


def _workflow_action_signature(action: SkillChatAction) -> str:
    return _shared_workflow_action_signature(action)


def _workflow_action_progress_status(action: SkillChatAction) -> str | None:
    if action.kind in {"edit", "yaml_edit"}:
        return "Attempting file edit"
    if action.kind in {"file_management", "delete_file"}:
        return f"Managing file: {action.file_operation} {action.file_path}"
    if action.kind == "read_document":
        return "Reading file"
    if action.kind == "gather_context":
        return "Gathering structured context"
    if action.kind == "invoke_tool":
        command = action.parameters.get("command")
        if isinstance(command, str):
            command_line = command
        elif isinstance(command, Sequence) and not isinstance(
            command, (str, bytes, bytearray)
        ):
            command_line = shlex.join(str(item) for item in command)
        else:
            command_line = action.tool or "tool"
        return f"Invoking {command_line}"
    return None


def _worktree_relative_path(path: Path, worktree_root: Path) -> str:
    try:
        return path.resolve().relative_to(worktree_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _workflow_step_requires_pull_request(step: Any) -> bool:
    return any(
        invocation.operation == "pr_create"
        or tuple(invocation.command[:3]) == ("gh", "pr", "create")
        for invocation in step.tool_invocations
    )


def _handle_workflow_action_complete(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stderr
    _ = input_func
    _ = config
    if action.text:
        print(action.text, file=stdout)
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_events.append(
        {
            "kind": action.kind,
            "text": action.text,
            "decisions_and_context": action.decisions_and_context,
        }
    )
    return False


def _handle_workflow_action_edit(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    _ = config
    file_edits = action.file_edits
    if not file_edits:
        if action.file_path is None:
            raise PowdrrExecutionError("Workflow edit action must include file_path.")
        file_edits = (SkillChatFileEdits(action.file_path, action.edits),)

    pending_writes: list[tuple[Path, str]] = []
    results: list[dict[str, Any]] = []
    for file_edit in file_edits:
        target_path = _resolve_worktree_file_path(
            file_edit.file_path,
            state.worktree_root,
        )
        current_text = ""
        if target_path.exists():
            current_text = target_path.read_text(encoding="utf-8")
        state.current_file_path = target_path
        updated_text = _apply_file_edits(current_text, file_edit.edits)
        updated_text = _normalize_structured_document_text(target_path, updated_text)
        _validate_structured_document_text(target_path, updated_text)
        pending_writes.append((target_path, updated_text))
        results.append(
            {
                "file_path": str(target_path),
                "line_count": len(updated_text.splitlines()),
            }
        )

    invoke_file_mutation(
        tuple(
            _worktree_relative_path(target_path, state.worktree_root)
            for target_path, _updated_text in pending_writes
        ),
        worktree_root=state.worktree_root,
        executor=lambda: _write_pending_file_mutations(pending_writes),
        runtime=_ensure_execution_runtime(state),
    )
    if state.file_added_callback is not None:
        state.file_added_callback(
            tuple(
                _worktree_relative_path(target_path, state.worktree_root)
                for target_path, _updated_text in pending_writes
            )
        )
    state.fuzzy_match_cache.clear()

    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    action_data = {
        "kind": action.kind,
        "file_edits": [_file_edits_to_data(group) for group in file_edits],
    }
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(action_data, ensure_ascii=False),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps({"edit_result": results}, ensure_ascii=False),
        }
    )
    state.execution_events.append(
        {
            "kind": action.kind,
            "file_edits": [_file_edits_to_data(group) for group in file_edits],
            "result": results,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    for result in results:
        print(f"Edited file: {result['file_path']}", file=stdout)
        _verbose_print(
            stderr,
            config.verbose,
            f"Applied edit to {result['file_path']}",
        )
    return True


def _write_pending_file_mutations(pending_writes: Sequence[tuple[Path, str]]) -> None:
    for target_path, updated_text in pending_writes:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(updated_text, encoding="utf-8")


def _handle_workflow_action_yaml_edit(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    if action.file_path is None:
        raise _WorkflowYamlEditError(
            "yaml_edit requires file_path. Use a repository-relative .yaml or "
            ".yml path."
        )
    target_path = _resolve_worktree_file_path(action.file_path, state.worktree_root)
    if not target_path.exists():
        raise _WorkflowYamlEditError(
            f"yaml_edit target {action.file_path!r} does not exist; no file was "
            "changed. Read or generate the YAML document before applying "
            "structural edits."
        )
    state.current_file_path = target_path
    current_text = target_path.read_text(encoding="utf-8")
    updated_text = _apply_yaml_operations(
        target_path,
        current_text,
        action.yaml_operations,
    )
    _validate_structured_document_text(target_path, updated_text)
    invoke_file_mutation(
        (action.file_path,),
        worktree_root=state.worktree_root,
        executor=lambda: target_path.write_text(updated_text, encoding="utf-8"),
        runtime=_ensure_execution_runtime(state),
    )
    if state.file_added_callback is not None:
        state.file_added_callback(
            (_worktree_relative_path(target_path, state.worktree_root),)
        )
    state.fuzzy_match_cache.clear()

    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    action_data = {
        "kind": action.kind,
        "file_path": action.file_path,
        "operations": [
            _yaml_operation_to_data(operation) for operation in action.yaml_operations
        ],
    }
    result = {
        "file_path": str(target_path),
        "line_count": len(updated_text.splitlines()),
    }
    state.transcript.extend(
        [
            {
                "role": "assistant",
                "content": json.dumps(action_data, ensure_ascii=False),
            },
            {
                "role": "user",
                "content": json.dumps({"yaml_edit_result": result}, ensure_ascii=False),
            },
        ]
    )
    state.execution_events.append(
        {
            **action_data,
            "result": result,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    print(f"Edited YAML file: {target_path}", file=stdout)
    _verbose_print(stderr, config.verbose, f"Applied YAML edit to {target_path}")
    return True


def _handle_workflow_action_file_management(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    if action.file_operation is None or action.file_path is None:
        raise PowdrrExecutionError(
            "file_management action requires operation and file_path."
        )
    file_operation = action.file_operation
    file_path = action.file_path
    mutation_paths: tuple[str, ...] = (file_path,)
    if action.destination_path is not None:
        mutation_paths += (action.destination_path,)
    result = invoke_file_mutation(
        mutation_paths,
        worktree_root=state.worktree_root,
        executor=lambda: manage_worktree_file(
            state.worktree_root,
            operation=file_operation,
            file_path=file_path,
            destination_path=action.destination_path,
        ),
        runtime=_ensure_execution_runtime(state),
    )
    if state.file_added_callback is not None:
        changed_paths = [action.file_path]
        if action.destination_path is not None:
            changed_paths.append(action.destination_path)
        state.file_added_callback(tuple(changed_paths))
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    action_data = {
        "kind": action.kind,
        "operation": action.file_operation,
        "file_path": action.file_path,
        "destination_path": action.destination_path,
    }
    state.transcript.extend(
        [
            {"role": "assistant", "content": json.dumps(action_data)},
            {
                "role": "user",
                "content": json.dumps(
                    {"file_management_result": result}, ensure_ascii=False
                ),
            },
        ]
    )
    state.execution_events.append(
        {
            **action_data,
            "result": result,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    state.fuzzy_match_cache.clear()
    print(
        f"File {action.file_operation}: {result['file_path']}"
        + (
            f" -> {result['destination_path']}"
            if result["destination_path"] is not None
            else ""
        ),
        file=stdout,
    )
    _verbose_print(stderr, config.verbose, f"File management result: {result}")
    return True


def _handle_workflow_action_read_document(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stdout
    _ = input_func
    if action.file_path is None:
        raise PowdrrExecutionError(
            "Workflow read_document action must include file_path."
        )
    if action.start_line is None or action.end_line is None:
        raise PowdrrExecutionError(
            "Workflow read_document action must include start_line and end_line."
        )
    effective_start_line = max(1, action.start_line)
    if action.end_line < effective_start_line:
        raise PowdrrExecutionError(
            "Workflow read_document action end_line must be >= start_line."
        )
    target_path = _resolve_worktree_file_path(action.file_path, state.worktree_root)
    if not target_path.exists() or not target_path.is_file():
        directory = target_path.parent
        if directory.is_dir():
            directory_files = sorted(
                path.name for path in directory.iterdir() if path.is_file()
            )
            directory_context = (
                f" Files currently in {directory.relative_to(state.worktree_root)}: "
                f"{', '.join(directory_files) or '<no files>'}."
            )
        else:
            directory_context = (
                f" Directory does not exist: "
                f"{directory.relative_to(state.worktree_root)}."
            )
        raise PowdrrExecutionError(
            f"Workflow read_document action file does not exist: {action.file_path}."
            f"{directory_context} Use an exact existing file path; do not infer or "
            "compose a filename from the template id or workflow description."
        )
    lines = target_path.read_text(encoding="utf-8").splitlines()
    if lines and (action.end_line < 1 or action.start_line > len(lines)):
        raise PowdrrExecutionError(
            f"Workflow read_document action line range {action.start_line}-"
            f"{action.end_line} has no overlap with the document, which has "
            f"{len(lines)} lines. Request an overlapping range."
        )

    excerpt_start_line = max(1, action.start_line)
    excerpt_end_line = min(
        action.end_line,
        len(lines),
        excerpt_start_line + _MAX_DOCUMENT_CONTEXT_LINES - 1,
    )
    document_complete = not lines or (
        excerpt_start_line == 1 and excerpt_end_line == len(lines)
    )
    excerpt = {
        "path": str(target_path.relative_to(state.worktree_root)),
        "requested_start_line": action.start_line,
        "requested_end_line": action.end_line,
        "start_line": excerpt_start_line,
        "end_line": excerpt_end_line,
        "document_line_count": len(lines),
        "document_complete": document_complete,
        "next_start_line": None if document_complete else excerpt_end_line + 1,
        "lines": [
            {
                "line_number": line_number,
                "text": lines[line_number - 1],
            }
            for line_number in range(excerpt_start_line, excerpt_end_line + 1)
        ],
    }
    excerpt_text = json.dumps(excerpt, ensure_ascii=False)
    action_data = {
        "kind": action.kind,
        "file_path": action.file_path,
        "start_line": action.start_line,
        "end_line": action.end_line,
    }
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(action_data, ensure_ascii=False),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps(
                {"document_context": excerpt},
                ensure_ascii=False,
            ),
        }
    )
    state.execution_context.append(f"Document context: {excerpt_text}")
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_events.append(
        {
            "kind": action.kind,
            "file_path": action.file_path,
            "start_line": action.start_line,
            "end_line": action.end_line,
            "result": excerpt,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    _verbose_print(
        stderr,
        config.verbose,
        f"Read document context {action.file_path}:{action.start_line}-"
        f"{action.end_line}",
    )
    return True


def _list_worktree_files(
    directory_value: str,
    pattern: str | None,
    recursive: bool,
    worktree_root: Path,
) -> dict[str, Any]:
    directory = _resolve_worktree_file_path(directory_value, worktree_root)
    if not directory.exists() or not directory.is_dir():
        raise PowdrrExecutionError(
            f"Workflow list_files directory does not exist: {directory_value}."
        )
    normalized_pattern = pattern or "*"
    paths = (
        directory.rglob(normalized_pattern)
        if recursive
        else directory.glob(normalized_pattern)
    )
    return {
        "directory": directory_value,
        "pattern": normalized_pattern,
        "recursive": recursive,
        "files": sorted(
            str(path.relative_to(worktree_root)) for path in paths if path.is_file()
        ),
    }


def _handle_workflow_action_list_files(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = (stdout, input_func)
    directory = action.directory or "."
    result = invoke_repository_read(
        "list_files",
        {
            "directory": directory,
            "pattern": action.pattern,
            "recursive": action.recursive,
        },
        worktree_root=state.worktree_root,
        executor=lambda _arguments: _list_worktree_files(
            directory,
            action.pattern,
            action.recursive,
            state.worktree_root,
        ),
    )
    action_data = {
        "kind": action.kind,
        "directory": directory,
        "pattern": result["pattern"],
        "recursive": action.recursive,
    }
    state.transcript.extend(
        [
            {"role": "assistant", "content": json.dumps(action_data)},
            {
                "role": "user",
                "content": json.dumps({"list_files_result": result}),
            },
        ]
    )
    state.execution_events.append(
        {
            **action_data,
            "result": result,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    _verbose_print(stderr, config.verbose, f"Listed files: {result}")
    return True


def _handle_workflow_action_next_step(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stdout
    _ = stderr
    _ = input_func
    _ = config
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_events.append(
        {
            "kind": action.kind,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    state.step_index += 1
    return True


def _handle_workflow_action_emit_outputs(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stdout, stderr, input_func, config
    state.execution_events.append(
        {
            "kind": action.kind,
            "outputs": dict(action.outputs),
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    return True


def _handle_workflow_action_prompt_user(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    print(action.text or "", file=stdout)
    answer = _prompt_user(
        "> ",
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )
    _verbose_print(stderr, config.verbose, f"Follow-up answer: {answer}")
    state.transcript.append(
        {
            "role": "assistant",
            "content": action.text or "",
        }
    )
    state.transcript.append({"role": "user", "content": answer})
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_events.append(
        {
            "kind": action.kind,
            "text": action.text,
            "answer": answer,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    return True


def _handle_workflow_action_invoke_tool(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    if action.tool == "fuzzy-match":
        tool_result = invoke_fuzzy_match_capability(
            action.parameters,
            worktree_root=state.worktree_root,
            path_cache=state.fuzzy_match_cache,
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool == ENRICH_TOOL:
        tool_result = invoke_intrinsic_capability(
            ENRICH_TOOL,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool == VALIDATE_EDIT_TOOL:
        tool_result = invoke_deferred_edit_capability(
            VALIDATE_EDIT_TOOL,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool == APPLY_EDIT_TOOL:
        tool_result = invoke_deferred_edit_capability(
            APPLY_EDIT_TOOL,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool in {"shell", _INTERNAL_TOOL}:
        if action.tool == _INTERNAL_TOOL and action.parameters.get("help") is not True:
            _validate_internal_command(action.parameters.get("command"))
        command_items = (
            []
            if action.parameters.get("help") is True
            else _command_items_for_validation(action.parameters.get("command"))
        )
        tool_result = invoke_shell_capability(
            {**action.parameters, "_tool_name": action.tool},
            worktree_root=state.worktree_root,
            executor=lambda invocation: _execute_shell_tool(
                dict(invocation),
                worktree_root=state.worktree_root,
                stdout=stdout,
                stderr=stderr,
                verbose=config.verbose,
                announce=False,
                print_stdout=not (
                    action.tool == _INTERNAL_TOOL
                    and command_items[1:2] == ["pull-request-description"]
                ),
            ),
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool in {GIT_TOOL, GH_TOOL}:
        if action.tool == GH_TOOL and action.parameters.get("operation") == "pr_create":
            _ensure_execution_runtime(state).require_publish_readiness()
        tool_result = invoke_intrinsic_capability(
            action.tool,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
        if tool_result.get("stdout"):
            print(str(tool_result["stdout"]), end="", file=stdout)
        if tool_result.get("stderr"):
            print(str(tool_result["stderr"]), end="", file=stderr)
    elif is_basedpyright_tool(action.tool or ""):
        assert action.tool is not None
        tool_result = invoke_basedpyright_capability(
            action.tool,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
    else:
        raise PowdrrExecutionError(
            f"Unsupported workflow tool {action.tool!r}; supported tools are shell, "
            "internal, git, gh, enrich, validate_edit, apply_edit, fuzzy-match, "
            "basedpyright-symbol, and basedpyright-structure."
        )
    if (
        action.tool == GIT_TOOL
        and action.parameters.get("operation") == "add"
        and tool_result.get("returncode") == 0
        and state.file_added_callback is not None
    ):
        paths = action.parameters.get("paths")
        if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)):
            added_paths = tuple(
                path.strip() for path in paths if isinstance(path, str) and path.strip()
            )
            if added_paths:
                state.file_added_callback(added_paths)
    if action.tool in {"shell", GH_TOOL}:
        _record_chat_pull_request(action, state, tool_result)
    inferred_path = _resolve_generated_file_path_from_command(
        action.parameters.get("command"),
        worktree_root=state.worktree_root,
    )
    if inferred_path is not None:
        state.current_file_path = inferred_path
    if action.tool == "shell":
        state.fuzzy_match_cache.clear()
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "kind": action.kind,
                    "parameters": action.parameters,
                },
                ensure_ascii=False,
            ),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps(
                {"tool_result": tool_result},
                ensure_ascii=False,
            ),
        }
    )
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    event = {
        "kind": action.kind,
        "tool": action.tool,
        "parameters": action.parameters,
        "result": tool_result,
        "decisions_and_context": action.decisions_and_context,
        "step_index": state.step_index,
    }
    state.execution_events.append(event)
    state.audit_events.append(event)
    return True


def _record_chat_pull_request(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    tool_result: Mapping[str, Any],
) -> None:
    _record_skill_pull_request(
        action,
        state.worktree_root,
        state.selected_skill,
        state.audit_events,
        tool_result,
        root_skill=state.root_skill or state.selected_skill,
        step_index=state.step_index,
        runtime=_ensure_execution_runtime(state),
    )


def _record_skill_pull_request(
    action: SkillChatAction,
    repo_root: Path,
    skill: SkillCatalogEntry,
    events: Sequence[Mapping[str, Any]],
    tool_result: Mapping[str, Any],
    runtime: ExecutionRuntime,
    root_skill: SkillCatalogEntry | None = None,
    step_index: int | None = None,
) -> None:
    command_value = action.parameters.get("command", tool_result.get("command"))
    command = _command_items_for_validation(command_value)
    if not is_pull_request_create_command(command):
        return
    if tool_result.get("returncode") != 0:
        return
    output = tool_result.get("stdout")
    if not isinstance(output, str):
        return
    number = pull_request_number(output)
    if number is None:
        raise PowdrrExecutionError(
            "GitHub did not return a pull-request URL, so the workflow record "
            "could not be named under docs/prs/<pr-number>.yaml."
        )
    event = {
        "kind": action.kind,
        "tool": action.tool,
        "parameters": action.parameters,
        "result": tool_result,
        "decisions_and_context": action.decisions_and_context,
        "step_index": step_index,
    }
    record_skill = root_skill or skill
    with runtime.without_action_contract():
        branch_result = invoke_intrinsic_capability(
            GIT_TOOL,
            {"operation": "branch_current"},
            worktree_root=repo_root,
            runtime=runtime,
        )
    branch = str(branch_result.get("stdout", "")).strip()
    if not branch:
        raise PowdrrExecutionError(
            "Could not determine the branch for the workflow record."
        )
    record_pull_request_workflow(
        repo_root,
        number,
        branch=branch,
        base_branch="main",
        title="Agent-created pull request",
        workflow_name=record_skill.skill.name,
        workflow_path=str(record_skill.path),
        steps=[step.to_data() for step in record_skill.skill.steps],
        events=[*events, event],
        explanation=(
            "This record documents the selected skill steps and the tool calls "
            "observed before the agent created the pull request."
        ),
    )


def _validate_workflow_action_for_step(
    action: SkillChatAction,
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
    observer_allowed_action: ObserverActionRecommendation | None = None,
) -> None:
    """Reject tool actions that do not match a current-step invocation."""
    allowed_actions = _declared_action_names(
        step, execution_events=execution_events, step_index=step_index
    )
    if (
        allowed_actions is not None
        and action.kind not in allowed_actions
        and action.kind not in {"prompt_user", "next_step"}
        and not observer_action_matches(action, observer_allowed_action)
        and not (
            action.kind == "complete" and not getattr(step, "actions_declared", False)
        )
        and not (
            action.kind == "invoke_tool"
            and action.tool == "internal"
            and not getattr(step, "actions_declared", False)
        )
    ):
        raise _WorkflowToolValidationError(
            ValidationError(
                code="workflow_action_not_allowed",
                message=(
                    f"The {action.kind} action is not allowed in this step. "
                    + (
                        "This step explicitly supports: none; next_step is implicit."
                        if (
                            allowed_actions == ("next_step",)
                            or (
                                action.kind == "invoke_tool"
                                and action.tool == "shell"
                                and not getattr(step, "tool_invocations", ())
                            )
                        )
                        else "Use one of: " + ", ".join(allowed_actions) + "."
                    )
                ),
                path="kind",
            )
        )
    if action.kind != "invoke_tool":
        return
    try:
        _validate_workflow_action_for_step_unwrapped(
            action,
            step,
            execution_events=execution_events,
            step_index=step_index,
            observer_allowed_action=observer_allowed_action,
        )
    except RuntimeError as exc:
        if isinstance(exc, _WorkflowToolValidationError):
            raise
        raise _WorkflowToolValidationError(
            ValidationError(
                code="workflow_tool_action_invalid",
                message=str(exc),
                path="parameters.command",
            )
        ) from exc


def _validation_gate_config(step: Any) -> Mapping[str, Any] | None:
    value = getattr(step, "validation_gate", None)
    return value if isinstance(value, Mapping) else None


def _validation_gate_enabled(step: Any) -> bool:
    return _validation_gate_config(step) is not None


def _validation_gate_id(step: Any) -> str:
    config = _validation_gate_config(step)
    gate_id = config.get("id") if config is not None else None
    if not isinstance(gate_id, str) or not gate_id.strip():
        raise PowdrrExecutionError("Every validation gate must have a non-empty id.")
    return gate_id.strip()


def _validation_gate_state(
    state: _WorkflowExecutionState,
    step: Any,
) -> _ValidationGateState:
    gate_id = _validation_gate_id(step)
    gate_state = state.validation_gates.get(gate_id)
    if gate_state is None:
        gate_state = _ValidationGateState(step_index=state.step_index)
        state.validation_gates[gate_id] = gate_state
    return gate_state


def _nested_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _workflow_action_data(action: SkillChatAction) -> dict[str, Any]:
    return {
        "kind": action.kind,
        "tool": action.tool,
        "file_operation": action.file_operation,
        "file_path": action.file_path,
        "destination_path": action.destination_path,
        "parameters": dict(action.parameters),
        "types": list(action.types),
        "keywords": list(action.keywords),
        "filters": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in action.filters.items()
        },
        "feature_id": action.feature_id,
    }


def _action_template_matches(
    template: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> bool:
    for key, expected in template.items():
        value = actual.get(key)
        if isinstance(expected, Mapping):
            if not isinstance(value, Mapping) or not _action_template_matches(
                expected, value
            ):
                return False
        elif value != expected:
            return False
    return True


def _validation_actions_match(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> bool:
    """Match equivalent shell commands regardless of JSON string/list shape."""
    expected_parameters = expected.get("parameters")
    actual_parameters = actual.get("parameters")
    if not isinstance(expected_parameters, Mapping) or not isinstance(
        actual_parameters, Mapping
    ):
        return expected == actual

    def normalize_command(command: Any) -> Any:
        if isinstance(command, str):
            try:
                return tuple(shlex.split(command))
            except ValueError:
                return None
        if isinstance(command, Sequence) and not isinstance(
            command, (bytes, bytearray)
        ):
            return tuple(command)
        return command

    expected_command = normalize_command(expected_parameters.get("command"))
    actual_command = normalize_command(actual_parameters.get("command"))

    normalized_expected = dict(expected)
    normalized_actual = dict(actual)
    normalized_expected["parameters"] = {
        **expected_parameters,
        "command": expected_command,
    }
    normalized_actual["parameters"] = {
        **actual_parameters,
        "command": actual_command,
    }
    return normalized_expected == normalized_actual


def _discover_validation_obligations(
    result: Mapping[str, Any],
    *,
    state: _WorkflowExecutionState,
    gate: Any,
    gate_step_index: int,
    discovery_action: Mapping[str, Any] | None = None,
) -> None:
    config = _validation_gate_config(gate)
    assert config is not None
    discovery = config.get("discovery")
    if not isinstance(discovery, Mapping):
        raise PowdrrExecutionError("Validation gate discovery must be an object.")
    configured_discovery_action = discovery.get("action")
    if configured_discovery_action is not None and not isinstance(
        configured_discovery_action, Mapping
    ):
        raise PowdrrExecutionError(
            "Validation gate discovery.action must be an action object."
        )
    if configured_discovery_action is None and not isinstance(
        discovery.get("input_ref"), str
    ):
        raise PowdrrExecutionError(
            "Validation gate discovery must declare an action or input_ref."
        )
    obligation_config = config.get("obligations")
    if not isinstance(obligation_config, Mapping):
        raise PowdrrExecutionError("Validation gate obligations must be an object.")
    matches = _nested_value(
        result,
        str(obligation_config.get("source", discovery.get("result_path", "matches"))),
    )
    if not isinstance(matches, Sequence) or isinstance(
        matches, (str, bytes, bytearray)
    ):
        raise PowdrrExecutionError("Validation tool discovery did not return matches.")
    obligations: dict[str, _ValidationObligation] = {}
    filter_values = obligation_config.get("filter", {})
    if not isinstance(filter_values, Mapping):
        raise PowdrrExecutionError(
            "Validation gate obligations.filter must be an object."
        )
    id_path = obligation_config.get("id")
    action_path = obligation_config.get("action")
    if not isinstance(id_path, str) or not isinstance(action_path, str):
        raise PowdrrExecutionError(
            "Validation gate obligations must declare id and action projections."
        )
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        if any(match.get(key) != expected for key, expected in filter_values.items()):
            continue
        raw_id = _nested_value(match, id_path)
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise PowdrrExecutionError(
                "Every discovered validation item must have an id."
            )
        normalized_id = raw_id.strip()
        if normalized_id in obligations:
            raise PowdrrExecutionError(
                "Validation discovery returned duplicate obligation id "
                f"{normalized_id!r}."
            )
        expected_action = _nested_value(match, action_path)
        if not isinstance(expected_action, Mapping):
            raise PowdrrExecutionError(
                f"Validation item {normalized_id!r} must provide action at "
                f"{action_path}."
            )
        obligations[normalized_id] = _ValidationObligation(
            obligation_id=normalized_id,
            expected_action=dict(expected_action),
            source=dict(match),
        )
    gate_state = _validation_gate_state(state, gate)
    gate_state.step_index = gate_step_index
    gate_state.discovered = True
    gate_state.epoch = 1
    gate_state.obligations = obligations
    gate_state.correction_required = False
    gate_state.discovery_action = (
        dict(discovery_action) if discovery_action is not None else None
    )
    output_name = discovery.get("output_ref")
    if isinstance(output_name, str) and output_name.strip():
        state.handoff_records[output_name.strip()] = {
            "name": output_name.strip(),
            "type": "any",
            "value": [
                {
                    "obligation_id": obligation.obligation_id,
                    "action": dict(obligation.expected_action),
                }
                for obligation in obligations.values()
            ],
            "produced_by": {
                "step_index": state.step_index,
                "action": "gather_context",
            },
            "scope": "skill",
        }


def _auto_register_validation_handoff(
    state: _WorkflowExecutionState,
    *,
    gate: Any,
    gate_step_index: int,
) -> bool:
    """Register a valid discovered-obligation handoff when resuming a workflow."""
    config = _validation_gate_config(gate)
    if config is None:
        return False
    discovery = config.get("discovery")
    if not isinstance(discovery, Mapping):
        return False
    input_ref = discovery.get("input_ref")
    if not isinstance(input_ref, str) or not input_ref.strip():
        return False
    record = state.handoff_records.get(input_ref.strip())
    if not isinstance(record, Mapping):
        return False
    raw_obligations = record.get("value")
    if not isinstance(raw_obligations, Sequence) or isinstance(
        raw_obligations, (str, bytes, bytearray)
    ):
        return False
    matches: list[dict[str, Any]] = []
    for item in raw_obligations:
        if not isinstance(item, Mapping):
            return False
        obligation_id = item.get("id")
        validation_action = item.get("validation_action")
        if not isinstance(obligation_id, str) or not obligation_id.strip():
            return False
        if not isinstance(validation_action, Mapping) or not validation_action:
            return False
        matches.append(
            {
                "section": "tools",
                "item": {
                    "id": obligation_id.strip(),
                    "validation_action": dict(validation_action),
                },
            }
        )
    _discover_validation_obligations(
        {"matches": matches},
        state=state,
        gate=gate,
        gate_step_index=gate_step_index,
        discovery_action={"kind": "handoff", "name": input_ref.strip()},
    )
    return True


def _register_validation_gate_discovery(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
) -> None:
    for gate_step_index, gate in enumerate(state.selected_skill.skill.steps):
        config = _validation_gate_config(gate)
        if gate_step_index <= state.step_index or config is None:
            continue
        discovery = config.get("discovery")
        if not isinstance(discovery, Mapping):
            continue
        discovery_action = discovery.get("action")
        input_ref = discovery.get("input_ref")
        matches_discovery = (
            isinstance(discovery_action, Mapping)
            and _action_template_matches(
                discovery_action, _workflow_action_data(action)
            )
        ) or (
            isinstance(input_ref, str)
            and input_ref.strip()
            and action.kind == "gather_context"
        )
        if not matches_discovery:
            continue
        gate_state = _validation_gate_state(state, gate)
        if not gate_state.discovered:
            event = state.execution_events[-1] if state.execution_events else None
            if not isinstance(event, Mapping) or not isinstance(
                event.get("result"), Mapping
            ):
                raise PowdrrExecutionError(
                    "Validation discovery action did not produce a result."
                )
            _discover_validation_obligations(
                event["result"],
                state=state,
                gate=gate,
                gate_step_index=gate_step_index,
                discovery_action=_workflow_action_data(action),
            )


def _validation_result_passed(
    result: Mapping[str, Any],
    gate: Any,
) -> bool:
    config = _validation_gate_config(gate) or {}
    success = config.get("success")
    if isinstance(success, Mapping):
        field = success.get("result_field")
        accepted = success.get("accepted_values")
        value = result.get(field) if isinstance(field, str) else None
        if isinstance(accepted, Sequence) and not isinstance(
            accepted, (str, bytes, bytearray)
        ):
            return value in accepted
    if result.get("returncode") not in (None, 0):
        return False
    if result.get("validation_successful") is False:
        return False
    return result.get("status") not in {"failed", "failure", "error"}


def _validation_issue_fingerprint(result: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a stable semantic fingerprint for a validation result.

    Validators commonly return YAML in ``stdout`` rather than structured JSON.
    Fingerprinting the parsed issue records lets us distinguish a real repair
    from formatting churn or an edit that merely changes the error wording.
    """
    payload: Any = result
    stdout = result.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        try:
            parsed = yaml.safe_load(stdout)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, Mapping):
            payload = parsed
    issues = payload.get("issues") if isinstance(payload, Mapping) else None
    if isinstance(issues, Sequence) and not isinstance(issues, (str, bytes, bytearray)):
        return tuple(
            sorted(
                json.dumps(issue, sort_keys=True, ensure_ascii=False, default=str)
                for issue in issues
            )
        )
    if _validation_result_passed(result, None):
        return ()
    return (
        json.dumps(
            {
                "returncode": result.get("returncode"),
                "stderr": result.get("stderr", ""),
                "stdout": result.get("stdout", ""),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ),
    )


def _validation_gate_incomplete(
    state: _WorkflowExecutionState,
    gate: Any,
) -> list[_ValidationObligation]:
    gate_state = _validation_gate_state(state, gate)
    return [
        obligation
        for obligation in gate_state.obligations.values()
        if obligation.epoch != gate_state.epoch or obligation.status != "passed"
    ]


def _validate_dynamic_validation_gate_action(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    step: Any,
) -> None:
    if not _validation_gate_enabled(step):
        return
    gate_state = _validation_gate_state(state, step)
    if not gate_state.discovered:
        if _auto_register_validation_handoff(
            state, gate=step, gate_step_index=state.step_index
        ):
            gate_state = _validation_gate_state(state, step)
        else:
            raise _WorkflowToolValidationError(
                ValidationError(
                    code="validation_discovery_required",
                    message=(
                        "Validation obligations have not been discovered in the "
                        "current "
                        "gate epoch. Do not invoke a validation command or invent a "
                        "discovery CLI command. Return to the configured discovery "
                        "step and run its exact gather_context action before invoking "
                        "any obligation."
                    ),
                    path="action",
                )
            )
    if state.step_index != gate_state.step_index:
        return
    if gate_state.correction_required and action.kind != "gather_context":
        raise _WorkflowToolValidationError(
            ValidationError(
                code="validation_correction_required",
                message=(
                    "A validation tool failed. Apply its corrective action before "
                    "running validation tools again."
                ),
                path="action",
            )
        )
    actual = {
        "kind": action.kind,
        "tool": action.tool,
        "parameters": dict(action.parameters),
    }
    if action.kind == "gather_context":
        return
    config = _validation_gate_config(step) or {}
    correction_actions = config.get(
        "correction_actions",
        ["edit", "yaml_edit", "file_management", "gather_context"],
    )
    if isinstance(correction_actions, Sequence) and action.kind in correction_actions:
        return
    if action.kind in {"next_step", "goto_step", "complete"}:
        return
    obligations = gate_state.obligations.values()
    if not any(
        _validation_actions_match(obligation.expected_action, actual)
        for obligation in obligations
    ):
        declared = "; ".join(
            json.dumps(obligation.expected_action, sort_keys=True)
            for obligation in gate_state.obligations.values()
        )
        raise _WorkflowToolValidationError(
            ValidationError(
                code="validation_action_not_discovered",
                message=(
                    "The action is not one of the discovered obligations. "
                    f"Use one of: {declared or 'none'}."
                ),
                path="action",
            )
        )


def _record_dynamic_validation_result(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
) -> None:
    if state.step_index >= len(state.selected_skill.skill.steps):
        return
    step = state.selected_skill.skill.steps[state.step_index]
    if not _validation_gate_enabled(step):
        return
    event = state.execution_events[-1] if state.execution_events else None
    if not isinstance(event, Mapping) or not isinstance(event.get("result"), Mapping):
        return
    actual = {
        "kind": action.kind,
        "tool": action.tool,
        "parameters": dict(action.parameters),
    }
    gate_state = _validation_gate_state(state, step)
    obligation = next(
        (
            item
            for item in gate_state.obligations.values()
            if _validation_actions_match(item.expected_action, actual)
        ),
        None,
    )
    if obligation is None:
        return
    result = dict(event["result"])
    obligation.attempts += 1
    obligation.last_result = result
    if _validation_result_passed(result, step):
        obligation.status = "passed"
        obligation.last_issue_fingerprint = ()
    else:
        obligation.status = "failed"
        fingerprint = _validation_issue_fingerprint(result)
        previous_fingerprint = obligation.last_issue_fingerprint
        previous_issues = set(previous_fingerprint or ())
        current_issues = set(fingerprint)
        if (
            previous_fingerprint == fingerprint
            or fingerprint in obligation.issue_history
            or (previous_issues and not current_issues < previous_issues)
        ):
            obligation.semantic_stalls += 1
        else:
            obligation.semantic_stalls = 0
        obligation.last_issue_fingerprint = fingerprint
        obligation.issue_history.add(fingerprint)
        gate_state.correction_required = True
        progress_note = (
            " The repair made no semantic improvement or repeated a prior "
            "validation state; choose a materially different correction."
            if obligation.semantic_stalls
            else ""
        )
        state.execution_context.append(
            "Validation failed for "
            f"{obligation.obligation_id}. Exact result: "
            f"{json.dumps(result, ensure_ascii=False, default=str)}. "
            "Apply the corrective action indicated by that result, then rerun every "
            "discovered validation obligation." + progress_note
        )


def _reset_validation_gate_after_correction(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
) -> None:
    if state.step_index >= len(state.selected_skill.skill.steps):
        return
    step = state.selected_skill.skill.steps[state.step_index]
    if not _validation_gate_enabled(step):
        return
    if action.kind not in {"edit", "yaml_edit"}:
        return
    gate_state = _validation_gate_state(state, step)
    if not gate_state.correction_required:
        return
    gate_state.epoch += 1
    gate_state.correction_required = False
    for obligation in gate_state.obligations.values():
        obligation.epoch = gate_state.epoch
        obligation.status = "pending"
        obligation.attempts = 0
        obligation.last_result = None
    state.execution_events.append(
        {
            "kind": "validation_gate_epoch",
            "gate_id": _validation_gate_id(step),
            "epoch": gate_state.epoch,
            "step_index": state.step_index,
            "reason": "correction_applied",
        }
    )


def _validation_gate_prompt_data(
    state: _WorkflowExecutionState,
) -> dict[str, Any] | None:
    if state.step_index >= len(state.selected_skill.skill.steps):
        return None
    step = state.selected_skill.skill.steps[state.step_index]
    if not _validation_gate_enabled(step):
        return None
    gate_state = _validation_gate_state(state, step)
    if not gate_state.discovered:
        return {"gate_id": _validation_gate_id(step), "discovered": False}
    incomplete = _validation_gate_incomplete(state, step)
    return {
        "gate_id": _validation_gate_id(step),
        "discovered": True,
        "epoch": gate_state.epoch,
        "correction_required": gate_state.correction_required,
        "can_advance": not gate_state.correction_required and not incomplete,
        "discovery_action": gate_state.discovery_action,
        "discovered_tools": [
            {
                "obligation_id": obligation.obligation_id,
                "action": dict(obligation.expected_action),
            }
            for obligation in gate_state.obligations.values()
        ],
        "obligations": [
            obligation.to_data() for obligation in gate_state.obligations.values()
        ],
        "repair_guidance": (
            "For every failed obligation, inspect the exact current file and validator "
            "result before editing. Use the reported issue path and a structural YAML "
            "operation, preserve valid fields, and combine independent fixes in one "
            "yaml_edit. Never repeat an operation or semantically equivalent operation "
            "that produced the same issue fingerprint. If the issue fingerprint is "
            "unchanged or worse, choose a different target or repair strategy; do not "
            "retry the same action. Rerun every obligation after any correction."
        ),
    }


def _validate_workflow_action_outputs(action: SkillChatAction, step: Any) -> None:
    if not action.outputs or not step.outputs:
        return
    declared_names = {output.name for output in step.outputs}
    unexpected = sorted(set(action.outputs) - declared_names)
    if unexpected:
        raise PowdrrExecutionError(
            "Workflow action outputs are not declared by the current step: "
            + ", ".join(unexpected)
        )
    declarations = {output.name: output for output in step.outputs}
    for name, value in action.outputs.items():
        declaration = declarations[name]
        if declaration.schema is None:
            continue
        schema_error = _json_schema_error(
            value,
            declaration.schema,
            path=f"outputs.{name}",
        )
        if schema_error is not None:
            raise PowdrrExecutionError(
                f"Workflow action output does not match its declared schema: "
                f"{schema_error}"
            )


def _json_schema_error(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str,
) -> str | None:
    """Validate the strict JSON-schema subset used by workflow handoffs."""
    expected_type = schema.get("type")
    type_matches = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray)),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected_type, str) and not type_matches.get(expected_type, False):
        return f"{path} must be {expected_type}."
    enum = schema.get("enum")
    if isinstance(enum, Sequence) and value not in enum:
        return f"{path} must be one of {list(enum)!r}."
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return f"{path} schema properties must be an object."
        required = schema.get("required", ())
        required_names = (
            tuple(required)
            if isinstance(required, Sequence)
            and not isinstance(required, (str, bytes, bytearray))
            else ()
        )
        missing = sorted(str(name) for name in required_names if name not in value)
        if missing:
            return f"{path} is missing required properties: {', '.join(missing)}."
        if schema.get("additionalProperties") is False:
            unknown = sorted(str(name) for name in set(value) - set(properties))
            if unknown:
                return f"{path} has unknown properties: {', '.join(unknown)}."
        for name, item in value.items():
            item_schema = properties.get(name)
            if not isinstance(item_schema, Mapping):
                continue
            error = _json_schema_error(item, item_schema, path=f"{path}.{name}")
            if error is not None:
                return error
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return f"{path} must contain at least {min_items} items."
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                error = _json_schema_error(item, item_schema, path=f"{path}[{index}]")
                if error is not None:
                    return error
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            return f"{path} must contain at least {min_length} characters."
    return None


def _parse_action_response_with_schema(
    payload: dict[str, Any],
    *,
    schema: Mapping[str, Any],
) -> SkillChatAction:
    schema_error = _json_schema_error(payload, schema, path="response")
    if schema_error is not None:
        raise PowdrrExecutionError(
            f"Workflow action response does not match the active schema: {schema_error}"
        )
    return _parse_action_response(payload)


def _record_workflow_action_outputs(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    step: Any,
) -> None:
    if not action.outputs:
        return
    declarations = {output.name: output for output in step.outputs}
    for name, value in action.outputs.items():
        declaration = declarations.get(name)
        state.handoff_records[name] = {
            "name": name,
            "type": declaration.type if declaration is not None else "any",
            "value": value,
            "produced_by": {
                "step_index": state.step_index,
                "action": action.kind,
            },
            "scope": declaration.scope if declaration is not None else "skill",
        }


def _record_runtime_readiness_artifact(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    step: Any,
) -> None:
    """Replace model-asserted readiness with a runtime-evaluated artifact."""
    if "readiness_report" not in action.outputs or state.runtime is None:
        return
    report_data = _runtime_readiness_report(state.runtime)
    record = state.handoff_records.get("readiness_report")
    if record is not None:
        record["value"] = report_data
        record["produced_by"] = {
            "step_index": state.step_index,
            "action": "runtime_readiness_evaluation",
        }
    if not report_data["ready"]:
        return
    content_ref = (
        "readiness:"
        + hashlib.sha256(
            json.dumps(report_data, sort_keys=True).encode("utf-8")
        ).hexdigest()
    )
    artifact = ExecutionArtifact(
        artifact_id="readiness-report-" + content_ref[-16:],
        artifact_type="readiness_report",
        schema_version="readiness-report-v1",
        owner_persona_id="code_reviewer",
        content_ref=content_ref,
        accepted=True,
    )
    state.runtime.record_artifact(artifact)


def _materialize_split_pr_specification(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    step: Any,
) -> None:
    """Compile validated split decisions into the proposed-PR document."""
    allocation = action.outputs.get("effect_allocation")
    if allocation is None or step.id != "allocate-proposed-pr-effects":
        return
    if not isinstance(allocation, Mapping):
        raise PowdrrExecutionError("effect_allocation must be a JSON object.")
    plan_record = state.handoff_records.get("proposed_pr_plan")
    effects_record = state.handoff_records.get("authoritative_effects")
    plan = plan_record.get("value") if isinstance(plan_record, Mapping) else None
    effects = (
        effects_record.get("value") if isinstance(effects_record, Mapping) else None
    )
    if not isinstance(plan, Mapping) or not isinstance(effects, Mapping):
        raise PowdrrExecutionError(
            "Cannot compile effect_allocation without validated proposed_pr_plan "
            "and authoritative_effects handoffs."
        )
    target = state.current_file_path
    if target is None or target.name != "proposed-pr-specification.yaml":
        candidates = tuple(state.worktree_root.rglob("proposed-pr-specification.yaml"))
        target = candidates[0] if len(candidates) == 1 else None
    if target is None:
        raise PowdrrExecutionError(
            "Cannot materialize effect_allocation without the proposed PR "
            "specification path in current file context."
        )
    try:
        compiled = compile_split_pr_specification(
            plan,
            allocation,
            effects,
            work_item_name=target.parent.name,
            repo_root=state.worktree_root,
            file_path=target,
        )
    except ValueError as exc:
        raise PowdrrExecutionError(str(exc)) from exc
    updated_text = (
        "# This file is read-only and should never be edited by a tool or agent.\n"
        + yaml.safe_dump(compiled, sort_keys=False)
    )
    _validate_structured_document_text(target, updated_text)
    runtime = _ensure_execution_runtime(state)
    with runtime.without_action_contract():
        invoke_file_mutation(
            (_worktree_relative_path(target, state.worktree_root),),
            worktree_root=state.worktree_root,
            executor=lambda: target.write_text(updated_text, encoding="utf-8"),
            runtime=runtime,
        )
    state.current_file_path = target


def _reset_split_pr_handoffs_for_repair(
    state: _WorkflowExecutionState,
    *,
    skill_name: str,
    target_step_id: str | None,
) -> None:
    if skill_name != "start-implementing-feature":
        return
    if target_step_id == "plan-proposed-pr-specification":
        for name in (
            "proposed_pr_plan",
            "authoritative_effects",
            "effect_allocation",
        ):
            state.handoff_records.pop(name, None)
        effect_step_index = _step_index_by_id(
            state.selected_skill, "load-authoritative-pr-effects"
        )
        _invalidate_deterministic_pre_step(
            state.execution_events,
            skill_name=skill_name,
            step_index=effect_step_index,
        )
    elif target_step_id == "allocate-proposed-pr-effects":
        state.handoff_records.pop("effect_allocation", None)


def _runtime_readiness_report(runtime: ExecutionRuntime) -> dict[str, Any]:
    report = runtime.publish_readiness(required_artifact_types=())
    return {
        "ready": report.ready,
        "reasons": list(report.reasons),
        "satisfied_requirements": list(getattr(report, "satisfied_requirements", ())),
    }


def _record_runtime_readiness_from_pre_step(
    step: Any,
    runtime: ExecutionRuntime,
) -> None:
    if not any(output.name == "readiness_report" for output in step.outputs):
        return
    report_data = _runtime_readiness_report(runtime)
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


def _workflow_handoff_inputs(
    step: Any,
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "declared": [input_spec.to_data() for input_spec in step.inputs],
        "resolved": {
            input_spec.name: records[input_spec.name]
            for input_spec in step.inputs
            if input_spec.name in records
        },
        "missing_required": [
            input_spec.name
            for input_spec in step.inputs
            if input_spec.required and input_spec.name not in records
        ],
    }


def _workflow_context_handoff_records(
    workflow_context: WorkflowContext | None,
) -> dict[str, dict[str, Any]]:
    if workflow_context is None:
        return {}
    records: dict[str, dict[str, Any]] = {}
    for name, value in workflow_context.to_data().items():
        if value is None:
            continue
        value_type = (
            "path"
            if name == "worktree_root"
            else "integer"
            if isinstance(value, int) and not isinstance(value, bool)
            else "string"
            if isinstance(value, str)
            else "any"
        )
        records[name] = {
            "name": name,
            "type": value_type,
            "value": value,
            "produced_by": {"source": "workflow_context"},
            "source": "workflow_context",
            "scope": "skill",
        }
    return records


def _validate_workflow_handoff(
    current_step: Any,
    next_step: Any,
    records: Mapping[str, Mapping[str, Any]],
    *,
    current_step_index: int,
) -> None:
    def is_current_step_record(record: Mapping[str, Any]) -> bool:
        producer = record.get("produced_by")
        return (
            isinstance(producer, Mapping)
            and producer.get("step_index") == current_step_index
        )

    def is_prior_step_record(record: Mapping[str, Any]) -> bool:
        producer = record.get("produced_by")
        return (
            isinstance(producer, Mapping)
            and isinstance(producer.get("step_index"), int)
            and producer["step_index"] < current_step_index
        )

    required_outputs = {
        output.name for output in current_step.outputs if output.required_for_next_step
    }
    missing_outputs = sorted(
        name
        for name in required_outputs
        if name not in records or not is_current_step_record(records[name])
    )
    if missing_outputs:
        raise PowdrrExecutionError(
            "Cannot advance: the current step has not produced required outputs: "
            + ", ".join(missing_outputs)
        )
    if next_step is None:
        return
    missing_inputs = []
    for input_spec in next_step.inputs:
        record = records.get(input_spec.name)
        if not input_spec.required and record is None:
            continue
        if (
            record is None
            or (
                input_spec.source == "previous_step"
                and not (is_current_step_record(record) or is_prior_step_record(record))
            )
            or (
                input_spec.source == "workflow_context"
                and record.get("source") != "workflow_context"
                and not (is_current_step_record(record) or is_prior_step_record(record))
            )
        ):
            missing_inputs.append(input_spec.name)
    if missing_inputs:
        raise PowdrrExecutionError(
            "Cannot advance: the next step is missing required inputs: "
            + ", ".join(missing_inputs)
            + "; available handoffs: "
            + ", ".join(sorted(str(name) for name in records))
        )
    mismatched = []
    for input_spec in next_step.inputs:
        record = records.get(input_spec.name)
        if record is None or input_spec.type == "any":
            continue
        if record.get("type") != input_spec.type:
            mismatched.append(
                f"{input_spec.name} (expected {input_spec.type}, "
                f"got {record.get('type')})"
            )
    if mismatched:
        raise PowdrrExecutionError(
            "Cannot advance: handoff input types do not match: " + ", ".join(mismatched)
        )


def _predicated_step_complete(step: Any, state: _WorkflowExecutionState) -> bool:
    completion = getattr(step, "completion", None)
    if completion is None:
        return False
    for output_name in completion.required_outputs:
        record = state.handoff_records.get(output_name)
        producer = record.get("produced_by") if isinstance(record, Mapping) else None
        if (
            not isinstance(producer, Mapping)
            or producer.get("step_index") != state.step_index
        ):
            return False
    for requirement in getattr(completion, "required_actions", ()):
        events = _predicated_action_evidence(requirement, state)
        if requirement.targets_from is not None:
            targets = _resolve_predicated_targets(requirement.targets_from, state)
            if any(
                not any(
                    event.get(requirement.match_field) == target for event in events
                )
                for target in targets
            ):
                return False
        elif not events:
            return False
        if requirement.exactly is not None and len(events) != requirement.exactly:
            return False
    return True


def _predicated_action_evidence(
    requirement: Any, state: _WorkflowExecutionState
) -> list[Mapping[str, Any]]:
    parameters = requirement.parameters or {}
    return [
        event
        for event in state.execution_events
        if event.get("step_index") == state.step_index
        and event.get("kind") == requirement.action
        and all(
            _predicated_parameter_matches(event.get(name), value, name)
            for name, value in parameters.items()
        )
    ]


def _predicated_parameter_matches(actual: Any, expected: Any, name: str) -> bool:
    if (
        name == "types"
        and isinstance(actual, Sequence)
        and isinstance(expected, Sequence)
    ):
        try:
            return [normalize_context_type(str(item)) for item in actual] == [
                normalize_context_type(str(item)) for item in expected
            ]
        except ValueError:
            return list(actual) == list(expected)
    return actual == expected


def _resolve_predicated_targets(path: str, state: _WorkflowExecutionState) -> list[Any]:
    """Resolve a small deterministic handoff path such as ``x.items[*].file``."""
    segments = path.split(".")
    if not segments or segments[0] not in state.handoff_records:
        return []
    values: list[Any] = [state.handoff_records[segments[0]].get("value")]
    for segment in segments[1:]:
        wildcard = segment.endswith("[*]")
        key = segment[:-3] if wildcard else segment
        next_values: list[Any] = []
        for value in values:
            if key and isinstance(value, Mapping) and key in value:
                nested = value[key]
                if (
                    wildcard
                    and isinstance(nested, Sequence)
                    and not isinstance(nested, (str, bytes, bytearray))
                ):
                    next_values.extend(nested)
                else:
                    next_values.append(nested)
            elif (
                wildcard
                and isinstance(value, Sequence)
                and not isinstance(value, (str, bytes, bytearray))
            ):
                next_values.extend(value)
        values = next_values
    flattened: list[Any] = []
    for value in values:
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            flattened.extend(value)
        else:
            flattened.append(value)
    return flattened


def _advance_predicated_step(
    state: _WorkflowExecutionState,
    current_step: Any,
) -> None:
    next_index = state.step_index + 1
    next_step = (
        state.selected_skill.skill.steps[next_index]
        if next_index < len(state.selected_skill.skill.steps)
        else None
    )
    _validate_workflow_handoff(
        current_step,
        next_step,
        state.handoff_records,
        current_step_index=state.step_index,
    )
    state.execution_events.append(
        {
            "kind": "predicated_advance",
            "step_index": state.step_index,
            "required_outputs": list(current_step.completion.required_outputs),
        }
    )
    state.step_index = next_index


def _validate_workflow_step_transition(
    action: SkillChatAction,
    step: Any,
    execution_events: Sequence[Mapping[str, Any]],
    current_step_index: int,
    state: _WorkflowExecutionState | None = None,
) -> None:
    """Prevent the LLM from skipping a step's required tool invocation."""
    behavior = behavior_for_step(step)
    if state is not None and behavior.is_predicated:
        for requirement in getattr(
            getattr(step, "completion", None), "required_actions", ()
        ):
            if (
                action.kind == requirement.action
                and requirement.exactly is not None
                and len(_predicated_action_evidence(requirement, state))
                >= requirement.exactly
            ):
                raise PowdrrExecutionError(
                    f"Predicated step permits at most {requirement.exactly} "
                    f"{requirement.action} action(s) matching its required parameters."
                )
    if action.kind == "emit_outputs":
        if not behavior.is_predicated:
            raise PowdrrExecutionError(
                "emit_outputs is valid only for predicated steps."
            )
        if not action.outputs:
            raise PowdrrExecutionError(
                "emit_outputs must include at least one declared output."
            )
        if (
            state is not None
            and not _predicated_step_complete(step, state)
            and getattr(step, "completion", None) is not None
        ):
            missing = [
                requirement.targets_from or requirement.action
                for requirement in getattr(step.completion, "required_actions", ())
                if (
                    not _predicated_action_evidence(requirement, state)
                    or (
                        requirement.exactly is not None
                        and len(_predicated_action_evidence(requirement, state))
                        > requirement.exactly
                    )
                )
                or (
                    requirement.targets_from is not None
                    and not all(
                        any(
                            event.get(requirement.match_field) == target
                            for event in _predicated_action_evidence(requirement, state)
                        )
                        for target in _resolve_predicated_targets(
                            requirement.targets_from, state
                        )
                    )
                )
            ]
            if missing:
                raise PowdrrExecutionError(
                    "Cannot emit_outputs until required action evidence is recorded: "
                    + ", ".join(missing)
                )
    if behavior.rejects_model_transition(action.kind):
        raise PowdrrExecutionError(
            "Predicated steps advance automatically when their completion "
            "predicate is satisfied; next_step is not a valid model action."
        )
    if action.kind not in {"next_step", "goto_step", "complete"}:
        return
    if action.kind == "next_step":
        next_step_override = getattr(step, "next_step_override", None)
        if next_step_override:
            if state is None:
                raise PowdrrExecutionError(
                    "Workflow next_step_override requires the current skill state."
                )
            target_index = _step_index_by_id(state.selected_skill, next_step_override)
            if target_index >= current_step_index:
                raise PowdrrExecutionError(
                    "Workflow next_step_override must target a prior step; "
                    f"{next_step_override!r} is not before step {current_step_index}."
                )
    if action.kind == "goto_step":
        if state is None:
            raise PowdrrExecutionError(
                "Workflow goto_step validation requires the current skill state."
            )
        target_index = _step_index_by_id(state.selected_skill, action.step_id)
        if target_index >= current_step_index:
            raise PowdrrExecutionError(
                "Workflow goto_step may target only a prior step in the current "
                f"skill; {action.step_id!r} is not before step "
                f"{current_step_index}."
            )
    if action.kind == "complete":
        if state is None:
            raise PowdrrExecutionError(
                "Workflow complete validation requires the current skill state."
            )
        later_gates = tuple(
            step.id or f"index {index}"
            for index, step in enumerate(state.selected_skill.skill.steps)
            if index > current_step_index and behavior_for_step(step).runs_gate
        )
        if later_gates:
            raise PowdrrExecutionError(
                "Cannot complete the skill while later gate steps remain: "
                + ", ".join(later_gates)
            )
    if _validation_gate_enabled(step):
        gate_state = _validation_gate_state(state, step) if state is not None else None
        if gate_state is None or not gate_state.discovered:
            raise _WorkflowToolValidationError(
                ValidationError(
                    code="validation_obligations_not_discovered",
                    message=(
                        "Validation obligations have not been discovered; the "
                        "validation gate cannot be skipped."
                    ),
                    path="kind",
                )
            )
        if gate_state.correction_required:
            raise _WorkflowToolValidationError(
                ValidationError(
                    code="validation_correction_required",
                    message=(
                        "A validation obligation failed. Apply corrective steps "
                        "and rerun every discovered obligation before advancing."
                    ),
                    path="kind",
                )
            )
        assert state is not None
        incomplete = _validation_gate_incomplete(state, step)
        if incomplete:
            raise _WorkflowToolValidationError(
                ValidationError(
                    code="validation_tool_obligations_incomplete",
                    message=(
                        "Every discovered validation obligation must pass before "
                        "advancing. "
                        "Still pending: "
                        + ", ".join(item.obligation_id for item in incomplete)
                    ),
                    path="kind",
                )
            )
    invocations = tuple(
        invocation for invocation in step.tool_invocations if invocation.tool != "ref"
    )
    if not invocations:
        return
    # Shell invocations are the externally visible commands that can mutate
    # the branch. Internal inspection and validator actions retain their
    # existing corrective-action behavior; a shell command in the same step
    # remains the transition gate.
    required_invocations = tuple(
        invocation for invocation in invocations if invocation.tool == "shell"
    )
    if not required_invocations:
        return

    def successful_event_matches(invocation: Any, event: Mapping[str, Any]) -> bool:
        if event.get("kind") != "invoke_tool":
            return False
        event_tool = event.get("tool")
        if event_tool not in {None, invocation.tool} and not (
            event_tool == _INTERNAL_TOOL and invocation.tool == "shell"
        ):
            return False
        if event.get("step_index") != current_step_index:
            return False
        result = event.get("result")
        if isinstance(result, Mapping) and result.get("returncode") not in (None, 0):
            return False
        parameters = event.get("parameters")
        if isinstance(parameters, Mapping) and parameters.get("help") is True:
            return False
        if not isinstance(parameters, Mapping) or parameters.get("command") is None:
            return True
        try:
            command_items = _command_items_for_validation(parameters["command"])
        except RuntimeError:
            return False
        return _command_matches_invocation(command_items, invocation.command)

    if any(
        any(successful_event_matches(invocation, event) for event in execution_events)
        for invocation in required_invocations
    ):
        return
    expected = ", ".join(invocation.tool for invocation in required_invocations)
    raise _WorkflowToolValidationError(
        ValidationError(
            code="workflow_step_tool_required",
            message=(
                f"The current step requires a successful tool invocation before "
                f"{action.kind}: {expected}. Current step index: "
                f"{current_step_index}. Invoke the declared tool and wait for its "
                "result before advancing."
            ),
            path="kind",
        )
    )


def _validate_workflow_action_for_step_unwrapped(
    action: SkillChatAction,
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
    observer_allowed_action: ObserverActionRecommendation | None = None,
) -> None:
    """Validate a tool action while preserving the original error wording."""
    allowed_actions = _declared_action_names(
        step, execution_events=execution_events, step_index=step_index
    )
    if (
        allowed_actions is not None
        and action.kind not in allowed_actions
        and action.kind not in {"prompt_user", "next_step"}
        and not observer_action_matches(action, observer_allowed_action)
        and not (
            action.kind == "complete" and not getattr(step, "actions_declared", False)
        )
        and not (
            action.kind == "invoke_tool"
            and action.tool == "internal"
            and not getattr(step, "actions_declared", False)
        )
    ):
        raise _WorkflowToolValidationError(
            ValidationError(
                code="workflow_action_not_allowed",
                message=(
                    f"The {action.kind} action is not allowed in this step. "
                    + (
                        "This step explicitly supports: none; next_step is implicit."
                        if (
                            allowed_actions == ("next_step",)
                            or (
                                action.kind == "invoke_tool"
                                and action.tool == "shell"
                                and not getattr(step, "tool_invocations", ())
                            )
                        )
                        else "Use one of: " + ", ".join(allowed_actions) + "."
                    )
                ),
                path="kind",
            )
        )
    if (
        action.kind == "invoke_tool"
        and action.parameters.get("help") is True
        and action.tool in BUILTIN_TOOL_NAMES
    ):
        return
    supported_invocations = tuple(
        invocation
        for invocation in (
            tuple(step.tool_invocations)
            + _recovery_tool_invocations(step, execution_events, step_index)
        )
        if invocation.tool != "ref"
    )
    if action.tool == _INTERNAL_TOOL:
        internal_invocations = tuple(
            invocation
            for invocation in supported_invocations
            if invocation.tool in {_INTERNAL_TOOL, "shell"}
        )
        if not internal_invocations:
            supported_tools = sorted(
                {invocation.tool for invocation in supported_invocations}
            )
            supported_tools_text = ", ".join(supported_tools) or "none"
            raise PowdrrExecutionError(
                "The internal tool is not declared by the current workflow step. "
                f"The step explicitly supports: {supported_tools_text}."
            )
        _validate_internal_command(action.parameters.get("command"))
        command_items = _command_items_for_validation(action.parameters.get("command"))
        if not any(
            _command_matches_invocation(command_items, invocation.command)
            for invocation in internal_invocations
        ):
            declared = "; ".join(
                " ".join(invocation.command) for invocation in internal_invocations
            )
            raise PowdrrExecutionError(
                "The internal command is not declared by the current workflow "
                f"step. Use one of: {declared}."
            )
        return
    if action.tool == "shell":
        command_items = _command_items_for_validation(action.parameters.get("command"))
        if command_items == ["git", "diff", "--cached", "--name-only"] and any(
            invocation.tool == GIT_TOOL and invocation.operation == "add"
            for invocation in supported_invocations
        ):
            return
    matching_invocations = tuple(
        invocation
        for invocation in supported_invocations
        if invocation.tool == action.tool
    )
    if action.tool in {GIT_TOOL, GH_TOOL}:
        operation = action.parameters.get("operation")
        if any(
            invocation.operation == operation
            for invocation in matching_invocations
            if invocation.operation is not None
        ):
            return
        intrinsic_items = intrinsic_command(action.parameters, tool=action.tool)
        if not matching_invocations or any(
            _intrinsic_command_matches(intrinsic_items, invocation.command)
            for invocation in matching_invocations
        ):
            return
    if action.tool in {"basedpyright-symbol", "basedpyright-structure"}:
        # BasedPyright is a structured builtin. New declarations use an
        # operation discriminator so these calls cannot be confused with shell
        # command templates; legacy command declarations remain compatible.
        declared_operations = {
            invocation.operation
            for invocation in matching_invocations
            if invocation.operation is not None
        }
        if not matching_invocations:
            supported_tools = sorted(
                {invocation.tool for invocation in supported_invocations}
            )
            supported_tools_text = ", ".join(supported_tools) or "none"
            raise PowdrrExecutionError(
                f"Tool {action.tool!r} is not supported by the current workflow "
                f"step. The step explicitly supports: {supported_tools_text}."
            )
        if declared_operations:
            operation = action.parameters.get("operation")
            if operation not in declared_operations:
                expected = ", ".join(sorted(declared_operations))
                raise PowdrrExecutionError(
                    f"Tool {action.tool!r} requires one of the declared intrinsic "
                    f"operations: {expected}."
                )
        return
    if not matching_invocations:
        supported_tools = sorted(
            {invocation.tool for invocation in supported_invocations}
        )
        supported_tools_text = ", ".join(supported_tools) or "none"
        raise PowdrrExecutionError(
            f"Tool {action.tool!r} is not supported by the current workflow step. "
            f"The step explicitly supports: {supported_tools_text}."
        )
    command = (
        intrinsic_command(action.parameters, tool=action.tool)
        if action.tool in {GIT_TOOL, GH_TOOL}
        else action.parameters.get("command")
    )
    command_items = _command_items_for_validation(command)
    if any(
        _command_matches_invocation(command_items, invocation.command)
        for invocation in matching_invocations
    ):
        return
    expected_commands = "; ".join(
        " ".join(invocation.command) for invocation in matching_invocations
    )
    raise PowdrrExecutionError(
        f"Tool {action.tool!r} command {' '.join(command_items)!r} does not match "
        f"the command shape explicitly supported by the current workflow step: "
        f"{expected_commands}."
    )


def _command_items_for_validation(command: object) -> list[str]:
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


def _command_matches_invocation(
    command: Sequence[str],
    expected_command: Sequence[str],
) -> bool:
    actual_index = 0
    for expected_index, expected in enumerate(expected_command):
        if expected == "<files-to-publish>":
            return expected_index == len(expected_command) - 1 and actual_index < len(
                command
            )
        if actual_index >= len(command):
            return False
        if not _command_token_matches(command[actual_index], expected):
            return False
        actual_index += 1
    return actual_index == len(command)


def _intrinsic_command_matches(
    command: Sequence[str], expected_command: Sequence[str]
) -> bool:
    """Allow structured intrinsic operations to omit optional CLI flags."""
    if len(command) > len(expected_command):
        return False
    return all(
        _command_token_matches(actual, expected)
        for actual, expected in zip(command, expected_command, strict=True)
    )


def _command_token_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    placeholder_matches = list(re.finditer(r"<[^<>]+>", expected))
    if not placeholder_matches:
        return False
    if len(placeholder_matches) == 1 and placeholder_matches[0].span() == (
        0,
        len(expected),
    ):
        # A command-template argument such as <populated-pr-description> can
        # intentionally contain spaces and newlines.  The command parser keeps
        # that value as one argv item when the LLM returns an array (or quotes
        # it in a shell command), so validating it as a non-whitespace token
        # incorrectly rejects otherwise valid commands.
        return bool(actual)
    pattern_parts: list[str] = []
    previous_end = 0
    for placeholder_match in placeholder_matches:
        pattern_parts.append(
            re.escape(expected[previous_end : placeholder_match.start()])
        )
        # One argv item may contain spaces when a placeholder is embedded in a
        # token (for example ``verification-command=<command>``).
        pattern_parts.append(r".+?")
        previous_end = placeholder_match.end()
    pattern_parts.append(re.escape(expected[previous_end:]))
    return re.fullmatch("".join(pattern_parts), actual) is not None


def _execute_fuzzy_match_tool(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
    path_cache: dict[tuple[str, int, int | None], tuple[Path, ...]] | None = None,
) -> dict[str, Any]:
    if parameters.get("help") is True:
        return builtin_tool_help("fuzzy-match")
    command = parameters.get("command")
    if not isinstance(command, (str, list, tuple)):
        raise PowdrrExecutionError(
            "Workflow fuzzy-match tool parameters must include a command array."
        )
    return {
        "tool": "fuzzy-match",
        "command": list(command) if not isinstance(command, str) else command,
        "result": json.loads(
            fuzzy_match_json(
                command, worktree_root=worktree_root, path_cache=path_cache
            )
        ),
    }


def _handle_workflow_action_gather_context(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    gathered_context = invoke_repository_read(
        "gather_context",
        {
            "types": list(action.types),
            "keywords": list(action.keywords) if action.keywords else None,
            "filters": action.filters,
            "feature_id": action.feature_id,
        },
        worktree_root=state.worktree_root,
        executor=lambda _arguments: gather_specification_context(
            state.worktree_root,
            types=list(action.types),
            keywords=list(action.keywords) if action.keywords else None,
            filters=action.filters,
            feature_id=action.feature_id,
        ),
        runtime=_ensure_execution_runtime(state),
    )
    gathered_context_text = render_gather_context_report(gathered_context)
    _verbose_print(
        stderr,
        config.verbose,
        (
            "Gathered context for "
            f"types={list(action.types)} keywords={list(action.keywords)}"
        ),
    )
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_context.append(f"Gathered context:\n{gathered_context_text}")
    state.execution_events.append(
        {
            "kind": action.kind,
            "types": list(action.types),
            "keywords": list(action.keywords),
            "filters": action.filters,
            "result": json.loads(gathered_context_text),
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    _ = stdout
    return True


def _parse_action_response(payload: dict[str, Any]) -> SkillChatAction:
    action = payload.get("action")
    if action is None:
        raise PowdrrExecutionError(
            "Workflow action response must include action. Return one JSON object "
            'with a top-level "action" field.'
        )
    if not isinstance(action, str):
        raise PowdrrExecutionError("Workflow action response action must be a string.")
    normalized_kind = action.strip()
    if not normalized_kind:
        raise PowdrrExecutionError("Workflow action response action must not be empty.")
    decisions_and_context = _optional_string(payload.get("decisions_and_context"))
    llm_type = _optional_llm_type(payload.get("llm_type"))
    parser = _workflow_action_parsers().get(normalized_kind)
    if parser is None:
        raise PowdrrExecutionError(f"Unknown workflow action: {normalized_kind!r}")
    raw_outputs = payload.get("outputs", {})
    if not isinstance(raw_outputs, Mapping) or any(
        not isinstance(name, str) or not name.strip() for name in raw_outputs
    ):
        raise PowdrrExecutionError(
            "Workflow action outputs must be an object with named values."
        )
    raw_outputs = dict(raw_outputs)
    nested_decisions = raw_outputs.pop("decisions_and_context", None)
    if decisions_and_context is None and nested_decisions is not None:
        decisions_and_context = _optional_string(nested_decisions)
    parser_payload = payload
    if normalized_kind in {"edit", "gather_context", "read_document"} and isinstance(
        payload.get("parameters"), Mapping
    ):
        parser_payload = dict(payload)
        for name, value in payload["parameters"].items():
            parser_payload.setdefault(name, value)
    if normalized_kind == "edit":
        raw_edits = parser_payload.get("edits")
        if isinstance(raw_edits, Mapping):
            if parser_payload is payload:
                parser_payload = dict(payload)
            parser_payload["edits"] = [dict(raw_edits)]
    if normalized_kind == "gather_context" and isinstance(
        parser_payload.get("types"), str
    ):
        if parser_payload is payload:
            parser_payload = dict(payload)
        parser_payload["types"] = [parser_payload["types"]]
    action = parser(parser_payload, decisions_and_context, llm_type)
    return replace(
        action,
        outputs=dict(raw_outputs),
    )


def _workflow_action_parsers() -> dict[str, WorkflowActionParser]:
    return {
        "complete": _parse_workflow_action_complete,
        "get-human-input": _parse_workflow_action_human_input,
        "edit": _parse_workflow_action_edit,
        "yaml_edit": _parse_workflow_action_yaml_edit,
        "file_management": _parse_workflow_action_file_management,
        "delete_file": _parse_workflow_action_delete_file,
        "gather_context": _parse_workflow_action_gather_context,
        "invoke_tool": _parse_workflow_action_invoke_tool,
        "invoke_skill": _parse_workflow_action_invoke_skill,
        "goto_step": _parse_workflow_action_goto_step,
        "read_document": _parse_workflow_action_read_document,
        "list_files": _parse_workflow_action_list_files,
        "next_step": _parse_workflow_action_next_step,
        "emit_outputs": _parse_workflow_action_emit_outputs,
        "prompt_user": _parse_workflow_action_prompt_user,
    }


def _parse_workflow_action_emit_outputs(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    return SkillChatAction(
        kind="emit_outputs",
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_invoke_skill(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    skill_name = payload.get("skill")
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise PowdrrExecutionError("Workflow invoke_skill action must include skill.")
    provider_role = payload.get("provider_role")
    if provider_role is not None and provider_role not in {"normal", "adversarial"}:
        raise PowdrrExecutionError(
            'provider_role must be "normal" or "adversarial" when provided.'
        )
    clean = payload.get("clean", False)
    if not isinstance(clean, bool):
        raise PowdrrExecutionError("clean must be a boolean when provided.")
    raw_context = payload.get("context", [])
    if not isinstance(raw_context, list) or not all(
        isinstance(value, str) and value.strip() for value in raw_context
    ):
        raise PowdrrExecutionError("context must be a list of non-empty strings.")
    return SkillChatAction(
        kind="invoke_skill",
        skill_name=skill_name.strip(),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
        provider_role=provider_role,
        clean=clean,
        context=tuple(value.strip() for value in raw_context),
    )


def _parse_workflow_action_goto_step(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    step_id = payload.get("step_id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise PowdrrExecutionError("Workflow goto_step action must include step_id.")
    return SkillChatAction(
        kind="goto_step",
        step_id=step_id.strip(),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_complete(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        raise PowdrrExecutionError("Workflow complete action text must be a string.")
    return SkillChatAction(
        kind="complete",
        text=(text.strip() if text else None),
        output_state=payload.get("output_state"),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_human_input(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    value = payload.get("human_input")
    if not isinstance(value, Mapping):
        raise PowdrrExecutionError("get-human-input must include human_input.")
    human_task = _parse_human_input_task(value.get("human_task"), "human_task")
    instructions = value.get("incorporation_instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise PowdrrExecutionError(
            "get-human-input must include incorporation_instructions."
        )
    follow_up_value = value.get("follow_up_task")
    follow_up = (
        _parse_human_input_task(follow_up_value, "follow_up_task")
        if follow_up_value is not None
        else None
    )
    return SkillChatAction(
        kind="get-human-input",
        human_input={
            "human_task": human_task,
            "incorporation_instructions": instructions.strip(),
            "follow_up_task": follow_up,
        },
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_human_input_task(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PowdrrExecutionError(f"{field_name} must be an object.")
    description = value.get("description")
    role = value.get("role")
    output_state_type = value.get("output_state_type")
    if not isinstance(description, str) or not description.strip():
        raise PowdrrExecutionError(f"{field_name}.description must be non-empty.")
    if not isinstance(role, str) or not role.strip():
        raise PowdrrExecutionError(f"{field_name}.role must be provided.")
    if not isinstance(output_state_type, str) or not output_state_type.strip():
        raise PowdrrExecutionError(f"{field_name}.output_state_type must be non-empty.")
    if "input_state" not in value:
        raise PowdrrExecutionError(f"{field_name}.input_state must be provided.")
    return {
        "description": description.strip(),
        "role": role.strip(),
        "input_state": value["input_state"],
        "output_state_type": output_state_type.strip(),
    }


def _parse_workflow_action_edit(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_edits_value = payload.get("file_edits")
    if file_edits_value is not None:
        parsed_file_edits = _required_file_edits(file_edits_value)
        return SkillChatAction(
            kind="edit",
            file_edits=parsed_file_edits,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError(
            "Workflow edit action must include file_path.", action_kind="edit"
        )
    edits = _required_edit_operations(payload.get("edits"))
    return SkillChatAction(
        kind="edit",
        file_path=file_path.strip(),
        edits=edits,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_yaml_edit(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError(
            "yaml_edit requires file_path. Use a repository-relative .yaml or "
            ".yml path."
        )
    if not file_path.strip().lower().endswith((".yaml", ".yml")):
        raise PowdrrExecutionError("yaml_edit file_path must end in .yaml or .yml.")
    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, Sequence) or isinstance(
        raw_operations,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            "yaml_edit requires a non-empty operations array. Supported "
            "operations are upsert_item, remove_item, remove_key, and set_value. "
            "Include "
            "all independent edits for this YAML file in one operations array."
        )
    operations = tuple(_parse_yaml_operation(item) for item in raw_operations)
    if not operations:
        raise PowdrrExecutionError(
            "yaml_edit operations must not be empty. Use upsert_item, "
            "remove_item, remove_key, or set_value. Try to include multiple "
            "independent "
            "edits in this one operations array when possible."
        )
    return SkillChatAction(
        kind="yaml_edit",
        file_path=file_path.strip(),
        yaml_operations=operations,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_yaml_operation(value: object) -> SkillChatYamlOperation:
    if not isinstance(value, Mapping):
        raise PowdrrExecutionError(
            'yaml_edit operations must be objects. Example: {"op": '
            '"upsert_item", "section": "features", "id": '
            '"feature-id", "value": {...}}.'
        )
    operation = value.get("op")
    if operation not in {"upsert_item", "remove_item", "remove_key", "set_value"}:
        raise PowdrrExecutionError(
            f"yaml_edit operation op must be upsert_item, remove_item, remove_key, or "
            f"set_value; received {operation!r}."
        )
    if operation in {"set_value", "remove_key"}:
        raw_path = value.get("path")
        if (
            not isinstance(raw_path, Sequence)
            or isinstance(
                raw_path,
                (str, bytes, bytearray),
            )
            or not raw_path
            or not all(
                (isinstance(item, str) and item.strip())
                or (isinstance(item, int) and not isinstance(item, bool) and item >= 0)
                for item in raw_path
            )
        ):
            raise PowdrrExecutionError(
                "yaml_edit set_value requires a non-empty path array of mapping "
                'keys or non-negative list indexes, for example ["title"].'
            )
        if operation == "remove_key":
            return SkillChatYamlOperation(
                operation=operation,
                path=tuple(str(item).strip() for item in raw_path),
            )
        if "value" not in value:
            raise PowdrrExecutionError("yaml_edit set_value requires value.")
        return SkillChatYamlOperation(
            operation=operation,
            path=tuple(str(item).strip() for item in raw_path),
            value=value["value"],
        )

    section = value.get("section")
    item_id = value.get("id")
    item_index = value.get("index")
    if not isinstance(section, str) or not section.strip():
        raise PowdrrExecutionError(
            f"yaml_edit {operation} requires a non-empty section, such as "
            "features or decisions."
        )
    if (
        operation == "remove_item"
        and isinstance(item_index, int)
        and not isinstance(item_index, bool)
    ):
        if item_index < 0:
            raise PowdrrExecutionError(
                "yaml_edit remove_item index must be non-negative. Corrective "
                "action: use the zero-based list index reported by the validator, "
                'for example {"op":"remove_item","section":"requirements",'
                '"index":0}; do not use a negative index.'
            )
        if item_id is not None:
            raise PowdrrExecutionError(
                "yaml_edit remove_item must use exactly one of id or index. "
                "Corrective action: remove id when using the validator-reported "
                'index, for example {"op":"remove_item",'
                '"section":"requirements","index":0}; do not include both.'
            )
        return SkillChatYamlOperation(
            operation=operation,
            section=section.strip(),
            item_index=item_index,
        )
    if not isinstance(item_id, str) or not item_id.strip():
        raise PowdrrExecutionError(
            f"yaml_edit {operation} requires a non-empty id. Use read_document "
            "to discover the existing item id."
        )
    if operation == "upsert_item" and not isinstance(value.get("value"), Mapping):
        raise PowdrrExecutionError(
            "yaml_edit upsert_item requires value to be a mapping containing the "
            "complete item fields."
        )
    return SkillChatYamlOperation(
        operation=operation,
        section=section.strip(),
        item_id=item_id.strip(),
        value=value.get("value"),
    )


def _parse_workflow_action_gather_context(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    types = _required_action_string_sequence(
        payload.get("types"),
        field_name="types",
    )
    keywords = _optional_action_string_sequence(
        payload.get("keywords"),
        field_name="keywords",
    )
    filters = _optional_action_filters(payload.get("filters"))
    feature_id = payload.get("feature_id")
    if feature_id is not None and (
        not isinstance(feature_id, str) or not feature_id.strip()
    ):
        raise PowdrrExecutionError(
            "Workflow gather_context action feature_id must be a non-empty string."
        )
    try:
        normalized_types = tuple(
            normalize_context_type(context_type) for context_type in types
        )
    except ValueError as exc:
        raise PowdrrExecutionError(str(exc)) from exc
    return SkillChatAction(
        kind="gather_context",
        types=normalized_types,
        keywords=keywords,
        filters=filters,
        feature_id=feature_id.strip() if isinstance(feature_id, str) else None,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_invoke_tool(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    tool = payload.get("tool", "shell")
    if not isinstance(tool, str) or not tool.strip():
        raise PowdrrExecutionError("Workflow invoke_tool action must include tool.")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise PowdrrExecutionError(
            "Workflow invoke_tool action must include parameters."
        )
    normalized_tool = tool.strip()
    if normalized_tool in {GIT_TOOL, GH_TOOL}:
        if normalized_tool == GH_TOOL and parameters.get("operation") in {
            "pr_create",
            "pr_edit",
        }:
            forbidden = {"pr_reference", "head", "base"}.intersection(parameters)
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise PowdrrExecutionError(
                    f"gh {parameters['operation']} does not accept model-owned "
                    f"identity fields: {names}. Provide only title and body."
                )
            if parameters.get("operation") == "pr_edit":
                validation_parameters = {
                    **parameters,
                    "pr_reference": "__current_branch__",
                }
            else:
                validation_parameters = parameters
        else:
            validation_parameters = parameters
        intrinsic_command(validation_parameters, tool=normalized_tool)
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=dict(parameters),
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    if normalized_tool in {
        ENRICH_TOOL,
        VALIDATE_EDIT_TOOL,
        APPLY_EDIT_TOOL,
    }:
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=dict(parameters),
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    if is_basedpyright_tool(normalized_tool):
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=dict(parameters),
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    command = parameters.get("command")
    first_class_action = _first_class_action_from_command(command)
    if normalized_tool in {"internal", "shell"} and first_class_action is not None:
        raise PowdrrExecutionError(
            f"Use the first-class action `{first_class_action}`, not "
            f"`invoke_tool` with a {normalized_tool} command. Return this "
            "kind of action directly: "
            f"{_first_class_action_example(first_class_action)}"
        )
    if isinstance(command, str):
        normalized_parameters = dict(parameters)
        normalized_command = command.strip()
        if not normalized_command:
            raise PowdrrExecutionError(
                "Workflow invoke_tool action command must be non-empty."
            )
        normalized_parameters["command"] = normalized_command
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=normalized_parameters,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    if isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        normalized_command_list = [
            _required_shell_command_item(item) for item in command
        ]
        if not normalized_command_list:
            raise PowdrrExecutionError(
                "Workflow invoke_tool action command must not be empty."
            )
        normalized_parameters = dict(parameters)
        normalized_parameters["command"] = normalized_command_list
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=normalized_parameters,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    raise PowdrrExecutionError(
        "Workflow invoke_tool action command must be a string or array."
    )


def _first_class_action_from_command(command: object) -> str | None:
    if isinstance(command, str):
        try:
            command_items = shlex.split(command)
        except ValueError:
            return None
    elif isinstance(command, Sequence) and not isinstance(
        command, (str, bytes, bytearray)
    ):
        command_items = list(command)
    else:
        return None
    if (
        len(command_items) < 2
        or not isinstance(command_items[0], str)
        or not isinstance(command_items[1], str)
        or command_items[0] != "powdrr-lift"
    ):
        return None
    action_name = command_items[1].replace("-", "_")
    if action_name in _workflow_action_parsers() and action_name != "invoke_tool":
        return action_name
    return None


def _first_class_action_example(action_name: str) -> str:
    examples = {
        "gather_context": '{"action":"gather_context","types":["requirements"]}',
        "prompt_user": '{"action":"prompt_user","text":"What is the decision?"}',
        "edit": '{"action":"edit","file_path":"src/example.py","edits":[...]}',
        "yaml_edit": (
            '{"action":"yaml_edit","file_path":"docs/example.yaml","operations":[...]}'
        ),
        "file_management": (
            '{"action":"file_management","operation":"rename",'
            '"file_path":"old.txt","destination_path":"new.txt"}'
        ),
        "delete_file": '{"action":"delete_file","file_path":"old.txt"}',
        "invoke_skill": '{"action":"invoke_skill","skill":"skill-name"}',
        "goto_step": '{"action":"goto_step","step_id":"step-id"}',
        "read_document": (
            '{"action":"read_document","file_path":"docs/example.md",'
            '"start_line":0,"end_line":20}'
        ),
        "list_files": (
            '{"action":"list_files","directory":"src",'
            '"pattern":"*.py","recursive":true}'
        ),
        "next_step": '{"action":"next_step"}',
        "complete": '{"action":"complete"}',
    }
    return examples[action_name]


def _parse_workflow_action_file_management(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    operation = payload.get("operation")
    if operation not in {"delete", "move", "rename"}:
        raise PowdrrExecutionError(
            "file_management operation must be delete, move, or rename."
        )
    file_path = payload.get("file_path")
    # Some models use the move/rename field name for the target of a delete.
    # Treat it as the source path for delete, while keeping the internal action
    # canonical so execution and event recording remain unchanged.
    if operation == "delete" and (
        not isinstance(file_path, str) or not file_path.strip()
    ):
        file_path = payload.get("destination_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError(
            "file_management action requires file_path or destination_path for delete."
            if operation == "delete"
            else "file_management action requires file_path."
        )
    destination_path = payload.get("destination_path")
    if operation == "delete":
        destination_path = None
    if operation in {"move", "rename"} and (
        not isinstance(destination_path, str) or not destination_path.strip()
    ):
        raise PowdrrExecutionError(
            f"file_management {operation} requires destination_path."
        )
    if destination_path is not None and not isinstance(destination_path, str):
        raise PowdrrExecutionError("file_management destination_path must be a string.")
    return SkillChatAction(
        kind="file_management",
        file_operation=operation,
        file_path=file_path.strip(),
        destination_path=(
            destination_path.strip() if isinstance(destination_path, str) else None
        ),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_delete_file(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError("delete_file action requires file_path.")
    return SkillChatAction(
        kind="delete_file",
        file_operation="delete",
        file_path=file_path.strip(),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_read_document(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PowdrrExecutionError(
            "Workflow read_document action must include file_path."
        )
    start_line = _required_document_line_number(
        payload.get("start_line"),
        field_name="start_line",
    )
    end_line = _required_document_line_number(
        payload.get("end_line"),
        field_name="end_line",
    )
    effective_start_line = max(1, start_line)
    if end_line < effective_start_line:
        raise PowdrrExecutionError(
            "Workflow read_document action end_line must be >= start_line."
        )
    return SkillChatAction(
        kind="read_document",
        file_path=file_path.strip(),
        start_line=start_line,
        end_line=end_line,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_list_files(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    directory = payload.get("directory", ".")
    if not isinstance(directory, str) or not directory.strip():
        raise PowdrrExecutionError(
            "Workflow list_files directory must be a non-empty string."
        )
    pattern = payload.get("pattern")
    if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
        raise PowdrrExecutionError(
            "Workflow list_files pattern must be a non-empty string."
        )
    recursive = payload.get("recursive", False)
    if not isinstance(recursive, bool):
        raise PowdrrExecutionError("Workflow list_files recursive must be a boolean.")
    return SkillChatAction(
        kind="list_files",
        directory=directory.strip(),
        pattern=pattern.strip() if isinstance(pattern, str) else None,
        recursive=recursive,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_next_step(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    _ = payload
    return SkillChatAction(
        kind="next_step",
        output_state=payload.get("output_state"),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_prompt_user(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    text = payload.get("text")
    if not isinstance(text, str):
        raise PowdrrExecutionError(
            'Workflow prompt_user action requires a string text field; use "text".'
        )
    text = _validate_user_question(
        text,
        field_name="Workflow prompt_user action text",
    )
    return SkillChatAction(
        kind="prompt_user",
        text=(text.strip() if text else None),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _execute_shell_tool(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
    announce: bool = True,
    print_stdout: bool = True,
) -> dict[str, Any]:
    if parameters.get("help") is True:
        return builtin_tool_help(str(parameters.get("_tool_name", "shell")))
    command = parameters.get("command")
    validation_command: str | Sequence[str]
    retry_command_source: list[str] | None = None
    if isinstance(command, str):
        command_display = _rtk_command_display(command)
        run_command: str | list[str] = _wrap_shell_command(command)
        use_shell = True
        validation_command = command
        try:
            command_items = shlex.split(command)
        except ValueError:
            command_items = []
        if not any(item in {"&&", ";", "|", ">", "<"} for item in command_items):
            retry_command_source = command_items
    elif isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        normalized_command = [_required_shell_command_item(item) for item in command]
        wrapped_command = _wrap_argument_command(normalized_command)
        command_display = " ".join(shlex.quote(item) for item in wrapped_command)
        run_command = wrapped_command
        use_shell = False
        validation_command = normalized_command
        retry_command_source = normalized_command
    else:
        raise PowdrrExecutionError(
            "Workflow invoke_tool action parameters must include a command."
        )

    empty_pr_error = _empty_pull_request_error(validation_command, worktree_root)
    if empty_pr_error is not None:
        print(empty_pr_error, file=stderr)
        return {
            "command": command_display,
            "cwd": str(worktree_root.resolve()),
            "returncode": 2,
            "stdout": "",
            "stderr": empty_pr_error,
        }

    cwd_value = parameters.get("cwd")
    if cwd_value is None:
        resolved_cwd = worktree_root
    elif isinstance(cwd_value, str) and cwd_value.strip():
        cwd_path = Path(cwd_value.strip())
        resolved_cwd = (
            cwd_path if cwd_path.is_absolute() else worktree_root / cwd_path
        ).resolve(strict=False)
        resolved_worktree_root = worktree_root.resolve(strict=False)
        if not resolved_cwd.is_relative_to(resolved_worktree_root):
            raise PowdrrExecutionError(
                "Workflow shell tool cwd must stay within the current worktree: "
                f"{resolved_worktree_root}"
            )
    else:
        raise PowdrrExecutionError("Workflow invoke_tool action cwd must be a string.")

    env_value = parameters.get("env")
    env = os.environ.copy()
    env["POWDRR_FILE_ADDED_EVENTS"] = "1"
    worktree_python_path = str(worktree_root.resolve())
    existing_python_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        worktree_python_path
        if not existing_python_path
        else worktree_python_path + os.pathsep + existing_python_path
    )
    if env_value is not None:
        if not isinstance(env_value, dict):
            raise PowdrrExecutionError(
                "Workflow invoke_tool action env must be an object."
            )
        for key, value in env_value.items():
            if not isinstance(key, str) or not key:
                raise PowdrrExecutionError(
                    "Workflow invoke_tool action env keys must be non-empty strings."
                )
            if not isinstance(value, str):
                raise PowdrrExecutionError(
                    "Workflow invoke_tool action env values must be strings."
                )
            env[key] = value

    if announce:
        print(f"Invoking shell tool: {command_display}", file=stdout)
        _verbose_print(stderr, verbose, f"Invoking shell tool: {command_display}")
    process = subprocess.run(
        run_command,
        shell=use_shell,
        check=False,
        capture_output=True,
        text=True,
        cwd=resolved_cwd,
        env=env,
    )
    attempted_commands = [command_display]
    if (
        process.returncode != 0
        and retry_command_source is not None
        and missing_executable_output(
            stdout=process.stdout,
            stderr=process.stderr,
        )
    ):
        for variant in dependency_backed_command_variants(
            retry_command_source,
            project_root=resolved_cwd,
        ):
            retry_command = list(variant.command)
            if use_shell:
                retry_run_command: str | list[str] = _wrap_shell_command(
                    shlex.join(retry_command)
                )
                retry_shell = True
            else:
                retry_run_command = _wrap_argument_command(retry_command)
                retry_shell = False
            retry_display = (
                retry_run_command
                if isinstance(retry_run_command, str)
                else " ".join(shlex.quote(item) for item in retry_run_command)
            )
            _verbose_print(
                stderr,
                verbose,
                f"Retrying shell tool with project dependency metadata: "
                f"{retry_display} ({variant.reason})",
            )
            process = subprocess.run(
                retry_run_command,
                shell=retry_shell,
                check=False,
                capture_output=True,
                text=True,
                cwd=resolved_cwd,
                env=env,
            )
            attempted_commands.append(retry_display)
            command_display = retry_display
            if process.returncode == 0 or not missing_executable_output(
                stdout=process.stdout,
                stderr=process.stderr,
            ):
                break
    original_returncode = process.returncode
    process = _normalize_noop_git_commit_result(
        process, validation_command, resolved_cwd
    )
    if process.stdout:
        if print_stdout:
            print(process.stdout, end="", file=stdout)
            _verbose_print(
                stderr, verbose, f"Shell tool stdout:\n{process.stdout.rstrip()}"
            )
    if process.stderr:
        print(process.stderr, end="", file=stderr)
    _verbose_print(
        stderr,
        verbose,
        f"Shell tool exited with code {process.returncode}",
    )
    result = {
        "command": command_display,
        "cwd": str(resolved_cwd),
        "returncode": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
        "no_op": original_returncode != 0 and process.returncode == 0,
    }
    if len(attempted_commands) > 1:
        result["attempted_commands"] = attempted_commands
    return result


def _run_coding_loop_verification(
    step: Any,
    *,
    worktree_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
    runtime: ExecutionRuntime | None,
) -> dict[str, Any] | None:
    coding_loop = getattr(step, "coding_loop", None)
    if coding_loop is None or not coding_loop.verification:
        return None
    results: list[dict[str, Any]] = []
    for verification in coding_loop.verification:
        command = (
            verification.command
            if isinstance(verification.command, str)
            else list(verification.command)
        )
        result = invoke_shell_capability(
            {
                "command": command,
                "cwd": verification.cwd,
                "env": dict(verification.env),
                "_tool_name": "coding_loop_verification",
            },
            worktree_root=worktree_root,
            executor=lambda parameters: _execute_shell_tool(
                dict(parameters),
                worktree_root=worktree_root,
                stdout=stdout,
                stderr=stderr,
                verbose=verbose,
                announce=False,
                print_stdout=False,
            ),
            runtime=runtime,
        )
        passed = isinstance(result, Mapping) and result.get("returncode") == 0
        status = "passed" if passed else "failed"
        returncode = result.get("returncode") if isinstance(result, Mapping) else None
        print(
            f"Coding-loop verification {verification.id}: {status} (exit {returncode})",
            file=stderr,
            flush=True,
        )
        if not passed and isinstance(result, Mapping):
            verification_stdout = result.get("stdout")
            if isinstance(verification_stdout, str) and verification_stdout:
                print(
                    f"Coding-loop verification output:\n{verification_stdout[-4000:]}",
                    file=stderr,
                    flush=True,
                )
        results.append(
            {
                "id": verification.id,
                "command": command,
                "passed": passed,
                "result": result,
            }
        )
    all_passed = all(item["passed"] for item in results)
    return {
        "results": results,
        "all_passed": all_passed,
        "worktree_fingerprint": _coding_loop_worktree_fingerprint(worktree_root),
        "error": None if all_passed else "One or more coding-loop checks failed.",
    }


def _coding_loop_worktree_fingerprint(worktree_root: Path) -> str:
    """Hash material worktree contents for verification evidence binding."""
    digest = hashlib.sha256()
    ignored_directories = {".git", ".venv", "__pycache__", ".pytest_cache"}
    for directory, dirnames, filenames in os.walk(worktree_root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if name not in ignored_directories
        )
        for filename in sorted(filenames):
            path = Path(directory) / filename
            relative_path = path.relative_to(worktree_root).as_posix()
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            try:
                if path.is_symlink():
                    digest.update(b"symlink\0")
                    digest.update(os.readlink(path).encode("utf-8"))
                else:
                    digest.update(b"file\0")
                    digest.update(path.read_bytes())
            except OSError as error:
                digest.update(f"unreadable:{error}".encode())
            digest.update(b"\0")
    return digest.hexdigest()


def _require_coding_loop_verification(
    step: Any,
    events: Sequence[Mapping[str, Any]],
    *,
    step_index: int | None = None,
    worktree_root: Path | None = None,
) -> None:
    _validate_coding_loop_action(
        step,
        events,
        action_kind="next_step",
        step_index=step_index,
        worktree_root=worktree_root,
    )


def _validate_coding_loop_action(
    step: Any,
    events: Sequence[Mapping[str, Any]],
    *,
    action_kind: str,
    step_index: int | None = None,
    worktree_root: Path | None = None,
) -> None:
    """Enforce coding-loop completion from typed events, not model guidance."""
    coding_loop = getattr(step, "coding_loop", None)
    if coding_loop is None or not coding_loop.verification:
        return
    latest = next(
        (
            event
            for event in reversed(events)
            if event.get("kind") == "coding_loop_verification"
            and (step_index is None or event.get("step_index") == step_index)
        ),
        None,
    )
    if action_kind == "next_step" and (
        latest is None or latest.get("all_passed") is not True
    ):
        raise PowdrrExecutionError(
            "This coding_loop step cannot choose next_step until its declared "
            "verification commands pass. Make or repair the implementation, "
            "then wait for the automatic verification result."
        )
    if (
        latest is not None
        and latest.get("all_passed") is True
        and action_kind != "next_step"
    ):
        raise PowdrrExecutionError(
            "This coding_loop step has already passed all declared checks. "
            "Choose next_step with the required output_state; do not perform "
            "another edit, read, or verification run."
        )
    if latest is not None and latest.get("all_passed") is True:
        recorded_fingerprint = latest.get("worktree_fingerprint")
        if (
            worktree_root is not None
            and isinstance(recorded_fingerprint, str)
            and recorded_fingerprint != _coding_loop_worktree_fingerprint(worktree_root)
        ):
            raise PowdrrExecutionError(
                "This coding_loop verification is stale because the worktree "
                "changed after the checks passed. Run the declared verification "
                "commands again before choosing next_step."
            )


def _normalize_noop_git_commit_result(
    process: subprocess.CompletedProcess[str],
    command: str | Sequence[str],
    worktree_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Treat a clean-worktree empty commit as a successful no-op."""
    command_items = shlex.split(command) if isinstance(command, str) else list(command)
    if command_items[:2] != ["git", "commit"] or process.returncode == 0:
        return process
    combined_output = f"{process.stdout}\n{process.stderr}".lower()
    if not any(
        marker in combined_output
        for marker in (
            "nothing to commit",
            "nothing added to commit",
            "no changes added to commit",
        )
    ):
        return process
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        paths = [line[3:] for line in status.stdout.splitlines() if len(line) >= 4]
        path_hint = ", ".join(paths) if paths else "the reported files"
        guidance = (
            "Commit was skipped because Git found no staged changes, but the "
            f"worktree is not clean ({path_hint}). For each intended path, use "
            "the intrinsic git add action, for example "
            '{"action":"invoke_tool","tool":"git","parameters":'
            '{"operation":"add","paths":["path/to/file"]}}. '
            "For an unintended untracked file, delete it with the file-management "
            "action or remove it explicitly, then rerun git status before retrying "
            "the commit."
        )
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout=process.stdout,
            stderr=f"{process.stderr.rstrip()}\n{guidance}\n",
        )
    return subprocess.CompletedProcess(
        process.args,
        0,
        stdout=f"{process.stdout}No changes to commit; existing HEAD retained.\n",
        stderr=process.stderr,
    )


def _empty_pull_request_error(
    command: str | Sequence[str],
    worktree_root: Path,
) -> str | None:
    """Prevent an avoidable GitHub error when a branch has no committed delta."""
    if isinstance(command, str):
        try:
            command_items = shlex.split(command)
        except ValueError:
            return None
    else:
        command_items = list(command)
    if command_items[:3] != ["gh", "pr", "create"]:
        return None

    base = "main"
    for index, item in enumerate(command_items[:-1]):
        if item == "--base":
            base = command_items[index + 1]
            break
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..HEAD"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if ahead.returncode != 0 or ahead.stdout.strip() != "0":
        return None
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (
        "Uncommitted changes are present."
        if status.stdout.strip()
        else "There are no uncommitted changes either."
    )
    return (
        f"Cannot create a pull request: no commits exist between {base} and HEAD. "
        f"{detail} Stage the exact files_to_publish paths, commit them, push the "
        "branch, and retry the pull-request creation step. Do not claim PR "
        "creation succeeded until a PR URL is returned."
    )


def _validate_internal_command(command: object) -> None:
    command_items = _command_items_for_validation(command)
    if command_items[0] != _INTERNAL_BINARY:
        raise PowdrrExecutionError(
            "The internal tool may invoke only the powdrr-lift binary."
        )


def _skill_step_to_data(step: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "description": step.description,
        "step_type": getattr(step, "step_type", "governed"),
        "details": step.details,
        "uses_skill": (
            step.uses_skill.to_data()
            if getattr(step, "uses_skill", None) is not None
            else None
        ),
    }
    if step.id is not None:
        data["id"] = step.id
    if step.tool_invocations:
        data["tool_invocations"] = [
            _tool_invocation_to_data(tool_invocation)
            for tool_invocation in step.tool_invocations
        ]
    if getattr(step, "pre_step", None) is not None:
        data["pre_step"] = step.pre_step.to_data()
    if getattr(step, "coding_loop", None) is not None:
        data["coding_loop"] = step.coding_loop.to_data()
    return data


def _tool_invocation_to_data(tool_invocation: Any) -> dict[str, Any]:
    return tool_invocation.to_data()


def _required_shell_command_item(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(
            "Workflow invoke_tool action command items must be non-empty strings."
        )
    return value.strip()


def _wrap_shell_command(command: str) -> str:
    """Run a shell tool command through rtk without wrapping it twice."""
    if (
        shutil.which("rtk") is None
        or _shell_command_starts_with_rtk(command)
        or _shell_command_is_unwrapped(command)
    ):
        return command
    return f"rtk {command}"


def _rtk_command_display(command: str) -> str:
    return _wrap_shell_command(command)


def _wrap_argument_command(command: list[str]) -> list[str]:
    if shutil.which("rtk") is None or (
        command and (command[0] == "rtk" or command[0] in _RTK_BYPASS_COMMANDS)
    ):
        return command
    return ["rtk", *command]


def _shell_command_starts_with_rtk(command: str) -> bool:
    try:
        command_items = shlex.split(command)
    except ValueError:
        return False
    return bool(command_items) and command_items[0] == "rtk"


# `test` is used by deterministic file-existence gates, but is not an rtk
# subcommand. Wrapping `test -f ...` as `rtk test -f ...` invokes bash help and
# makes an existence gate fail even when the file is present.
_RTK_BYPASS_COMMANDS = frozenset({"test"})


def _shell_command_is_unwrapped(command: str) -> bool:
    try:
        command_items = shlex.split(command)
    except ValueError:
        return False
    return bool(command_items) and command_items[0] in _RTK_BYPASS_COMMANDS


def _required_action_string_item(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(
            "Workflow gather_context action "
            f"{field_name} must contain non-empty strings."
        )
    return value.strip()


def _required_action_string_sequence(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            f"Workflow gather_context action {field_name} must be an array. "
            f"Return a JSON array in the {field_name!r} field, for example "
            f'"{field_name}": ["requirements"].'
        )

    normalized_values = tuple(
        _required_action_string_item(item, field_name=field_name) for item in value
    )
    if not normalized_values:
        raise PowdrrExecutionError(
            f"Workflow gather_context action {field_name} must not be empty."
        )
    return normalized_values


def _optional_action_string_sequence(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            f"Workflow gather_context action {field_name} must be an array."
        )
    return tuple(
        _required_action_string_item(item, field_name=field_name) for item in value
    )


def _optional_action_filters(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PowdrrExecutionError(
            "Workflow gather_context action filters must be an object."
        )
    filters: dict[str, object] = {}
    for raw_field, raw_values in value.items():
        field = _required_action_string_item(raw_field, field_name="filters")
        if isinstance(raw_values, (str, bytes, bytearray)):
            values: object = (raw_values,)
        elif isinstance(raw_values, Sequence):
            values = tuple(
                _required_action_string_item(item, field_name=f"filters.{field}")
                for item in raw_values
            )
        else:
            raise PowdrrExecutionError(
                f"Workflow gather_context action filters.{field} must be an array."
            )
        if not values:
            raise PowdrrExecutionError(
                f"Workflow gather_context action filters.{field} must not be empty."
            )
        filters[field] = values
    return filters


def _required_edit_operations(value: object) -> tuple[SkillChatEdit, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            "Workflow edit action edits must be an array.", action_kind="edit"
        )

    edits = tuple(_required_edit_operation(item) for item in value)
    if not edits:
        raise PowdrrExecutionError(
            "Workflow edit action edits must not be empty.", action_kind="edit"
        )
    return edits


def _required_file_edits(value: object) -> tuple[SkillChatFileEdits, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise PowdrrExecutionError(
            "Workflow edit action file_edits must be an array.", action_kind="edit"
        )

    file_edits: list[SkillChatFileEdits] = []
    for item in value:
        if not isinstance(item, dict):
            raise PowdrrExecutionError(
                "Workflow edit action file_edits must contain objects.",
                action_kind="edit",
            )
        file_path = item.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise PowdrrExecutionError(
                "Workflow edit action file_edits entries must include file_path.",
                action_kind="edit",
            )
        file_edits.append(
            SkillChatFileEdits(
                file_path=file_path.strip(),
                edits=_required_edit_operations(item.get("edits")),
            )
        )
    if not file_edits:
        raise PowdrrExecutionError(
            "Workflow edit action file_edits must not be empty.", action_kind="edit"
        )
    return tuple(file_edits)


def _required_edit_operation(value: object) -> SkillChatEdit:
    if not isinstance(value, dict):
        raise PowdrrExecutionError(
            "Workflow edit action edits must be objects.", action_kind="edit"
        )

    kind = value.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise PowdrrExecutionError(
            "Workflow edit action edit kind must be a string.", action_kind="edit"
        )
    normalized_kind = kind.strip()
    if normalized_kind not in {"add", "remove", "replace"}:
        raise PowdrrExecutionError(
            "Workflow edit action edit kind must be add, remove, or replace.",
            action_kind="edit",
        )

    start_line = _required_edit_line_number(
        value.get("start_line"),
        field_name="start_line",
    )
    end_line_value = value.get("end_line")
    end_line = None
    if end_line_value is not None:
        end_line = _required_edit_line_number(end_line_value, field_name="end_line")
        if end_line < start_line:
            raise PowdrrExecutionError(
                "Workflow edit action end_line must be >= start_line.",
                action_kind="edit",
            )

    text_value = value.get("text")
    if normalized_kind in {"add", "replace"}:
        if not isinstance(text_value, str):
            raise PowdrrExecutionError(
                "Workflow edit action add/replace edits must include text.",
                action_kind="edit",
            )
        text = text_value
    else:
        if text_value is not None:
            raise PowdrrExecutionError(
                "Workflow edit action remove edits must not include text.",
                action_kind="edit",
            )
        text = None
        if end_line is None:
            end_line = start_line

    return SkillChatEdit(
        kind=normalized_kind,
        start_line=start_line,
        end_line=end_line,
        text=text,
    )


def _required_edit_line_number(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PowdrrExecutionError(
            f"Workflow edit action {field_name} must be a positive integer.",
            action_kind="edit",
        )
    return value


def _required_document_line_number(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PowdrrExecutionError(
            f"Workflow read_document action {field_name} must be a "
            "non-negative integer."
        )
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PowdrrExecutionError(
            "Workflow action decisions_and_context must be a string."
        )
    normalized_value = value.strip()
    return normalized_value or None


def _optional_llm_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError("Workflow llm_type must be a non-empty string.")
    return value.strip().lower().replace("-", "_")


def _complete_json_with_model_fallback(
    *,
    client_for: Callable[[str, str], WorkflowLLMClient],
    messages: list[dict[str, str]],
    context: str,
    model: str,
    parser: Callable[[dict[str, Any]], Any],
    repair_instructions: str,
    config: SkillChatConfig,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
    model_mappings: Sequence[tuple[str, LLMModelMapping]],
    provider: str,
    empty_response_fallback_payload: dict[str, Any] | None = None,
    error_recorder: Callable[[RuntimeError, dict[str, Any] | None], None] | None = None,
    response_schema: Mapping[str, Any] | None = None,
) -> tuple[Any | None, str, str]:
    active_model = model
    active_provider = provider
    attempted_models = {model.casefold()}
    while True:
        long_context_backup = long_context_backup_for(
            active_model,
            model_mappings,
        )
        estimated_input_tokens = _estimate_message_tokens(messages)
        _verbose_json(
            stderr,
            config.verbose,
            "Prompt size breakdown",
            prompt_size_breakdown(messages),
        )
        active_limits = _model_limits_for(active_provider, active_model)
        if (
            long_context_backup is not None
            and estimated_input_tokens + _CONTEXT_SAFETY_MARGIN_TOKENS
            >= active_limits.context_window
            and long_context_backup.model.casefold() not in attempted_models
        ):
            print(
                f"{context} estimated context is too large for model "
                f"{active_model!r} ({estimated_input_tokens} input tokens; "
                f"limit {active_limits.context_window}). Switching to long-"
                f"context backup model {long_context_backup.model!r}.",
                file=stderr,
            )
            attempted_models.add(long_context_backup.model.casefold())
            active_model = long_context_backup.model
            active_provider = long_context_backup.provider
            continue
        try:
            result = _complete_json_with_repair(
                client_for(active_model, active_provider),
                messages,
                context=context,
                model=active_model,
                parser=parser,
                repair_instructions=repair_instructions,
                config=config,
                input_func=input_func,
                stdout=stdout,
                stderr=stderr,
                fallback_on_transient_exhaustion=(
                    backup_model_for(active_model, model_mappings) is not None
                ),
                empty_response_fallback_payload=empty_response_fallback_payload,
                error_recorder=error_recorder,
                response_schema=response_schema,
            )
            return result, active_model, active_provider
        except _ModelUnavailableError as exc:
            backup_model = backup_model_for(active_model, model_mappings)
            if (
                backup_model is None
                or backup_model.model.casefold() in attempted_models
            ):
                print(
                    f"{context} model {active_model!r} is unavailable and no "
                    "unused backup model is configured.",
                    file=stderr,
                )
                return None, active_model, active_provider
                if isinstance(exc, _SemanticRepairExhaustedError):
                    print(
                        f"{context} semantic repair was exhausted for "
                        f"{active_model!r}: {exc}. Switching to backup model "
                        f"{backup_model.model!r}.",
                        file=stderr,
                    )
                else:
                    print(
                        f"{context} model {active_model!r} is unavailable: {exc}. "
                        f"Switching to backup model {backup_model.model!r}.",
                        file=stderr,
                    )
            attempted_models.add(backup_model.model.casefold())
            active_model = backup_model.model
            active_provider = backup_model.provider


def _complete_json_with_repair(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
    *,
    context: str,
    model: str,
    parser: Callable[[dict[str, Any]], Any],
    repair_instructions: str,
    config: WorkflowChatConfig,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
    fallback_on_transient_exhaustion: bool = False,
    empty_response_fallback_payload: dict[str, Any] | None = None,
    error_recorder: Callable[[RuntimeError, dict[str, Any] | None], None] | None = None,
    response_schema: Mapping[str, Any] | None = None,
) -> Any | None:
    empty_question_reprompts = 0
    empty_response_reprompts = 0
    last_repair_fingerprint: tuple[str, str] | None = None
    repeated_repair_count = 0
    while True:
        _verbose_json(
            stderr,
            config.verbose,
            f"{context} LLM input (model={model})",
            messages,
        )
        _print_waiting_for_model(stderr, model)
        try:
            payload = _request_json(client, messages, response_schema=response_schema)
            _verbose_json(
                stderr,
                config.verbose,
                f"{context} LLM output (model={model})",
                payload,
            )
        except RuntimeError as exc:
            if error_recorder is not None:
                error_recorder(exc, None)
            if isinstance(exc, _ModelUnavailableError):
                raise
            if isinstance(exc, LocalModelRuntimeError):
                raise
            if _is_model_unavailable_error(exc):
                raise _ModelUnavailableError(
                    f"provider reported that model {model!r} is unavailable"
                ) from exc
            if _is_transient_provider_error(exc):
                retry_attempts = max(1, config.provider_retry_attempts)
                for retry_attempt in range(1, retry_attempts + 1):
                    delay_seconds = max(0.0, config.provider_retry_delay_seconds)
                    print(
                        f"{context} failed for model {model!r}: {exc}. "
                        f"Waiting {delay_seconds:g} seconds before automatic "
                        f"retry {retry_attempt}/{retry_attempts}.",
                        file=stderr,
                    )
                    time.sleep(delay_seconds)
                    try:
                        _print_waiting_for_model(stderr, model)
                        payload = _request_json(
                            client, messages, response_schema=response_schema
                        )
                        _verbose_json(
                            stderr,
                            config.verbose,
                            f"{context} LLM retry output (model={model})",
                            payload,
                        )
                        break
                    except RuntimeError as retry_exc:
                        if error_recorder is not None:
                            error_recorder(retry_exc, None)
                        if _is_model_unavailable_error(retry_exc):
                            raise _ModelUnavailableError(
                                f"provider reported that model {model!r} is unavailable"
                            ) from retry_exc
                        exc = retry_exc
                else:
                    payload = None

                if payload is not None:
                    try:
                        return parser(payload)
                    except RuntimeError as retry_parse_exc:
                        if error_recorder is not None:
                            error_recorder(retry_parse_exc, payload)
                        print(
                            f"{context} retry response needs repair: {retry_parse_exc}",
                            file=stderr,
                        )
                else:
                    print(
                        f"{context} automatic retries exhausted for model {model!r}.",
                        file=stderr,
                    )
                    if fallback_on_transient_exhaustion:
                        raise _ModelUnavailableError(
                            f"provider retries were exhausted for model {model!r}"
                        ) from exc
            elif _is_json_repairable_error(exc):
                _verbose_print(
                    stderr,
                    config.verbose,
                    f"Attempting automatic repair for {context} after provider failure",
                )
                repair_error_message = str(exc)
                if _is_empty_response_error(exc):
                    repair_error_message += (
                        " The response cannot be empty. Return a complete "
                        "corrected JSON object."
                    )
                try:
                    repaired_payload = _attempt_json_repair(
                        client,
                        messages,
                        context=context,
                        model=model,
                        error_message=repair_error_message,
                        repair_instructions=repair_instructions,
                        stderr=stderr,
                        verbose=config.verbose,
                        error_recorder=error_recorder,
                        response_schema=response_schema,
                    )
                except _EmptyProviderResponseError as empty_exc:
                    empty_response_reprompts += 1
                    _print_empty_response_exchange(
                        context=context,
                        model=model,
                        attempts=empty_response_reprompts,
                        provider_error=str(empty_exc),
                        messages=empty_exc.messages or messages,
                        stderr=stderr,
                    )
                    if empty_response_reprompts > 1:
                        if empty_response_fallback_payload is not None:
                            print(
                                f"{context} corrective response was empty; "
                                "interpreting it as next_step.",
                                file=stderr,
                            )
                            return parser(empty_response_fallback_payload)
                        if not _ask_to_retry_empty_response(
                            context=context,
                            model=model,
                            attempts=empty_response_reprompts,
                            provider_error=str(empty_exc),
                            messages=empty_exc.messages or messages,
                            input_func=input_func,
                            stdout=stdout,
                            stderr=stderr,
                        ):
                            return None
                        empty_response_reprompts = 0
                        continue
                    print(
                        f"{context} returned an empty response; requesting a "
                        "corrected response.",
                        file=stderr,
                    )
                    messages = _build_json_repair_messages(
                        messages,
                        context=context,
                        error_message=(
                            f"{empty_exc} The response cannot be empty. Return a "
                            "complete corrected JSON object."
                        ),
                        repair_instructions=repair_instructions,
                        previous_payload=None,
                    )
                    continue
                if repaired_payload is not None:
                    repair_fingerprint = _repair_response_fingerprint(
                        messages,
                        repaired_payload,
                    )
                    if repair_fingerprint == last_repair_fingerprint:
                        repeated_repair_count += 1
                    else:
                        repeated_repair_count = 0
                    if repeated_repair_count >= _MAX_REPEATED_REPAIR_ATTEMPTS:
                        print(
                            f"{context} made no progress during response repair; "
                            "switching to the configured fallback model.",
                            file=stderr,
                        )
                        raise _SemanticRepairExhaustedError(
                            f"{context} repeated the same invalid response "
                            f"{_MAX_REPEATED_REPAIR_ATTEMPTS} times"
                        ) from None
                    last_repair_fingerprint = repair_fingerprint
                    try:
                        return parser(repaired_payload)
                    except RuntimeError as repair_exc:
                        if error_recorder is not None:
                            error_recorder(repair_exc, repaired_payload)
                        print(
                            "Repaired "
                            f"{context} response was still invalid: {repair_exc}",
                            file=stderr,
                        )
                        if _is_invalid_user_question_error(repair_exc):
                            empty_question_reprompts += 1
                            if empty_question_reprompts > _MAX_EMPTY_QUESTION_REPROMPTS:
                                raise PowdrrExecutionError(
                                    f"{context} LLM repeatedly returned an invalid "
                                    "user question."
                                ) from repair_exc
                            print(
                                f"{context} received an invalid user question. "
                                "Requesting a properly formed English question "
                                "from the LLM "
                                f"(attempt {empty_question_reprompts}/"
                                f"{_MAX_EMPTY_QUESTION_REPROMPTS}).",
                                file=stderr,
                            )
                            messages = _build_json_repair_messages(
                                messages,
                                context=context,
                                error_message=str(repair_exc),
                                repair_instructions=(
                                    repair_instructions
                                    + " Return a concise, specific, properly formed "
                                    "English question ending with a question mark in "
                                    "the user-question field."
                                ),
                                previous_payload=repaired_payload,
                            )
                            continue
                print(
                    f"{context} repair request failed; requesting the original "
                    "response again with an updated correction instruction.",
                    file=stderr,
                )
                messages = _build_json_repair_messages(
                    messages,
                    context=context,
                    error_message=(
                        f"{exc} The repair request itself failed. Correct the "
                        "original response directly."
                    ),
                    repair_instructions=repair_instructions,
                    previous_payload=None,
                )
                continue
            else:
                print(f"{context} failed: {exc}", file=stderr)
            retry = _prompt_user(
                "Type 'retry' to try again or 'abort' to stop: ",
                input_func=input_func,
                stdout=stdout,
                status_stream=stderr,
            )
            _verbose_print(
                stderr,
                config.verbose,
                f"User chose {retry!r} after {context} failure",
            )
            if retry.strip().lower() == "retry":
                continue
            print(f"Stopping after {context} failure.", file=stderr)
            return None
        assert payload is not None
        try:
            return parser(payload)
        except RuntimeError as exc:
            if error_recorder is not None:
                error_recorder(exc, payload)
            print(f"{context} response needs repair: {exc}", file=stderr)
            _verbose_print(
                stderr,
                config.verbose,
                f"Attempting automatic repair for {context} after validation failure",
            )
            repair_error_message = str(exc)
            if _is_empty_response_error(exc):
                repair_error_message += (
                    " The response cannot be empty. Return a complete corrected "
                    "JSON object."
                )
            try:
                repaired_payload = _attempt_json_repair(
                    client,
                    messages,
                    context=context,
                    model=model,
                    error_message=repair_error_message,
                    repair_instructions=repair_instructions,
                    previous_payload=payload,
                    stderr=stderr,
                    verbose=config.verbose,
                    error_recorder=error_recorder,
                    response_schema=response_schema,
                )
            except _EmptyProviderResponseError as empty_exc:
                empty_response_reprompts += 1
                _print_empty_response_exchange(
                    context=context,
                    model=model,
                    attempts=empty_response_reprompts,
                    provider_error=str(empty_exc),
                    messages=empty_exc.messages or messages,
                    stderr=stderr,
                )
                if empty_response_reprompts > 1:
                    if not _ask_to_retry_empty_response(
                        context=context,
                        model=model,
                        attempts=empty_response_reprompts,
                        provider_error=str(empty_exc),
                        messages=empty_exc.messages or messages,
                        input_func=input_func,
                        stdout=stdout,
                        stderr=stderr,
                    ):
                        return None
                    empty_response_reprompts = 0
                    continue
                print(
                    f"{context} returned an empty response; requesting a corrected "
                    "response.",
                    file=stderr,
                )
                messages = _build_json_repair_messages(
                    messages,
                    context=context,
                    error_message=(
                        f"{empty_exc} The response cannot be empty. Return a "
                        "complete corrected JSON object."
                    ),
                    repair_instructions=repair_instructions,
                    previous_payload=payload,
                )
                continue
            if repaired_payload is not None:
                repair_fingerprint = _repair_response_fingerprint(
                    messages,
                    repaired_payload,
                )
                if repair_fingerprint == last_repair_fingerprint:
                    repeated_repair_count += 1
                else:
                    repeated_repair_count = 0
                if repeated_repair_count >= _MAX_REPEATED_REPAIR_ATTEMPTS:
                    print(
                        f"{context} made no progress during response repair; "
                        "switching to the configured fallback model.",
                        file=stderr,
                    )
                    raise _SemanticRepairExhaustedError(
                        f"{context} repeated the same invalid response "
                        f"{_MAX_REPEATED_REPAIR_ATTEMPTS} times"
                    ) from None
                last_repair_fingerprint = repair_fingerprint
                try:
                    return parser(repaired_payload)
                except RuntimeError as repair_exc:
                    if error_recorder is not None:
                        error_recorder(repair_exc, repaired_payload)
                    print(
                        f"{context} repaired response was still invalid: {repair_exc}",
                        file=stderr,
                    )
                    if _is_invalid_user_question_error(repair_exc):
                        empty_question_reprompts += 1
                        if empty_question_reprompts > _MAX_EMPTY_QUESTION_REPROMPTS:
                            raise PowdrrExecutionError(
                                f"{context} LLM repeatedly returned an invalid user "
                                "question."
                            ) from repair_exc
                        print(
                            f"{context} received an invalid user question. "
                            "Requesting a properly formed English question from "
                            "the LLM "
                            f"(attempt {empty_question_reprompts}/"
                            f"{_MAX_EMPTY_QUESTION_REPROMPTS}).",
                            file=stderr,
                        )
                        messages = _build_json_repair_messages(
                            messages,
                            context=context,
                            error_message=str(repair_exc),
                            repair_instructions=(
                                repair_instructions
                                + " Return a concise, specific, properly formed "
                                "English question ending with a question mark in the "
                                "user-question field."
                            ),
                            previous_payload=repaired_payload,
                        )
                        continue
            print(
                f"{context} repair request failed; requesting the original "
                "response again with an updated correction instruction.",
                file=stderr,
            )
            messages = _build_json_repair_messages(
                messages,
                context=context,
                error_message=(
                    f"{exc} The repair request itself failed. Correct the original "
                    "response directly."
                ),
                repair_instructions=repair_instructions,
                previous_payload=(
                    repaired_payload if repaired_payload is not None else payload
                ),
            )
            continue
            retry = _prompt_user(
                "Type 'retry' to try again or 'abort' to stop: ",
                input_func=input_func,
                stdout=stdout,
                status_stream=stderr,
            )
            _verbose_print(
                stderr,
                config.verbose,
                f"User chose {retry!r} after {context} repair failure",
            )
            if retry.strip().lower() == "retry":
                continue
            print(f"Stopping after {context} failure.", file=stderr)
            return None


def _print_waiting_for_model(stderr: TextIO, model: str) -> None:
    print(f"waiting for {model} LLM response...", file=stderr, flush=True)


def _is_invalid_user_question_error(exc: RuntimeError) -> bool:
    return "must be a non-empty, properly formed English question" in str(exc)


def _ask_to_retry_empty_response(
    *,
    context: str,
    model: str,
    attempts: int,
    provider_error: str,
    messages: Sequence[dict[str, str]],
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
) -> bool:
    """Ask for an explicit recovery choice instead of completing silently."""
    _print_empty_response_exchange(
        context=context,
        model=model,
        attempts=attempts,
        provider_error=provider_error,
        messages=messages,
        stderr=stderr,
    )

    answer = _prompt_user(
        (
            f"{context} returned an empty response after {attempts} corrective "
            "reprompt attempts. Would you like me to retry this LLM request? "
            "Answer 'retry' or 'stop': "
        ),
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )
    return answer.strip().lower() in {"retry", "yes", "y"}


def _print_empty_response_exchange(
    *,
    context: str,
    model: str,
    attempts: int,
    provider_error: str,
    messages: Sequence[dict[str, str]],
    stderr: TextIO,
) -> None:
    """Print every empty exchange, including ones recovered automatically."""
    diagnostic = (
        f"Empty-response context: {context}; model={model!r}; "
        f"corrective-reprompt-attempts={attempts}; provider_error={provider_error}\n"
        "LLM request messages:\n"
        f"{json.dumps(list(messages), indent=2, ensure_ascii=False)}\n"
        "LLM response: <empty>"
    )
    serialized_prompt = json.dumps(list(messages), ensure_ascii=False)
    print(diagnostic, file=stderr, flush=True)
    print(
        "[workflow] Empty-response exchange: "
        f"prompt={serialized_prompt} response=<empty>",
        file=stderr,
        flush=True,
    )


def _parse_json_object(content: str, context: str) -> dict[str, Any]:
    normalized_content = content.strip()
    try:
        parsed_content = json.loads(normalized_content)
    except json.JSONDecodeError as exc:
        try:
            parsed_content = laga.repair(normalized_content)
        except laga.LagaError:
            parsed_content = _extract_embedded_json_object(normalized_content)
            if parsed_content is None:
                raise PowdrrExecutionError(
                    f"{context} was not valid JSON: {exc.msg} at line "
                    f"{exc.lineno}, column {exc.colno}.\nResponse content:\n{content}"
                ) from exc
    if not isinstance(parsed_content, dict):
        raise PowdrrExecutionError(f"{context} must be a JSON object.")
    return cast("dict[str, Any]", parsed_content)


def _extract_embedded_json_object(content: str) -> dict[str, Any] | None:
    """Accept JSON objects surrounded by common LLM presentation noise."""
    fenced_blocks = re.findall(
        r"```(?:json|JSON)?\s*\n?(.*?)```",
        content,
        flags=re.DOTALL,
    )
    candidates = [*fenced_blocks, content]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return cast("dict[str, Any]", parsed)
    return None


def _normalize_structured_document_text(path: Path, text: str) -> str:
    """Remove a single Markdown fence when an LLM wraps a structured file."""
    if path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        return text
    match = re.fullmatch(
        r"\s*```(?:json|yaml|yml)?\s*\n(.*?)\n?```\s*",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) + "\n" if match is not None else text


def _validate_structured_document_text(path: Path, text: str) -> None:
    """Reject malformed JSON/YAML before an edit is persisted."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise _WorkflowStructuredDocumentError(
                f"Edited JSON file {path} is invalid at line {exc.lineno}, "
                f"column {exc.colno}: {exc.msg}. Correct the JSON before "
                "continuing."
            ) from exc
    elif suffix in {".yaml", ".yml"}:
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            problem_mark = getattr(exc, "problem_mark", None)
            if problem_mark is not None:
                location = (
                    f"line {problem_mark.line + 1}, column {problem_mark.column + 1}"
                )
            else:
                location = "an unknown location"
            problem = getattr(exc, "problem", None) or str(exc)
            raise _WorkflowStructuredDocumentError(
                f"Edited YAML file {path} is invalid at {location}: {problem}. "
                "Correct the YAML before continuing."
            ) from exc


def _edit_to_data(edit: SkillChatEdit) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": edit.kind,
        "start_line": edit.start_line,
    }
    if edit.end_line is not None:
        data["end_line"] = edit.end_line
    if edit.text is not None:
        data["text"] = edit.text
    return data


def _yaml_operation_to_data(operation: SkillChatYamlOperation) -> dict[str, Any]:
    data: dict[str, Any] = {"op": operation.operation}
    if operation.section is not None:
        data["section"] = operation.section
    if operation.item_id is not None:
        data["id"] = operation.item_id
    if operation.item_index is not None:
        data["index"] = operation.item_index
    if operation.path:
        data["path"] = list(operation.path)
    if operation.value is not None:
        data["value"] = operation.value
    return data


def _file_edits_to_data(file_edits: SkillChatFileEdits) -> dict[str, Any]:
    return {
        "file_path": file_edits.file_path,
        "edits": [_edit_to_data(edit) for edit in file_edits.edits],
    }


def _current_file_context(
    worktree_root: Path,
    current_file_path: Path | None,
    *,
    cache: dict[tuple[str, int, int], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if current_file_path is None:
        return None

    resolved_path = _resolve_worktree_file_path(
        str(current_file_path),
        worktree_root,
    )
    if not resolved_path.exists():
        return {
            "path": str(resolved_path.relative_to(worktree_root)),
            "exists": False,
        }
    if not resolved_path.is_file():
        return {
            "path": str(resolved_path.relative_to(worktree_root)),
            "exists": False,
        }

    stat = resolved_path.stat()
    cache_key = (str(resolved_path), stat.st_mtime_ns, stat.st_size)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    lines = resolved_path.read_text(encoding="utf-8").splitlines()
    serialized_size = sum(len(line) for line in lines)
    content_omitted = (
        len(lines) > _MAX_PROMPT_FILE_LINES or serialized_size > _MAX_PROMPT_FILE_CHARS
    )
    prompt_lines = [] if content_omitted else lines
    context = {
        "path": str(resolved_path.relative_to(worktree_root)),
        "exists": True,
        "line_count": len(lines),
        "lines": [
            {
                "line_number": line_number,
                "text": line,
            }
            for line_number, line in enumerate(prompt_lines, start=1)
        ],
    }
    if content_omitted:
        context.update(
            {
                "content_omitted": True,
                "content_omitted_reason": (
                    "Use read_document to inspect the required line range."
                ),
            }
        )
    if cache is not None:
        cache.clear()
        cache[cache_key] = context
    return context


def _apply_file_edits(current_text: str, edits: Sequence[SkillChatEdit]) -> str:
    lines = current_text.splitlines()
    for edit in sorted(edits, key=_edit_sort_key, reverse=True):
        start_index = edit.start_line - 1
        if edit.kind == "add":
            if start_index > len(lines):
                raise _WorkflowEditRangeError(
                    "Workflow edit action add start_line "
                    f"{edit.start_line} is beyond the end of the file, which has "
                    f"{len(lines)} lines."
                )
            insert_lines = edit.text.splitlines() if edit.text is not None else []
            lines[start_index:start_index] = insert_lines
            continue

        end_line = edit.end_line if edit.end_line is not None else edit.start_line
        end_index = end_line
        if end_index > len(lines):
            raise _WorkflowEditRangeError(
                "Workflow edit action range ends at line "
                f"{end_line}, but the file has {len(lines)} lines."
            )

        if edit.kind == "remove":
            del lines[start_index:end_index]
            continue

        replacement_lines = edit.text.splitlines() if edit.text is not None else []
        lines[start_index:end_index] = replacement_lines

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _apply_yaml_operations(
    path: Path,
    current_text: str,
    operations: Sequence[SkillChatYamlOperation],
) -> str:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise _WorkflowYamlEditError(
            "yaml_edit only supports .yaml and .yml files. Use edit for other "
            "file types."
        )
    try:
        document = yaml.safe_load(current_text)
    except yaml.YAMLError as exc:
        raise _WorkflowYamlEditError(
            f"Cannot apply yaml_edit because {path} is already invalid YAML: "
            f"{exc}. Repair the YAML with edit first, then retry yaml_edit."
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise _WorkflowYamlEditError(
            "yaml_edit requires the document root to be a YAML mapping."
        )
    updated: dict[str, Any] = dict(document)

    for operation in operations:
        if operation.operation == "set_value":
            if not operation.path:
                raise _WorkflowYamlEditError(
                    "set_value requires a non-empty path, for example "
                    '["title"] or ["features", "0", "description"].'
                )
            target: Any = updated
            for key in operation.path[:-1]:
                if isinstance(target, dict) and key in target:
                    target = target[key]
                    continue
                if isinstance(target, list) and key.isdigit():
                    index = int(key)
                    if 0 <= index < len(target):
                        target = target[index]
                        continue
                raise _WorkflowYamlEditError(
                    f"set_value path {list(operation.path)!r} cannot find "
                    f"mapping key or list index {key!r}. Re-read the YAML and "
                    "use the exact path from the current document."
                )
            final_key = operation.path[-1]
            if isinstance(target, dict):
                target[final_key] = operation.value
            elif isinstance(target, list) and final_key.isdigit():
                index = int(final_key)
                if not 0 <= index < len(target):
                    raise _WorkflowYamlEditError(
                        f"set_value list index {final_key!r} is outside the "
                        f"current list of length {len(target)}."
                    )
                target[index] = operation.value
            else:
                raise _WorkflowYamlEditError(
                    f"set_value path {list(operation.path)!r} does not resolve "
                    "to a YAML mapping or list index."
                )
            continue

        if operation.operation == "remove_key":
            if not operation.path:
                raise _WorkflowYamlEditError(
                    "remove_key requires a non-empty mapping-key path."
                )
            remove_target: Any = updated
            for key in operation.path[:-1]:
                if isinstance(remove_target, dict) and key in remove_target:
                    remove_target = remove_target[key]
                    continue
                if isinstance(remove_target, list) and key.isdigit():
                    index = int(key)
                    if 0 <= index < len(remove_target):
                        remove_target = remove_target[index]
                        continue
                raise _WorkflowYamlEditError(
                    f"remove_key path {list(operation.path)!r} cannot find "
                    f"mapping key {key!r}. Re-read the YAML and use the exact "
                    "path from the current document."
                )
            final_key = operation.path[-1]
            if not isinstance(remove_target, dict):
                raise _WorkflowYamlEditError(
                    f"remove_key path {list(operation.path)!r} does not resolve "
                    "to a YAML mapping."
                )
            if final_key not in remove_target:
                raise _WorkflowYamlEditError(
                    f"No mapping key {final_key!r} exists at path "
                    f"{list(operation.path)!r}. Re-read the YAML before retrying."
                )
            del remove_target[final_key]
            continue

        if operation.section is None or (
            operation.item_id is None and operation.item_index is None
        ):
            raise _WorkflowYamlEditError(
                f"{operation.operation} requires section plus id or index. "
                "Use an operation "
                'such as {"op": "upsert_item", "section": "features", '
                '"id": "feature-id", "value": {...}} or {"op": '
                '"remove_item", "section": "requirements", "index": 0}.'
            )
        raw_items = updated.get(operation.section)
        if raw_items is None and operation.operation == "upsert_item":
            raw_items = []
            updated[operation.section] = raw_items
        if not isinstance(raw_items, list):
            raise _WorkflowYamlEditError(
                f"YAML section {operation.section!r} must be a list for "
                f"{operation.operation}. Re-read the document and use the exact "
                "section name."
            )

        matching_indexes = [
            index
            for index, item in enumerate(raw_items)
            if isinstance(item, Mapping) and item.get("id") == operation.item_id
        ]
        if operation.operation == "remove_item" and operation.item_index is not None:
            if not 0 <= operation.item_index < len(raw_items):
                raise _WorkflowYamlEditError(
                    f"No item at index {operation.item_index} exists in section "
                    f"{operation.section!r}. Re-read the YAML before retrying."
                )
            del raw_items[operation.item_index]
            continue
        if len(matching_indexes) > 1:
            raise _WorkflowYamlEditError(
                f"YAML section {operation.section!r} contains duplicate id "
                f"{operation.item_id!r}; repair the duplicates before yaml_edit."
            )
        if operation.operation == "remove_item":
            if not matching_indexes:
                raise _WorkflowYamlEditError(
                    f"No item with id {operation.item_id!r} exists in section "
                    f"{operation.section!r}. Use read_document to inspect ids "
                    "before retrying."
                )
            del raw_items[matching_indexes[0]]
            continue

        if operation.operation == "upsert_item":
            if not isinstance(operation.value, Mapping):
                raise _WorkflowYamlEditError(
                    "upsert_item requires value to be a mapping containing the "
                    "complete item fields."
                )
            item = dict(operation.value)
            item["id"] = operation.item_id
            if matching_indexes:
                raw_items[matching_indexes[0]] = item
            else:
                raw_items[:] = [
                    existing
                    for existing in raw_items
                    if not (
                        isinstance(existing, Mapping) and existing.get("id") is None
                    )
                ]
                raw_items.append(item)
            continue

        raise _WorkflowYamlEditError(
            f"Unsupported yaml_edit operation {operation.operation!r}. Use "
            "upsert_item, remove_item, remove_key, or set_value."
        )

    return yaml.safe_dump(
        updated,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


def _workflow_edit_failure_feedback(
    action: SkillChatAction,
    error: Exception,
    current_file_context: dict[str, Any] | None,
) -> str:
    error = _underlying_execution_error(error)
    feedback = (
        f"Workflow {action.kind} action failed: {error}. "
        "Re-read the current file context and return a corrected action."
    )
    if isinstance(error, PowdrrExecutionError):
        feedback += f" Error code: {error.error_code}."
        if error.remediation:
            feedback += f" Remediation: {error.remediation}"
        if error.details:
            feedback += (
                " Diagnostic details: "
                + json.dumps(error.details, sort_keys=True)
                + "."
            )
        if (
            error.error_code == "capability_not_executable"
            and error.details.get("capability") == "basedpyright-structure"
        ):
            feedback += (
                " This is a capability argument error, not a reason to reread the "
                "same document. Choose a materially different exact .py path from "
                "candidate_python_files, or use an available discovery action."
            )
    if isinstance(error, _WorkflowEditRangeError):
        if current_file_context and current_file_context.get("exists"):
            feedback += (
                " The current file has "
                f"{current_file_context['line_count']} lines; every edit range "
                "must stay within that line count."
            )
    elif isinstance(error, _WorkflowStructuredDocumentError):
        feedback += (
            " The edit range was within the file, but the resulting structured "
            "document is invalid. Preserve surrounding mapping keys and YAML "
            "indentation, such as section headers like `entities:`. If a prose "
            "value contains embedded double quotes, colons, or YAML punctuation, "
            "use a single-quoted scalar or a `>-` block scalar; do not use "
            "unescaped double quotes inside a double-quoted value. Correct the "
            "document before retrying."
        )
    elif isinstance(error, _WorkflowYamlEditError):
        feedback += (
            " Prefer yaml_edit for .yaml or .yml files. Its operations are "
            "structural: upsert_item replaces or appends a list item by section "
            "and id, remove_item deletes one by section and id (or by an exact "
            "validator-reported list index for boilerplate), and set_value "
            "updates a mapping value by path; remove_key deletes an exact mapping "
            "key path. Include multiple independent "
            "operations in one yaml_edit action when correcting the same file. "
            "If the structural operations cannot express the repair, use a normal "
            "edit with exact line ranges, preserve YAML indentation and section "
            "headers, and rerun the validator."
        )
    if action.kind == "yaml_edit" and "does not exist" in str(error):
        feedback += (
            " This target is absent and no change was applied. yaml_edit cannot "
            "create a document: do not retry this file_path or invent a name such "
            "as requirements.yaml. First use the declared read/list or generator "
            "action to identify or create the exact repository-relative YAML path, "
            "then apply yaml_edit to that existing path."
        )
    return feedback


def _underlying_execution_error(error: Exception) -> Exception:
    """Recover the concrete Powdrr action error for specialized feedback."""
    if isinstance(error, PowdrrExecutionError) and error.cause_error is not None:
        return error.cause_error
    return error


def _rejected_edit_guidance(action: SkillChatAction) -> str:
    if action.kind not in {"edit", "yaml_edit"}:
        return ""
    return (
        "\n\nLast proposed edit (NOT APPLIED):\n"
        f"{_workflow_action_signature(action)}\n"
        "Do not repeat it unchanged. Re-read the current file and return a "
        "corrected action using the structural yaml_edit contract when editing "
        "YAML."
    )


def _edit_sort_key(edit: SkillChatEdit) -> tuple[int, int]:
    end_line = edit.end_line if edit.end_line is not None else edit.start_line
    return edit.start_line, end_line


def _resolve_worktree_file_path(file_path_value: str, worktree_root: Path) -> Path:
    resolved_path = Path(file_path_value.strip())
    if resolved_path.is_absolute():
        candidate_path = resolved_path.resolve(strict=False)
    else:
        candidate_path = (worktree_root / resolved_path).resolve(strict=False)

    resolved_worktree_root = worktree_root.resolve(strict=False)
    if not candidate_path.is_relative_to(resolved_worktree_root):
        raise PowdrrExecutionError(
            f"Workflow edit action file_path must stay within {resolved_worktree_root}."
        )
    return candidate_path


def _resolve_generated_file_path_from_command(
    command: object,
    *,
    worktree_root: Path,
) -> Path | None:
    command_items = _command_items(command)
    if not command_items or command_items[0] != "powdrr-lift" or len(command_items) < 2:
        return None

    output_path_value = _extract_command_option(command_items, "--output")
    if output_path_value is not None:
        return _resolve_worktree_file_path(output_path_value, worktree_root)

    work_item_name = _extract_command_option(command_items, "--work-item-name")
    if work_item_name is None:
        return None

    subcommand = command_items[1]
    if subcommand == "system-specification":
        return system_specification_default_output_path(work_item_name, worktree_root)
    if subcommand == "architecture-specification":
        return architecture_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "implementation-specification":
        return implementation_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "pr-specification":
        return pr_specification_default_output_path(work_item_name, worktree_root)
    if subcommand == "feature-pr-specification":
        return feature_pr_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "system-map-specification":
        return system_map_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "current-state":
        return current_state_specification_default_output_path(worktree_root)
    if subcommand == "codebase-state":
        return codebase_state_default_output_path(worktree_root)
    return None


def _command_items(command: object) -> list[str]:
    if isinstance(command, str):
        return [item for item in shlex.split(command) if item]
    if isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        items: list[str] = []
        for item in command:
            if not isinstance(item, str):
                raise PowdrrExecutionError(
                    "Workflow invoke_tool action command items must be strings."
                )
            normalized_item = item.strip()
            if normalized_item:
                items.append(normalized_item)
        return items
    return []


def _extract_command_option(
    command_items: Sequence[str],
    option_name: str,
) -> str | None:
    for index, item in enumerate(command_items):
        if item != option_name:
            continue
        if index + 1 >= len(command_items):
            return None
        return command_items[index + 1]
    return None


def _attempt_json_repair(
    client: WorkflowLLMClient,
    messages: Sequence[dict[str, str]],
    *,
    context: str,
    model: str,
    error_message: str,
    repair_instructions: str,
    stderr: TextIO,
    verbose: bool,
    previous_payload: dict[str, Any] | None = None,
    error_recorder: Callable[[RuntimeError, dict[str, Any] | None], None] | None = None,
    response_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    repair_messages = _build_json_repair_messages(
        messages,
        context=context,
        error_message=error_message,
        repair_instructions=repair_instructions,
        previous_payload=previous_payload,
    )
    _verbose_json(
        stderr,
        verbose,
        f"{context} repair LLM input (model={model})",
        repair_messages,
    )
    try:
        _print_waiting_for_model(stderr, model)
        repaired_payload = _request_json(
            client, repair_messages, response_schema=response_schema
        )
        _verbose_json(
            stderr,
            verbose,
            f"{context} repair LLM output (model={model})",
            repaired_payload,
        )
        return repaired_payload
    except RuntimeError as exc:
        if error_recorder is not None:
            error_recorder(exc, None)
        if isinstance(exc, LocalModelRuntimeError):
            raise
        if _is_empty_response_error(exc):
            raise _EmptyProviderResponseError(
                str(exc),
                messages=repair_messages,
            ) from exc
        print(f"{context} repair request failed: {exc}", file=stderr)
    return None


def _repair_response_fingerprint(
    messages: Sequence[dict[str, str]],
    payload: dict[str, Any],
) -> tuple[str, str]:
    # The prompt history necessarily grows on every repair attempt.  Including
    # it in the fingerprint therefore made an identical malformed payload look
    # like progress forever.  Compare the response itself so a provider that
    # keeps replaying the same invalid action is stopped deterministically.
    return (
        "response",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _build_json_repair_messages(
    messages: Sequence[dict[str, str]],
    *,
    context: str,
    error_message: str,
    repair_instructions: str,
    previous_payload: dict[str, Any] | None,
) -> list[dict[str, str]]:
    # All model-required correction requests use the same clean-room builder as
    # semantic action recovery.  The original conversation is deliberately not
    # carried forward: it was the context in which the invalid response was
    # produced.  Retain only the structured failure facts needed to correct it.
    recovery_context = json.dumps(
        {
            "response_context": context,
            "previous_response": previous_payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    repaired_messages, _manifest = build_clean_room_repair_prompt(
        context=recovery_context,
        error_message=error_message,
        repair_instructions=(
            repair_instructions
            + (
                " Return only a complete corrected JSON object with no markdown or "
                "commentary."
            )
            + (
                " Do not repeat that response; change the field identified "
                "by the validation error."
                if previous_payload is not None
                else ""
            )
        ),
        model="json-repair",
    )
    return repaired_messages


def _is_json_repairable_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "was not valid json",
            "content was empty",
            "did not include any content",
            "did not include any choices",
            "choice was not an object",
            "message was not an object",
            "must be a json object",
        )
    )


def _is_empty_response_error(exc: RuntimeError) -> bool:
    return "response message content was empty" in str(exc).lower() or (
        "response content was empty" in str(exc).lower()
    )


def _is_transient_provider_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return (
        "http 429" in message
        or '"code":"1305"' in message
        or "temporarily overloaded" in message
        or _is_timeout_error(exc)
    )


def _is_timeout_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def _is_model_unavailable_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "model" in message and (
        "not available" in message
        or "unavailable" in message
        or "not found" in message
        or "does not exist" in message
        or "unsupported" in message
    )


def _selection_repair_prompt(catalog: Sequence[SkillCatalogEntry]) -> str:
    catalog_entries = ", ".join(str(entry.path) for entry in catalog)
    return (
        "Task: repair the previous skill-routing response so it answers the "
        "routing task and obeys the response contract. Choose ready when one "
        "skill is sufficiently specified, or clarification when one specific "
        "missing fact or decision must be asked of the user.\n"
        "Response: return exactly one JSON object with keys "
        "selected_skill_path, selected_skill_reason, next_question, "
        "ready_to_execute, and llm_type. Set ready_to_execute=true and "
        "next_question=null for ready; set ready_to_execute=false and provide "
        "exactly one English question ending in '?' for clarification. The "
        f"selected_skill_path must be one of: {catalog_entries}. "
        "If next_question is present, it must be a concise, properly formed "
        "English question with meaningful words and a trailing question mark; "
        "it cannot be empty or only whitespace."
    )


def _current_step_contract(
    step: Any | None,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
) -> dict[str, Any]:
    """Describe the current step's authoritative action and tool contract."""
    if step is None:
        return {}
    invocations = tuple(getattr(step, "tool_invocations", ()) or ())
    recovery_invocations = _recovery_tool_invocations(
        step, execution_events, step_index
    )
    nested_skill = getattr(step, "uses_skill", None)
    actions = _step_actions(
        step, execution_events=execution_events, step_index=step_index
    )
    return {
        "step_type": getattr(step, "step_type", "governed"),
        "coding_loop": (
            step.coding_loop.to_data()
            if getattr(step, "coding_loop", None) is not None
            else None
        ),
        "completion": (
            step.completion.to_data()
            if getattr(step, "completion", None) is not None
            else None
        ),
        "outputs": [
            output.to_data() for output in (getattr(step, "outputs", ()) or ())
        ],
        "actions": [name for name, _instructions in actions],
        "declared_tool_invocations": [
            _tool_invocation_to_data(invocation) for invocation in invocations
        ],
        "tool_invocation_packages": list(
            getattr(step, "tool_invocation_packages", ()) or ()
        ),
        "recovery_tool_invocations": [
            _tool_invocation_to_data(invocation) for invocation in recovery_invocations
        ],
        "declared_nested_skill": (
            nested_skill.to_data() if nested_skill is not None else None
        ),
        "validation_gate": getattr(step, "validation_gate", None),
        "requires_successful_declared_tool_before_next_step": any(
            invocation.tool == "shell" for invocation in invocations
        ),
    }


def _step_action_response_schema(
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
) -> dict[str, Any]:
    """Derive a strict provider envelope from the active step contract."""
    behavior = behavior_for_step(step)
    outputs = tuple(getattr(step, "outputs", ()) or ())
    output_properties = {
        output.name: (
            dict(output.schema)
            if output.schema is not None
            else _json_schema_for_declared_type(output.type)
        )
        for output in outputs
    }
    action_names = [
        name
        for name, _instructions in _step_actions(
            step, execution_events=execution_events, step_index=step_index
        )
    ]
    if not getattr(step, "actions_declared", False):
        for legacy_action in (
            "gather_context",
            "prompt_user",
            "edit",
            "yaml_edit",
            "file_management",
            "delete_file",
            "invoke_skill",
            "invoke_tool",
            "read_document",
            "list_files",
            "goto_step",
            "next_step",
            "complete",
        ):
            if legacy_action == "next_step" and behavior.is_predicated:
                continue
            if legacy_action not in action_names:
                action_names.append(legacy_action)
    properties: dict[str, Any] = {
        "action": {
            "type": "string",
            "enum": action_names,
        },
        "decisions_and_context": {"type": "string"},
        "llm_type": {"type": "string"},
    }
    action_properties: dict[str, dict[str, Any]] = {
        "gather_context": {
            "types": {"type": "array", "items": {"type": "string"}},
            "feature_id": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "filters": {"type": "object"},
        },
        "prompt_user": {"text": {"type": "string"}},
        "edit": {
            "file_path": {"type": "string"},
            "edits": {"type": "array", "items": {"type": "object"}},
            "file_edits": {"type": "array", "items": {"type": "object"}},
        },
        "yaml_edit": {
            "file_path": {"type": "string"},
            "operations": {"type": "array", "items": {"type": "object"}},
        },
        "file_management": {
            "operation": {"type": "string"},
            "file_path": {"type": "string"},
            "destination_path": {"type": "string"},
        },
        "delete_file": {"file_path": {"type": "string"}},
        "invoke_skill": {
            "skill": {"type": "string"},
            "provider_role": {
                "type": "string",
                "enum": ["normal", "adversarial"],
            },
            "clean": {"type": "boolean"},
            "context": {"type": "array", "items": {"type": "string"}},
        },
        "invoke_tool": {
            "tool": {"type": "string"},
            "parameters": {"type": "object"},
        },
        "read_document": {
            "file_path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
        "list_files": {
            "directory": {"type": "string"},
            "pattern": {"type": "string"},
            "recursive": {"type": "boolean"},
        },
        "goto_step": {"step_id": {"type": "string"}},
        "next_step": {"output_state": {}},
        "complete": {"text": {"type": "string"}},
        "emit_outputs": {},
    }
    for action_name in action_names:
        properties.update(action_properties.get(action_name, {}))
    if outputs:
        properties["outputs"] = {
            "type": "object",
            "properties": output_properties,
            "required": [
                output.name
                for output in outputs
                if output.required_for_next_step
                or (
                    behavior.is_predicated
                    and getattr(step, "completion", None) is not None
                    and output.name in step.completion.required_outputs
                )
            ],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["action"],
        "additionalProperties": False,
    }


def _json_schema_for_declared_type(type_name: str) -> dict[str, Any]:
    normalized = type_name.strip().casefold()
    if normalized in {"string", "array", "object", "integer", "number", "boolean"}:
        return {"type": normalized}
    return {}


_DEFAULT_ACTION_INSTRUCTIONS = {
    "gather_context": "Discover checked-in specifications relevant to this step.",
    "prompt_user": "Ask one necessary human question.",
    "edit": "Apply a known line-based change to a file.",
    "yaml_edit": "Apply a structural change to a YAML file.",
    "file_management": "Move or rename one relative file.",
    "delete_file": "Delete one relative file using file_path.",
    "invoke_skill": "Run one listed nested skill.",
    "invoke_tool": "Run one command declared by this step.",
    "read_document": "Read a bounded range from a known document.",
    "list_files": "Discover exact file paths.",
    "goto_step": "Repeat one declared prior step when another pass is needed.",
    "next_step": "Advance after this step is complete.",
    "emit_outputs": "Publish the completed outputs for a predicated step.",
    "complete": "End the skill after all work is finished.",
}


def _step_actions(
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return declared actions plus the universal prompt and advance actions."""
    behavior = behavior_for_step(step)
    completion = None
    context_complete = False
    recovery_invocations = _recovery_tool_invocations(
        step, execution_events, step_index
    )
    declared = tuple(getattr(step, "actions", ()) or ())
    if declared or getattr(step, "actions_declared", False):
        actions = [(name, _DEFAULT_ACTION_INSTRUCTIONS[name]) for name in declared]
    else:
        # Older skill definitions may omit an explicit action catalog. Infer the
        # available actions from their declared capabilities until those definitions
        # are migrated to the explicit per-step contract.
        names: list[str] = []
        if (
            getattr(step, "details", None)
            or getattr(step, "tool_invocations", ())
            or getattr(step, "uses_skill", None)
        ):
            names.extend(
                [
                    "gather_context",
                    "edit",
                    "yaml_edit",
                    "file_management",
                    "delete_file",
                    "read_document",
                    "prompt_user",
                ]
            )
        else:
            names.append("invoke_skill")
        if getattr(step, "tool_invocations", ()):
            names.insert(0, "invoke_tool")
        if getattr(step, "uses_skill", None):
            names.insert(0, "invoke_skill")
        if _validation_gate_enabled(step):
            names = [
                "invoke_tool",
                "edit",
                "yaml_edit",
                "file_management",
                "delete_file",
                "prompt_user",
            ]
        if not behavior.invokes_llm:
            names = []
        actions = [(name, _DEFAULT_ACTION_INSTRUCTIONS[name]) for name in names]
    if recovery_invocations and not any(name == "invoke_tool" for name, _ in actions):
        actions.insert(0, ("invoke_tool", _DEFAULT_ACTION_INSTRUCTIONS["invoke_tool"]))
    if behavior.is_predicated:
        completion = getattr(step, "completion", None)
        context_complete = (
            completion is not None
            and step_index is not None
            and _predicated_context_complete(step, execution_events, step_index)
        )
        if completion is not None and completion.required_actions:
            if context_complete:
                actions = [
                    ("emit_outputs", _DEFAULT_ACTION_INSTRUCTIONS["emit_outputs"])
                ]
        else:
            actions.append(
                ("emit_outputs", _DEFAULT_ACTION_INSTRUCTIONS["emit_outputs"])
            )
        action_names = {name for name, _ in actions}
    else:
        action_names = {name for name, _ in actions}
    if "prompt_user" not in action_names and not (
        behavior.is_predicated
        and completion is not None
        and completion.required_actions
        and context_complete
    ):
        actions.append(("prompt_user", "Ask one necessary human question."))
    if "next_step" not in action_names and not behavior.is_predicated:
        actions.append(("next_step", "Advance only after this step is complete."))
    outputs = tuple(
        output
        for output in (getattr(step, "outputs", ()) or ())
        if getattr(output, "required_for_next_step", False)
    )
    if outputs:
        suffix = (
            " Include outputs: " + ", ".join(output.name for output in outputs) + "."
        )
        actions = [
            (name, instructions + suffix if name == "next_step" else instructions)
            for name, instructions in actions
        ]
    return tuple(actions)


def _predicated_context_complete(
    step: Any, execution_events: Sequence[Mapping[str, Any]], step_index: int
) -> bool:
    completion = getattr(step, "completion", None)
    if completion is None or not completion.required_actions:
        return False
    for requirement in completion.required_actions:
        matching = [
            event
            for event in execution_events
            if event.get("step_index") == step_index
            and event.get("kind") == requirement.action
            and all(
                _predicated_parameter_matches(event.get(name), value, name)
                for name, value in (requirement.parameters or {}).items()
            )
        ]
        if requirement.exactly is not None and len(matching) != requirement.exactly:
            return False
        if not matching:
            return False
    return True


def _declared_action_names(
    step: Any,
    *,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
) -> tuple[str, ...]:
    # next_step is an implicit runtime action; its output-specific guidance is
    # rendered only when the step declares required handoff outputs.
    behavior = behavior_for_step(step)
    names = [
        name
        for name, _ in _step_actions(
            step, execution_events=execution_events, step_index=step_index
        )
    ]
    if "next_step" not in names and not behavior.is_predicated:
        names.append("next_step")
    return tuple(names)


def _recovery_tool_invocations(
    step: Any,
    execution_events: Sequence[Mapping[str, Any]],
    step_index: int | None,
) -> tuple[SkillToolInvocation, ...]:
    """Return bounded diagnostics/corrections after a tool failure in this step."""
    if step_index is None or not any(
        event.get("step_index") == step_index
        and event.get("kind") in {"action_error", "tool_error"}
        for event in execution_events
    ):
        return ()
    if not any(
        invocation.tool in {"shell", GIT_TOOL} for invocation in step.tool_invocations
    ):
        return ()
    commands = (
        ("git", "status", "--short"),
        ("git", "diff", "--cached", "--stat"),
        ("git", "diff", "--cached", "--name-only"),
        ("git", "add", "<files-to-stage>"),
        ("git", "commit", "-m", "<commit-message>"),
    )
    declared = {invocation.command for invocation in step.tool_invocations}
    return tuple(
        SkillToolInvocation(tool="shell", command=command, label="recovery")
        for command in commands
        if command not in declared
    )


def _is_infrastructure_tool_failure(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.casefold()
    return any(
        marker in lowered
        for marker in (
            "operation not permitted",
            "permission denied",
            "index.lock",
            "read-only file system",
            "no such file or directory",
            "timed out",
            "connection refused",
        )
    )


def _action_repair_prompt(
    selected_skill: SkillCatalogEntry,
    *,
    current_step: Any | None = None,
    execution_events: Sequence[Mapping[str, Any]] = (),
    step_index: int | None = None,
    failed_action: SkillChatAction | None = None,
    validation_error: str | None = None,
) -> str:
    predicated = (
        current_step is not None and behavior_for_step(current_step).is_predicated
    )
    empty_response_guidance = (
        "If the original action response was empty, return a valid action that "
        "can produce the missing completion output."
        if predicated
        else (
            "If the original action response was empty, choose next_step when the "
            "current step is complete instead of returning an empty response. If "
            "this corrective response is also empty, the system will interpret it "
            "as next_step."
        )
    )
    prompt = (
        "Generate a JSON document selecting the best action based on this "
        "context. The current step's action declarations below are the only "
        "actions available. Do not use action names or instructions from a "
        "previous step.\n" + empty_response_guidance + "\n"
        'Return exactly one JSON object with a top-level "action" field and the '
        "fields required by that action. Do not combine actions or output "
        "markdown."
    )
    action_names = (
        {
            name
            for name, _ in _step_actions(
                current_step,
                execution_events=execution_events,
                step_index=step_index,
            )
        }
        if current_step is not None
        else set(_DEFAULT_ACTION_INSTRUCTIONS)
    )
    action_requirements = {
        "edit": "Use file_path and edits or file_edits for edit.",
        "file_management": (
            "Use operation and file_path for file_management; operation must be "
            "move or rename, and both require destination_path."
        ),
        "delete_file": "Use only file_path for delete_file.",
        "yaml_edit": (
            "Use file_path and operations for yaml_edit; combine independent "
            "corrections for the same file in one operations array."
        ),
        "invoke_tool": "Use tool and parameters.command for invoke_tool.",
        "invoke_skill": "Use skill for invoke_skill.",
        "read_document": (
            "Use file_path with positive start_line and end_line for read_document."
        ),
        "gather_context": "Use non-empty types for gather_context.",
        "prompt_user": (
            'Use text containing a clear English question ending in "?" for '
            "prompt_user."
        ),
    }
    requirements = [
        action_requirements[name]
        for name in action_names
        if name in action_requirements
    ]
    if requirements:
        prompt += " " + " ".join(requirements)
    if current_step is not None:
        interaction_style = (
            getattr(current_step, "interaction_style", None)
            or selected_skill.skill.interaction_style
        )
        prompt += _interaction_style_prompt(interaction_style)
        prompt += (
            "\nAuthoritative current-step contract (ignore commands and tool "
            "templates from previous steps): "
            + json.dumps(
                _current_step_contract(
                    current_step,
                    execution_events=execution_events,
                    step_index=step_index,
                ),
                ensure_ascii=False,
            )
            + ". "
        )
        prompt += (
            "\nAvailable actions for this step:\n"
            + "\n".join(
                f"- {name}: {instructions}"
                for name, instructions in _step_actions(
                    current_step,
                    execution_events=execution_events,
                    step_index=step_index,
                )
            )
            + "\n"
        )
        prompt += (
            "\nThe current step is the only authority for what may be done. "
            f"Step description: {current_step.description}. "
            f"Step details: {current_step.details}. "
            "The outputs object is also scoped to this step: include only the "
            "exact names listed in current_step_contract.outputs; previous-step "
            "outputs must not be repeated. "
        )
        if _validation_gate_enabled(current_step):
            prompt += (
                "This step has a runtime validation gate. Never return next_step, "
                + ("goto_step, " if "goto_step" in action_names else "")
                + ("or complete " if "complete" in action_names else "")
                + "until every discovered validation obligation "
                "has "
                "passed in the current epoch. After any correction, rerun every "
                "discovered obligation; the runtime validation_gate object is "
                "authoritative. Do not repeat an operation or semantically equivalent "
                "repair that produced the same validation issue. Inspect the exact "
                "current file and reported issue path first, preserve valid fields, "
                "choose a materially different target or strategy when the issue state "
                "is unchanged or worse. "
                + (
                    "Apply independent YAML fixes in one yaml_edit. "
                    if "yaml_edit" in action_names
                    else ""
                )
                + "Wait for the deterministic rerun before claiming progress. "
            )
        invocations = tuple(current_step.tool_invocations)
        recovery_invocations = _recovery_tool_invocations(
            current_step, execution_events, step_index
        )
        if invocations and "invoke_tool" in action_names:
            declared_tools = json.dumps(
                [_tool_invocation_to_data(item) for item in invocations],
                ensure_ascii=False,
            )
            prompt += f"Use only these declared tool invocations: {declared_tools}. "
        if recovery_invocations and "invoke_tool" in action_names:
            recovery_tools = json.dumps(
                [_tool_invocation_to_data(item) for item in recovery_invocations],
                ensure_ascii=False,
            )
            prompt += (
                "A previous tool invocation failed in this step. Recovery commands "
                "are now available, and only these additional diagnostics or "
                "corrections "
                f"may be used: {recovery_tools}. Use them only to diagnose or correct "
                "the failed tool; do not invent another command. "
            )
        if failed_action is not None and failed_action.tool == "basedpyright-structure":
            prompt += (
                "BasedPyright structure repair rule: parameters.path must be one "
                "exact existing repository-relative Python file path ending in "
                ".py. A directory, missing path, or non-Python path is invalid. "
                "Do not reread the same specification document to repair this "
                "error; select a concrete implementation file from the diagnostic "
                "candidate_python_files list or use an available discovery action. "
            )
        shapes: list[str] = []
        if "prompt_user" in action_names:
            shapes.append(
                '{"action":"prompt_user","text":"One clear English question?",'
                '"decisions_and_context":"More information is required."}'
            )
        if "next_step" in action_names:
            shapes.append(
                '{"action":"next_step","decisions_and_context":"The current '
                'step is complete."}'
            )
        if shapes:
            prompt += (
                "For this repair, use one of these exact action shapes allowed for "
                "the current step: " + " or ".join(shapes) + ". "
            )
        if "yaml_edit" in action_names:
            prompt += (
                'For a YAML correction, use exactly {"action":"yaml_edit",'
                '"file_path":"relative/file.yaml","operations":[{"op":"set_value",'
                '"path":["id"],"value":"feature-id"}]}; include all required '
                'fields in the operation. For list items, use {"op":"upsert_item",'
                '"section":"requirements","id":"req-1","value":{"description":'
                '"...","state":"added"}}. '
            )
        if invocations and "invoke_tool" in action_names:
            prompt += (
                "invoke_tool is also allowed only with one of the declared tool "
                "templates shown above; do not invent another tool or parameter "
                "shape. "
            )
    if validation_error is not None:
        prompt += (
            "\nThe previous action returned a validation_error and was not "
            "executed. Do not repeat it unchanged. Read the validation_error "
            "message and return an action that matches the current step's "
            "declared tool template exactly."
        )
        if "prompt_user action" in validation_error.lower() and "text" in (
            validation_error.lower()
        ):
            prompt += (
                " The previous response used the wrong prompt_user field. "
                'prompt_user requires a string in "text"; rename "prompt" to '
                '"text" and return the complete corrected action. Do not use '
                '"text".'
            )
    if failed_action is not None:
        prompt += (
            f"\nRecovery is mandatory: the previous {failed_action.kind} action "
            "failed and was not applied. Do not repeat that action or an equivalent "
            "action with only different prose; choose a materially different action "
            "from the current step's allowed actions. The failure reason was: "
            + (validation_error or "the action violated the active workflow contract")
            + ". The rejected action was:\n"
            + _workflow_action_signature(failed_action)
            + ". "
            + (
                "For YAML, prefer yaml_edit with upsert_item, remove_item, "
                "remove_key, or set_value; use a normal edit with exact line "
                "ranges when those operations cannot express the repair. "
                if {"edit", "yaml_edit"} & action_names
                else ""
            )
        )
        successful_reads = _successful_document_reads_for_prompt(
            execution_events, step_index
        )
        if successful_reads:
            prompt += (
                " Documents already read successfully in this step (do not reread "
                "them as a substitute for correcting the failed action): "
                + ", ".join(str(item["path"]) for item in successful_reads)
                + "."
            )
    if _is_infrastructure_tool_failure(validation_error):
        prompt += (
            " The failure appears to be an environment or permission problem. "
            "Do not switch to unrelated diagnostic commands. Retry the exact "
            "required action at most once; if it fails again, return prompt_user "
            "to request human intervention."
        )
    return prompt


def _prompt_user(
    prompt: str,
    *,
    input_func: Callable[[], str],
    stdout: TextIO,
    status_stream: TextIO | None = None,
) -> str:
    if input_func is input and _supports_readline_input(stdout):
        answer = input(prompt).strip()
    else:
        stdout.write(prompt)
        stdout.flush()
        if input_func is input and _supports_interactive_line_editing(stdout):
            answer = _read_interactive_line(prompt, stdout=stdout).strip()
        else:
            answer = input_func().strip()
        if input_func is not input:
            stdout.write("\n")
            stdout.flush()
    if status_stream is not None:
        print("[workflow] calling LLM...", file=status_stream, flush=True)
    return answer


def _supports_readline_input(stdout: TextIO) -> bool:
    """Return whether native readline can safely own this terminal prompt."""
    return sys.stdin.isatty() and stdout.isatty() and readline is not None


def _supports_interactive_line_editing(stdout: TextIO) -> bool:
    """Return whether the process has a terminal we can safely edit in place."""
    return sys.stdin.isatty() and stdout.isatty() and hasattr(termios, "tcgetattr")


def _read_interactive_line(prompt: str, *, stdout: TextIO) -> str:
    """Read a line with cursor movement support when readline is unavailable."""
    stdin = sys.stdin
    chars: list[str] = []
    cursor = 0
    original_attributes = termios.tcgetattr(stdin.fileno())

    def redraw() -> None:
        # Repaint the line and position the cursor after the edited prefix.
        stdout.write("\r" + prompt + "".join(chars) + "\x1b[K")
        distance_from_end = len(chars) - cursor
        if distance_from_end:
            stdout.write(f"\x1b[{distance_from_end}D")
        stdout.flush()

    try:
        tty.setraw(stdin.fileno())
        while True:
            character = stdin.read(1)
            if character in {"\r", "\n"}:
                stdout.write("\r\n")
                stdout.flush()
                return "".join(chars)
            if character == "\x03":
                raise KeyboardInterrupt
            if character == "\x04":
                if not chars:
                    raise EOFError
                continue
            if character in {"\x7f", "\b"}:
                if cursor:
                    del chars[cursor - 1]
                    cursor -= 1
                    redraw()
                continue
            if character != "\x1b":
                chars[cursor:cursor] = [character]
                cursor += 1
                stdout.write(character)
                stdout.flush()
                continue

            # Arrow and editing keys arrive as ANSI escape sequences. Read the
            # rest only when it is immediately available so a lone Escape can
            # still be entered as a normal control key without hanging.
            sequence = ""
            while select.select([stdin], [], [], 0.01)[0]:
                sequence += stdin.read(1)
                if sequence[-1:] in "~ABCDEFGH":
                    break
            if sequence in {"[D", "OD"}:
                cursor = max(0, cursor - 1)
            elif sequence in {"[C", "OC"}:
                cursor = min(len(chars), cursor + 1)
            elif sequence in {"[H", "OH", "[1~"}:
                cursor = 0
            elif sequence in {"[F", "OF", "[4~"}:
                cursor = len(chars)
            elif sequence == "[3~" and cursor < len(chars):
                del chars[cursor]
            if sequence:
                redraw()
    finally:
        termios.tcsetattr(stdin.fileno(), termios.TCSADRAIN, original_attributes)


def _build_chat_client(
    credentials: ProviderCredentials,
    *,
    model: str,
    model_cache_dir: Path,
    progress_stream: TextIO | None = None,
) -> WorkflowLLMClient:
    provider = provider_definition(credentials.provider)
    return build_provider_client(
        provider=credentials.provider,
        model=model,
        api_key=credentials.api_key,
        base_url=credentials.base_url,
        local_model_path=(
            resolve_local_model_path(model_cache_dir)
            if provider.client_kind == "local"
            else None
        ),
        local_context=_resolve_local_model_context(),
        progress_stream=progress_stream,
    )


def _model_limits_for(provider: str, model: str) -> LLMModelLimits:
    return provider_model_limits(
        provider,
        model,
        local_context=_resolve_local_model_context(),
    )


def _resolve_provider(
    provider_override: str,
    model: str,
    *,
    mapping: LLMModelMapping | None = None,
) -> str:
    if mapping is not None:
        provider_definition(mapping.provider)
        return mapping.provider
    if provider_override != "auto":
        provider_definition(provider_override)
        return provider_override
    candidates = auto_provider_candidates()
    if candidates:
        return candidates[0]
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"


def resolve_workflow_provider(
    provider: str = "auto",
    *,
    normal_provider: str | None = None,
) -> str:
    """Resolve the normal provider using workflow-chat's provider policy."""
    return resolve_provider_roles(
        provider,
        normal_provider=normal_provider,
    ).normal


def available_workflow_providers() -> tuple[str, ...]:
    """Return API-backed providers that have usable credentials configured."""
    return available_provider_names()


def choose_workflow_provider(
    *,
    input_func: Callable[[], str] = input,
    stdout: TextIO = sys.stdout,
) -> str:
    """Present configured API providers and return the user's selection."""
    providers = available_workflow_providers()
    if not providers:
        raise PowdrrExecutionError(
            "No workflow-chat providers are configured. Set an API key for at "
            "least one supported provider before starting workflow-chat."
        )
    stdout.write("Available workflow-chat providers:\n")
    for index, provider in enumerate(providers, start=1):
        definition = provider_definition(provider)
        stdout.write(f"  {index}. {definition.display_name} ({provider})\n")
    stdout.write("Choose a provider by number or name: ")
    stdout.flush()
    while True:
        answer = input_func().strip()
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(providers):
                stdout.write("\n")
                return providers[index]
        normalized = answer.casefold()
        for provider in providers:
            if normalized == provider.casefold():
                stdout.write("\n")
                return provider
        stdout.write(
            f"Choose a number from 1 to {len(providers)} or enter a provider name: "
        )
        stdout.flush()


def download_local_qwen_model(model_cache_dir: Path) -> Path:
    """Download the local Qwen GGUF shards into the configured cache."""
    model_cache_dir.mkdir(parents=True, exist_ok=True)
    cached_model_paths = sorted(model_cache_dir.glob(LOCAL_MODEL_PATTERN))
    if _has_all_local_model_shards(cached_model_paths):
        return cached_model_paths[0]
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise PowdrrExecutionError(
            "Automatic local model downloads require huggingface-hub."
        ) from exc
    try:
        snapshot_directory = Path(
            snapshot_download(
                repo_id=_LOCAL_MODEL_REPOSITORY,
                allow_patterns=[LOCAL_MODEL_PATTERN],
                local_dir=str(model_cache_dir),
            )
        )
    except Exception as exc:
        raise PowdrrExecutionError(
            "Could not download the Qwen Q5_K_M model from Hugging Face. "
            f"Repository={_LOCAL_MODEL_REPOSITORY}, cache={model_cache_dir}. "
            f"Underlying error: {type(exc).__name__}: {exc}"
        ) from exc
    model_paths = sorted(snapshot_directory.glob(LOCAL_MODEL_PATTERN))
    if not _has_all_local_model_shards(model_paths):
        raise PowdrrExecutionError(
            "The Hugging Face Qwen repository did not provide all Q5_K_M GGUF shards."
        )
    return model_paths[0]


def _has_all_local_model_shards(model_paths: Sequence[Path]) -> bool:
    if not model_paths:
        return False
    match = re.search(r"-00001-of-(\d+)\.gguf$", model_paths[0].name)
    expected_shards = int(match.group(1)) if match else 1
    return len(model_paths) >= expected_shards


def _resolve_local_model_context() -> int:
    configured_context = os.environ.get(_LOCAL_MODEL_CONTEXT_ENV)
    if configured_context is None or not configured_context.strip():
        return _DEFAULT_LOCAL_MODEL_CONTEXT
    try:
        context = int(configured_context)
    except ValueError as exc:
        raise PowdrrExecutionError(
            f"{_LOCAL_MODEL_CONTEXT_ENV} must be a positive integer; got "
            f"{configured_context!r}."
        ) from exc
    if context <= 0:
        raise PowdrrExecutionError(
            f"{_LOCAL_MODEL_CONTEXT_ENV} must be a positive integer; got "
            f"{configured_context!r}."
        )
    return context


def _split_system_message(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    if not messages:
        return None, []
    first_message = messages[0]
    if first_message.get("role") != "system":
        return None, list(messages)
    system_content = first_message.get("content")
    if not isinstance(system_content, str):
        return None, list(messages)
    return system_content, list(messages[1:])


def _anthropic_message(message: dict[str, str]) -> dict[str, Any]:
    role = message.get("role")
    content = message.get("content")
    if role not in {"user", "assistant"}:
        raise PowdrrExecutionError(
            "Anthropic messages must use user or assistant roles after splitting "
            "the system prompt."
        )
    if not isinstance(content, str):
        raise PowdrrExecutionError("Anthropic message content must be a string.")
    return {
        "role": role,
        "content": [{"type": "text", "text": content}],
    }
