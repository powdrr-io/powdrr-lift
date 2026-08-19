"""Git-backed lifecycle state for durable workflow runs."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKFLOW_GIT_STATE_FILENAME = ".workflow-git.json"


class WorkflowGitInconsistency(RuntimeError):
    """A workflow's Git-backed state cannot be safely advanced."""


@dataclass(frozen=True, slots=True)
class WorkflowGitState:
    """The immutable Git identity of one workflow run."""

    proposed_pr_id: str
    base_branch: str
    integration_branch: str
    workflow_relative_directory: str

    def to_data(self) -> dict[str, str]:
        return {
            "proposed_pr_id": self.proposed_pr_id,
            "base_branch": self.base_branch,
            "integration_branch": self.integration_branch,
            "workflow_relative_directory": self.workflow_relative_directory,
        }

    @classmethod
    def from_data(cls, data: object) -> WorkflowGitState:
        if not isinstance(data, dict):
            raise ValueError("Workflow Git state must be an object.")
        values: dict[str, str] = {}
        for key in (
            "proposed_pr_id",
            "base_branch",
            "integration_branch",
            "workflow_relative_directory",
        ):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Workflow Git state requires {key!r}.")
            values[key] = value.strip()
        return cls(**values)


def workflow_git_state_path(workflow_directory: str | Path) -> Path:
    return Path(workflow_directory) / WORKFLOW_GIT_STATE_FILENAME


def load_workflow_git_state(workflow_directory: str | Path) -> WorkflowGitState | None:
    path = workflow_git_state_path(workflow_directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return WorkflowGitState.from_data(data)
    except ValueError:
        return None


def save_workflow_git_state(
    workflow_directory: str | Path,
    state: WorkflowGitState,
) -> Path:
    path = workflow_git_state_path(workflow_directory)
    path.write_text(
        json.dumps(state.to_data(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def slugify_workflow_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    if not slug:
        raise ValueError("The proposed PR id must contain a letter or digit.")
    return slug


def integration_branch_name(proposed_pr_id: str) -> str:
    return f"powdrr/{slugify_workflow_id(proposed_pr_id)}"


def task_branch_name(proposed_pr_id: str, task_id: str) -> str:
    return (
        f"powdrr/{slugify_workflow_id(proposed_pr_id)}-task/"
        f"{slugify_workflow_id(task_id)}"
    )


def workflow_worktree_path(repo_root: str | Path, proposed_pr_id: str) -> Path:
    return (
        Path(repo_root) / ".worktrees" / "powdrr" / slugify_workflow_id(proposed_pr_id)
    )


def task_worktree_path(
    repo_root: str | Path,
    proposed_pr_id: str,
    task_id: str,
) -> Path:
    return (
        workflow_worktree_path(repo_root, proposed_pr_id)
        / "tasks"
        / slugify_workflow_id(task_id)
    )


def create_workflow_worktree(
    repo_root: str | Path,
    proposed_pr_id: str,
    *,
    base_branch: str = "main",
) -> tuple[Path, str]:
    repo_root_path = Path(repo_root).resolve()
    branch = integration_branch_name(proposed_pr_id)
    worktree = workflow_worktree_path(repo_root_path, proposed_pr_id)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        if not (worktree / ".git").exists():
            raise RuntimeError(
                f"Workflow worktree path is not a Git worktree: {worktree}"
            )
        return worktree, branch
    if (
        _git(
            repo_root_path, ["show-ref", "--verify", f"refs/heads/{branch}"]
        ).returncode
        == 0
    ):
        _run_git(repo_root_path, ["worktree", "add", str(worktree), branch])
    else:
        _run_git(
            repo_root_path,
            ["worktree", "add", str(worktree), "-b", branch, base_branch],
        )
    return worktree, branch


def create_task_worktree(
    repo_root: str | Path,
    state: WorkflowGitState,
    task_id: str,
) -> tuple[Path, str]:
    repo_root_path = Path(repo_root).resolve()
    branch = task_branch_name(state.proposed_pr_id, task_id)
    worktree = task_worktree_path(repo_root_path, state.proposed_pr_id, task_id)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        if not (worktree / ".git").exists():
            raise RuntimeError(f"Task worktree path is not a Git worktree: {worktree}")
        return worktree, branch
    if (
        _git(
            repo_root_path, ["show-ref", "--verify", f"refs/heads/{branch}"]
        ).returncode
        == 0
    ):
        _run_git(repo_root_path, ["worktree", "add", str(worktree), branch])
    else:
        _run_git(
            repo_root_path,
            ["worktree", "add", str(worktree), "-b", branch, state.integration_branch],
        )
    return worktree, branch


def claim_workflow_task(
    repo_root: str | Path,
    state: WorkflowGitState,
    task_id: str,
) -> None:
    """Atomically claim a task using a Git ref compare-and-swap."""
    repo_root_path = Path(repo_root).resolve()
    integration_head = _run_git(
        repo_root_path,
        ["rev-parse", state.integration_branch],
    )
    claim_ref = (
        f"refs/agents/claims/{slugify_workflow_id(state.proposed_pr_id)}/"
        f"{slugify_workflow_id(task_id)}"
    )
    result = _git(
        repo_root_path,
        [
            "update-ref",
            claim_ref,
            integration_head,
            "0" * 40,
        ],
    )
    if result.returncode != 0:
        raise WorkflowGitInconsistency(
            json.dumps(
                {
                    "proposed_pr_id": state.proposed_pr_id,
                    "task_id": task_id,
                    "inconsistencies": [
                        f"task claim already exists: {claim_ref}",
                    ],
                    "recovery_command": (
                        "powdrr-lift workflow-recovery --proposed-pr-id "
                        f"{state.proposed_pr_id} --cleanup"
                    ),
                },
                indent=2,
            )
        )


def commit_and_push_workflow_initialization(
    worktree: str | Path,
    workflow_directory: str | Path,
    *,
    push: bool = True,
) -> None:
    """Commit the initial task graph and optionally publish its branch."""
    worktree_path = Path(worktree).resolve()
    workflow_path = Path(workflow_directory).resolve()
    relative_workflow = workflow_path.relative_to(worktree_path)
    _run_git(worktree_path, ["add", str(relative_workflow)])
    _run_git(
        worktree_path,
        ["commit", "-m", "Initialize durable workflow run"],
    )
    if push:
        branch = _run_git(worktree_path, ["branch", "--show-current"])
        _run_git(worktree_path, ["push", "--set-upstream", "origin", branch])


def synchronize_workflow_initialization(
    integration_worktree: str | Path,
    source_worktree: str | Path,
) -> None:
    """Make the integration branch contain the active workflow commit."""
    integration_path = Path(integration_worktree).resolve()
    source_path = Path(source_worktree).resolve()
    source_branch = _run_git(source_path, ["branch", "--show-current"])
    if source_branch in {"main", "master"}:
        raise RuntimeError(
            "Cannot initialize a workflow from a protected branch; use a "
            "dedicated feature worktree."
        )
    _run_git(integration_path, ["merge", "--ff-only", source_branch])
    integration_branch = _run_git(integration_path, ["branch", "--show-current"])
    _run_git(
        integration_path,
        ["push", "--set-upstream", "origin", integration_branch],
    )


def inspect_workflow_run(
    repo_root: str | Path,
    proposed_pr_id: str,
) -> dict[str, Any]:
    """Collect Git-backed state for one workflow run without changing it."""
    repo_root_path = Path(repo_root).resolve()
    slug = slugify_workflow_id(proposed_pr_id)
    integration_branch = integration_branch_name(proposed_pr_id)
    task_prefix = f"powdrr/{slug}-task/"
    claim_prefix = f"refs/agents/claims/{slug}/"
    task_branches = _for_each_ref(repo_root_path, f"refs/heads/{task_prefix}")
    claim_refs = _for_each_ref(repo_root_path, claim_prefix)
    worktrees = _workflow_worktrees(repo_root_path, integration_branch, task_prefix)
    registered_integration_worktrees = [
        Path(item["path"])
        for item in worktrees
        if item.get("branch") == integration_branch
    ]
    integration_worktree = (
        registered_integration_worktrees[0]
        if registered_integration_worktrees
        else workflow_worktree_path(repo_root_path, proposed_pr_id)
    )
    state_paths = (
        sorted(integration_worktree.rglob(WORKFLOW_GIT_STATE_FILENAME))
        if integration_worktree.is_dir()
        else []
    )
    state = load_workflow_git_state(state_paths[0].parent) if state_paths else None
    tasks: list[dict[str, Any]] = []
    if state is not None:
        workflow_directory = integration_worktree / state.workflow_relative_directory
        for task_path in sorted(workflow_directory.glob("*.json")):
            if task_path.name == WORKFLOW_GIT_STATE_FILENAME:
                continue
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                task = {"error": "could not read task file"}
            tasks.append(
                {
                    "path": str(task_path),
                    "task_id": task.get("task_id") if isinstance(task, dict) else None,
                    "status": task.get("status") if isinstance(task, dict) else None,
                }
            )
    report: dict[str, Any] = {
        "proposed_pr_id": proposed_pr_id,
        "integration_branch": integration_branch,
        "integration_branch_exists": _branch_exists(repo_root_path, integration_branch),
        "integration_worktree": str(integration_worktree),
        "integration_worktree_exists": integration_worktree.is_dir(),
        "workflow_git_state": state.to_data() if state is not None else None,
        "task_branches": [
            {
                "branch": branch,
                "integrated": _is_ancestor(repo_root_path, branch, integration_branch),
            }
            for branch in task_branches
        ],
        "claim_refs": claim_refs,
        "worktrees": worktrees,
        "tasks": tasks,
    }
    report["pull_requests"], report["pull_requests_error"] = _related_pull_requests(
        repo_root_path,
        [item["branch"] for item in report["task_branches"]],
    )
    report["inconsistencies"] = _find_inconsistencies(report)
    if report["pull_requests_error"]:
        report["errors"] = [report["pull_requests_error"]]
    return report


def cleanup_workflow_run(
    repo_root: str | Path,
    proposed_pr_id: str,
    *,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove task artifacts while preserving the integration checkpoint."""
    repo_root_path = Path(repo_root).resolve()
    state_report = report or inspect_workflow_run(repo_root_path, proposed_pr_id)
    removed: list[str] = []
    errors: list[str] = []
    integration_worktree = Path(state_report["integration_worktree"])
    state_data = state_report.get("workflow_git_state")
    if integration_worktree.is_dir() and isinstance(state_data, dict):
        relative_directory = state_data.get("workflow_relative_directory")
        if isinstance(relative_directory, str) and relative_directory:
            restore = _git(
                integration_worktree,
                [
                    "restore",
                    "--source=HEAD",
                    "--staged",
                    "--worktree",
                    "--",
                    relative_directory,
                ],
            )
            clean = _git(
                integration_worktree,
                ["clean", "-fd", "--", relative_directory],
            )
            if restore.returncode == 0 and clean.returncode == 0:
                removed.append(f"incomplete-workflow-files:{relative_directory}")
            else:
                errors.append(
                    "integration-worktree: could not restore workflow files: "
                    f"{restore.stderr.strip() or clean.stderr.strip()}"
                )

    for pull_request in state_report.get("pull_requests", []):
        if pull_request.get("state") != "OPEN":
            continue
        number = pull_request.get("number")
        if not isinstance(number, int):
            continue
        result = _gh(
            repo_root_path,
            [
                "pr",
                "close",
                str(number),
                "--comment",
                f"Closed by workflow recovery cleanup for {proposed_pr_id}.",
            ],
        )
        if result.returncode == 0:
            removed.append(f"pull-request:{number}")
        else:
            errors.append(f"pull-request:{number}: {result.stderr.strip()}")

    for worktree in state_report["worktrees"]:
        path = Path(worktree["path"])
        if worktree["branch"] == state_report["integration_branch"]:
            continue
        result = _git(repo_root_path, ["worktree", "remove", "--force", str(path)])
        if result.returncode == 0:
            removed.append(f"worktree:{path}")
        else:
            errors.append(f"worktree:{path}: {result.stderr.strip()}")
    for item in state_report["task_branches"]:
        branch = item["branch"]
        result = _git(repo_root_path, ["branch", "-D", branch])
        if result.returncode == 0:
            removed.append(f"branch:{branch}")
        elif "not found" not in result.stderr.casefold():
            errors.append(f"branch:{branch}: {result.stderr.strip()}")
        if _remote_exists(repo_root_path, "origin"):
            remote_result = _git(
                repo_root_path,
                ["push", "origin", "--delete", branch],
            )
            if remote_result.returncode == 0:
                removed.append(f"remote-branch:{branch}")
            elif "remote ref does not exist" not in remote_result.stderr.casefold():
                errors.append(f"remote-branch:{branch}: {remote_result.stderr.strip()}")
    for claim_ref in state_report["claim_refs"]:
        result = _git(repo_root_path, ["update-ref", "-d", claim_ref])
        if result.returncode == 0:
            removed.append(f"claim:{claim_ref}")
        else:
            errors.append(f"claim:{claim_ref}: {result.stderr.strip()}")
    return {
        **state_report,
        "removed": removed,
        "errors": [*state_report.get("errors", []), *errors],
        "integration_checkpoint_preserved": bool(
            state_report["integration_branch_exists"]
            and state_report["integration_worktree_exists"]
        ),
    }


def validate_workflow_git_state(
    repo_root: str | Path,
    state: WorkflowGitState,
    task_id: str,
) -> dict[str, Any]:
    """Return actionable consistency errors before a task mutates Git state."""
    report = inspect_workflow_run(repo_root, state.proposed_pr_id)
    errors = list(report["inconsistencies"])
    expected_branch = task_branch_name(state.proposed_pr_id, task_id)
    task_branches = {item["branch"] for item in report["task_branches"]}
    if expected_branch in task_branches:
        matching_worktrees = [
            item
            for item in report["worktrees"]
            if item.get("branch") == expected_branch
        ]
        if not matching_worktrees:
            errors.append(
                f"task branch {expected_branch!r} exists without its "
                "registered worktree"
            )
    if not report["integration_branch_exists"]:
        errors.append(f"integration branch {state.integration_branch!r} does not exist")
    if errors:
        raise WorkflowGitInconsistency(
            json.dumps(
                {
                    "proposed_pr_id": state.proposed_pr_id,
                    "task_id": task_id,
                    "inconsistencies": sorted(set(errors)),
                    "recovery_command": (
                        "powdrr-lift workflow-recovery --proposed-pr-id "
                        f"{state.proposed_pr_id} --cleanup"
                    ),
                },
                indent=2,
            )
        )
    return report


def _for_each_ref(repo_root: Path, prefix: str) -> list[str]:
    result = _git(repo_root, ["for-each-ref", "--format=%(refname)", prefix])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    refs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if prefix.startswith("refs/heads/"):
        return [ref.removeprefix("refs/heads/") for ref in refs]
    return refs


def _branch_exists(repo_root: Path, branch: str) -> bool:
    return (
        _git(repo_root, ["show-ref", "--verify", f"refs/heads/{branch}"]).returncode
        == 0
    )


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    if not _branch_exists(repo_root, ancestor) or not _branch_exists(
        repo_root, descendant
    ):
        return False
    return (
        _git(
            repo_root, ["merge-base", "--is-ancestor", ancestor, descendant]
        ).returncode
        == 0
    )


def _workflow_worktrees(
    repo_root: Path,
    integration_branch: str,
    task_prefix: str,
) -> list[dict[str, str]]:
    result = _git(repo_root, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in [*result.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            current = {"path": line.removeprefix("worktree ")}
        elif line.startswith("branch "):
            branch = line.removeprefix("branch refs/heads/")
            if branch == integration_branch or branch.startswith(task_prefix):
                current["branch"] = branch
        elif not line and current.get("branch"):
            worktrees.append(current)
            current = {}
    return worktrees


def _find_inconsistencies(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report["integration_branch_exists"] != report["integration_worktree_exists"]:
        errors.append(
            "integration branch and integration worktree do not agree: "
            f"branch_exists={report['integration_branch_exists']}, "
            f"worktree_exists={report['integration_worktree_exists']}"
        )
    task_branches = {item["branch"] for item in report["task_branches"]}
    worktree_branches = {item["branch"] for item in report["worktrees"]}
    for branch in sorted(task_branches - worktree_branches):
        errors.append(f"task branch {branch!r} has no registered worktree")
    for branch in sorted(worktree_branches - task_branches):
        if branch != report["integration_branch"]:
            errors.append(f"task worktree {branch!r} has no local task branch")
    if report["integration_branch_exists"] and not report["workflow_git_state"]:
        errors.append(
            "integration branch exists but .workflow-git.json is missing or invalid"
        )
    for task in report["tasks"]:
        if "error" in task:
            errors.append(f"task file {task['path']!r} cannot be read")
    return errors


def _related_pull_requests(
    repo_root: Path,
    branches: list[str],
) -> tuple[list[dict[str, Any]], str | None]:
    pull_requests: list[dict[str, Any]] = []
    for branch in branches:
        result = _gh(
            repo_root,
            [
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,state,url,headRefName,baseRefName",
            ],
        )
        if result.returncode != 0:
            return pull_requests, result.stderr.strip() or "gh pr list failed"
        try:
            values = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return pull_requests, "gh pr list returned invalid JSON"
        if isinstance(values, list):
            pull_requests.extend(value for value in values if isinstance(value, dict))
    return pull_requests, None


def _remote_exists(repo_root: Path, remote: str) -> bool:
    return _git(repo_root, ["remote", "get-url", remote]).returncode == 0


def _gh(repo_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            ["gh", *arguments],
            127,
            "",
            str(exc),
        )


def _git(repo_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_git(repo_root: Path, arguments: list[str]) -> str:
    result = _git(repo_root, arguments)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()
