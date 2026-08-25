from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from powdrr_lift.basedpyright_tools import (
    BASEDPYRIGHT_STRUCTURE_TOOL,
    BASEDPYRIGHT_SYMBOL_TOOL,
    execute_basedpyright_tool,
    is_basedpyright_tool,
)
from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    HumanRole,
    TaskComplexity,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
    resolve_repo_root,
)
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.file_management import manage_worktree_file
from powdrr_lift.pr_workflow_record import (
    is_pull_request_create_command,
    pull_request_number,
    record_pull_request_workflow,
)
from powdrr_lift.workflow_chat_agent import (
    _DEFAULT_LLM_TYPE,
    _DEFAULT_MODEL,
    GH_TOOL,
    GIT_TOOL,
    LLMModelMapping,
    LocalLlamaChatClient,
    SkillCatalogEntry,
    _action_system_prompt,
    _apply_file_edits,
    _apply_yaml_operations,
    _build_step_execution_messages,
    _default_llm_mappings,
    _estimate_message_tokens,
    _execute_fuzzy_match_tool,
    _execute_shell_tool,
    _find_skill_by_name,
    _interaction_style_prompt,
    _invalidate_deterministic_pre_step,
    _load_skill_catalog,
    _long_context_backup_for,
    _maybe_record_llm_exchanges,
    _model_limits_for,
    _parse_action_response,
    _print_waiting_for_model,
    _record_skill_pull_request,
    _resolve_credentials,
    _resolve_llm_mapping,
    _resolve_local_model_path,
    _resolve_pre_step_template,
    _resolve_project_root,
    _resolve_worktree_file_path,
    _run_deterministic_pre_step,
    _run_gate,
    _step_index_by_id,
    _validate_internal_command,
    _validate_workflow_action_outputs,
    _validate_workflow_handoff,
    execute_intrinsic_git_gh_tool,
    resolve_workflow_provider,
)
from powdrr_lift.workflow_error_logging import record_workflow_llm_error
from powdrr_lift.workflow_git import (
    WorkflowGitInconsistency,
    WorkflowGitState,
    claim_workflow_task,
    create_workflow_worktree,
    load_workflow_git_state,
    slugify_workflow_id,
    validate_workflow_git_state,
    workflow_dependencies_completion,
    workflow_id_from_task_id,
)
from powdrr_lift.workflow_llm import (
    ProgressDecision,
    WorkflowAction,
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionProgressStrategy,
    WorkflowActionRequest,
    WorkflowExecutionStrategy,
    WorkflowLLMActionEngine,
    WorkflowLLMClient,
    WorkflowLLMExecutionDriver,
    WorkflowLLMTimeoutExhausted,
    complete_json_with_timeout_retry,
    prompt_size_breakdown,
    prune_execution_events,
    workflow_action_signature,
    workflow_action_summary,
)
from powdrr_lift.workflow_observer import (
    ObserverDecision,
    ObserverExecutionContext,
    ShadowWorkflowObserver,
    compact_observer_mapping,
)

_TASK_PROMPT_PLACEHOLDER_RE = re.compile(r"<([A-Za-z0-9_-]+)>")
_TASK_PROMPT_INPUT_REFERENCE_RE = re.compile(r"\binput_state\.([A-Za-z0-9_-]+)\b")


def _task_prompt_input_values(input_state: Any) -> dict[str, str]:
    if not isinstance(input_state, Mapping):
        return {}
    values: dict[str, str] = {}
    for key, value in input_state.items():
        if isinstance(value, (dict, list, tuple)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(value)
        normalized_key = str(key).replace("-", "_")
        values[normalized_key] = rendered
        values[str(key)] = rendered

    # These are the stable template names used by workflow instantiation.
    if "feature_id" in values:
        values.update(
            {
                "feature-id": values["feature_id"],
                "work-item-name": values["feature_id"],
                "work_item_name": values["feature_id"],
            }
        )
    if "proposed_pr" in values:
        values.update(
            {
                "proposed-pr-id": values["proposed_pr"],
                "proposed_pr_id": values["proposed_pr"],
            }
        )
    return values


def _resolve_task_prompt_text(text: str, input_state: Any) -> str:
    values = _task_prompt_input_values(input_state)

    def replace_input_reference(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, values.get(key.replace("-", "_"), match.group(0)))

    def replace_placeholder(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, values.get(key.replace("-", "_"), match.group(0)))

    resolved = _TASK_PROMPT_INPUT_REFERENCE_RE.sub(replace_input_reference, text)
    return _TASK_PROMPT_PLACEHOLDER_RE.sub(replace_placeholder, resolved)


def _resolve_task_prompt_data(value: Any, input_state: Any) -> Any:
    if isinstance(value, str):
        return _resolve_task_prompt_text(value, input_state)
    if isinstance(value, Mapping):
        return {
            key: _resolve_task_prompt_data(item, input_state)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_task_prompt_data(item, input_state) for item in value]
    if isinstance(value, tuple):
        return tuple(_resolve_task_prompt_data(item, input_state) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class WorkflowTaskAgentConfig:
    workflow_dir: Path
    repo_root: Path = Path(".")
    provider: str = "auto"
    workflow_id: str | None = None
    task_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_roundtrips: int | None = None
    max_stalled_roundtrips: int = 3
    max_timeout_retries: int = 8
    timeout_backoff_seconds: float = 10.0
    context_compaction_threshold: float = 0.75
    verbose: bool = False


def _context_compaction_threshold(context_window: int, fraction: float) -> int:
    if not 0 < fraction <= 1:
        raise ValueError(
            "context_compaction_threshold must be greater than 0 and at most 1."
        )
    return max(1, int(context_window * fraction))


def _select_workflow_instance(
    workflow: WorkflowInstance,
    workflow_id: str | None,
) -> WorkflowInstance:
    if workflow_id is None:
        return workflow
    prefix = f"{slugify_workflow_id(workflow_id)}-task-"
    return WorkflowInstance(
        workflow.directory,
        {
            task.task_id: task
            for task in workflow.tasks
            if task.task_id.startswith(prefix)
        },
    )


@dataclass(slots=True)
class _TaskActionProgressStrategy(WorkflowActionProgressStrategy[WorkflowAction]):
    """Persist durable-task progress while the shared engine owns the policy."""

    action_engine: WorkflowLLMActionEngine
    repo_root: Path
    events: list[dict[str, Any]]
    stderr: TextIO

    def material_state(self, action: WorkflowAction) -> object:
        return _task_action_material_state(action, self.repo_root)

    def record_no_progress(
        self,
        action: WorkflowAction,
        observation: WorkflowActionObservation,
    ) -> None:
        assert observation.correction is not None
        self.events.append(
            {
                "kind": "no_progress",
                "action_kind": action.kind,
                "message": observation.correction,
            }
        )
        print(
            "Workflow task action made no progress; requesting a different action "
            "from the LLM.",
            file=self.stderr,
        )
        if observation.decision == ProgressDecision.THRESHOLD:
            self.action_engine.reset_progress()


class _WorkflowTaskDisplayClient:
    """Display complete workflow-task exchanges instead of transport chunks."""

    def __init__(self, client: WorkflowLLMClient, *, stderr: TextIO) -> None:
        self._client = client
        self._stderr = stderr

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        print(
            "Workflow task LLM input:\n"
            f"{json.dumps(messages, indent=2, ensure_ascii=False)}",
            file=self._stderr,
            flush=True,
        )
        response = self._client.complete_json(messages)
        print(
            "Workflow task LLM output:\n"
            f"{json.dumps(response, indent=2, ensure_ascii=False)}",
            file=self._stderr,
            flush=True,
        )
        return response


@dataclass(slots=True)
class _TaskWorkflowExecutionStrategy(WorkflowExecutionStrategy):
    """Durable-task adapter for the shared action-roundtrip driver.

    Its policy is intentionally limited to durable task state, publishing, and
    human handoff. Requesting, parsing, retrying, failure thresholds, and
    no-progress correction are all owned by ``WorkflowLLMExecutionDriver``.
    """

    config: WorkflowTaskAgentConfig
    workflow: WorkflowInstance
    task: WorkflowTask
    skill_catalog: tuple[SkillCatalogEntry, ...]
    repo_root: Path
    error_log_root: Path
    client: WorkflowLLMClient
    compaction_client: WorkflowLLMClient
    model: str
    mapping_provider: str
    stdout: TextIO
    stderr: TextIO
    action_engine: WorkflowLLMActionEngine
    events: list[dict[str, Any]]
    deterministic_output_state: Any = None
    requires_deterministic_output_state: bool = False
    response_correction: str | None = None
    compacted_context: dict[str, Any] | None = None
    observer_intervention: str | None = None
    observer_rejected_action_signature: str | None = None

    def next_request(self) -> WorkflowActionRequest:
        while True:
            messages = _build_task_messages(
                self.workflow,
                self.task,
                self.events,
                repo_root=self.repo_root,
                skill_catalog=self.skill_catalog,
                response_correction=self.response_correction,
                compacted_context=self.compacted_context,
                observer_intervention=self.observer_intervention,
            )
            limits = _model_limits_for(self.mapping_provider, self.model)
            estimated_input_tokens = _estimate_message_tokens(messages)
            print(
                f"Workflow task context: {estimated_input_tokens} estimated input "
                f"tokens of {limits.context_window} allowed.",
                file=self.stderr,
                flush=True,
            )
            if self.config.verbose:
                prompt_breakdown = json.dumps(
                    prompt_size_breakdown(messages), indent=2, sort_keys=True
                )
                print(
                    f"Workflow task prompt size breakdown:\n{prompt_breakdown}",
                    file=self.stderr,
                    flush=True,
                )
            compaction_threshold = _context_compaction_threshold(
                limits.context_window,
                self.config.context_compaction_threshold,
            )
            if (
                estimated_input_tokens < compaction_threshold
                and estimated_input_tokens + 1024 < limits.context_window
            ):
                _print_waiting_for_model(self.stderr, self.model)
                return WorkflowActionRequest(
                    client=self.client,
                    messages=messages,
                    parser=_parse_action_response,
                    model=self.model,
                    stderr=self.stderr,
                    max_timeout_retries=self.config.max_timeout_retries,
                    timeout_backoff_seconds=self.config.timeout_backoff_seconds,
                )

            print(
                "Compacting workflow task context before the next LLM call: "
                f"{estimated_input_tokens} estimated input tokens would exceed "
                f"the {compaction_threshold}-token proactive threshold or the "
                f"{limits.context_window}-token context window.",
                file=self.stderr,
                flush=True,
            )
            self.compacted_context, before_tokens, after_tokens = (
                _compact_workflow_task_context(
                    self.workflow,
                    self.task,
                    self.events,
                    client=self.compaction_client,
                    stderr=self.stderr,
                    max_timeout_retries=self.config.max_timeout_retries,
                    timeout_backoff_seconds=self.config.timeout_backoff_seconds,
                )
            )
            self.events.append(
                {
                    "kind": "context_compaction",
                    "before_estimated_input_tokens": before_tokens,
                    "after_estimated_input_tokens": after_tokens,
                }
            )
            print(
                "Compacted workflow task context: "
                f"{before_tokens} -> {after_tokens} estimated input tokens.",
                file=self.stderr,
                flush=True,
            )
            self.response_correction = None

    def material_state(self, action: WorkflowAction) -> object:
        return _task_action_material_state(action, self.repo_root)

    def _llm_error_context(self) -> dict[str, Any]:
        recent_events = self.events[-8:]
        last_successful_action = next(
            (
                event
                for event in reversed(recent_events)
                if event.get("kind")
                not in {"validation_error", "action_error", "tool_error", "no_progress"}
            ),
            None,
        )
        return {
            "task": {
                "task_id": self.task.task_id,
                "description": self.task.description,
                "details": self.task.details,
                "uses_skills": list(self.task.uses_skills),
            },
            "workflow": {
                "directory": str(self.workflow.directory),
                "task_id": self.task.task_id,
            },
            "worktree_root": str(self.repo_root),
            "provider": self.mapping_provider,
            "model": self.model,
            "action_contract": {
                "allowed_actions": [
                    "invoke_tool",
                    "invoke_skill",
                    "get-human-input",
                    "next_step",
                    "complete",
                ],
                "declared_nested_skills": list(self.task.uses_skills),
            },
            "recent_events": recent_events,
            "last_successful_action": last_successful_action,
            "error_count": sum(
                event.get("kind")
                in {"validation_error", "action_error", "tool_error", "no_progress"}
                for event in self.events
            ),
        }

    def report_roundtrip(self, roundtrip: int, action: WorkflowAction) -> None:
        if self.config.verbose:
            print(
                f"Workflow task LLM action:\n{workflow_action_signature(action)}",
                file=self.stdout,
                flush=True,
            )
        print(
            f"Workflow task roundtrip {roundtrip}: {workflow_action_summary(action)}",
            file=self.stdout,
            flush=True,
        )

    def record_no_progress(
        self,
        action: WorkflowAction,
        observation: WorkflowActionObservation,
    ) -> None:
        _TaskActionProgressStrategy(
            action_engine=self.action_engine,
            repo_root=self.repo_root,
            events=self.events,
            stderr=self.stderr,
        ).record_no_progress(action, observation)
        self.response_correction = observation.correction

    def record_response_error(
        self,
        error: RuntimeError,
        payload: dict[str, Any] | None,
    ) -> None:
        response_details = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if payload is not None
            else f"<no parsed response; client error: {error}>"
        )
        guidance = (
            "Return exactly one complete JSON object matching one of the "
            "documented workflow-task action shapes. Do not return markdown, "
            "prose, or an empty response."
        )
        record_workflow_llm_error(
            self.error_log_root,
            execution_mode="process_workflow_task",
            phase="llm_output_parse",
            error=error,
            context=self._llm_error_context(),
            llm_output=payload,
            guidance=guidance,
        )
        if not _is_repairable_task_response_error(error):
            raise error
        print(
            f"Workflow task LLM response requiring repair:\n{response_details}",
            file=self.stderr,
            flush=True,
        )
        self.response_correction = (
            f"The previous response was invalid: {error} {guidance}"
        )
        print(
            "Workflow task response needs repair; requesting a corrected "
            "JSON response from the LLM.",
            file=self.stderr,
        )
        self.events.append(
            {
                "kind": "llm_response_error",
                "error": str(error),
                "response": payload,
            }
        )

    def execute_action(self, action: WorkflowAction) -> WorkflowActionOutcome:
        self.response_correction = None
        if workflow_action_signature(action) == self.observer_rejected_action_signature:
            raise RuntimeError(
                "The observer rejected this exact action after it failed to "
                "make progress. Choose a materially different action."
            )
        if action.kind == "gather_context":
            report = gather_specification_context(
                self.repo_root,
                types=list(action.types),
                keywords=list(action.keywords),
                filters=action.filters,
                feature_id=action.feature_id,
            )
            self.events.append(
                {
                    "kind": action.kind,
                    "feature_id": action.feature_id,
                    "types": list(action.types),
                    "keywords": list(action.keywords),
                    "filters": action.filters,
                    "result": json.loads(render_gather_context_report(report)),
                }
            )
            return WorkflowActionOutcome()
        if action.kind == "next_step":
            self.events.append({"kind": action.kind})
            return WorkflowActionOutcome()
        if action.kind == "read_document":
            self.events.append(
                {
                    "kind": action.kind,
                    "result": _read_task_document(action, self.repo_root),
                }
            )
        if action.kind == "edit":
            self.events.append(
                {
                    "kind": action.kind,
                    "result": _apply_task_edits(action, self.repo_root),
                }
            )
            return WorkflowActionOutcome()
        if action.kind == "yaml_edit":
            if action.file_path is None:
                raise RuntimeError("yaml_edit action must include file_path.")
            path = _resolve_worktree_file_path(action.file_path, self.repo_root)
            if not path.exists():
                raise RuntimeError(
                    f"yaml_edit target {action.file_path!r} does not exist. "
                    "Read or generate the YAML document first."
                )
            updated = _apply_yaml_operations(
                path,
                path.read_text(encoding="utf-8"),
                action.yaml_operations,
            )
            path.write_text(updated, encoding="utf-8")
            self.events.append(
                {
                    "kind": action.kind,
                    "file_path": action.file_path,
                    "operations": [
                        {
                            "op": operation.operation,
                            "section": operation.section,
                            "id": operation.item_id,
                            "path": list(operation.path),
                            "value": operation.value,
                        }
                        for operation in action.yaml_operations
                    ],
                    "result": {"line_count": len(updated.splitlines())},
                }
            )
            return WorkflowActionOutcome()
        if action.kind == "file_management":
            if action.file_operation is None or action.file_path is None:
                raise RuntimeError(
                    "file_management action requires operation and file_path."
                )
            result = manage_worktree_file(
                self.repo_root,
                operation=action.file_operation,
                file_path=action.file_path,
                destination_path=action.destination_path,
            )
            self.events.append(
                {
                    "kind": action.kind,
                    "operation": action.file_operation,
                    "file_path": action.file_path,
                    "destination_path": action.destination_path,
                    "result": result,
                }
            )
            return WorkflowActionOutcome()
        if action.kind == "prompt_user":
            return self._handoff(_prompt_user_handoff(action, self.task), "handoff")
        if action.kind == "invoke_skill":
            if action.skill_name is None:
                raise RuntimeError("invoke_skill action must include a skill name.")
            self.events.append(
                {
                    "kind": action.kind,
                    "skill": action.skill_name,
                    "result": _run_skill_for_agent(
                        action.skill_name,
                        catalog=self.skill_catalog,
                        client=self.client,
                        task=self.task,
                        repo_root=self.repo_root,
                        stdout=self.stdout,
                        stderr=self.stderr,
                        max_timeout_retries=self.config.max_timeout_retries,
                        timeout_backoff_seconds=self.config.timeout_backoff_seconds,
                        context=action.context,
                        clean=action.clean,
                    ),
                }
            )
            return WorkflowActionOutcome()
        if action.kind == "invoke_tool":
            self._execute_tool(action)
            return WorkflowActionOutcome()
        if action.kind == "complete":
            if self.requires_deterministic_output_state and action.output_state != (
                self.deterministic_output_state
            ):
                raise ValueError(
                    "This task must persist the exact deterministic pre-step result "
                    "in output_state; do not replace it with a summary."
                )
            completed = self.workflow.complete_task(
                self.task.task_id,
                action.output_state,
            )
            _publish_workflow_progress(
                self.repo_root,
                self.workflow,
                workflow_id=workflow_id_from_task_id(self.task.task_id),
                reason=f"complete {completed.task_id}",
                stdout=self.stdout,
                open_pull_request=False,
                events=self.events,
            )
            if action.text:
                print(action.text, file=self.stdout)
            print(f"Completed workflow task: {completed.task_id}", file=self.stdout)
            return WorkflowActionOutcome(continue_running=False)
        if action.kind == "get-human-input":
            return self._handoff(action.human_input or {}, "human input required by")
        raise RuntimeError(f"Unsupported workflow task action: {action.kind}")

    def apply_observer_decision(
        self,
        decision: ObserverDecision,
        action: WorkflowAction,
        observation: WorkflowActionObservation | None,
    ) -> bool:
        """Apply advisory coaching to the next durable-task request."""
        if decision.verdict in {"continue", "request_human"}:
            return False
        guidance = [f"Reason: {decision.reason}", *decision.guidance]
        if decision.expected_progress:
            guidance.append(f"Evidence expected: {decision.expected_progress}")
        self.observer_intervention = "Observer intervention\n" + "\n".join(
            f"- {item}" for item in guidance
        )
        if decision.verdict == "redirect" and decision.target_step_id:
            self.observer_intervention += (
                "\n- Redirect ignored: durable workflow tasks do not expose "
                "skill step ids."
            )
        self.observer_rejected_action_signature = workflow_action_signature(action)
        self.events.append(
            {
                "kind": "observer_intervention",
                "verdict": decision.verdict,
                "reason": decision.reason,
                "target_step_id": decision.target_step_id,
                "action": json.loads(workflow_action_signature(action)),
                "material_progress": (
                    observation.made_progress if observation is not None else None
                ),
            }
        )
        return observation is None

    def clear_observer_intervention(self) -> None:
        self.observer_intervention = None
        self.observer_rejected_action_signature = None

    def _handoff(
        self, human_input: dict[str, Any], reason_prefix: str
    ) -> WorkflowActionOutcome:
        human_task, follow_up_task = _insert_human_handoff(
            self.workflow,
            self.task,
            human_input,
        )
        print(f"Workflow blocked on human task: {human_task.task_id}", file=self.stdout)
        if follow_up_task is not None:
            print(
                f"Inserted follow-up task: {follow_up_task.task_id}",
                file=self.stdout,
            )
        _publish_workflow_progress(
            self.repo_root,
            self.workflow,
            workflow_id=workflow_id_from_task_id(self.task.task_id),
            reason=f"{reason_prefix} {self.task.task_id}",
            stdout=self.stdout,
            open_pull_request=False,
            events=self.events,
        )
        return WorkflowActionOutcome(continue_running=False)

    def _execute_tool(self, action: WorkflowAction) -> None:
        repaired_parameters = _repair_workflow_file_command(
            action.parameters,
            self.workflow.directory,
        )
        if repaired_parameters is not None:
            print(
                "Corrected malformed workflow filename suffix to the exact workflow "
                "filename.",
                file=self.stderr,
            )
            action = WorkflowAction(
                kind=action.kind,
                tool=action.tool,
                parameters=repaired_parameters,
            )
        command_error = _workflow_file_command_error(
            action.parameters,
            self.workflow.directory,
        )
        if command_error is not None:
            print(command_error, file=self.stderr)
            result: Any = {
                "command": action.parameters.get("command"),
                "cwd": str(self.repo_root),
                "returncode": 2,
                "stdout": "",
                "stderr": command_error,
            }
        elif action.tool in {"shell", "internal"}:
            if action.tool == "internal":
                _validate_internal_command(action.parameters.get("command"))
            result = _execute_shell_tool(
                action.parameters,
                worktree_root=self.repo_root,
                stdout=self.stdout,
                stderr=self.stderr,
                verbose=self.config.verbose,
            )
        elif action.tool in {GIT_TOOL, GH_TOOL}:
            result = execute_intrinsic_git_gh_tool(
                action.tool,
                action.parameters,
                worktree_root=self.repo_root,
            )
            if result.get("stdout"):
                print(str(result["stdout"]), end="", file=self.stdout)
            if result.get("stderr"):
                print(str(result["stderr"]), end="", file=self.stderr)
        elif action.tool == "fuzzy-match":
            result = _execute_fuzzy_match_tool(
                action.parameters,
                worktree_root=self.repo_root,
            )
        elif action.tool is not None and is_basedpyright_tool(action.tool):
            result = execute_basedpyright_tool(
                action.tool,
                action.parameters,
                worktree_root=self.repo_root,
            )
        else:
            raise RuntimeError(
                f"Unsupported workflow task tool {action.tool!r}; supported tools "
                "are shell, internal, git, gh, fuzzy-match, basedpyright-symbol, and "
                "basedpyright-structure."
            )
        event = {
            "kind": action.kind,
            "tool": action.tool,
            "parameters": action.parameters,
            "result": result,
        }
        _record_task_pull_request(
            action,
            self.workflow,
            self.task.task_id,
            self.repo_root,
            self.events,
            result,
        )
        self.events.append(event)

    def record_action_error(self, action: WorkflowAction, error: Exception) -> None:
        self.response_correction = _action_response_correction(action, error)
        record_workflow_llm_error(
            self.error_log_root,
            execution_mode="process_workflow_task",
            phase="action_validation_or_execution",
            error=error,
            context=self._llm_error_context(),
            attempted_action=json.loads(workflow_action_signature(action)),
            guidance=self.response_correction,
        )
        self.events.append(
            {
                "kind": (
                    "tool_error" if action.kind == "invoke_tool" else "action_error"
                ),
                "action_kind": action.kind,
                "tool": action.tool,
                "parameters": action.parameters,
                "error": str(error),
            }
        )
        print(
            "Workflow task action needs correction; requesting a corrected action "
            "from the LLM.",
            file=self.stderr,
        )

    def action_failure_exit_code(self, action: WorkflowAction) -> int:
        _ = action
        print(
            "Workflow task stopped after repeated corrective-action failures.",
            file=self.stderr,
        )
        return 1

    def observe_outcome(
        self,
        action: WorkflowAction,
        observation: WorkflowActionObservation,
        outcome: WorkflowActionOutcome,
    ) -> WorkflowActionOutcome:
        _ = action
        if observation.correction is not None:
            self.response_correction = observation.correction
        return outcome

    def exhausted_roundtrips_exit_code(self) -> int:
        print(
            "Workflow task "
            f"{self.task.task_id} stopped after reaching the configured "
            "roundtrip limit.",
            file=self.stderr,
        )
        _publish_workflow_progress(
            self.repo_root,
            self.workflow,
            workflow_id=workflow_id_from_task_id(self.task.task_id),
            reason=f"roundtrip limit for {self.task.task_id}",
            stdout=self.stdout,
            open_pull_request=False,
            events=self.events,
        )
        return 2


def _record_task_pull_request(
    action: WorkflowAction,
    workflow: WorkflowInstance,
    task_id: str,
    repo_root: Path,
    events: Sequence[Mapping[str, Any]],
    result: object,
) -> None:
    command = action.parameters.get("command")
    if command is None and isinstance(result, Mapping):
        command = result.get("command")
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
        return
    if not is_pull_request_create_command([str(item) for item in command]):
        return
    if not isinstance(result, Mapping) or result.get("returncode") != 0:
        return
    output = result.get("stdout")
    if not isinstance(output, str):
        return
    number = pull_request_number(output)
    if number is None:
        raise RuntimeError(
            "GitHub did not return a pull-request URL, so the workflow record "
            "could not be named under docs/prs/<pr-number>.yaml."
        )
    branch = _git_output(repo_root, ["branch", "--show-current"])
    state = load_workflow_git_state(
        workflow.directory,
        workflow_id=workflow_id_from_task_id(task_id),
    )
    record_pull_request_workflow(
        repo_root,
        number,
        branch=branch,
        base_branch=state.base_branch if state is not None else "main",
        title="Agent-created pull request",
        workflow_name=workflow.directory.name,
        workflow_path=_relative_workflow_path(repo_root, workflow.directory),
        steps=[_workflow_task_record(task) for task in workflow.tasks],
        events=[
            *events,
            {
                "kind": action.kind,
                "tool": action.tool,
                "parameters": action.parameters,
                "result": result,
            },
        ],
        explanation=(
            "This record documents the durable workflow steps and tool calls "
            "observed before the agent created the pull request."
        ),
    )


def run_workflow_task(
    config: WorkflowTaskAgentConfig,
    *,
    client: WorkflowLLMClient | None = None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    configured_repo_root = resolve_repo_root(config.repo_root)
    configured_workflow_dir = config.workflow_dir.resolve()
    try:
        configured_workflow = _select_workflow_instance(
            WorkflowInstance.from_directory(configured_workflow_dir),
            config.workflow_id,
        )
        configured_task = _select_task(configured_workflow, config.task_id)
        configured_workflow_id = workflow_id_from_task_id(
            configured_task.task_id if configured_task is not None else ""
        )
        configured_workflow_id = config.workflow_id or configured_workflow_id
        configured_git_state = load_workflow_git_state(
            configured_workflow_dir,
            workflow_id=configured_workflow_id,
        )
        if configured_git_state is None and configured_workflow_id is None:
            configured_git_state = load_workflow_git_state(configured_workflow_dir)
        if configured_git_state is not None:
            project_root = _resolve_project_root(
                configured_repo_root,
                configured_repo_root,
            )
            dependencies_complete, incomplete_dependencies = (
                workflow_dependencies_completion(
                    project_root,
                    configured_git_state,
                )
            )
            if not dependencies_complete:
                print(
                    "Workflow dependencies are not complete; no task work was started.",
                    file=stderr,
                )
                for dependency in incomplete_dependencies:
                    print(f"  - {dependency}", file=stderr)
                return 1
        repo_root, workflow_dir = _resolve_workflow_task_context(
            config,
            configured_repo_root=configured_repo_root,
            workflow_git_state=configured_git_state,
            stdout=stdout,
            stderr=stderr,
        )
    except WorkflowGitInconsistency as exc:
        print(
            "Workflow Git state is inconsistent; no task work was started.",
            file=stderr,
        )
        print(str(exc), file=stderr)
        return 2
    skill_catalog = _load_skill_catalog(
        repo_root / "skill-definitions",
        stderr=stderr,
    )
    workflow = _select_workflow_instance(
        WorkflowInstance.from_directory(workflow_dir),
        configured_workflow_id,
    )
    requested_task_id = config.task_id
    client_was_provided = client is not None
    dump_root = _resolve_project_root(
        configured_repo_root,
        repo_root,
    )
    workflow_git_state = load_workflow_git_state(
        workflow_dir,
        workflow_id=(
            configured_git_state.proposed_pr_id
            if configured_git_state is not None
            else configured_workflow_id
        ),
    )
    try:
        if configured_git_state is not None and workflow_git_state is None:
            raise WorkflowGitInconsistency(
                json.dumps(
                    {
                        "proposed_pr_id": configured_git_state.proposed_pr_id,
                        "inconsistencies": [
                            "integration worktree does not contain the expected "
                            "workflow Git metadata"
                        ],
                        "recovery_command": (
                            "powdrr-lift workflow-recovery --proposed-pr-id "
                            f"{configured_git_state.proposed_pr_id} --cleanup"
                        ),
                    },
                    indent=2,
                )
            )
        if workflow_git_state is not None:
            project_root = _resolve_project_root(configured_repo_root, repo_root)
            validate_workflow_git_state(
                project_root,
                workflow_git_state,
                config.task_id or f"{workflow_git_state.proposed_pr_id}-workflow",
            )
        else:
            project_root = configured_repo_root
    except WorkflowGitInconsistency as exc:
        print(
            "Workflow Git state is inconsistent; no task work was started.",
            file=stderr,
        )
        print(str(exc), file=stderr)
        return 2

    while True:
        task = _select_task(workflow, requested_task_id)
        requested_task_id = None
        if task is None:
            ready_human_tasks = workflow.ready_tasks(assignee_type=AssigneeType.HUMAN)
            if ready_human_tasks:
                print(
                    f"Workflow waiting on human task: {ready_human_tasks[0].task_id}",
                    file=stdout,
                )
                return 0
            if workflow.tasks and all(
                item.status is TaskStatus.COMPLETED for item in workflow.tasks
            ):
                if workflow_git_state is not None:
                    _open_final_workflow_pull_request(
                        repo_root,
                        workflow,
                        workflow_git_state,
                        stdout=stdout,
                    )
                    return 0
                if not _is_git_worktree(repo_root):
                    return 0
                print(
                    "Workflow is complete, but no workflow Git state was found; "
                    "no final pull request was created.",
                    file=stderr,
                )
                return 1
            print("No ready agent task found.", file=stderr)
            return 1

        try:
            if workflow_git_state is not None:
                validate_workflow_git_state(
                    project_root,
                    workflow_git_state,
                    task.task_id,
                )
                claim_workflow_task(project_root, workflow_git_state, task.task_id)
            task = workflow.claim_task(task.task_id)
        except (RuntimeError, ValueError) as exc:
            print(
                "Workflow state changed or is inconsistent; no task work was "
                "started for this task.",
                file=stderr,
            )
            print(str(exc), file=stderr)
            return 2
        print(f"Claimed workflow task: {task.task_id}", file=stdout)
        print("Publishing claimed task state to GitHub...", file=stdout, flush=True)
        _publish_workflow_progress(
            repo_root,
            workflow,
            workflow_id=workflow_id_from_task_id(task.task_id),
            reason=f"claim {task.task_id}",
            stdout=stdout,
            open_pull_request=False,
        )

        provider = resolve_workflow_provider(config.provider)
        mappings = tuple(_default_llm_mappings(provider).items())
        mapping = _resolve_workflow_task_mapping(
            task.llm_type,
            mappings=mappings,
            provider=provider,
        )
        if mapping is None:
            raise RuntimeError(f"Workflow task has no LLM mapping: {task.task_id}")
        model = mapping.model
        if client_was_provided:
            assert client is not None
            task_client = client
        else:
            task_client = _build_workflow_client(config, task)
        if config.verbose:
            task_client = _WorkflowTaskDisplayClient(task_client, stderr=stderr)
        task_client = _maybe_record_llm_exchanges(task_client, dump_root)
        compaction_client = task_client
        long_context_backup = _long_context_backup_for(model, mappings)
        if not client_was_provided and long_context_backup is not None:
            backup_client = _build_workflow_client_for_mapping(
                config,
                task,
                long_context_backup,
            )
            if config.verbose:
                backup_client = _WorkflowTaskDisplayClient(
                    backup_client,
                    stderr=stderr,
                )
            compaction_client = _maybe_record_llm_exchanges(backup_client, dump_root)

        driver_events: list[dict[str, Any]] = []
        deterministic_output_state, requires_deterministic_output_state = (
            _run_task_deterministic_pre_step(
                task,
                repo_root=repo_root,
                events=driver_events,
            )
        )
        driver = WorkflowLLMExecutionDriver(
            max_stalled_roundtrips=config.max_stalled_roundtrips
        )
        strategy = _TaskWorkflowExecutionStrategy(
            config=config,
            workflow=workflow,
            task=task,
            skill_catalog=skill_catalog,
            repo_root=repo_root,
            error_log_root=dump_root,
            client=task_client,
            compaction_client=compaction_client,
            model=model,
            mapping_provider=mapping.provider,
            stdout=stdout,
            stderr=stderr,
            action_engine=driver.action_engine,
            events=driver_events,
            deterministic_output_state=deterministic_output_state,
            requires_deterministic_output_state=requires_deterministic_output_state,
        )
        observer_mapping = _resolve_workflow_task_mapping(
            "high_reasoning",
            mappings=mappings,
            provider=provider,
        )
        if observer_mapping is not None:
            observer_client = (
                task_client
                if client_was_provided
                else _maybe_record_llm_exchanges(
                    _build_workflow_client_for_mapping(
                        config,
                        task,
                        observer_mapping,
                    ),
                    dump_root,
                )
            )

            def observer_context(
                _workflow: WorkflowInstance = workflow,
                _task: WorkflowTask = task,
                _events: list[dict[str, Any]] = driver_events,
            ) -> ObserverExecutionContext:
                return ObserverExecutionContext(
                    execution_mode="process_workflow_task",
                    root_intent=(
                        f"Complete workflow {_workflow.directory.name}: "
                        f"{_task.description}"
                    ),
                    skill_or_workflow=_workflow.directory.name,
                    current_step_id=_task.task_id,
                    current_step_intent=_task.details or _task.description,
                    validation_state={
                        "status": _task.status.value,
                        "output_state_type": _task.output_state_type,
                        "issue_count": sum(
                            event.get("kind")
                            in {
                                "validation_error",
                                "action_error",
                                "tool_error",
                                "no_progress",
                            }
                            for event in _events
                        ),
                    },
                    handoff_state=compact_observer_mapping(
                        {
                            "input_state": _task.input_state,
                            "output_state": _task.output_state,
                            "upstream_task_ids": list(_task.upstream_task_ids),
                        }
                    ),
                )

            driver.observer = ShadowWorkflowObserver(
                client=observer_client,
                model=observer_mapping.model,
                provider=observer_mapping.provider,
                worktree_root=repo_root,
                log_root=dump_root,
                context_provider=observer_context,
            )
        try:
            exit_code = driver.run(
                strategy,
                max_roundtrips=config.max_roundtrips,
                signature=workflow_action_signature,
            )
        except WorkflowLLMTimeoutExhausted as exc:
            return _handle_exhausted_timeout(
                repo_root,
                task,
                stdout=stdout,
                stderr=stderr,
                error=exc,
            )
        if exit_code != 0:
            return exit_code
        workflow = _select_workflow_instance(
            WorkflowInstance.from_directory(workflow_dir),
            configured_workflow_id,
        )


def _select_task(
    workflow: WorkflowInstance,
    task_id: str | None,
) -> WorkflowTask | None:
    ready_tasks = workflow.ready_tasks(assignee_type=AssigneeType.AGENT)
    if task_id is not None:
        selected = next((task for task in ready_tasks if task.task_id == task_id), None)
        if selected is None:
            raise ValueError(f"Task is not a ready agent task: {task_id}")
        return selected
    return ready_tasks[0] if ready_tasks else None


def _resolve_workflow_task_context(
    config: WorkflowTaskAgentConfig,
    *,
    configured_repo_root: Path,
    workflow_git_state: WorkflowGitState | None,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[Path, Path]:
    """Resolve the single integration worktree used by the workflow run."""
    configured_workflow_dir = config.workflow_dir.resolve()
    if not _is_git_worktree(configured_repo_root):
        return configured_repo_root, configured_workflow_dir
    if workflow_git_state is None:
        raise WorkflowGitInconsistency(
            json.dumps(
                {
                    "inconsistencies": [
                        "workflow Git metadata is missing or invalid; "
                        "cannot determine the integration branch"
                    ],
                    "recovery_command": (
                        "powdrr-lift workflow-recovery --proposed-pr-id "
                        "<proposed-pr-id> --cleanup"
                    ),
                },
                indent=2,
            )
        )

    project_root = _resolve_project_root(
        configured_repo_root,
        configured_repo_root,
    )
    try:
        integration_worktree, integration_branch = create_workflow_worktree(
            project_root,
            workflow_git_state.proposed_pr_id,
            base_branch=workflow_git_state.base_branch,
        )
    except RuntimeError as exc:
        raise WorkflowGitInconsistency(
            json.dumps(
                {
                    "proposed_pr_id": workflow_git_state.proposed_pr_id,
                    "inconsistencies": [str(exc)],
                    "recovery_command": (
                        "powdrr-lift workflow-recovery --proposed-pr-id "
                        f"{workflow_git_state.proposed_pr_id} --cleanup"
                    ),
                },
                indent=2,
            )
        ) from exc
    print(
        f"Using workflow integration branch {integration_branch} in "
        f"{integration_worktree}",
        file=stdout,
        flush=True,
    )
    return (
        integration_worktree,
        integration_worktree / workflow_git_state.workflow_relative_directory,
    )


def _publish_workflow_progress(
    repo_root: Path,
    workflow: WorkflowInstance,
    *,
    workflow_id: str | None = None,
    reason: str,
    stdout: TextIO,
    open_pull_request: bool = True,
    events: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Commit and publish durable workflow progress for execution tasks.

    Unit tests and callers operating outside a git checkout can still use the
    execution loop; in that case task JSON is durable locally and publishing is
    skipped. A real workflow execution always runs from a git worktree and
    commits to the integration branch; final pull-request creation is handled
    when the workflow reaches completion.
    """
    if not _is_git_worktree(repo_root):
        return

    workflow_git_state = load_workflow_git_state(
        workflow.directory,
        workflow_id=workflow_id,
    )
    branch = _git_output(repo_root, ["branch", "--show-current"])
    if (
        workflow_git_state is not None
        and branch != workflow_git_state.integration_branch
    ):
        raise WorkflowGitInconsistency(
            json.dumps(
                {
                    "proposed_pr_id": workflow_git_state.proposed_pr_id,
                    "inconsistencies": [
                        f"workflow execution is on {branch!r}, expected "
                        f"integration branch {workflow_git_state.integration_branch!r}"
                    ],
                    "recovery_command": (
                        "powdrr-lift workflow-recovery --proposed-pr-id "
                        f"{workflow_git_state.proposed_pr_id} --cleanup"
                    ),
                },
                indent=2,
            )
        )
    if branch in {"", "main", "master"}:
        branch = _workflow_branch_name(workflow)
        if _git_result(
            repo_root, ["show-ref", "--verify", f"refs/heads/{branch}"]
        ).returncode:
            _run_git(repo_root, ["switch", "-c", branch])
        else:
            _run_git(repo_root, ["switch", branch])

    _run_git(repo_root, ["add", "--all"])
    status = _git_result(repo_root, ["status", "--porcelain"])
    if status.stdout.strip():
        _run_git(
            repo_root,
            ["commit", "-m", f"Persist workflow progress: {reason}"],
        )
    # Always push at a task boundary. A nested operation may have created a
    # local commit, leaving no working-tree changes for the add/status check.
    _run_git(repo_root, ["push", "--set-upstream", "origin", branch])
    if not open_pull_request:
        print(f"Published workflow progress on branch: {branch}", file=stdout)
        return

    default_branch = (
        workflow_git_state.integration_branch
        if workflow_git_state is not None
        else _default_branch(repo_root)
    )
    existing_pr = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
        cwd=repo_root,
        capture_output=True,
        env=_noninteractive_environment(),
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if existing_pr.returncode == 0 and existing_pr.stdout.strip():
        _write_workflow_record(
            repo_root,
            workflow,
            workflow_git_state,
            existing_pr.stdout.strip(),
            events=events,
        )
        print(
            f"Updated workflow progress PR: {existing_pr.stdout.strip()}", file=stdout
        )
        return

    title = f"Workflow progress: {workflow.directory.name}"
    body = (
        "Automated durable workflow progress. This draft PR contains task state "
        "and worktree changes so execution can resume from the next task."
    )
    created_pr = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            default_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=repo_root,
        capture_output=True,
        env=_noninteractive_environment(),
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if created_pr.returncode != 0:
        raise RuntimeError(
            "Could not create workflow progress pull request: "
            f"{created_pr.stderr.strip()}"
        )
    _write_workflow_record(
        repo_root,
        workflow,
        workflow_git_state,
        created_pr.stdout.strip(),
        events=events,
        title=title,
    )
    print(f"Created workflow progress PR: {created_pr.stdout.strip()}", file=stdout)


def publish_workflow_progress(
    repo_root: Path,
    workflow: WorkflowInstance,
    *,
    reason: str,
    stdout: TextIO,
    open_pull_request: bool = True,
    events: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Publish workflow progress for non-LLM workflow participants."""
    _publish_workflow_progress(
        repo_root,
        workflow,
        reason=reason,
        stdout=stdout,
        open_pull_request=open_pull_request,
        events=events,
    )


def _write_workflow_record(
    repo_root: Path,
    workflow: WorkflowInstance,
    workflow_git_state: WorkflowGitState | None,
    pull_request_output: str,
    *,
    events: Sequence[Mapping[str, Any]],
    title: str | None = None,
) -> None:
    number = pull_request_number(pull_request_output)
    if number is None:
        raise RuntimeError(
            "GitHub did not return a pull-request URL, so the workflow record "
            "could not be named under docs/prs/<pr-number>.yaml."
        )
    branch = (
        workflow_git_state.integration_branch
        if workflow_git_state is not None
        else _git_output(repo_root, ["branch", "--show-current"])
    )
    base_branch = (
        workflow_git_state.base_branch
        if workflow_git_state is not None
        else _default_branch(repo_root)
    )
    record_pull_request_workflow(
        repo_root,
        number,
        branch=branch,
        base_branch=base_branch,
        title=title or f"Workflow: {workflow.directory.name}",
        workflow_name=workflow.directory.name,
        workflow_path=_relative_workflow_path(repo_root, workflow.directory),
        steps=[_workflow_task_record(task) for task in workflow.tasks],
        events=events,
        explanation=(
            "This record documents the workflow steps selected for the pull "
            "request and the tool calls observed while the durable agent ran."
        ),
    )


def _workflow_task_record(task: WorkflowTask) -> dict[str, Any]:
    data = task.to_data()
    return {
        key: data[key]
        for key in (
            "task_id",
            "status",
            "description",
            "assignee_role",
            "uses_skills",
            "tool_invocations",
        )
        if key in data
    }


def _relative_workflow_path(repo_root: Path, workflow_directory: Path) -> str:
    try:
        return str(workflow_directory.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(workflow_directory)


def _open_final_workflow_pull_request(
    repo_root: Path,
    workflow: WorkflowInstance,
    workflow_git_state: WorkflowGitState,
    *,
    stdout: TextIO,
) -> None:
    """Open the human-facing integration PR after every task is complete."""
    existing_pr = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            workflow_git_state.integration_branch,
            "--json",
            "url",
            "--jq",
            ".url",
        ],
        cwd=repo_root,
        capture_output=True,
        env=_noninteractive_environment(),
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if existing_pr.returncode == 0 and existing_pr.stdout.strip():
        _write_workflow_record(
            repo_root,
            workflow,
            workflow_git_state,
            existing_pr.stdout.strip(),
            events=(),
            title=f"Workflow: {workflow.directory.name}",
        )
        print(f"Updated final workflow PR: {existing_pr.stdout.strip()}", file=stdout)
        return
    created_pr = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            workflow_git_state.base_branch,
            "--head",
            workflow_git_state.integration_branch,
            "--title",
            f"Workflow: {workflow.directory.name}",
            "--body",
            (
                "Final integration pull request for durable workflow "
                f"{workflow.directory.name}. All workflow tasks are complete on "
                "the integration branch."
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        env=_noninteractive_environment(),
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if created_pr.returncode != 0:
        raise RuntimeError(
            f"Could not create final workflow pull request: {created_pr.stderr.strip()}"
        )
    _write_workflow_record(
        repo_root,
        workflow,
        workflow_git_state,
        created_pr.stdout.strip(),
        events=(),
        title=f"Workflow: {workflow.directory.name}",
    )
    print(f"Created final workflow PR: {created_pr.stdout.strip()}", file=stdout)


def _is_git_worktree(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_result(
    repo_root: Path, arguments: list[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        env=_noninteractive_environment(),
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )


def _noninteractive_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GH_PROMPT_DISABLED"] = "1"
    return environment


def _run_git(repo_root: Path, arguments: list[str]) -> str:
    result = _git_result(repo_root, arguments)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_output(repo_root: Path, arguments: list[str]) -> str:
    return _run_git(repo_root, arguments)


def _workflow_branch_name(workflow: WorkflowInstance) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", workflow.directory.name.casefold()).strip("-")
    return f"workflow/{slug or 'execution'}"


def _default_branch(repo_root: Path) -> str:
    remote_head = _git_result(
        repo_root,
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
    )
    if remote_head.returncode == 0 and remote_head.stdout.strip():
        return remote_head.stdout.strip().removeprefix("origin/")
    return "main"


def _handle_exhausted_timeout(
    repo_root: Path,
    task: WorkflowTask,
    *,
    stdout: TextIO,
    stderr: TextIO,
    error: Exception,
) -> int:
    print(
        f"LLM request timed out after retries for workflow task {task.task_id}: "
        f"{error}",
        file=stderr,
        flush=True,
    )
    if ".worktrees" in repo_root.parts:
        print(
            "Keeping the workflow integration worktree so the task can be "
            "resumed after the timeout.",
            file=stderr,
            flush=True,
        )
    else:
        print(
            "Keeping the configured repository because it is not a dedicated worktree.",
            file=stderr,
            flush=True,
        )
    return 2


def _delete_workflow_task_worktree(repo_root: Path, *, stderr: TextIO) -> None:
    project_root = _resolve_project_root(repo_root, repo_root)
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(repo_root)],
        cwd=project_root,
        capture_output=True,
        env=_noninteractive_environment(),
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"Failed to delete dedicated workflow worktree: {result.stderr.strip()}",
            file=stderr,
            flush=True,
        )


def _build_task_messages(
    workflow: WorkflowInstance,
    task: WorkflowTask,
    events: list[dict[str, Any]],
    *,
    repo_root: Path | None = None,
    response_correction: str | None = None,
    compacted_context: dict[str, Any] | None = None,
    skill_catalog: tuple[SkillCatalogEntry, ...] = (),
    observer_intervention: str | None = None,
) -> list[dict[str, str]]:
    resolved_task_data = _resolve_task_prompt_data(task.to_data(), task.input_state)
    context_data: dict[str, Any]
    if compacted_context is None:
        context_data = {
            "task_context": _task_context_prompt_data(workflow, task),
            "events": _task_events_for_prompt(events),
        }
    else:
        context_data = {"compacted_context": compacted_context}
    workflow_dir = str(workflow.directory)
    if repo_root is not None:
        try:
            workflow_dir = str(workflow.directory.relative_to(repo_root))
        except ValueError:
            pass
    available_skills: list[dict[str, Any]] = []
    for entry in skill_catalog:
        skill_data: dict[str, Any] = {"name": entry.skill.name}
        if entry.skill.name in task.uses_skills:
            skill_data["when_to_use"] = list(entry.skill.when_to_use)
        available_skills.append(skill_data)
    return [
        {
            "role": "system",
            "content": _task_system_prompt(
                interaction_style=task.interaction_style,
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "execution_mode": "process_workflow_task",
                    "task": resolved_task_data,
                    **context_data,
                    "step_context": (
                        [response_correction] if response_correction is not None else []
                    ),
                    **(
                        {"observer_intervention": observer_intervention}
                        if observer_intervention is not None
                        else {}
                    ),
                    "workflow_dir": workflow_dir,
                    "workflow_files": _workflow_file_names(workflow.directory),
                    "available_tools": [
                        {
                            "name": "shell",
                            "description": (
                                "Execute a shell command in the current worktree."
                            ),
                        },
                        {
                            "name": "internal",
                            "description": (
                                "Execute a powdrr-lift CLI command. This tool is "
                                "always available, but may invoke only powdrr-lift."
                            ),
                        },
                        {
                            "name": GIT_TOOL,
                            "description": (
                                "Intrinsic Git tool; supports status, add, and move. "
                                "Use invoke_tool with parameters.operation."
                            ),
                        },
                        {
                            "name": GH_TOOL,
                            "description": (
                                "Intrinsic GitHub tool for pull-request creation, "
                                "inspection, and inline review comments. Use "
                                "parameters.operation."
                            ),
                        },
                        {
                            "name": "fuzzy-match",
                            "description": (
                                "Search worktree paths with find-like filters and "
                                "fuzzy name matching."
                            ),
                        },
                        {
                            "name": BASEDPYRIGHT_SYMBOL_TOOL,
                            "description": (
                                "Find Python symbols by name across the worktree. "
                                "Parameters: query and optional limit."
                            ),
                        },
                        {
                            "name": BASEDPYRIGHT_STRUCTURE_TOOL,
                            "description": (
                                "Discover the classes, functions, methods, and "
                                "variables in a Python file. Parameter: path."
                            ),
                        },
                    ],
                    "available_skills": available_skills,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def _compact_workflow_task_context(
    workflow: WorkflowInstance,
    task: WorkflowTask,
    events: list[dict[str, Any]],
    *,
    client: WorkflowLLMClient,
    stderr: TextIO,
    max_timeout_retries: int,
    timeout_backoff_seconds: float,
) -> tuple[dict[str, Any], int, int]:
    resolved_task_data = _resolve_task_prompt_data(task.to_data(), task.input_state)
    actionable_context = _task_events_for_prompt(events)
    compaction_messages = [
        {
            "role": "system",
            "content": (
                "You are compacting context for a workflow task. Review the full "
                "task and all current context, then preserve only the information "
                "necessary for another LLM to perform this exact task. Preserve "
                "specific paths, commands, requirements, decisions, constraints, "
                "errors, and outputs that remain actionable. Do not invent or "
                "execute anything. Return exactly one JSON object with a single "
                "compacted_context key whose value is the necessary context."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": resolved_task_data,
                    "task_description": resolved_task_data["description"],
                    "task_details": resolved_task_data.get("details"),
                    "current_context": {
                        "task_context": _resolve_task_prompt_data(
                            workflow.task_context(task.task_id), task.input_state
                        ),
                        "events": events,
                        "latest_actionable": actionable_context,
                    },
                    "response_shape": {
                        "compacted_context": (
                            "Only context necessary to perform the full task"
                        )
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
    before_tokens = _estimate_message_tokens(compaction_messages)
    print(
        "waiting for context compaction LLM response...",
        file=stderr,
        flush=True,
    )
    response = complete_json_with_timeout_retry(
        client,
        compaction_messages,
        model="context-compaction",
        stderr=stderr,
        max_timeout_retries=max_timeout_retries,
        timeout_backoff_seconds=timeout_backoff_seconds,
    )
    compacted_context = response.get("compacted_context")
    if not isinstance(compacted_context, dict):
        raise RuntimeError(
            "Context compaction response must include a compacted_context object."
        )
    # The compaction model may summarize aggressively, but it must never be
    # allowed to discard the latest repairable result or failure.
    compacted_context = {
        **compacted_context,
        "latest_actionable": actionable_context,
    }
    after_tokens = _estimate_message_tokens(
        [
            {
                "role": "user",
                "content": json.dumps(
                    {"task": task.to_data(), "compacted_context": compacted_context},
                    ensure_ascii=False,
                ),
            }
        ]
    )
    return compacted_context, before_tokens, after_tokens


def _is_repairable_task_response_error(exc: RuntimeError) -> bool:
    message = str(exc).casefold()
    return any(
        phrase in message
        for phrase in (
            "was not valid json",
            "must be a json object",
            "workflow task action must include",
            "workflow action response must include action",
            "unknown workflow task action",
            "must include parameters.command",
            "must include output_state",
        )
    )


def _action_response_correction(
    action: WorkflowAction,
    error: Exception,
) -> str:
    correction = (
        f"The previous {action.kind} action failed: {error}. "
        "Return a corrected JSON action and "
        "do not repeat the failed command unchanged."
    )
    if action.tool == "fuzzy-match":
        correction += (
            " A fuzzy-match command must be an array beginning with "
            "['fuzzy-match', '<search-root>', '-name', '<query>']; add "
            "-name and its non-empty query, then any optional -type, -path, "
            "-maxdepth, -mindepth, -threshold, or -print options."
        )
    if action.kind == "read_document":
        correction += (
            " For read_document, use a positive start_line and end_line, keep "
            "end_line greater than or equal to start_line, and keep the entire "
            "range within the document line count stated in the error."
        )
    return correction


def _task_action_material_state(
    action: WorkflowAction,
    repo_root: Path,
) -> tuple[tuple[str, str | None], ...] | None:
    """Return the stable state that a repeat of this action may change.

    This intentionally avoids a repository-wide ``git status`` scan for every
    action.  Edits are the only action whose target is known in advance; tool
    output and event logging are context, not material progress.
    """
    if action.kind not in {"edit", "file_management"}:
        return None
    if action.kind == "file_management":
        file_paths = tuple(
            path
            for path in (action.file_path, action.destination_path)
            if path is not None
        )
    else:
        file_paths = (
            tuple(group.file_path for group in action.file_edits)
            if action.file_edits
            else ((action.file_path,) if action.file_path is not None else ())
        )
    if not file_paths:
        return None
    material_state: list[tuple[str, str | None]] = []
    for file_path in file_paths:
        path = _resolve_worktree_file_path(file_path, repo_root)
        material_state.append(
            (
                str(path),
                path.read_text(encoding="utf-8") if path.exists() else None,
            )
        )
    return tuple(material_state)


def _task_action_failure_reached(
    action_engine: WorkflowLLMActionEngine,
    action: WorkflowAction,
    *,
    stderr: TextIO,
) -> bool:
    if (
        action_engine.record_action_failure(
            action,
            signature=workflow_action_signature,
        )
        != ProgressDecision.THRESHOLD
    ):
        return False
    print(
        "Workflow task stopped after repeated corrective-action failures.",
        file=stderr,
    )
    return True


def _task_system_prompt(*, interaction_style: str | None = None) -> str:
    return (
        _action_system_prompt()
        + "\nDurable workflow-task contract: this is one task that may contain "
        "multiple actions. Use `next_step` to acknowledge an intermediate action "
        "or a completed sub-step and continue the task; use `complete` only when "
        "the task requirements are satisfied, with the declared `output_state`. "
        "Use `invoke_tool`, `invoke_skill`, `edit`, or another action only when "
        "it advances this task.\n" + _interaction_style_prompt(interaction_style)
    )


def _workflow_file_names(workflow_dir: Path) -> list[str]:
    return sorted(
        path.name
        for pattern in ("*.yaml", "*.yml", "*.json")
        for path in workflow_dir.glob(pattern)
        if path.is_file() and not path.name.endswith("-workflow.yaml")
    )


_TASK_PROMPT_EVENT_KEYS = (
    "kind",
    "action_kind",
    "tool",
    "skill",
    "step_id",
    "step_index",
    "feature_id",
    "types",
    "keywords",
    "file_path",
    "operations",
    "error",
    "message",
    "decisions_and_context",
)


def _task_context_prompt_data(
    workflow: WorkflowInstance, task: WorkflowTask
) -> dict[str, Any]:
    """Return only durable context needed to continue the current task.

    The workflow files and complete event log remain the source of truth. This
    projection is deliberately smaller because it is sent on every LLM
    roundtrip and upstream outputs are already the only cross-task values the
    task can consume.
    """
    context = workflow.task_context(task.task_id)
    return _resolve_task_prompt_data(
        {
            "input_state": context.get("input_state", {}),
            "upstream_outputs": context.get("upstream_outputs", {}),
        },
        task.input_state,
    )


def _run_task_deterministic_pre_step(
    task: WorkflowTask,
    *,
    repo_root: Path,
    events: list[dict[str, Any]],
) -> tuple[Any, bool]:
    """Run a task's deterministic context pre-step before asking the LLM.

    Gathered context is a durable handoff, not merely prompt-time context. The
    first workflow task must persist the exact report so downstream tasks do
    not rediscover it or replace it with a lossy summary.
    """
    pre_step = task.pre_step
    if pre_step is None or pre_step.action != "gather_context":
        return None, False
    template = _resolve_pre_step_template(
        pre_step.template,
        _task_prompt_input_values(task.input_state),
    )
    if not isinstance(template, Mapping):
        raise RuntimeError(
            "Deterministic gather_context template must resolve to an object."
        )
    raw_types = template.get("types")
    feature_id = template.get("feature_id")
    if (
        not isinstance(raw_types, Sequence)
        or isinstance(raw_types, (str, bytes, bytearray))
        or not raw_types
        or not isinstance(feature_id, str)
        or not feature_id.strip()
    ):
        raise RuntimeError(
            "Deterministic gather_context pre-step requires types and feature_id."
        )
    keywords = template.get("keywords")
    filters = template.get("filters")
    report = gather_specification_context(
        repo_root,
        types=[str(value) for value in raw_types],
        keywords=(
            [str(value) for value in keywords]
            if isinstance(keywords, Sequence)
            and not isinstance(keywords, (str, bytes, bytearray))
            else None
        ),
        filters=dict(filters) if isinstance(filters, Mapping) else None,
        feature_id=feature_id,
    )
    result = json.loads(render_gather_context_report(report))
    events.append(
        {
            "kind": "deterministic_pre_step",
            "action": pre_step.action,
            "template": template,
            "result": result,
        }
    )
    return {task.output_state_type: result}, True


def _task_event_metadata(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: event[key]
        for key in _TASK_PROMPT_EVENT_KEYS
        if key in event and event[key] not in (None, [], {}, "")
    }


def _task_event_has_failure(event: Mapping[str, Any]) -> bool:
    if event.get("kind") in {
        "action_error",
        "tool_error",
        "llm_response_error",
        "no_progress",
    }:
        return True
    if event.get("error"):
        return True
    result = event.get("result")
    return isinstance(result, Mapping) and result.get("returncode", 0) != 0


def _task_event_result_for_prompt(event: Mapping[str, Any]) -> Any:
    compacted = prune_execution_events(
        [{"kind": event.get("kind"), "result": event.get("result")}],
        include_results=True,
    )
    return compacted[0].get("result") if compacted else None


def _task_events_for_prompt(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a compact event view without discarding durable execution data.

    Action parameters and results can contain whole files or command output.
    Older results are not useful once the next action has succeeded, while the
    most recent result is still important for choosing the next action. Errors
    remain attached to their event so a repair response has its cause.
    """
    recent_events = list(events[-12:])
    metadata = [_task_event_metadata(event) for event in recent_events]
    latest_result_event: dict[str, Any] | None = None
    latest_failure_event: dict[str, Any] | None = None
    latest_failure_result: Any = None
    for event in reversed(events):
        if "result" in event:
            if latest_result_event is None:
                latest_result_event = _task_event_metadata(event)
            if _task_event_has_failure(event) and latest_failure_event is None:
                latest_failure_event = _task_event_metadata(event)
                latest_failure_result = _task_event_result_for_prompt(event)
        elif _task_event_has_failure(event) and latest_failure_event is None:
            latest_failure_event = _task_event_metadata(event)
        if latest_result_event is not None and latest_failure_event is not None:
            break
    prompt_data: dict[str, Any] = {"recent": metadata}
    if latest_result_event is not None:
        prompt_data["latest_result"] = {
            "event": latest_result_event,
            "value": _task_event_result_for_prompt(
                next(event for event in reversed(events) if "result" in event)
            ),
        }
    if latest_failure_event is not None:
        prompt_data["latest_failure"] = {
            "event": latest_failure_event,
            "value": latest_failure_result,
        }
    if len(events) > len(recent_events):
        prompt_data["omitted_event_count"] = len(events) - len(recent_events)
    return prompt_data


def _workflow_file_command_error(
    parameters: dict[str, Any],
    workflow_dir: Path,
) -> str | None:
    command = parameters.get("command")
    if isinstance(command, str):
        command_text = command
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        command_text = " ".join(command)
    else:
        return None

    workflow_path = str(workflow_dir.resolve())
    valid_files = _workflow_file_names(workflow_dir)
    if not valid_files or workflow_path not in command_text:
        return None

    missing_references: list[str] = []
    pattern = re.compile(re.escape(workflow_path) + r"/[^\s'\";&|()]+")
    for match in pattern.finditer(command_text):
        reference = match.group(0).rstrip(",")
        if reference.endswith("."):
            missing_references.append(reference)

    if not missing_references:
        return None
    valid_paths = ", ".join(str(workflow_dir / name) for name in valid_files)
    return (
        "Rejected workflow shell command because it referenced nonexistent "
        f"filename(s): {', '.join(missing_references)}. Use the exact filenames "
        f"from workflow_files: {valid_paths}."
    )


def _repair_workflow_file_command(
    parameters: dict[str, Any],
    workflow_dir: Path,
) -> dict[str, Any] | None:
    command = parameters.get("command")
    valid_files = _workflow_file_names(workflow_dir)
    if not valid_files:
        return None

    def repair_text(text: str) -> str:
        repaired = text
        for filename in valid_files:
            exact_stem = str(workflow_dir.resolve() / Path(filename).stem)
            repaired = re.sub(
                re.escape(exact_stem) + r"\.+(?=[\s'\";&|()]|$)",
                str(workflow_dir.resolve() / filename),
                repaired,
            )
        return repaired

    repaired_command: str | list[str]
    if isinstance(command, str):
        repaired_command = repair_text(command)
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        repaired_command = [repair_text(item) for item in command]
    else:
        return None

    if repaired_command == command:
        return None
    repaired_parameters = dict(parameters)
    repaired_parameters["command"] = repaired_command
    return repaired_parameters


def _run_skill_for_agent(
    skill_name: str,
    *,
    catalog: tuple[SkillCatalogEntry, ...],
    client: WorkflowLLMClient,
    task: WorkflowTask,
    repo_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    max_timeout_retries: int,
    timeout_backoff_seconds: float,
    context: tuple[str, ...] = (),
    clean: bool = False,
) -> dict[str, Any]:
    selected_skill = _find_skill_by_name(catalog, skill_name)
    stack: list[
        tuple[
            SkillCatalogEntry,
            int,
            bool,
            list[dict[str, str]],
            list[dict[str, Any]],
            list[str],
            dict[str, dict[str, Any]],
        ]
    ] = [(selected_skill, 0, False, [], [], [], {})]
    transcript = [] if clean else [{"role": "user", "content": task.description}]
    execution_events: list[dict[str, Any]] = []
    execution_context = list(context)
    handoff_records: dict[str, dict[str, Any]] = {}
    if isinstance(task.input_state, Mapping):
        handoff_records.update(
            {
                str(name): {
                    "name": str(name),
                    "type": "any",
                    "value": value,
                    "produced_by": {"source": "task_context"},
                    "source": "task_context",
                    "scope": "skill",
                }
                for name, value in task.input_state.items()
            }
        )
    if not clean:
        resolved_task_data = _resolve_task_prompt_data(task.to_data(), task.input_state)
        execution_context = [
            (
                "Task input: "
                f"{json.dumps(resolved_task_data['input_state'], ensure_ascii=False)}"
            ),
            resolved_task_data.get("details") or "",
            *execution_context,
        ]
    action_engine = WorkflowLLMActionEngine(max_stalled_roundtrips=3)
    while stack:
        (
            current_skill,
            step_index,
            clean_context,
            parent_transcript,
            parent_events,
            parent_context,
            parent_handoff_records,
        ) = stack[-1]
        if step_index >= len(current_skill.skill.steps):
            stack.pop()
            if clean_context:
                transcript = parent_transcript
                execution_events = parent_events
                execution_context = parent_context
                handoff_records = parent_handoff_records
            continue
        step = current_skill.skill.steps[step_index]
        if step.step_type == "gate":
            if step.gate is None:
                raise RuntimeError("gate steps require gate settings.")
            passed = _run_gate(
                step,
                skill_name=current_skill.skill.name,
                worktree_root=repo_root,
                execution_events=execution_events,
                execution_context=execution_context,
                handoff_records=handoff_records,
                step_index=step_index,
                workflow_context=None,
                stdout=stdout,
                stderr=stderr,
                verbose=False,
            )
            target_index = step_index + 1
            if not passed:
                target_index = _step_index_by_id(current_skill, step.gate.goto_step)
                _invalidate_deterministic_pre_step(
                    execution_events,
                    skill_name=current_skill.skill.name,
                    step_index=target_index,
                )
                execution_events.append(
                    {
                        "kind": "goto_step",
                        "skill": current_skill.skill.name,
                        "step_id": step.gate.goto_step,
                        "target_step_index": target_index,
                        "source": "gate",
                    }
                )
            stack[-1] = (
                current_skill,
                target_index,
                clean_context,
                parent_transcript,
                parent_events,
                parent_context,
                handoff_records,
            )
            continue
        if step.step_type == "invoke_tool" and step.pre_step is not None:
            _run_deterministic_pre_step(
                step,
                skill_name=current_skill.skill.name,
                worktree_root=repo_root,
                execution_events=execution_events,
                execution_context=execution_context,
                handoff_records=handoff_records,
                step_index=step_index,
                workflow_context=None,
                stdout=stdout,
                stderr=stderr,
                verbose=False,
            )
        messages = _build_step_execution_messages(
            selected_skill=current_skill,
            current_step=step,
            current_step_index=step_index,
            transcript=transcript,
            execution_events=execution_events,
            execution_context=execution_context,
            handoff_records=handoff_records,
            current_file_path=None,
            worktree_root=repo_root,
            catalog=catalog,
        )
        action = action_engine.request_action(
            client=client,
            messages=messages,
            parser=_parse_action_response,
            model="nested-skill",
            stderr=stderr,
            max_timeout_retries=max_timeout_retries,
            timeout_backoff_seconds=timeout_backoff_seconds,
        )
        _validate_workflow_action_outputs(action, step)
        observation = action_engine.observe_action(
            action,
            signature=workflow_action_signature,
            before_state=None,
            after_state=None,
        )
        if observation.decision == ProgressDecision.THRESHOLD:
            execution_events.append(
                {
                    "kind": "no_progress",
                    "action_kind": action.kind,
                    "message": "Repeated nested workflow action made no progress.",
                }
            )
            execution_context.append(
                "The previous nested workflow action made no progress; choose "
                "a different action or next_step."
            )
            action_engine.reset_progress()
            continue
        if action.kind == "complete":
            _validate_workflow_action_outputs(action, step)
            _record_task_action_outputs(action, handoff_records, step, step_index)
            stack.pop()
            if clean_context:
                transcript = parent_transcript
                execution_events = parent_events
                execution_context = parent_context
                handoff_records = parent_handoff_records
            execution_events.append(
                {"kind": action.kind, "skill": current_skill.skill.name}
            )
            continue
        if action.kind == "next_step":
            _validate_workflow_action_outputs(action, step)
            next_step = (
                current_skill.skill.steps[step_index + 1]
                if step_index + 1 < len(current_skill.skill.steps)
                else None
            )
            _validate_workflow_handoff(
                step,
                next_step,
                handoff_records,
                current_step_index=step_index,
            )
            stack[-1] = (
                current_skill,
                step_index + 1,
                clean_context,
                parent_transcript,
                parent_events,
                parent_context,
                handoff_records,
            )
            execution_events.append(
                {"kind": action.kind, "skill": current_skill.skill.name}
            )
            continue
        if action.kind == "goto_step":
            target_index = _step_index_by_id(current_skill, action.step_id)
            stack[-1] = (
                current_skill,
                target_index,
                clean_context,
                parent_transcript,
                parent_events,
                parent_context,
                handoff_records,
            )
            if action.decisions_and_context:
                execution_context.append(action.decisions_and_context)
            execution_events.append(
                {
                    "kind": action.kind,
                    "skill": current_skill.skill.name,
                    "step_id": action.step_id,
                    "target_step_index": target_index,
                    "decisions_and_context": action.decisions_and_context,
                }
            )
            continue
        if action.kind == "invoke_skill":
            if action.skill_name is None:
                raise RuntimeError("invoke_skill action must include a skill name.")
            nested_skill = _find_skill_by_name(catalog, action.skill_name)
            if any(entry.path == nested_skill.path for entry, *_ in stack):
                raise RuntimeError(
                    f"Recursive skill invocation is not allowed: {action.skill_name!r}."
                )
            nested_context = list(action.context)
            if action.decisions_and_context is not None:
                nested_context.append(action.decisions_and_context)
            stack[-1] = (
                current_skill,
                step_index,
                clean_context,
                parent_transcript,
                parent_events,
                parent_context,
                handoff_records,
            )
            stack.append(
                (
                    nested_skill,
                    0,
                    action.clean,
                    list(transcript),
                    list(execution_events),
                    list(execution_context),
                    dict(handoff_records) if action.clean else handoff_records,
                )
            )
            execution_events.append({"kind": action.kind, "skill": action.skill_name})
            if action.clean:
                transcript = []
                execution_events = []
                execution_context = nested_context
                handoff_records = {}
            else:
                execution_context.extend(nested_context)
            continue
        if action.kind == "gather_context":
            report = gather_specification_context(
                repo_root,
                types=list(action.types),
                feature_id=action.feature_id,
                keywords=list(action.keywords),
            )
            execution_events.append(
                {
                    "kind": action.kind,
                    "result": json.loads(render_gather_context_report(report)),
                }
            )
            _record_task_action_outputs(action, handoff_records, step, step_index)
            continue
        if action.kind == "read_document":
            execution_events.append(
                {
                    "kind": action.kind,
                    "result": _read_task_document(action, repo_root),
                }
            )
            _record_task_action_outputs(action, handoff_records, step, step_index)
            continue
        if action.kind == "edit":
            execution_events.append(
                {
                    "kind": action.kind,
                    "result": _apply_task_edits(action, repo_root),
                }
            )
            _record_task_action_outputs(action, handoff_records, step, step_index)
            continue
        if action.kind == "invoke_tool":
            if action.tool in {"shell", "internal"}:
                if action.tool == "internal":
                    _validate_internal_command(action.parameters.get("command"))
                result = _execute_shell_tool(
                    action.parameters,
                    worktree_root=repo_root,
                    stdout=stdout,
                    stderr=stderr,
                    verbose=False,
                )
            elif action.tool == "fuzzy-match":
                result = _execute_fuzzy_match_tool(
                    action.parameters,
                    worktree_root=repo_root,
                )
            elif is_basedpyright_tool(action.tool or ""):
                result = execute_basedpyright_tool(
                    action.tool or "",
                    action.parameters,
                    worktree_root=repo_root,
                )
            else:
                raise RuntimeError(f"Unsupported nested skill tool: {action.tool!r}")
            _record_skill_pull_request(
                action,
                repo_root,
                current_skill,
                execution_events,
                result,
                step_index=step_index,
            )
            execution_events.append(
                {
                    "kind": action.kind,
                    "tool": action.tool,
                    "parameters": action.parameters,
                    "result": result,
                    "decisions_and_context": action.decisions_and_context,
                    "step_index": step_index,
                }
            )
            _record_task_action_outputs(action, handoff_records, step, step_index)
            continue
        if action.kind == "prompt_user":
            raise RuntimeError("Nested skills in agent workflows cannot prompt users.")
        raise RuntimeError(f"Unsupported nested skill action: {action.kind!r}")
    return {"skill": skill_name, "events": execution_events}


def _record_task_action_outputs(
    action: WorkflowAction,
    records: dict[str, dict[str, Any]],
    step: Any,
    step_index: int,
) -> None:
    if not action.outputs:
        return
    declarations = {output.name: output for output in step.outputs}
    for name, value in action.outputs.items():
        declaration = declarations.get(name)
        records[name] = {
            "name": name,
            "type": declaration.type if declaration is not None else "any",
            "value": value,
            "produced_by": {"step_index": step_index, "action": action.kind},
            "scope": declaration.scope if declaration is not None else "skill",
        }


def _read_task_document(
    action: WorkflowAction,
    repo_root: Path,
) -> dict[str, Any]:
    if action.file_path is None or action.start_line is None or action.end_line is None:
        raise RuntimeError("read_document action must include a file and line range.")
    path = _resolve_worktree_file_path(action.file_path, repo_root)
    lines = path.read_text(encoding="utf-8").splitlines()
    if action.start_line < 1 or action.end_line < action.start_line:
        raise RuntimeError(
            "read_document action line range "
            f"{action.start_line}-{action.end_line} is invalid. "
            f"Request a range from 1 through {len(lines)}."
        )
    if action.end_line > len(lines):
        raise RuntimeError(
            "read_document action line range "
            f"{action.start_line}-{action.end_line} is outside the document "
            f"with {len(lines)} lines. Request a range from 1 through "
            f"{len(lines)}."
        )
    return {
        "path": action.file_path,
        "start_line": action.start_line,
        "end_line": action.end_line,
        "lines": [
            {
                "line_number": number,
                "text": lines[number - 1],
            }
            for number in range(action.start_line, action.end_line + 1)
        ],
    }


def _apply_task_edits(
    action: WorkflowAction,
    repo_root: Path,
) -> list[dict[str, Any]]:
    if action.file_edits:
        edit_groups = [(group.file_path, group.edits) for group in action.file_edits]
    else:
        if action.file_path is None:
            raise RuntimeError("edit action must include a file path.")
        edit_groups = [(action.file_path, action.edits)]
    results: list[dict[str, Any]] = []
    for file_path, edits in edit_groups:
        path = _resolve_worktree_file_path(file_path, repo_root)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = _apply_file_edits(current, edits)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
        results.append(
            {"file_path": file_path, "line_count": len(updated.splitlines())}
        )
    return results


def _prompt_user_handoff(
    action: WorkflowAction,
    task: WorkflowTask,
) -> dict[str, Any]:
    question = action.text or "Please review the current workflow task."
    return {
        "human_task": {
            "description": question,
            "role": "reviewer",
            "input_state": {
                "question": question,
                "task": task.to_data(),
            },
            "output_state_type": "human-response-state",
        },
        "incorporation_instructions": (
            action.decisions_and_context
            or "Use the human response to continue the workflow task."
        ),
        "follow_up_task": {
            "description": task.description,
            "role": task.assignee_role.value,
            "input_state": task.input_state,
            "output_state_type": task.output_state_type,
        },
    }


def _insert_human_handoff(
    workflow: WorkflowInstance,
    blocked_task: WorkflowTask,
    human_input: dict[str, Any],
) -> tuple[WorkflowTask, WorkflowTask | None]:
    human_spec = human_input["human_task"]
    human_id = _next_handoff_id(workflow)
    human_task = WorkflowTask(
        task_id=human_id,
        status=TaskStatus.OPEN,
        description=human_spec["description"],
        complexity=TaskComplexity.LOW,
        input_state=human_spec["input_state"],
        assignee_type=AssigneeType.HUMAN,
        assignee_role=HumanRole(human_spec["role"]),
        output_state_type=human_spec["output_state_type"],
        upstream_task_ids=blocked_task.upstream_task_ids,
    )
    workflow.add_task(human_task)
    follow_up_spec = human_input.get("follow_up_task")
    if follow_up_spec is None:
        return human_task, None
    follow_up_task = WorkflowTask(
        task_id=f"{human_id}-follow-up",
        status=TaskStatus.OPEN,
        description=follow_up_spec["description"],
        complexity=TaskComplexity.MEDIUM,
        input_state=follow_up_spec["input_state"],
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole(follow_up_spec["role"]),
        details=human_input["incorporation_instructions"],
        output_state_type=follow_up_spec["output_state_type"],
        upstream_task_ids=(human_id,),
    )
    workflow.add_task(follow_up_task)
    return human_task, follow_up_task


def _next_handoff_id(workflow: WorkflowInstance) -> str:
    existing_ids = {task.task_id for task in workflow.tasks}
    index = 1
    while f"human-input-{index}" in existing_ids:
        index += 1
    return f"human-input-{index}"


def _build_workflow_client(
    config: WorkflowTaskAgentConfig,
    task: WorkflowTask,
    *,
    progress_stream: TextIO | None = None,
) -> WorkflowLLMClient:
    provider = resolve_workflow_provider(config.provider)
    mapping = _resolve_workflow_task_mapping(
        task.llm_type,
        mappings=tuple(_default_llm_mappings(provider).items()),
        provider=provider,
    )
    if mapping is None:
        raise RuntimeError(f"Workflow task has no llm_type mapping: {task.task_id}")
    return _build_workflow_client_for_mapping(
        config,
        task,
        mapping,
        progress_stream=progress_stream,
    )


def _resolve_workflow_task_mapping(
    llm_type: str | None,
    *,
    mappings: tuple[tuple[str, LLMModelMapping], ...],
    provider: str,
) -> LLMModelMapping | None:
    """Resolve task mappings, using workflow-chat's model for generic providers."""
    if llm_type is None:
        llm_type = _DEFAULT_LLM_TYPE
    if not mappings:
        return LLMModelMapping(_DEFAULT_MODEL, provider=provider)
    return _resolve_llm_mapping(
        llm_type,
        mappings=mappings,
        provider=provider,
    )


def _build_workflow_client_for_mapping(
    config: WorkflowTaskAgentConfig,
    task: WorkflowTask,
    mapping: Any,
    *,
    progress_stream: TextIO | None = None,
) -> WorkflowLLMClient:
    from powdrr_lift.workflow_chat_agent import OpenAIChatClient

    model = mapping.model
    if mapping.provider == "local":
        return LocalLlamaChatClient(
            model_path=_resolve_local_model_path(
                config.repo_root / ".powdrr" / "models"
            )
        )
    credentials = _resolve_credentials(
        mapping.provider,
        config.api_key,
        config.base_url,
    )
    return OpenAIChatClient(
        model=model,
        api_key=credentials.api_key,
        base_url=credentials.base_url,
        limits=_model_limits_for(mapping.provider, model),
        progress_stream=progress_stream,
    )
