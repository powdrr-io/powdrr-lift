"""Agent-owned Git lifecycle for project tasks."""

from __future__ import annotations

import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from powdrr_lift.errors import PowdrrExecutionError

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class AgentWorktree:
    repository: Path
    worktree: Path
    branch: str
    starting_commit: str
    remote: str | None
    base_branch: str | None


def prepare_agent_worktree(
    repo_root: str | Path,
    task_name: str,
    *,
    base_ref: str = "HEAD",
    worktree_root: str | Path | None = None,
    remote: str = "origin",
    base_branch: str | None = None,
    publish: bool = False,
    run_id: str | None = None,
    runner: Runner = subprocess.run,
) -> AgentWorktree:
    """Create a dedicated task branch and worktree before a workflow runs."""
    repository = Path(repo_root).resolve()
    _git(runner, repository, ["rev-parse", "--show-toplevel"])
    dirty = _git(runner, repository, ["status", "--porcelain"])
    if dirty:
        paths = ", ".join(line[3:] for line in dirty.splitlines() if len(line) > 3)
        raise PowdrrExecutionError(
            "Starting checkout must be clean; found changes in: "
            + (paths or "unknown paths")
        )

    starting_commit = _git(
        runner, repository, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"]
    )
    selected_remote: str | None = None
    selected_base_branch: str | None = None
    if publish:
        remotes = _git(runner, repository, ["remote"]).splitlines()
        if remote not in remotes:
            raise PowdrrExecutionError(
                f"Cannot publish: Git remote {remote!r} is not configured."
            )
        selected_base_branch = base_branch or _default_branch(
            runner, repository, remote
        )
        _git(runner, repository, ["fetch", remote, selected_base_branch])
        remote_base = _git(
            runner,
            repository,
            ["rev-parse", "--verify", f"refs/remotes/{remote}/{selected_base_branch}"],
        )
        ancestry = runner(
            ["git", "merge-base", "--is-ancestor", starting_commit, remote_base],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        if ancestry.returncode != 0:
            raise PowdrrExecutionError(
                f"Starting commit {starting_commit} diverges from PR base "
                f"{remote}/{selected_base_branch}; refusing to publish "
                "unrelated changes."
            )
        selected_remote = remote

    slug = _slugify_task_name(task_name)
    attempt = run_id or uuid.uuid4().hex[:12]
    attempt_slug = _slugify_task_name(attempt)
    branch = f"powdrr/{slug}-{attempt_slug}"
    common_git_directory = Path(
        _git(
            runner,
            repository,
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        )
    ).resolve()
    base_directory = (
        Path(worktree_root).resolve()
        if worktree_root is not None
        else common_git_directory / "powdrr-lift" / "worktrees"
    )
    worktree = base_directory / f"{slug}-{attempt_slug}"
    if worktree.exists():
        raise PowdrrExecutionError(f"Task worktree already exists: {worktree}")
    branch_check = runner(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if branch_check.returncode == 0:
        raise PowdrrExecutionError(f"Task branch already exists: {branch}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(
        runner,
        repository,
        ["worktree", "add", "-b", branch, str(worktree), starting_commit],
    )
    return AgentWorktree(
        repository,
        worktree,
        branch,
        starting_commit,
        selected_remote,
        selected_base_branch,
    )


def commit_paths(
    worktree: str | Path,
    paths: Sequence[str],
    message: str,
    *,
    runner: Runner = subprocess.run,
) -> bool:
    """Commit only the listed paths; return false when there are no changes."""
    root = Path(worktree).resolve()
    safe_paths = tuple(_relative_path(item) for item in paths)
    if not safe_paths:
        raise ValueError("at least one commit path is required")
    _git(runner, root, ["add", "--", *safe_paths])
    staged = runner(
        ["git", "diff", "--cached", "--quiet"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise PowdrrExecutionError(
            "Could not inspect staged task changes: " + staged.stderr.strip()
        )
    _git(
        runner,
        root,
        [
            "-c",
            "user.name=Powdrr Automation",
            "-c",
            "user.email=powdrr-automation@users.noreply.github.com",
            "commit",
            "-m",
            message,
        ],
    )
    return True


def publish_pull_request(
    task: AgentWorktree,
    *,
    title: str,
    body: str,
    runner: Runner = subprocess.run,
) -> str:
    """Push an agent-owned task branch and open its review pull request."""
    if not task.remote or not task.base_branch:
        raise PowdrrExecutionError("Publishing requires a prepared remote base.")
    _git(
        runner,
        task.worktree,
        ["push", "--set-upstream", task.remote, task.branch],
    )
    return _git(
        runner,
        task.worktree,
        [
            "gh",
            "pr",
            "create",
            "--base",
            task.base_branch,
            "--head",
            task.branch,
            "--title",
            title,
            "--body",
            body,
        ],
        executable="gh",
    ).splitlines()[-1]


def _default_branch(runner: Runner, repository: Path, remote: str) -> str:
    symbolic = runner(
        ["git", "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if symbolic.returncode == 0:
        value = symbolic.stdout.strip()
        prefix = f"{remote}/"
        if value.startswith(prefix) and len(value) > len(prefix):
            return value[len(prefix) :]
    raise PowdrrExecutionError(
        f"Could not determine the default branch for remote {remote!r}; "
        "pass --base-branch explicitly."
    )


def _git(
    runner: Runner,
    cwd: Path,
    args: list[str],
    *,
    executable: str = "git",
) -> str:
    result = runner(
        [executable, *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise PowdrrExecutionError(f"{executable} {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _relative_path(value: str) -> str:
    path = Path(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"commit path must be a safe repository-relative path: {value!r}"
        )
    return value


def _slugify_task_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    if not slug:
        raise ValueError("The task name must contain a letter or digit.")
    return slug
