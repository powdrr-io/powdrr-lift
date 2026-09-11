"""Workflow context persistence and dedicated-worktree lifecycle services."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from powdrr_lift.agent.context import WorkflowContext
from powdrr_lift.core import resolve_repo_root
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.builtin_tools import invoke_intrinsic_capability
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.intrinsic_git_gh import GH_TOOL, GIT_TOOL
from powdrr_lift.process.catalog import SkillCatalogEntry
from powdrr_lift.workflow_chat_io import _prompt_user, _verbose_print
from powdrr_lift.workflow_paths import is_dedicated_worktree

_WORKFLOW_CONTEXT_PATH = Path(".powdrr") / "workflow-context.json"


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
