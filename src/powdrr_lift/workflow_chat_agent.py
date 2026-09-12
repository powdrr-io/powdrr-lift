from __future__ import annotations

import hashlib
import inspect
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time  # noqa: F401 - retained as a patchable retry-delay seam
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, TextIO

import yaml

from powdrr_lift.agent.context import WorkflowContext
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
    available_provider_names,
    initial_model_for_provider,
    provider_model_limits,
    resolve_llm_mapping,
    resolve_local_model_context,
    resolve_provider,
    resolve_provider_roles,
)
from powdrr_lift.core import (
    resolve_repo_root,
)
from powdrr_lift.core.delivery_profile import PhaseType, load_delivery_profile
from powdrr_lift.core.validation_messages import (
    validation_error_to_data,
)
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.intrinsic_git_gh import (
    execute_intrinsic_git_gh_tool,  # noqa: F401 - compatibility monkeypatch seam
)
from powdrr_lift.process.action_catalog import (
    declared_action_names as _declared_action_names,
)
from powdrr_lift.process.action_catalog import (
    step_actions as _step_actions,
)
from powdrr_lift.process.branching import select_branch_target
from powdrr_lift.process.catalog import SkillCatalogEntry
from powdrr_lift.process.discovery import load_skill_catalog
from powdrr_lift.process.step_behavior import behavior_for_step
from powdrr_lift.workflow_action_protocol import _parse_action_response
from powdrr_lift.workflow_action_validation import (
    _advance_predicated_step,
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
    _validate_workflow_action_for_step,
    _validate_workflow_action_outputs,
    _validate_workflow_handoff,
    _validate_workflow_step_transition,
    _validation_actions_match,
    _validation_gate_enabled,
    _validation_gate_prompt_data,
    _validation_gate_state,
    _workflow_context_handoff_records,
    _WorkflowToolValidationError,
)
from powdrr_lift.workflow_chat_actions import (
    _workflow_action_handlers,
)
from powdrr_lift.workflow_chat_context import (
    _load_workflow_context,
    _persist_workflow_context,
    _resolve_worktree_for_request,
)
from powdrr_lift.workflow_chat_contract import (
    _action_repair_prompt,
    _command_items,
    _current_step_contract,
    _rejected_edit_guidance,
    _resolve_generated_file_path_from_command,
    _step_action_response_schema,
    _underlying_execution_error,
    _workflow_edit_failure_feedback,
)
from powdrr_lift.workflow_chat_io import (
    _prompt_user,
    _verbose_print,
    _write_agent_error,
)
from powdrr_lift.workflow_chat_selection import (
    SkillChatSelection,
    WorkflowChatConfig,
    _active_llm_mappings,
    _build_selection_messages,
    _parse_selection_response,
    _selection_repair_prompt,
)
from powdrr_lift.workflow_chat_transport import (
    _complete_json_with_model_fallback,
)
from powdrr_lift.workflow_error_logging import record_workflow_llm_error
from powdrr_lift.workflow_execution_loop import (
    _run_coding_loop_verification,
    _run_deterministic_pre_step,
    _run_gate,
    _validate_coding_loop_action,
)
from powdrr_lift.workflow_execution_state import (
    _begin_step_checkpoint,
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
    build_repair_prompt_manifest,
    complete_two_pass_action,
    constrain_action_response_schema,
    prune_execution_events,
    workflow_action_failure_signature,
    workflow_action_summary,
)
from powdrr_lift.workflow_llm import (
    WorkflowAction as SkillChatAction,
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
from powdrr_lift.workflow_paths import (
    resolve_project_root,
    resolve_worktree_file_path,
)
from powdrr_lift.workflow_prompting import (
    _build_step_execution_messages as _build_step_execution_messages_runtime,
)
from powdrr_lift.workflow_prompting import (
    _current_file_context,
    _effective_interaction_style,
)
from powdrr_lift.workflow_provider_runtime import (
    WorkflowClientRegistry,
)
from powdrr_lift.workflow_replay import (
    WORKFLOW_REPLAY_PROMPT_BUILDER_VERSION,
    build_workflow_replay_state,
    definition_content_sha256,
)

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
