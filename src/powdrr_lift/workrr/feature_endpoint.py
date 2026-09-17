"""One-shot Workrr orchestration for a planned feature handoff."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_parser import parse_change_log
from powdrr_lift.change_log_template import create_change_log_template
from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentAttemptStore,
    CodingAgentRunner,
    CodingAgentStatus,
    ImplementationRequest,
    OpenCodePermissionPolicy,
    OpenCodeProvider,
)
from powdrr_lift.workrr.coding_agent_validation import (
    ValidationProfile,
    ValidationReport,
    ValidationReportStatus,
    ValidationRunner,
)
from powdrr_lift.workrr.git import integration_branch_name, slugify_workflow_id
from procedrr import parse_and_validate

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class FeatureEndpointConfig:
    feature_description: str
    work_item_name: str
    repo_root: Path
    allowed_paths: tuple[str, ...]
    validation_command: tuple[str, ...]
    base_branch: str = "main"
    opencode_executable: str = "opencode"
    opencode_model: str = "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731"
    output_root: Path | None = None
    open_pr: bool = True


@dataclass(frozen=True, slots=True)
class FeatureEndpointResult:
    status: str
    branch: str
    worktree: Path
    baseline_path: Path
    plan_path: Path
    request_path: Path
    attempt: CodingAgentAttempt | None
    validation: ValidationReport | None
    review: dict[str, Any]
    pull_request_url: str | None = None
    changelog_path: Path | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "baseline_path": str(self.baseline_path),
            "plan_path": str(self.plan_path),
            "request_path": str(self.request_path),
            "attempt": self.attempt.to_data() if self.attempt else None,
            "validation": self.validation.to_data() if self.validation else None,
            "review": self.review,
            "pull_request_url": self.pull_request_url,
            "changelog_path": str(self.changelog_path) if self.changelog_path else None,
        }


def run_feature_endpoint(
    config: FeatureEndpointConfig,
    *,
    runner: Runner = subprocess.run,
) -> FeatureEndpointResult:
    """Plan, implement, review, and optionally open one feature PR."""
    if not config.feature_description.strip():
        raise ValueError("feature_description must not be empty")
    if not config.allowed_paths:
        raise ValueError("at least one allowed path is required")
    root = config.repo_root.resolve()
    slug = slugify_workflow_id(config.work_item_name)
    branch = integration_branch_name(config.work_item_name)
    worktree = root / ".worktrees" / "powdrr" / slug
    output_root = (
        config.output_root or root / ".powdrr" / "feature-runs" / slug
    ).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    _require_clean_root(root, runner)
    _run(runner, root, ["git", "fetch", "origin", config.base_branch])
    _run(
        runner,
        root,
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            f"origin/{config.base_branch}",
        ],
    )
    try:
        baseline_path = _resolve_current_baseline(worktree, runner)
        plan_path = _write_structrr_plan(worktree, config)
        procedrr_path = _validate_procedrr_flow(worktree)
        _commit(runner, worktree, "Record Structrr feature diff")

        base_commit = _git_output(runner, worktree, ["git", "rev-parse", "HEAD"])
        plan = ExecutionPlan(
            plan_id=f"{slug}-execution",
            proposed_pr_fingerprint=f"{slug}-v1",
            units=(
                ExecutionUnit(
                    unit_id=f"implement-{slug}",
                    objective=config.feature_description,
                    paths=config.allowed_paths,
                    validation_profiles=("feature-validation",),
                    acceptance_criteria=(
                        "The requested feature behavior is implemented.",
                        "Only the declared implementation paths are changed.",
                    ),
                ),
            ),
            allowed_paths=config.allowed_paths,
        )
        request = ImplementationRequest.from_execution_plan(
            plan,
            unit_id=f"implement-{slug}",
            request_id=f"{slug}-implementation",
            base_commit=base_commit,
            context_refs=(
                f"structrr:{baseline_path.relative_to(worktree)}",
                f"structrr-diff:{plan_path.relative_to(worktree)}",
                f"procedrr:{procedrr_path.relative_to(worktree)}",
            ),
            allowed_commands=(" ".join(config.validation_command) + " *",),
        )
        request_path = output_root / "implementation-request.json"
        request_path.write_text(request.to_json(), encoding="utf-8")
        attempt_store = CodingAgentAttemptStore(output_root / "artifacts")
        attempt = CodingAgentRunner(
            provider=OpenCodeProvider(
                executable=config.opencode_executable,
                model=config.opencode_model,
                permission_policy=OpenCodePermissionPolicy(
                    (" ".join(config.validation_command) + " *",)
                ),
            ),
            store=attempt_store,
        ).run(
            request,
            worktree_root=worktree,
            attempt_id=f"{slug}-attempt",
        )
        validation = ValidationRunner(
            {
                "feature-validation": ValidationProfile(
                    "feature-validation", config.validation_command
                )
            }
        ).run(request, attempt, worktree_root=worktree)
        attempt_store.save_validation_report(validation)
        review = review_feature_diff(
            worktree, request, attempt, validation=validation, runner=runner
        )
        if not review["passed"]:
            return FeatureEndpointResult(
                "review_failed",
                branch,
                worktree,
                baseline_path,
                plan_path,
                request_path,
                attempt,
                validation,
                review,
            )
        _commit(runner, worktree, f"Implement {config.work_item_name}")
        _run(runner, worktree, ["git", "push", "--set-upstream", "origin", branch])
        pull_request_url = (
            _open_pull_request(runner, worktree, config, branch, plan_path)
            if config.open_pr
            else None
        )
        changelog_path = None
        if pull_request_url:
            changelog_path = _create_pr_changelog(
                runner, worktree, branch, pull_request_url, config
            )
        return FeatureEndpointResult(
            "pr_opened" if pull_request_url else "completed",
            branch,
            worktree,
            baseline_path,
            plan_path,
            request_path,
            attempt,
            validation,
            review,
            pull_request_url,
            changelog_path,
        )

    except Exception:
        raise


def review_feature_diff(
    worktree: Path,
    request: ImplementationRequest,
    attempt: CodingAgentAttempt,
    validation: ValidationReport,
    *,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Review the worker result before any commit or PR operation."""
    changed = tuple(
        _git_output(runner, worktree, ["git", "diff", "--name-only"]).splitlines()
    )
    allowed = set(request.allowed_paths)
    out_of_scope = tuple(path for path in changed if path not in allowed)
    diff_check = _run(runner, worktree, ["git", "diff", "--check"])
    passed = (
        attempt.status is CodingAgentStatus.COMPLETED
        and validation.status is ValidationReportStatus.PASSED
        and bool(changed)
        and not out_of_scope
        and diff_check.returncode == 0
    )
    return {
        "passed": passed,
        "changed_paths": list(changed),
        "out_of_scope_paths": list(out_of_scope),
        "diff_check_returncode": diff_check.returncode,
        "worker_status": attempt.status.value,
        "validation_status": validation.status.value,
        "validation_error": validation.error,
    }


def _write_structrr_plan(worktree: Path, config: FeatureEndpointConfig) -> Path:
    slug = slugify_workflow_id(config.work_item_name)
    proposal = worktree / "docs" / "proposals" / slug
    proposal.mkdir(parents=True, exist_ok=True)
    path = proposal / "structrr-diff.yaml"
    document = {
        "schema": "https://powdrr.io/schema/changelog-v2",
        "change_id": slug,
        "title": config.work_item_name,
        "intent": {
            "problem": "The requested product behavior is not yet available.",
            "goal": config.feature_description,
        },
        "human-decisions": [],
        "files": [],
        "entities": [{"id": slug, "type": "Feature", "action": "added"}],
        "entity_relationships": [],
        "features": [
            {
                "id": slug,
                "description": config.feature_description,
                "action": "added",
            }
        ],
        "invariants": [],
        "guidance": [],
        "proposed_prs": [],
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    parse_change_log(path.read_text(encoding="utf-8"))
    return path


def _validate_procedrr_flow(worktree: Path) -> Path:
    path = (
        worktree / "docs" / "procedrr" / "skill-definitions" / "implement-feature.yaml"
    )
    try:
        parse_and_validate(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PowdrrExecutionError(
            f"Shared feature Procedrr definition is invalid: {path}: {error}"
        ) from error
    return path


def _resolve_current_baseline(worktree: Path, runner: Runner) -> Path:
    """Resolve an existing baseline; endpoint execution never bootstraps one."""
    relative_paths = _git_output(
        runner,
        worktree,
        ["git", "ls-files", "docs/structrr/current/baseline-*.yaml"],
    ).splitlines()
    if not relative_paths:
        raise PowdrrExecutionError(
            "No Structrr baseline exists under docs/structrr/current. "
            "Bootstrap Structrr before running a feature endpoint."
        )
    ranked: list[tuple[int, str]] = []
    for relative_path in relative_paths:
        timestamp = _git_output(
            runner,
            worktree,
            ["git", "log", "-1", "--format=%ct", "--", relative_path],
        )
        ranked.append((int(timestamp or "0"), relative_path))
    _, selected = max(ranked)
    return worktree / selected


def _require_clean_root(root: Path, runner: Runner) -> None:
    result = _run(runner, root, ["git", "status", "--porcelain"])
    if result.stdout.strip():
        raise PowdrrExecutionError("Feature endpoint requires a clean caller worktree.")


def _commit(runner: Runner, worktree: Path, message: str) -> None:
    _run(runner, worktree, ["git", "add", "docs"])
    _run(runner, worktree, ["git", "commit", "-am", message])


def _create_pr_changelog(
    runner: Runner,
    worktree: Path,
    branch: str,
    pull_request_url: str,
    config: FeatureEndpointConfig,
) -> Path:
    match = re.search(r"/pull/(\d+)", pull_request_url)
    if match is None:
        raise PowdrrExecutionError(
            f"Could not determine the pull request number from {pull_request_url!r}."
        )
    pr_number = match.group(1)
    path = worktree / "docs" / "changelogs" / f"PR-{pr_number}-changelog.yaml"
    create_change_log_template(
        branch_name=branch,
        output_path=path,
        repo_root=worktree,
        default_branch=config.base_branch,
    )
    _commit(runner, worktree, f"Add PR-{pr_number} changelog descriptor")
    _run(runner, worktree, ["git", "push", "origin", branch])
    _update_pull_request_description(
        runner, worktree, pull_request_url, config, path.relative_to(worktree)
    )
    return path


def _update_pull_request_description(
    runner: Runner,
    worktree: Path,
    pull_request_url: str,
    config: FeatureEndpointConfig,
    changelog_relative_path: Path,
) -> None:
    body = (
        f"## Feature\n\n{config.feature_description}\n\n"
        f"Structrr plan: `docs/proposals/{slugify_workflow_id(config.work_item_name)}"
        "/structrr-diff.yaml`\n"
        f"PR changelog descriptor: `{changelog_relative_path}`\n"
        "Implemented by the bounded Workrr/OpenCode handoff and reviewed before commit."
    )
    _run(
        runner,
        worktree,
        ["gh", "pr", "edit", pull_request_url, "--body", body],
    )


def _open_pull_request(
    runner: Runner,
    worktree: Path,
    config: FeatureEndpointConfig,
    branch: str,
    plan_path: Path,
) -> str:
    body = (
        f"## Feature\n\n{config.feature_description}\n\n"
        f"Structrr plan: `{plan_path.relative_to(worktree)}`\n"
        "Implemented by the bounded Workrr/OpenCode handoff and reviewed before commit."
    )
    result = _run(
        runner,
        worktree,
        [
            "gh",
            "pr",
            "create",
            "--base",
            config.base_branch,
            "--head",
            branch,
            "--title",
            config.work_item_name,
            "--body",
            body,
        ],
    )
    return result.stdout.strip().splitlines()[-1]


def _run(
    runner: Runner, cwd: Path, command: list[str]
) -> subprocess.CompletedProcess[str]:
    result = runner(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PowdrrExecutionError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result


def _git_output(runner: Runner, cwd: Path, command: list[str]) -> str:
    return _run(runner, cwd, command).stdout.strip()


__all__ = [
    "FeatureEndpointConfig",
    "FeatureEndpointResult",
    "review_feature_diff",
    "run_feature_endpoint",
]
