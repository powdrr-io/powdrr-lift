from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

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
    ZAI_LLM_MAPPINGS,
    LocalLlamaChatClient,
    SkillCatalogEntry,
    SkillChatAction,
    _apply_file_edits,
    _build_step_execution_messages,
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
    _resolve_credentials,
    _resolve_llm_mapping,
    _resolve_local_model_path,
    _resolve_project_root,
    _resolve_worktree_context,
    _resolve_worktree_file_path,
)


class WorkflowTaskChatClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkflowTaskAgentConfig:
    workflow_dir: Path
    repo_root: Path = Path(".")
    task_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_roundtrips: int = 12
    max_timeout_retries: int = 3
    timeout_backoff_seconds: float = 2.0
    verbose: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowTaskAction:
    kind: str
    tool: str = "shell"
    skill_name: str | None = None
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    edits: tuple[Any, ...] = ()
    file_edits: tuple[Any, ...] = ()
    types: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    filters: dict[str, object] = field(default_factory=dict)
    output_state: Any = None
    text: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    human_input: dict[str, Any] | None = None
    decisions_and_context: str | None = None
    provider_role: Literal["normal", "adversarial"] | None = None
    clean: bool = False
    context: tuple[str, ...] = ()


def run_workflow_task(
    config: WorkflowTaskAgentConfig,
    *,
    client: WorkflowTaskChatClient | None = None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    configured_repo_root = resolve_repo_root(config.repo_root)
    repo_root, workflow_dir = _resolve_workflow_task_context(
        config,
        configured_repo_root=configured_repo_root,
        stdout=stdout,
        stderr=stderr,
    )
    skill_catalog = _load_skill_catalog(
        repo_root / "skill-definitions",
        stderr=stderr,
    )
    workflow = WorkflowInstance.from_directory(workflow_dir)
    task = _select_task(workflow, config.task_id)
    if task is None:
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
    )
    mapping = _resolve_llm_mapping(
        task.llm_type,
        mappings=tuple(ZAI_LLM_MAPPINGS.items()),
        provider="zai",
    )
    if mapping is None:
        raise RuntimeError(f"Workflow task has no LLM mapping: {task.task_id}")
    model = mapping.model
    client_was_provided = client is not None
    if client is None:
        client = _build_zai_client(config, task, progress_stream=stderr)
    dump_root = _resolve_project_root(
        configured_repo_root,
        repo_root,
    )
    client = _LLMExchangeRecordingClient(client, dump_root)
    compaction_client = client
    long_context_backup = _long_context_backup_for(
        model,
        tuple(ZAI_LLM_MAPPINGS.items()),
    )
    if not client_was_provided and long_context_backup is not None:
        compaction_client = _LLMExchangeRecordingClient(
            _build_zai_client_for_mapping(
                config,
                task,
                long_context_backup,
                progress_stream=stderr,
            ),
            dump_root,
        )

    events: list[dict[str, Any]] = []
    response_correction: str | None = None
    compacted_context: dict[str, Any] | None = None
    for _roundtrip in range(max(1, config.max_roundtrips)):
        messages = _build_task_messages(
            workflow,
            task,
            events,
            skill_catalog=skill_catalog,
            response_correction=response_correction,
            compacted_context=compacted_context,
        )
        limits = _model_limits_for(mapping.provider, model)
        estimated_input_tokens = _estimate_message_tokens(messages)
        print(
            f"Workflow task context: {estimated_input_tokens} estimated input "
            f"tokens of {limits.context_window} allowed.",
            file=stderr,
            flush=True,
        )
        if estimated_input_tokens + 1024 >= limits.context_window:
            print(
                "Compacting workflow task context before the next LLM call: "
                f"{estimated_input_tokens} estimated input tokens would exceed "
                f"the {limits.context_window}-token context window.",
                file=stderr,
                flush=True,
            )
            try:
                compacted_context, before_tokens, after_tokens = (
                    _compact_workflow_task_context(
                        workflow,
                        task,
                        events,
                        client=compaction_client,
                        stderr=stderr,
                        max_timeout_retries=config.max_timeout_retries,
                        timeout_backoff_seconds=config.timeout_backoff_seconds,
                    )
                )
            except _WorkflowTaskTimeoutExhausted as exc:
                return _handle_exhausted_timeout(
                    repo_root,
                    task,
                    stdout=stdout,
                    stderr=stderr,
                    error=exc,
                )
            events.append(
                {
                    "kind": "context_compaction",
                    "before_estimated_input_tokens": before_tokens,
                    "after_estimated_input_tokens": after_tokens,
                }
            )
            print(
                "Compacted workflow task context: "
                f"{before_tokens} -> {after_tokens} estimated input tokens.",
                file=stderr,
                flush=True,
            )
            response_correction = None
            continue

        _print_waiting_for_model(stderr, model)
        response: dict[str, Any] | None = None
        try:
            response = _complete_task_json_with_timeout_retry(
                client,
                messages,
                model=model,
                stderr=stderr,
                max_timeout_retries=config.max_timeout_retries,
                timeout_backoff_seconds=config.timeout_backoff_seconds,
            )
            action = _parse_task_action(response)
        except _WorkflowTaskTimeoutExhausted as exc:
            return _handle_exhausted_timeout(
                repo_root,
                task,
                stdout=stdout,
                stderr=stderr,
                error=exc,
            )
        except RuntimeError as exc:
            if not _is_repairable_task_response_error(exc):
                raise
            if response is not None:
                response_details = json.dumps(
                    response,
                    indent=2,
                    ensure_ascii=False,
                )
            else:
                response_details = f"<no parsed response; client error: {exc}>"
            print(
                f"Workflow task LLM response requiring repair:\n{response_details}",
                file=stderr,
                flush=True,
            )
            response_correction = (
                "The previous response was invalid: "
                f"{exc} Return exactly one complete JSON object matching one of "
                "the documented action shapes. Do not return markdown, prose, "
                "or an empty response."
            )
            print(
                "Workflow task response needs repair; requesting a corrected "
                "JSON response from the LLM.",
                file=stderr,
            )
            events.append(
                {
                    "kind": "llm_response_error",
                    "error": str(exc),
                    "response": response,
                }
            )
            continue
        response_correction = None
        result: Any
        if action.kind == "gather_context":
            report = gather_specification_context(
                repo_root,
                types=list(action.types),
                keywords=list(action.keywords),
                filters=action.filters,
            )
            events.append(
                {
                    "kind": action.kind,
                    "types": list(action.types),
                    "keywords": list(action.keywords),
                    "filters": action.filters,
                    "result": json.loads(render_gather_context_report(report)),
                }
            )
            continue
        if action.kind == "next_step":
            events.append(
                {
                    "kind": action.kind,
                    "decisions_and_context": action.decisions_and_context,
                }
            )
            continue
        if action.kind == "read_document":
            try:
                result = _read_task_document(action, repo_root)
            except (RuntimeError, ValueError) as exc:
                response_correction = _action_response_correction(action, exc)
                events.append(
                    {
                        "kind": "action_error",
                        "action_kind": action.kind,
                        "error": str(exc),
                    }
                )
                print(
                    "Workflow task action needs correction; requesting a "
                    "corrected action from the LLM.",
                    file=stderr,
                )
                continue
            events.append({"kind": action.kind, "result": result})
            continue
        if action.kind == "edit":
            try:
                result = _apply_task_edits(action, repo_root)
            except (RuntimeError, ValueError) as exc:
                response_correction = _action_response_correction(action, exc)
                events.append(
                    {
                        "kind": "action_error",
                        "action_kind": action.kind,
                        "error": str(exc),
                    }
                )
                print(
                    "Workflow task action needs correction; requesting a "
                    "corrected action from the LLM.",
                    file=stderr,
                )
                continue
            events.append({"kind": action.kind, "result": result})
            continue
        if action.kind == "prompt_user":
            human_input = _prompt_user_handoff(action, task)
            human_task, follow_up_task = _insert_human_handoff(
                workflow,
                task,
                human_input,
            )
            print(f"Workflow blocked on human task: {human_task.task_id}", file=stdout)
            if follow_up_task is not None:
                print(
                    f"Inserted follow-up task: {follow_up_task.task_id}",
                    file=stdout,
                )
            _publish_workflow_progress(
                repo_root,
                workflow,
                reason=f"handoff from {task.task_id}",
                stdout=stdout,
            )
            return 0
        if action.kind == "invoke_skill":
            if action.skill_name is None:
                raise RuntimeError("invoke_skill action must include a skill name.")
            nested_result = _run_skill_for_agent(
                action.skill_name,
                catalog=skill_catalog,
                client=client,
                task=task,
                repo_root=repo_root,
                stdout=stdout,
                stderr=stderr,
                max_timeout_retries=config.max_timeout_retries,
                timeout_backoff_seconds=config.timeout_backoff_seconds,
            )
            events.append(
                {
                    "kind": action.kind,
                    "skill": action.skill_name,
                    "result": nested_result,
                }
            )
            continue
        if action.kind == "invoke_tool":
            repaired_parameters = _repair_workflow_file_command(
                action.parameters,
                workflow.directory,
            )
            if repaired_parameters is not None:
                print(
                    "Corrected malformed workflow filename suffix to the exact "
                    ".json filename.",
                    file=stderr,
                )
                action = WorkflowTaskAction(
                    kind=action.kind,
                    tool=action.tool,
                    parameters=repaired_parameters,
                )
            command_error = _workflow_file_command_error(
                action.parameters,
                workflow.directory,
            )
            if command_error is not None:
                print(command_error, file=stderr)
                result = {
                    "command": action.parameters.get("command"),
                    "cwd": str(repo_root),
                    "returncode": 2,
                    "stdout": "",
                    "stderr": command_error,
                }
            else:
                try:
                    if action.tool == "shell":
                        result = _execute_shell_tool(
                            action.parameters,
                            worktree_root=repo_root,
                            stdout=stdout,
                            stderr=stderr,
                            verbose=config.verbose,
                        )
                    elif action.tool == "fuzzy-match":
                        result = _execute_fuzzy_match_tool(
                            action.parameters,
                            worktree_root=repo_root,
                        )
                    elif is_basedpyright_tool(action.tool):
                        result = execute_basedpyright_tool(
                            action.tool,
                            action.parameters,
                            worktree_root=repo_root,
                        )
                    else:
                        raise RuntimeError(
                            f"Unsupported workflow task tool {action.tool!r}; "
                            "supported tools are shell, fuzzy-match, "
                            "basedpyright-symbol, and basedpyright-structure."
                        )
                except (RuntimeError, ValueError) as exc:
                    response_correction = _action_response_correction(action, exc)
                    events.append(
                        {
                            "kind": "tool_error",
                            "tool": action.tool,
                            "parameters": action.parameters,
                            "error": str(exc),
                        }
                    )
                    print(
                        "Workflow task tool call needs correction; requesting a "
                        "corrected action from the LLM.",
                        file=stderr,
                    )
                    continue
            events.append(
                {
                    "kind": action.kind,
                    "tool": action.tool,
                    "parameters": action.parameters,
                    "result": result,
                }
            )
            continue
        if action.kind == "complete":
            completed = workflow.complete_task(task.task_id, action.output_state)
            if action.text:
                print(action.text, file=stdout)
            print(f"Completed workflow task: {completed.task_id}", file=stdout)
            _publish_workflow_progress(
                repo_root,
                workflow,
                reason=f"complete {completed.task_id}",
                stdout=stdout,
            )
            return 0
        if action.kind == "get-human-input":
            human_task, follow_up_task = _insert_human_handoff(
                workflow,
                task,
                action.human_input or {},
            )
            print(f"Workflow blocked on human task: {human_task.task_id}", file=stdout)
            if follow_up_task is not None:
                print(
                    f"Inserted follow-up task: {follow_up_task.task_id}",
                    file=stdout,
                )
            _publish_workflow_progress(
                repo_root,
                workflow,
                reason=f"human input required by {task.task_id}",
                stdout=stdout,
            )
            return 0
        raise RuntimeError(f"Unsupported workflow task action: {action.kind}")

    print(
        f"Workflow task {task.task_id} blocked after reaching the roundtrip limit.",
        file=stderr,
    )
    _publish_workflow_progress(
        repo_root,
        workflow,
        reason=f"roundtrip limit for {task.task_id}",
        stdout=stdout,
    )
    return 2


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
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[Path, Path]:
    """Use the same dedicated worktree isolation as workflow chat."""
    if not _is_git_worktree(configured_repo_root):
        return configured_repo_root, config.workflow_dir.resolve()

    worktree_root = _resolve_worktree_context(
        configured_repo_root,
        stderr=stderr,
        verbose=config.verbose,
    )
    workflow_dir = config.workflow_dir.resolve()
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

    default_branch = _default_branch(repo_root)
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
            "--draft",
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


class _WorkflowTaskTimeoutExhausted(RuntimeError):
    pass


def _complete_task_json_with_timeout_retry(
    client: WorkflowTaskChatClient,
    messages: list[dict[str, str]],
    *,
    model: str,
    stderr: TextIO,
    max_timeout_retries: int,
    timeout_backoff_seconds: float,
) -> dict[str, Any]:
    timeout_retries = 0
    while True:
        try:
            return client.complete_json(messages)
        except RuntimeError as exc:
            if not _is_timeout_error(exc):
                raise
            if timeout_retries >= max(0, max_timeout_retries):
                raise _WorkflowTaskTimeoutExhausted(
                    f"LLM request timed out after {timeout_retries} retries: {exc}"
                ) from exc
            timeout_retries += 1
            delay_seconds = timeout_backoff_seconds * (2 ** (timeout_retries - 1))
            print(
                f"LLM request timed out for {model}; retrying in "
                f"{delay_seconds:g} seconds "
                f"(retry {timeout_retries}/{max_timeout_retries}).",
                file=stderr,
                flush=True,
            )
            time.sleep(delay_seconds)


def _is_timeout_error(error: RuntimeError) -> bool:
    message = str(error).casefold()
    return "timed out" in message or "timeout" in message


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
            "events": events,
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
                    "response_correction": response_correction,
                },
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]


def _compact_workflow_task_context(
    workflow: WorkflowInstance,
    task: WorkflowTask,
    events: list[dict[str, Any]],
    *,
    client: WorkflowTaskChatClient,
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
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]
    before_tokens = _estimate_message_tokens(compaction_messages)
    print(
        "waiting for context compaction LLM response...",
        file=stderr,
        flush=True,
    )
    response = _complete_task_json_with_timeout_retry(
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
    action: WorkflowTaskAction,
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


def _task_system_prompt() -> str:
    return (
        "Task: act as a staff engineer processing one claimed task from a durable "
        "workflow. Use the task "
        "input, completed upstream outputs, task details, and prior events to "
        "make safe progress toward the task's output.\n"
        "The user message includes workflow_files, the exact JSON filenames "
        "available in workflow_dir. When reading or writing workflow files, use "
        "those exact filenames, including the .json suffix. Never infer, shorten, "
        "or replace a filename with a trailing period. If a filename is not in "
        "workflow_files, list the directory before using it.\n"
        "If response_correction is non-null, it describes a validation failure "
        "from your previous response. Correct that failure and return only the "
        "required JSON object.\n"
        "Task input contract: input_state has been populated from the task's "
        "declared inputs and any typed upstream references when the task was "
        "claimed. Use the populated values as the source of truth. Your complete "
        "action output_state must satisfy this task's output_state_type and be "
        "sufficient for every downstream task that declares this task as an "
        "upstream dependency.\n"
        "Choose exactly one outcome:\n"
        "- gather_context: choose this when structured specification context must "
        "be discovered before deciding or acting.\n"
        "- prompt_user: choose this when a human decision or review is required; "
        "the execution agent will persist it as a human workflow task.\n"
        "- edit: choose this for a known line-based file change.\n"
        "- invoke_skill: choose this when a listed skill should run as a nested "
        "workflow in the current worktree. It inherits context and bindings by "
        "default; provider_role=adversarial selects the adversarial provider, and "
        "clean=true limits the child to the explicit context list and isolates "
        "its gathered context.\n"
        "- invoke_tool: choose this when a shell, fuzzy-match, or basedpyright "
        "query is needed "
        "to inspect the worktree or perform work required to determine the "
        "output.\n"
        "- read_document: choose this when specific lines from a known document "
        "are needed.\n"
        "- next_step: choose this when the current action is complete and the LLM "
        "should decide the next action.\n"
        "- complete: choose this when the task can be safely finished now; put "
        "the produced state in output_state.\n"
        "Response: return exactly one JSON object matching exactly one outcome "
        "shape below. Do not include markdown or combine outcomes.\n"
        '{"kind":"gather_context","types":["tools"],"filters":{"labels":["pr-prep"],"language":["python"]}}\n'
        '{"kind":"prompt_user","text":"Is this plan approved?"}\n'
        '{"kind":"edit","file_path":"path","edits":[{"kind":"replace","start_line":1,"end_line":1,"text":"..."}]}\n'
        '{"kind":"invoke_tool","tool":"shell","parameters":{"command":["..."]}}\n'
        '{"kind":"invoke_skill","skill":"bootstrap-code-structure",'
        '"provider_role":"adversarial","clean":true,'
        '"context":["Review only this explicitly supplied context."]}\n'
        '{"kind":"read_document","file_path":"path","start_line":1,"end_line":20}\n'
        '{"kind":"next_step"}\n'
        '{"kind":"complete","output_state":{},"text":"..."}\n'
        "Use gather_context with one or more supported context types and optional "
        "keywords and filters when repository specifications must be discovered. "
        "Filters match exact fields and list values such as labels. Use "
        "prompt_user instead of get-human-input; the execution agent converts it "
        "to a durable human task and follow-up task. Use invoke_tool for shell "
        "commands, fuzzy-match searches, or basedpyright symbol and structure "
        "queries. "
        "The fuzzy-match command starts with fuzzy-match and supports find-like "
        "options including -name, -path, -type, -maxdepth, -mindepth, "
        "-threshold, and -print. Use complete "
        "when the task can be finished and put the task's produced state in "
        "output_state. Use get-human-input when you cannot safely finish without "
        "Do not output markdown."
    )


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


def _parse_task_action(payload: dict[str, Any]) -> WorkflowTaskAction:
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise RuntimeError("Workflow task action must include kind.")
    normalized_kind = kind.strip()
    if normalized_kind == "complete" and "output_state" not in payload:
        raise RuntimeError("Complete action must include output_state.")
    if normalized_kind == "get-human-input":
        return WorkflowTaskAction(
            kind=normalized_kind,
            human_input=_parse_human_input(payload.get("human_input")),
        )
    normalized_payload = dict(payload)
    if normalized_kind == "invoke_tool" and "tool" not in normalized_payload:
        normalized_payload["tool"] = "shell"
    return _workflow_task_action_from_skill_action(
        _parse_action_response(normalized_payload)
    )


def _run_skill_for_agent(
    skill_name: str,
    *,
    catalog: tuple[SkillCatalogEntry, ...],
    client: WorkflowTaskChatClient,
    task: WorkflowTask,
    repo_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    max_timeout_retries: int,
    timeout_backoff_seconds: float,
) -> dict[str, Any]:
    selected_skill = _find_skill_by_name(catalog, skill_name)
    stack: list[tuple[SkillCatalogEntry, int]] = [(selected_skill, 0)]
    transcript = [{"role": "user", "content": task.description}]
    execution_events: list[dict[str, Any]] = []
    execution_context = [
        f"Task input: {json.dumps(task.input_state, ensure_ascii=False)}",
        task.details or "",
    ]
    while stack:
        current_skill, step_index = stack[-1]
        if step_index >= len(current_skill.skill.steps):
            stack.pop()
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
        response = _complete_task_json_with_timeout_retry(
            client,
            messages,
            model="nested-skill",
            stderr=stderr,
            max_timeout_retries=max_timeout_retries,
            timeout_backoff_seconds=timeout_backoff_seconds,
        )
        action = _parse_action_response(response)
        if action.kind == "complete":
            stack.pop()
            execution_events.append(
                {"kind": action.kind, "skill": current_skill.skill.name}
            )
            continue
        if action.kind == "next_step":
            stack[-1] = (current_skill, step_index + 1)
            execution_events.append(
                {"kind": action.kind, "skill": current_skill.skill.name}
            )
            continue
        if action.kind == "invoke_skill":
            if action.skill_name is None:
                raise RuntimeError("invoke_skill action must include a skill name.")
            nested_skill = _find_skill_by_name(catalog, action.skill_name)
            if any(entry.path == nested_skill.path for entry, _ in stack):
                raise RuntimeError(
                    f"Recursive skill invocation is not allowed: {action.skill_name!r}."
                )
            stack.append((nested_skill, 0))
            execution_events.append({"kind": action.kind, "skill": action.skill_name})
            continue
        if action.kind == "gather_context":
            report = gather_specification_context(
                repo_root,
                types=list(action.types),
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
            task_action = _workflow_task_action_from_skill_action(action)
            execution_events.append(
                {
                    "kind": action.kind,
                    "result": _read_task_document(task_action, repo_root),
                }
            )
            continue
        if action.kind == "edit":
            task_action = _workflow_task_action_from_skill_action(action)
            execution_events.append(
                {
                    "kind": action.kind,
                    "result": _apply_task_edits(task_action, repo_root),
                }
            )
            continue
        if action.kind == "invoke_tool":
            if action.tool == "shell":
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
    action: WorkflowTaskAction,
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
    action: WorkflowTaskAction,
    repo_root: Path,
) -> list[dict[str, Any]]:
    groups = action.file_edits
    if not groups:
        if action.file_path is None:
            raise RuntimeError("edit action must include a file path.")
        groups = ((action.file_path, action.edits),)
    results: list[dict[str, Any]] = []
    for group in groups:
        file_path = group[0] if isinstance(group, tuple) else group.file_path
        edits = group[1] if isinstance(group, tuple) else group.edits
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
    action: WorkflowTaskAction,
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


def _workflow_task_action_from_skill_action(
    action: SkillChatAction,
) -> WorkflowTaskAction:
    return WorkflowTaskAction(
        kind=action.kind,
        tool=action.tool or "shell",
        skill_name=action.skill_name,
        file_path=action.file_path,
        start_line=action.start_line,
        end_line=action.end_line,
        edits=action.edits,
        file_edits=action.file_edits,
        types=action.types,
        keywords=action.keywords,
        filters=action.filters,
        output_state=action.output_state,
        text=action.text,
        parameters=action.parameters,
        decisions_and_context=action.decisions_and_context,
        provider_role=action.provider_role,
        clean=action.clean,
        context=action.context,
    )


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


def _build_zai_client(
    config: WorkflowTaskAgentConfig,
    task: WorkflowTask,
    *,
    progress_stream: TextIO | None = None,
) -> WorkflowTaskChatClient:
    mapping = _resolve_llm_mapping(
        task.llm_type,
        mappings=tuple(ZAI_LLM_MAPPINGS.items()),
        provider="zai",
    )
    if mapping is None:
        raise RuntimeError(f"Workflow task has no llm_type mapping: {task.task_id}")
    return _build_zai_client_for_mapping(
        config,
        task,
        mapping,
        progress_stream=progress_stream,
    )


def _build_zai_client_for_mapping(
    config: WorkflowTaskAgentConfig,
    task: WorkflowTask,
    mapping: Any,
    *,
    progress_stream: TextIO | None = None,
) -> WorkflowTaskChatClient:
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
