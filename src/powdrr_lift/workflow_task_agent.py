from __future__ import annotations

import json
import os
import re
import subprocess
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
from powdrr_lift.workflow_chat_agent import (
    _DEFAULT_MODEL,
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
    _LLMExchangeRecordingClient,
    _load_skill_catalog,
    _long_context_backup_for,
    _model_limits_for,
    _parse_action_response,
    _print_waiting_for_model,
    _reject_line_edit_for_yaml,
    _resolve_credentials,
    _resolve_llm_mapping,
    _resolve_local_model_path,
    _resolve_project_root,
    _resolve_worktree_context,
    _resolve_worktree_file_path,
    _step_index_by_id,
    _validate_internal_command,
    resolve_workflow_provider,
)
from powdrr_lift.workflow_git import (
    WorkflowGitInconsistency,
    WorkflowGitState,
    claim_workflow_task,
    create_task_worktree,
    load_workflow_git_state,
    task_branch_name,
    validate_workflow_git_state,
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
    prune_execution_events,
    workflow_action_signature,
    workflow_action_summary,
)


@dataclass(frozen=True, slots=True)
class WorkflowTaskAgentConfig:
    workflow_dir: Path
    repo_root: Path = Path(".")
    provider: str = "auto"
    task_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_roundtrips: int | None = None
    max_stalled_roundtrips: int = 3
    max_timeout_retries: int = 8
    timeout_backoff_seconds: float = 10.0
    verbose: bool = False


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
    client: WorkflowLLMClient
    compaction_client: WorkflowLLMClient
    model: str
    mapping_provider: str
    stdout: TextIO
    stderr: TextIO
    action_engine: WorkflowLLMActionEngine
    events: list[dict[str, Any]]
    response_correction: str | None = None
    compacted_context: dict[str, Any] | None = None

    def next_request(self) -> WorkflowActionRequest:
        while True:
            messages = _build_task_messages(
                self.workflow,
                self.task,
                self.events,
                skill_catalog=self.skill_catalog,
                response_correction=self.response_correction,
                compacted_context=self.compacted_context,
            )
            limits = _model_limits_for(self.mapping_provider, self.model)
            estimated_input_tokens = _estimate_message_tokens(messages)
            print(
                f"Workflow task context: {estimated_input_tokens} estimated input "
                f"tokens of {limits.context_window} allowed.",
                file=self.stderr,
                flush=True,
            )
            if estimated_input_tokens + 1024 < limits.context_window:
                _print_waiting_for_model(self.stderr, self.model)
                return WorkflowActionRequest(
                    client=self.client,
                    messages=messages,
                    parser=_parse_task_action,
                    model=self.model,
                    stderr=self.stderr,
                    max_timeout_retries=self.config.max_timeout_retries,
                    timeout_backoff_seconds=self.config.timeout_backoff_seconds,
                )

            print(
                "Compacting workflow task context before the next LLM call: "
                f"{estimated_input_tokens} estimated input tokens would exceed "
                f"the {limits.context_window}-token context window.",
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

    def report_roundtrip(self, roundtrip: int, action: WorkflowAction) -> None:
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
        if not _is_repairable_task_response_error(error):
            raise error
        response_details = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if payload is not None
            else f"<no parsed response; client error: {error}>"
        )
        print(
            f"Workflow task LLM response requiring repair:\n{response_details}",
            file=self.stderr,
            flush=True,
        )
        self.response_correction = (
            "The previous response was invalid: "
            f"{error} Return exactly one complete JSON object matching one of "
            "the documented action shapes. Do not return markdown, prose, "
            "or an empty response."
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
            self.events.append(
                {
                    "kind": action.kind,
                    "decisions_and_context": action.decisions_and_context,
                }
            )
            return WorkflowActionOutcome()
        if action.kind == "read_document":
            self.events.append(
                {
                    "kind": action.kind,
                    "result": _read_task_document(action, self.repo_root),
                }
            )
            return WorkflowActionOutcome()
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
            completed = self.workflow.complete_task(
                self.task.task_id,
                action.output_state,
            )
            if action.text:
                print(action.text, file=self.stdout)
            print(f"Completed workflow task: {completed.task_id}", file=self.stdout)
            _publish_workflow_progress(
                self.repo_root,
                self.workflow,
                reason=f"complete {completed.task_id}",
                stdout=self.stdout,
            )
            return WorkflowActionOutcome(continue_running=False)
        if action.kind == "get-human-input":
            return self._handoff(action.human_input or {}, "human input required by")
        raise RuntimeError(f"Unsupported workflow task action: {action.kind}")

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
            reason=f"{reason_prefix} {self.task.task_id}",
            stdout=self.stdout,
        )
        return WorkflowActionOutcome(continue_running=False)

    def _execute_tool(self, action: WorkflowAction) -> None:
        repaired_parameters = _repair_workflow_file_command(
            action.parameters,
            self.workflow.directory,
        )
        if repaired_parameters is not None:
            print(
                "Corrected malformed workflow filename suffix to the exact .json "
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
                "are shell, internal, fuzzy-match, basedpyright-symbol, and "
                "basedpyright-structure."
            )
        self.events.append(
            {
                "kind": action.kind,
                "tool": action.tool,
                "parameters": action.parameters,
                "result": result,
            }
        )

    def record_action_error(self, action: WorkflowAction, error: Exception) -> None:
        self.response_correction = _action_response_correction(action, error)
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
            reason=f"roundtrip limit for {self.task.task_id}",
            stdout=self.stdout,
        )
        return 2


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
        configured_workflow = WorkflowInstance.from_directory(configured_workflow_dir)
        configured_task = _select_task(configured_workflow, config.task_id)
        configured_git_state = load_workflow_git_state(configured_workflow_dir)
        if configured_git_state is not None and configured_task is not None:
            project_root = _resolve_project_root(
                configured_repo_root,
                configured_repo_root,
            )
            validate_workflow_git_state(
                project_root,
                configured_git_state,
                configured_task.task_id,
            )
            claim_workflow_task(
                project_root,
                configured_git_state,
                configured_task.task_id,
            )
        repo_root, workflow_dir = _resolve_workflow_task_context(
            config,
            configured_repo_root=configured_repo_root,
            task_id=configured_task.task_id if configured_task is not None else None,
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
    workflow = WorkflowInstance.from_directory(workflow_dir)
    task = _select_task(workflow, config.task_id)
    if task is None:
        workflow_git_state = load_workflow_git_state(workflow_dir)
        if workflow_git_state is not None and workflow.tasks:
            if all(item.status is TaskStatus.COMPLETED for item in workflow.tasks):
                _open_final_workflow_pull_request(
                    repo_root,
                    workflow,
                    workflow_git_state,
                    stdout=stdout,
                )
                return 0
        print("No ready agent task found.", file=stderr)
        return 1
    task = workflow.claim_task(task.task_id)
    print(f"Claimed workflow task: {task.task_id}", file=stdout)
    print("Publishing claimed task state to GitHub...", file=stdout, flush=True)
    _publish_workflow_progress(
        repo_root,
        workflow,
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
    client_was_provided = client is not None
    if client is None:
        client = _build_workflow_client(config, task)
    dump_root = _resolve_project_root(
        configured_repo_root,
        repo_root,
    )
    if config.verbose:
        client = _WorkflowTaskDisplayClient(client, stderr=stderr)
    client = _LLMExchangeRecordingClient(client, dump_root)
    compaction_client = client
    long_context_backup = _long_context_backup_for(
        model,
        mappings,
    )
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
        compaction_client = _LLMExchangeRecordingClient(
            backup_client,
            dump_root,
        )

    driver_events: list[dict[str, Any]] = []
    driver = WorkflowLLMExecutionDriver(
        max_stalled_roundtrips=config.max_stalled_roundtrips
    )
    strategy = _TaskWorkflowExecutionStrategy(
        config=config,
        workflow=workflow,
        task=task,
        skill_catalog=skill_catalog,
        repo_root=repo_root,
        client=client,
        compaction_client=compaction_client,
        model=model,
        mapping_provider=mapping.provider,
        stdout=stdout,
        stderr=stderr,
        action_engine=driver.action_engine,
        events=driver_events,
    )
    try:
        return driver.run(
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
    task_id: str | None,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[Path, Path]:
    """Use the same dedicated worktree isolation as workflow chat."""
    configured_workflow_dir = config.workflow_dir.resolve()
    workflow_git_state = load_workflow_git_state(configured_workflow_dir)
    if workflow_git_state is not None and task_id is not None:
        current_branch = _git_output(
            configured_repo_root,
            ["branch", "--show-current"],
        )
        expected_task_branch = task_branch_name(
            workflow_git_state.proposed_pr_id,
            task_id,
        )
        if current_branch != expected_task_branch:
            project_root = _resolve_project_root(
                configured_repo_root,
                configured_repo_root,
            )
            try:
                task_worktree, task_branch = create_task_worktree(
                    project_root,
                    workflow_git_state,
                    task_id,
                )
            except RuntimeError as exc:
                raise WorkflowGitInconsistency(
                    json.dumps(
                        {
                            "proposed_pr_id": workflow_git_state.proposed_pr_id,
                            "task_id": task_id,
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
                f"Using workflow task branch {task_branch} in {task_worktree}",
                file=stdout,
                flush=True,
            )
            return (
                task_worktree,
                task_worktree / workflow_git_state.workflow_relative_directory,
            )

    if not _is_git_worktree(configured_repo_root):
        return configured_repo_root, configured_workflow_dir

    worktree_root = _resolve_worktree_context(
        configured_repo_root,
        stderr=stderr,
        verbose=config.verbose,
    )
    workflow_dir = configured_workflow_dir
    try:
        workflow_relative_path = workflow_dir.relative_to(configured_repo_root)
    except ValueError as exc:
        raise RuntimeError(
            "Workflow directory must be inside the configured repository root "
            "when workflow task execution creates a dedicated worktree."
        ) from exc
    relocated_workflow_dir = worktree_root / workflow_relative_path
    print(f"Using workflow task worktree: {worktree_root}", file=stdout, flush=True)
    return worktree_root, relocated_workflow_dir


def _publish_workflow_progress(
    repo_root: Path,
    workflow: WorkflowInstance,
    *,
    reason: str,
    stdout: TextIO,
    open_pull_request: bool = True,
) -> None:
    """Commit and publish durable workflow progress for execution tasks.

    Unit tests and callers operating outside a git checkout can still use the
    execution loop; in that case task JSON is durable locally and publishing is
    skipped. A real workflow execution always runs from a git worktree and
    creates or updates one draft PR for the branch.
    """
    if not _is_git_worktree(repo_root):
        return

    branch = _git_output(repo_root, ["branch", "--show-current"])
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
    if not status.stdout.strip():
        return

    _run_git(
        repo_root,
        ["commit", "-m", f"Persist workflow progress: {reason}"],
    )
    _run_git(repo_root, ["push", "--set-upstream", "origin", branch])
    if not open_pull_request:
        print(f"Published workflow task branch: {branch}", file=stdout)
        return

    workflow_git_state = load_workflow_git_state(workflow.directory)
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
    print(f"Created workflow progress PR: {created_pr.stdout.strip()}", file=stdout)


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
                f"{workflow.directory.name}. All task branches have been merged."
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
            f"Deleting dedicated workflow worktree after timeout: {repo_root}",
            file=stderr,
            flush=True,
        )
        _delete_workflow_task_worktree(repo_root, stderr=stderr)
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
    response_correction: str | None = None,
    compacted_context: dict[str, Any] | None = None,
    skill_catalog: tuple[SkillCatalogEntry, ...] = (),
) -> list[dict[str, str]]:
    context_data: dict[str, Any]
    if compacted_context is None:
        context_data = {
            "task_context": workflow.task_context(task.task_id),
            "events": prune_execution_events(events, include_results=True),
        }
    else:
        context_data = {"compacted_context": compacted_context}
    return [
        {"role": "system", "content": _task_system_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "execution_mode": "process_workflow_task",
                    "task": task.to_data(),
                    **context_data,
                    "step_context": (
                        [response_correction] if response_correction is not None else []
                    ),
                    "workflow_dir": str(workflow.directory),
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
                    "available_skills": [
                        {
                            "name": entry.skill.name,
                            "path": str(entry.path),
                            "when_to_use": list(entry.skill.when_to_use),
                        }
                        for entry in skill_catalog
                    ],
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
                    "task": task.to_data(),
                    "task_description": task.description,
                    "task_details": task.details,
                    "current_context": {
                        "task_context": workflow.task_context(task.task_id),
                        "events": events,
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
    if action.kind != "edit":
        return None
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


def _task_system_prompt() -> str:
    return _action_system_prompt()


def _workflow_file_names(workflow_dir: Path) -> list[str]:
    return sorted(path.name for path in workflow_dir.glob("*.json") if path.is_file())


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
        f"filename(s): {', '.join(missing_references)}. Use the exact .json "
        f"filenames from workflow_files: {valid_paths}."
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
            exact_stem = str(workflow_dir.resolve() / filename.removesuffix(".json"))
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


def _parse_task_action(payload: dict[str, Any]) -> WorkflowAction:
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise RuntimeError("Workflow task action must include kind.")
    normalized_kind = kind.strip()
    if normalized_kind == "complete" and "output_state" not in payload:
        raise RuntimeError("Complete action must include output_state.")
    if normalized_kind == "get-human-input":
        return WorkflowAction(
            kind=normalized_kind,
            human_input=_parse_human_input(payload.get("human_input")),
        )
    normalized_payload = dict(payload)
    if normalized_kind == "invoke_tool" and "tool" not in normalized_payload:
        normalized_payload["tool"] = "shell"
    return _parse_action_response(normalized_payload)


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
    stack: list[tuple[SkillCatalogEntry, int, bool, list[str]]] = [
        (selected_skill, 0, False, [])
    ]
    transcript = [] if clean else [{"role": "user", "content": task.description}]
    execution_events: list[dict[str, Any]] = []
    execution_context = list(context)
    if not clean:
        execution_context = [
            f"Task input: {json.dumps(task.input_state, ensure_ascii=False)}",
            task.details or "",
            *execution_context,
        ]
    action_engine = WorkflowLLMActionEngine(max_stalled_roundtrips=3)
    while stack:
        current_skill, step_index, clean_context, parent_context = stack[-1]
        if step_index >= len(current_skill.skill.steps):
            stack.pop()
            if clean_context:
                execution_context = parent_context
            continue
        step = current_skill.skill.steps[step_index]
        messages = _build_step_execution_messages(
            selected_skill=current_skill,
            current_step=step,
            current_step_index=step_index,
            transcript=transcript,
            execution_events=execution_events,
            execution_context=execution_context,
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
            stack.pop()
            if clean_context:
                execution_context = parent_context
            execution_events.append(
                {"kind": action.kind, "skill": current_skill.skill.name}
            )
            continue
        if action.kind == "next_step":
            stack[-1] = (
                current_skill,
                step_index + 1,
                clean_context,
                parent_context,
            )
            execution_events.append(
                {"kind": action.kind, "skill": current_skill.skill.name}
            )
            continue
        if action.kind == "goto_step":
            target_index = _step_index_by_id(current_skill, action.step_id)
            stack[-1] = (current_skill, target_index, clean_context, parent_context)
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
            if any(entry.path == nested_skill.path for entry, _, _, _ in stack):
                raise RuntimeError(
                    f"Recursive skill invocation is not allowed: {action.skill_name!r}."
                )
            nested_context = list(action.context)
            if action.decisions_and_context is not None:
                nested_context.append(action.decisions_and_context)
            stack[-1] = (current_skill, step_index, clean_context, parent_context)
            stack.append(
                (
                    nested_skill,
                    0,
                    action.clean,
                    list(execution_context),
                )
            )
            if action.clean:
                execution_context = nested_context
            else:
                execution_context.extend(nested_context)
            execution_events.append({"kind": action.kind, "skill": action.skill_name})
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
            continue
        if action.kind == "read_document":
            execution_events.append(
                {
                    "kind": action.kind,
                    "result": _read_task_document(action, repo_root),
                }
            )
            continue
        if action.kind == "edit":
            execution_events.append(
                {
                    "kind": action.kind,
                    "result": _apply_task_edits(action, repo_root),
                }
            )
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
            execution_events.append({"kind": action.kind, "result": result})
            continue
        if action.kind == "prompt_user":
            raise RuntimeError("Nested skills in agent workflows cannot prompt users.")
        raise RuntimeError(f"Unsupported nested skill action: {action.kind!r}")
    return {"skill": skill_name, "events": execution_events}


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
        _reject_line_edit_for_yaml(file_path)
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


def _parse_human_input(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("get-human-input must include human_input.")
    human_task = _parse_handoff_task(value.get("human_task"), HumanRole, "human_task")
    instructions = value.get("incorporation_instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise RuntimeError("get-human-input must include incorporation_instructions.")
    follow_up = value.get("follow_up_task")
    return {
        "human_task": human_task,
        "incorporation_instructions": instructions.strip(),
        "follow_up_task": (
            _parse_handoff_task(follow_up, AgentRole, "follow_up_task")
            if follow_up is not None
            else None
        ),
    }


def _parse_handoff_task(
    value: object,
    role_type: type[HumanRole] | type[AgentRole],
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{field_name} must be an object.")
    description = value.get("description")
    role = value.get("role")
    output_state_type = value.get("output_state_type")
    if not isinstance(description, str) or not description.strip():
        raise RuntimeError(f"{field_name}.description must be non-empty.")
    if not isinstance(role, str):
        raise RuntimeError(f"{field_name}.role must be provided.")
    try:
        normalized_role = role_type(role).value
    except ValueError as exc:
        raise RuntimeError(f"Invalid {field_name}.role: {role}") from exc
    if not isinstance(output_state_type, str) or not output_state_type.strip():
        raise RuntimeError(f"{field_name}.output_state_type must be non-empty.")
    if "input_state" not in value:
        raise RuntimeError(f"{field_name}.input_state must be provided.")
    return {
        "description": description.strip(),
        "role": normalized_role,
        "input_state": value["input_state"],
        "output_state_type": output_state_type.strip(),
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
        return None
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
