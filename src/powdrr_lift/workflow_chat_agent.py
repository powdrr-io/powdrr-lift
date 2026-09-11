from __future__ import annotations

import hashlib
import inspect
import json
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
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

from powdrr_lift.agent.provider_config import (
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
    _EmptyProviderResponseError,
    _estimate_message_tokens,
    _ModelUnavailableError,
    _SemanticRepairExhaustedError,
    available_provider_names,
    backup_model_for,
    initial_model_for_provider,
    long_context_backup_for,
    provider_model_limits,
    resolve_llm_mapping,
    resolve_local_model_context,
    resolve_provider,
    resolve_provider_roles,
)
from powdrr_lift.basedpyright_tools import (
    is_basedpyright_tool,
)
from powdrr_lift.builtin_tool_help import (
    builtin_tool_help,
)
from powdrr_lift.core import (
    architecture_specification_default_output_path,
    codebase_state_default_output_path,
    current_state_specification_default_output_path,
    feature_pr_specification_default_output_path,
    implementation_specification_default_output_path,
    pr_specification_default_output_path,
    resolve_repo_root,
    system_map_specification_default_output_path,
    system_specification_default_output_path,
)
from powdrr_lift.core.delivery_profile import PhaseType, load_delivery_profile
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.core.validation_messages import (
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
)
from powdrr_lift.workflow_action_catalog import (
    DEFAULT_ACTION_INSTRUCTIONS as _DEFAULT_ACTION_INSTRUCTIONS,
)
from powdrr_lift.workflow_action_catalog import (
    declared_action_names as _declared_action_names,
)
from powdrr_lift.workflow_action_catalog import (
    recovery_tool_invocations as _recovery_tool_invocations,
)
from powdrr_lift.workflow_action_catalog import (
    step_actions as _step_actions,
)
from powdrr_lift.workflow_action_operations import (
    WorkflowEditRangeError as _WorkflowEditRangeError,
)
from powdrr_lift.workflow_action_operations import (
    WorkflowYamlEditError as _WorkflowYamlEditError,
)
from powdrr_lift.workflow_action_operations import (
    _apply_file_edits,
    _apply_yaml_operations,
    _list_worktree_files,
    _record_skill_pull_request,
)
from powdrr_lift.workflow_action_protocol import _parse_action_response
from powdrr_lift.workflow_action_validation import (
    _advance_predicated_step,
    _command_items_for_validation,
    _invalidate_deterministic_pre_step,
    _json_schema_error,
    _materialize_split_pr_specification,
    _parse_action_response_with_schema,
    _predicated_step_complete,
    _record_dynamic_validation_result,
    _record_runtime_readiness_artifact,
    _record_workflow_action_outputs,
    _register_validation_gate_discovery,
    _reset_split_pr_handoffs_for_repair,
    _reset_validation_gate_after_correction,
    _step_index_by_id,
    _validate_dynamic_validation_gate_action,
    _validate_internal_command,
    _validate_structured_document_text,
    _validate_workflow_action_for_step,
    _validate_workflow_action_outputs,
    _validate_workflow_handoff,
    _validate_workflow_step_transition,
    _validation_actions_match,
    _validation_gate_enabled,
    _validation_gate_prompt_data,
    _validation_gate_state,
    _workflow_context_handoff_records,
    _WorkflowStructuredDocumentError,
    _WorkflowToolValidationError,
    _worktree_relative_path,
)
from powdrr_lift.workflow_branching import select_branch_target
from powdrr_lift.workflow_catalog import load_skill_catalog
from powdrr_lift.workflow_chat_selection import (
    SkillChatConfig,
    SkillChatSelection,
    WorkflowChatConfig,
    _active_llm_mappings,
    _build_selection_messages,
    _parse_selection_response,
    _selection_repair_prompt,
)
from powdrr_lift.workflow_error_logging import record_workflow_llm_error
from powdrr_lift.workflow_execution_loop import (
    _execute_shell_tool,
    _print_waiting_for_model,
    _run_coding_loop_verification,
    _run_deterministic_pre_step,
    _run_gate,
    _validate_coding_loop_action,
)
from powdrr_lift.workflow_execution_state import (
    _begin_step_checkpoint,
    _ensure_execution_runtime,
    _record_durable_fact,
    _restore_step_checkpoint,
    _WorkflowExecutionState,
)
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
from powdrr_lift.workflow_models import SkillCatalogEntry, WorkflowContext
from powdrr_lift.workflow_observer import (
    ObserverActionRecommendation,
    ObserverDecision,
    ObserverExecutionContext,
    ShadowWorkflowObserver,
    compact_observer_mapping,
    observer_action_matches,
)
from powdrr_lift.workflow_paths import (
    is_dedicated_worktree,
    resolve_project_root,
    resolve_worktree_file_path,
)
from powdrr_lift.workflow_prompting import (
    _build_step_execution_messages as _build_step_execution_messages_runtime,
)
from powdrr_lift.workflow_prompting import (
    _current_file_context,
    _effective_interaction_style,
    _successful_document_reads_for_prompt,
    _tool_invocation_to_data,
    interaction_style_prompt,
)
from powdrr_lift.workflow_provider_runtime import (
    WorkflowClientRegistry,
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
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 3
_CONTEXT_SAFETY_MARGIN_TOKENS = 1024
_MAX_DOCUMENT_CONTEXT_LINES = 2000
_ENABLE_LLM_EXCHANGE_LOGGING = False
_MAX_PROMPT_TRANSCRIPT_ENTRIES = 12
_MAX_PROMPT_TRANSCRIPT_CHARS = 12000
_MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS = 8000
_MAX_PROMPT_STEP_CONTEXT_ENTRIES = 24
_MAX_PROMPT_STEP_CONTEXT_CHARS = 16000
_MAX_STREAM_CHUNKS = 4096
_MAX_STREAM_CONTENT_CHARS = 131072
_MAX_REPEATED_REPAIR_ATTEMPTS = 5
_WORKFLOW_CONTEXT_PATH = Path(".powdrr") / "workflow-context.json"
_INTERNAL_TOOL = "internal"
_INTERNAL_BINARY = "powdrr-lift"


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
            if step_behavior.runs_branch:
                target_step_id = select_branch_target(
                    self.current_step.branch, self.state.handoff_records
                )
                target_index = _step_index_by_id(self.selected_skill, target_step_id)
                self.state.execution_events.append(
                    {
                        "kind": "goto_step",
                        "step_id": target_step_id,
                        "target_step_index": target_index,
                        "source": "branch",
                        "step_index": self.current_step_index,
                    }
                )
                self.state.step_index = target_index
                continue
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
            self.provider = resolve_provider(
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
    error_log_root = resolve_project_root(configured_repo_root, configured_repo_root)
    project_root = configured_repo_root
    workflow_context = _load_workflow_context(project_root)
    skills_dir = config.skills_dir
    if not skills_dir.is_absolute():
        skills_dir = configured_repo_root / skills_dir

    catalog = load_skill_catalog(skills_dir, stderr=stderr)
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
    client_registry = WorkflowClientRegistry(
        api_key=config.api_key,
        base_url=config.base_url,
        project_root=project_root,
        progress_stream=stderr,
    )
    credentials = client_registry.credentials_for(provider)

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
            client_for=client_registry.client_for,
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
            provider = resolve_provider(
                config.provider,
                current_model,
                mapping=selection_mapping,
            )
        credentials = client_registry.credentials_for(provider)
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
    project_root = resolve_project_root(configured_repo_root, worktree_root)
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
        client_for_model=client_registry.client_for,
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

        observer_client = client_registry.client_for(
            observer_mapping.model,
            observer_mapping.provider,
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


def _write_skill_summary(summary: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "skill-execution.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary_path


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
        and is_dedicated_worktree(context.worktree_root)
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
    if is_dedicated_worktree(configured_repo_root):
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
    if is_dedicated_worktree(resolved_repo_root):
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
    return _build_step_execution_messages_runtime(
        selected_skill=selected_skill,
        current_step=current_step,
        current_step_index=current_step_index,
        transcript=transcript,
        execution_events=execution_events,
        execution_context=execution_context,
        handoff_records=handoff_records,
        durable_facts=durable_facts,
        current_file_path=current_file_path,
        worktree_root=worktree_root,
        catalog=catalog,
        workflow_context=workflow_context,
        current_file_context_cache=current_file_context_cache,
        validation_gate=validation_gate,
        stalled_step_context=stalled_step_context,
        inherited_interaction_style=inherited_interaction_style,
        observer_intervention=observer_intervention,
        runtime_prompt_context=runtime_prompt_context,
        step_actions=_step_actions(
            current_step,
            execution_events=execution_events,
            step_index=current_step_index,
        ),
        validation_gate_enabled=_validation_gate_enabled(current_step),
        pre_step_event=_latest_deterministic_pre_step(
            execution_events,
            skill_name=selected_skill.skill.name,
            step_index=current_step_index,
        ),
        failed_action=failed_action,
        failure_reason=failure_reason,
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
                        target_path := resolve_worktree_file_path(
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
        target_path = resolve_worktree_file_path(
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
    target_path = resolve_worktree_file_path(action.file_path, state.worktree_root)
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
    target_path = resolve_worktree_file_path(action.file_path, state.worktree_root)
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
    if action.capture_as:
        current_step = state.selected_skill.skill.steps[state.step_index]
        declaration = next(
            (
                output
                for output in current_step.outputs
                if output.name == action.capture_as
            ),
            None,
        )
        if declaration is None:
            raise PowdrrExecutionError(
                "prompt_user capture_as must name a declared output: "
                f"{action.capture_as}"
            )
        state.handoff_records[action.capture_as] = {
            "name": action.capture_as,
            "type": declaration.type,
            "value": answer,
            "produced_by": {"step_index": state.step_index, "action": action.kind},
            "scope": declaration.scope,
        }
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
        return resolve_worktree_file_path(output_path_value, worktree_root)

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
        "prompt_user": {
            "text": {"type": "string"},
            "capture_as": {"type": "string"},
        },
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
            "prompt_user. If the answer must be reused by a later step, include "
            "capture_as with the exact declared output name; the runtime records "
            "the answer as that output."
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
        prompt += interaction_style_prompt(interaction_style)
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
                '"capture_as":"declared_output_name",'
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


def _model_limits_for(provider: str, model: str) -> LLMModelLimits:
    return provider_model_limits(
        provider,
        model,
        local_context=resolve_local_model_context(),
    )


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
