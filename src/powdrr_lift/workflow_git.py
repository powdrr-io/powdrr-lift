"""Git-backed lifecycle state for durable workflow runs."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.errors import PowdrrExecutionError

WORKFLOW_GIT_STATE_SUFFIX = "-workflow.yaml"


class WorkflowGitInconsistency(PowdrrExecutionError):
    """A workflow's Git-backed state cannot be safely advanced."""


@dataclass(frozen=True, slots=True)
class WorkflowGitState:
    """The immutable Git identity of one workflow run."""

    proposed_pr_id: str
    base_branch: str
    integration_branch: str
    workflow_relative_directory: str
    depends_on_workflows: tuple[str, ...] = ()
    invariants: tuple[Mapping[str, Any], ...] = ()
    relationships: tuple[Mapping[str, Any], ...] = ()

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "proposed_pr_id": self.proposed_pr_id,
            "base_branch": self.base_branch,
            "integration_branch": self.integration_branch,
            "workflow_relative_directory": self.workflow_relative_directory,
        }
        if self.depends_on_workflows:
            data["depends_on_workflows"] = list(self.depends_on_workflows)
        if self.invariants:
            data["invariants"] = [dict(item) for item in self.invariants]
        if self.relationships:
            data["relationships"] = [dict(item) for item in self.relationships]
        return data

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
        dependencies = data.get("depends_on_workflows", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) and item.strip() for item in dependencies
        ):
            raise ValueError(
                "Workflow Git state depends_on_workflows must be an array."
            )
        invariants = _read_relationship_metadata(data.get("invariants"), "invariants")
        relationships = _read_relationship_metadata(
            data.get("relationships"), "relationships"
        )
        return cls(
            **values,
            depends_on_workflows=tuple(item.strip() for item in dependencies),
            invariants=invariants,
            relationships=relationships,
        )


def _read_relationship_metadata(
    value: object,
    field_name: str,
) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Workflow Git state {field_name} must be a list of mappings.")
    return tuple(value)


def workflow_git_state_filename(workflow_id: str) -> str:
    return f"{slugify_workflow_id(workflow_id)}{WORKFLOW_GIT_STATE_SUFFIX}"


def workflow_id_from_task_id(task_id: str) -> str | None:
    workflow_id, separator, _task_number = task_id.rpartition("-task-")
    if not separator or not workflow_id:
        return None
    # Instantiated workflows namespace task ids with the workflow instance
    # name.  The durable metadata and integration branch use the proposed PR
    # id without the conventional ``-workflow`` suffix.
    if workflow_id.endswith("-workflow"):
        workflow_id = workflow_id[: -len("-workflow")]
    return workflow_id or None


def workflow_git_state_path(
    workflow_directory: str | Path,
    workflow_id: str,
) -> Path:
    return Path(workflow_directory) / workflow_git_state_filename(workflow_id)


def load_workflow_git_state(
    workflow_directory: str | Path,
    workflow_id: str | None = None,
) -> WorkflowGitState | None:
    directory = Path(workflow_directory)
    paths = (
        [workflow_git_state_path(directory, workflow_id)]
        if workflow_id is not None
        else sorted(directory.glob(f"*{WORKFLOW_GIT_STATE_SUFFIX}"))
    )
    if len(paths) != 1:
        return None
    try:
        data = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    try:
        state = WorkflowGitState.from_data(data)
    except ValueError:
        return None
    if workflow_id is not None and state.proposed_pr_id != workflow_id:
        return None
    return state


def load_workflow_git_states(
    workflow_directory: str | Path,
) -> tuple[WorkflowGitState, ...]:
    """Load every valid workflow Git state in a shared workflow directory."""
    states: list[WorkflowGitState] = []
    for path in sorted(Path(workflow_directory).glob(f"*{WORKFLOW_GIT_STATE_SUFFIX}")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            states.append(WorkflowGitState.from_data(data))
        except (OSError, yaml.YAMLError, ValueError):
            continue
    return tuple(states)


def save_workflow_git_state(
    workflow_directory: str | Path,
    state: WorkflowGitState,
) -> Path:
    path = workflow_git_state_path(workflow_directory, state.proposed_pr_id)
    path.write_text(
        yaml.safe_dump(state.to_data(), sort_keys=False),
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


def workflow_dependencies_completion(
    repo_root: str | Path,
    state: WorkflowGitState,
) -> tuple[bool, tuple[str, ...]]:
    """Return whether every predecessor workflow has a merged integration PR."""
    incomplete: list[str] = []
    repo_root_path = Path(repo_root).resolve()
    for dependency in state.depends_on_workflows:
        branch = integration_branch_name(dependency)
        pull_requests, error = _related_pull_requests(repo_root_path, [branch])
        if error:
            incomplete.append(f"{dependency}: could not inspect pull request: {error}")
            continue
        if not any(item.get("state") == "MERGED" for item in pull_requests):
            incomplete.append(
                f"{dependency}: integration PR for {branch} is not merged"
            )
    return not incomplete, tuple(incomplete)


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


def resolve_git_repository_root(repo_root: str | Path) -> Path:
    """Return the common repository root for a checkout or Git worktree."""
    repo_root_path = Path(repo_root).resolve()
    result = _git(
        repo_root_path,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )
    if result.returncode != 0:
        raise PowdrrExecutionError(
            f"Could not determine the common Git directory for {repo_root_path}: "
            f"{result.stderr.strip()}"
        )
    common_git_directory = Path(result.stdout.strip()).resolve()
    if common_git_directory.name != ".git":
        raise PowdrrExecutionError(
            f"Git common directory is not a repository .git directory: "
            f"{common_git_directory}"
        )
    return common_git_directory.parent


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
            raise PowdrrExecutionError(
                f"Workflow worktree path is not a Git worktree: {worktree}"
            )
        existing_branch = _run_git(worktree, ["branch", "--show-current"])
        if existing_branch != branch:
            raise PowdrrExecutionError(
                f"Workflow worktree {worktree} is on {existing_branch!r}, "
                f"expected integration branch {branch!r}."
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
            raise PowdrrExecutionError(
                f"Task worktree path is not a Git worktree: {worktree}"
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
        claim_exists = (
            _git(
                repo_root_path,
                ["show-ref", "--verify", "--quiet", claim_ref],
            ).returncode
            == 0
        )
        error_detail = (
            result.stderr.strip() or result.stdout.strip() or "unknown git error"
        )
        inconsistency = (
            f"task claim already exists: {claim_ref}"
            if claim_exists
            else f"could not create task claim {claim_ref}: {error_detail}"
        )
        raise WorkflowGitInconsistency(
            json.dumps(
                {
                    "proposed_pr_id": state.proposed_pr_id,
                    "task_id": task_id,
                    "inconsistencies": [inconsistency],
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
        raise PowdrrExecutionError(
            "Cannot initialize a workflow from a protected branch; use a "
            "dedicated feature worktree."
        )
    _run_git(integration_path, ["merge", "--ff-only", source_branch])
    integration_branch = _run_git(integration_path, ["branch", "--show-current"])
    _run_git(
        integration_path,
        ["push", "--set-upstream", "origin", integration_branch],
    )


def _load_workflow_git_state_from_branch(
    repo_root: Path,
    branch: str,
    expected_workflow_id: str | None = None,
) -> tuple[WorkflowGitState | None, str | None]:
    result = _git(repo_root, ["ls-tree", "-r", "--name-only", branch])
    if result.returncode != 0:
        return None, None
    state_paths = sorted(
        path
        for path in result.stdout.splitlines()
        if Path(path).name.endswith(WORKFLOW_GIT_STATE_SUFFIX)
    )
    for state_path in state_paths:
        contents = _git(repo_root, ["show", f"{branch}:{state_path}"])
        if contents.returncode != 0:
            continue
        try:
            state = WorkflowGitState.from_data(yaml.safe_load(contents.stdout))
        except (yaml.YAMLError, ValueError):
            continue
        if expected_workflow_id is None or state.proposed_pr_id == expected_workflow_id:
            return state, state_path
    return None, None


def _workflow_task_files_from_branch(
    repo_root: Path,
    branch: str,
    workflow_directory: str,
) -> list[tuple[str, str]]:
    result = _git(
        repo_root, ["ls-tree", "-r", "--name-only", branch, "--", workflow_directory]
    )
    if result.returncode != 0:
        return []
    task_paths = sorted(
        path
        for path in result.stdout.splitlines()
        if Path(path).parent.as_posix() == workflow_directory
        and Path(path).suffix.lower() in {".yaml", ".yml", ".json"}
        and not Path(path).name.endswith(WORKFLOW_GIT_STATE_SUFFIX)
    )
    files: list[tuple[str, str]] = []
    for task_path in task_paths:
        contents = _git(repo_root, ["show", f"{branch}:{task_path}"])
        if contents.returncode == 0:
            files.append((f"{branch}:{task_path}", contents.stdout))
    return files


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
    state_paths: list[Path] = []
    if integration_worktree.is_dir():
        state_paths.extend(
            integration_worktree.rglob(workflow_git_state_filename(proposed_pr_id))
        )
        state_paths = sorted(set(state_paths))
    state = (
        load_workflow_git_state(state_paths[0].parent, proposed_pr_id)
        if state_paths
        else None
    )
    branch_state_path: str | None = None
    if state is None and _branch_exists(repo_root_path, integration_branch):
        state, branch_state_path = _load_workflow_git_state_from_branch(
            repo_root_path, integration_branch, proposed_pr_id
        )
    tasks: list[dict[str, Any]] = []
    if state is not None:
        if integration_worktree.is_dir():
            workflow_directory = (
                integration_worktree / state.workflow_relative_directory
            )
            task_paths = sorted(
                path
                for pattern in ("*.yaml", "*.yml", "*.json")
                for path in workflow_directory.glob(pattern)
            )
            task_contents = [
                (str(task_path), task_path.read_text(encoding="utf-8"))
                for task_path in task_paths
                if not task_path.name.endswith(WORKFLOW_GIT_STATE_SUFFIX)
            ]
        else:
            task_contents = _workflow_task_files_from_branch(
                repo_root_path,
                integration_branch,
                state.workflow_relative_directory,
            )
        for task_path, content in task_contents:
            try:
                suffix = Path(task_path).suffix.lower()
                task = (
                    yaml.safe_load(content)
                    if suffix in {".yaml", ".yml"}
                    else json.loads(content)
                )
            except (OSError, json.JSONDecodeError, yaml.YAMLError):
                task = {"error": "could not read task file"}
            tasks.append(
                {
                    "path": task_path,
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
        "workflow_git_state_source": (
            branch_state_path
            if branch_state_path is not None
            else (str(state_paths[0]) if state_paths else None)
        ),
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
    """Remove task artifacts and reset committed workflow progress."""
    repo_root_path = Path(repo_root).resolve()
    state_report = report or inspect_workflow_run(repo_root_path, proposed_pr_id)
    removed: list[str] = []
    errors: list[str] = []
    integration_worktree = Path(state_report["integration_worktree"])
    state_data = state_report.get("workflow_git_state")
    checkpoint_valid = isinstance(state_data, dict)
    if not checkpoint_valid and integration_worktree.is_dir():
        recovered_state = _find_workflow_git_state_in_checkout(
            repo_root_path, proposed_pr_id, integration_worktree
        )
        if recovered_state is not None:
            state_data = recovered_state.to_data()
            checkpoint_valid = True
    if not checkpoint_valid:
        # Without valid metadata there is no trustworthy workflow directory or
        # checkpoint to preserve. Treat the run as incomplete and remove the
        # integration artifacts rather than leaving a branch that every future
        # invocation will rediscover as inconsistent.
        if integration_worktree.is_dir():
            result = _git(
                repo_root_path,
                ["worktree", "remove", "--force", str(integration_worktree)],
            )
            if result.returncode == 0:
                removed.append(f"worktree:{integration_worktree}")
            else:
                errors.append(
                    f"integration-worktree:{integration_worktree}: "
                    f"{result.stderr.strip()}"
                )
        if state_report["integration_branch_exists"]:
            result = _git(
                repo_root_path,
                ["branch", "-D", state_report["integration_branch"]],
            )
            if result.returncode == 0:
                removed.append(f"branch:{state_report['integration_branch']}")
            elif "not found" not in result.stderr.casefold():
                errors.append(
                    f"branch:{state_report['integration_branch']}: "
                    f"{result.stderr.strip()}"
                )
        if _remote_exists(repo_root_path, "origin"):
            result = _git(
                repo_root_path,
                ["push", "origin", "--delete", state_report["integration_branch"]],
            )
            if result.returncode == 0:
                removed.append(f"remote-branch:{state_report['integration_branch']}")
            elif "remote ref does not exist" not in result.stderr.casefold():
                errors.append(
                    f"remote-branch:{state_report['integration_branch']}: "
                    f"{result.stderr.strip()}"
                )
    elif integration_worktree.is_dir():
        state_report = {**state_report, "workflow_git_state": state_data}
        recovery = _restore_workflow_git_state(
            state_report,
            integration_worktree,
            proposed_pr_id,
        )
        removed.extend(recovery["removed"])
        errors.extend(recovery["errors"])

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
            state_report["integration_branch_exists"] and checkpoint_valid
        ),
    }


def _find_workflow_git_state_in_checkout(
    repo_root: Path,
    proposed_pr_id: str,
    excluded_worktree: Path,
) -> WorkflowGitState | None:
    """Find the configured workflow identity outside its integration worktree."""
    workflows_directory = repo_root / "docs" / "workflows"
    if not workflows_directory.is_dir():
        return None
    filename = workflow_git_state_filename(proposed_pr_id)
    excluded_worktree = excluded_worktree.resolve()
    for path in sorted(workflows_directory.rglob(filename)):
        try:
            path.resolve().relative_to(excluded_worktree)
        except ValueError:
            pass
        else:
            continue
        try:
            state = WorkflowGitState.from_data(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
        except (OSError, yaml.YAMLError, ValueError):
            continue
        if state.proposed_pr_id == proposed_pr_id:
            return state
    return None


def _restore_workflow_git_state(
    report: Mapping[str, Any],
    integration_worktree: Path,
    proposed_pr_id: str,
) -> dict[str, list[str]]:
    """Restore committed workflow files to their initial generated version."""
    state_data = report.get("workflow_git_state")
    relative_directory = (
        state_data.get("workflow_relative_directory")
        if isinstance(state_data, dict)
        else None
    )
    if not isinstance(relative_directory, str) or not relative_directory:
        return {"removed": [], "errors": ["workflow state has no workflow directory"]}

    base_branch = (
        state_data.get("base_branch") if isinstance(state_data, dict) else None
    )
    source_ref: str | None = None
    if isinstance(base_branch, str) and base_branch:
        upstream_files = _git(
            integration_worktree,
            ["ls-tree", "-r", "--name-only", base_branch, "--", relative_directory],
        )
        if upstream_files.returncode == 0 and upstream_files.stdout.strip():
            source_ref = base_branch
    if source_ref is None:
        history = _git(
            integration_worktree,
            ["log", "--reverse", "--format=%H", "--", relative_directory],
        )
        source_ref = next(iter(history.stdout.splitlines()), None)
    if source_ref is None:
        return {
            "removed": [],
            "errors": [
                f"workflow:{proposed_pr_id}: could not identify initial generated state"
            ],
        }

    changed = _git(
        integration_worktree,
        ["diff", "--name-only", f"{source_ref}..HEAD", "--", relative_directory],
    )
    errors: list[str] = []
    for path in changed.stdout.splitlines():
        exists = _git(integration_worktree, ["cat-file", "-e", f"{source_ref}:{path}"])
        if exists.returncode != 0:
            removed_file = _git(integration_worktree, ["rm", "-f", "--", path])
            if removed_file.returncode != 0:
                errors.append(f"workflow-file:{path}: {removed_file.stderr.strip()}")
    if errors:
        return {"removed": [], "errors": errors}

    restore = _git(
        integration_worktree,
        [
            "restore",
            f"--source={source_ref}",
            "--staged",
            "--worktree",
            "--",
            relative_directory,
        ],
    )
    clean = _git(integration_worktree, ["clean", "-fd", "--", relative_directory])
    if restore.returncode != 0 or clean.returncode != 0:
        return {
            "removed": [],
            "errors": [
                "integration-worktree: could not restore initial workflow files: "
                + (restore.stderr.strip() or clean.stderr.strip())
            ],
        }

    save_workflow_git_state(
        integration_worktree / relative_directory,
        WorkflowGitState.from_data(state_data),
    )

    status = _git(integration_worktree, ["status", "--porcelain"])
    if status.stdout.strip():
        commit = _git(
            integration_worktree,
            ["commit", "-m", f"Reset workflow progress: {proposed_pr_id}"],
        )
        if commit.returncode != 0:
            return {
                "removed": [],
                "errors": [f"integration-branch: {commit.stderr.strip()}"],
            }
        push = _git(
            integration_worktree,
            ["push", "--set-upstream", "origin", report["integration_branch"]],
        )
        if push.returncode != 0:
            return {
                "removed": [],
                "errors": [f"integration-branch: {push.stderr.strip()}"],
            }

    return {
        "removed": [f"reset-workflow-state:{relative_directory}:{source_ref}"],
        "errors": [],
    }


def _restore_interrupted_workflow_checkpoint(
    repo_root: Path,
    report: Mapping[str, Any],
    integration_worktree: Path,
) -> dict[str, list[str]]:
    """Roll back committed task claims before removing their stale artifacts."""
    locked_tasks = [
        item
        for item in report.get("tasks", [])
        if isinstance(item, dict) and item.get("status") == "locked"
    ]
    if not locked_tasks:
        return {"removed": [], "errors": []}

    checkpoints: list[str] = []
    errors: list[str] = []
    for task in locked_tasks:
        task_path_value = task.get("path")
        task_id = task.get("task_id", "<unknown>")
        if not isinstance(task_path_value, str):
            errors.append(f"task:{task_id}: missing task path")
            continue
        task_path = Path(task_path_value)
        try:
            relative_path = task_path.relative_to(integration_worktree)
        except ValueError:
            errors.append(f"task:{task_id}: task path is outside integration worktree")
            continue
        history = _git(
            integration_worktree,
            ["log", "--format=%H", "--", str(relative_path)],
        )
        checkpoint: str | None = None
        for commit in history.stdout.splitlines():
            current = _git(
                integration_worktree,
                ["show", f"{commit}:{relative_path}"],
            )
            if current.returncode != 0:
                continue
            try:
                current_data = yaml.safe_load(current.stdout)
            except yaml.YAMLError:
                continue
            if (
                not isinstance(current_data, dict)
                or current_data.get("status") != "locked"
            ):
                continue
            parent = _git(
                integration_worktree,
                ["rev-parse", f"{commit}^"],
            )
            if parent.returncode != 0:
                continue
            previous = _git(
                integration_worktree,
                ["show", f"{parent.stdout.strip()}:{relative_path}"],
            )
            try:
                previous_data = yaml.safe_load(previous.stdout)
            except yaml.YAMLError:
                previous_data = None
            if (
                isinstance(previous_data, dict)
                and previous_data.get("status") != "locked"
            ):
                checkpoint = parent.stdout.strip()
                break
        if checkpoint is None:
            errors.append(
                f"task:{task_id}: could not identify the last consistent checkpoint"
            )
        else:
            checkpoints.append(checkpoint)

    if errors or not checkpoints:
        return {"removed": [], "errors": errors}
    checkpoint = checkpoints[0]
    reset = _git(integration_worktree, ["reset", "--hard", checkpoint])
    if reset.returncode != 0:
        return {
            "removed": [],
            "errors": [
                "integration-worktree: could not restore interrupted checkpoint: "
                + (reset.stderr.strip() or reset.stdout.strip())
            ],
        }
    push = _git(
        integration_worktree,
        [
            "push",
            "--force-with-lease",
            "origin",
            report["integration_branch"],
        ],
    )
    if push.returncode != 0:
        return {
            "removed": [],
            "errors": [
                "integration-branch: could not publish restored checkpoint: "
                + (push.stderr.strip() or push.stdout.strip())
            ],
        }
    return {
        "removed": [
            f"restored-interrupted-checkpoint:{report['integration_branch']}:{checkpoint}"
        ],
        "errors": [],
    }


def validate_workflow_git_state(
    repo_root: str | Path,
    state: WorkflowGitState,
    task_id: str,
) -> dict[str, Any]:
    """Return actionable consistency errors before a task mutates Git state.

    Tasks in the current execution model all run on the workflow integration
    branch.  The task id remains part of the diagnostic and claim identity,
    but it must not imply a task branch or task worktree.
    """
    report = inspect_workflow_run(repo_root, state.proposed_pr_id)
    errors = list(report["inconsistencies"])
    if not report["integration_branch_exists"]:
        errors.append(f"integration branch {state.integration_branch!r} does not exist")
    integration_worktrees = [
        item
        for item in report["worktrees"]
        if item.get("branch") == state.integration_branch
    ]
    if report["integration_worktree_exists"] and not integration_worktrees:
        errors.append(
            f"integration worktree for {state.integration_branch!r} is not "
            "registered with Git"
        )
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
        raise PowdrrExecutionError(result.stderr.strip() or "git for-each-ref failed")
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
        raise PowdrrExecutionError(result.stderr.strip() or "git worktree list failed")
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
            "integration branch exists but its <workflow-id>-workflow.yaml "
            "metadata file is missing or invalid"
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
        raise PowdrrExecutionError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()
