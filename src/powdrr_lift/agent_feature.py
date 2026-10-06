"""Agent-owned lifecycle for bounded headless feature tasks."""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from powdrr_lift.agent_bootstrap import _run_output_root
from powdrr_lift.agent_runtime import (
    AgentWorktree,
    commit_paths,
    prepare_agent_worktree,
    publish_pull_request,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.feature_endpoint import (
    FeatureEndpointConfig,
    FeatureEndpointResult,
    _write_run_result,
    run_feature_endpoint,
)
from powdrr_lift.workrr.run_artifacts import write_json_artifact


@dataclass(frozen=True, slots=True)
class AgentFeatureTaskResult:
    status: str
    task: AgentWorktree | None
    feature: FeatureEndpointResult | None
    report_json_path: Path
    report_markdown_path: Path
    pull_request_url: str | None = None
    error: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "branch": self.task.branch if self.task else None,
            "worktree": str(self.task.worktree) if self.task else None,
            "starting_commit": self.task.starting_commit if self.task else None,
            "pull_request_url": self.pull_request_url,
            "report_json_path": str(self.report_json_path),
            "report_markdown_path": str(self.report_markdown_path),
            "error": self.error,
            "feature": self.feature.to_data() if self.feature else None,
        }


def run_agent_feature_task(
    config: FeatureEndpointConfig,
    *,
    base_ref: str = "HEAD",
    remote: str = "origin",
    base_branch: str | None = None,
    open_pr: bool = False,
    run_id: str | None = None,
    output_root: Path | None = None,
    preflight: dict[str, Any] | None = None,
    runner: Any = subprocess.run,
) -> AgentFeatureTaskResult:
    """Prepare an isolated task tree, run feature gates, and publish on request."""
    repo_root = config.repo_root.resolve()
    attempt_id = run_id or uuid.uuid4().hex[:12]
    task: AgentWorktree | None = None
    failure: str | None = None
    selected_output = output_root or _run_output_root(runner, repo_root, attempt_id)
    result: FeatureEndpointResult | None = None
    pull_request_url: str | None = None
    status = "failed"
    try:
        task = prepare_agent_worktree(
            repo_root,
            config.work_item_name,
            base_ref=base_ref,
            remote=remote,
            base_branch=base_branch,
            publish=open_pr,
            run_id=attempt_id,
            runner=runner,
        )
        selected_output.mkdir(parents=True, exist_ok=True)
        task_config = replace(
            config,
            repo_root=repo_root,
            output_root=selected_output,
            base_branch=task.base_branch or "main",
            open_pr=False,
            push_changes=False,
            uncertainty_policy="normative_default",
            benchmark_mode=False,
            cleanup_temporary_artifacts=True,
            prepared_worktree=task.worktree,
            prepared_branch=task.branch,
            agent_managed_git=True,
        )
        result = run_feature_endpoint(task_config, runner=runner)
        if result.status != "completed" or result.review.get("passed") is not True:
            failure = (
                result.failure.message
                if result.failure
                else (f"Feature workflow ended with status {result.status}.")
            )
            status = "failed"
        else:
            paths = _changed_paths(runner, task.worktree)
            if paths:
                commit_paths(
                    task.worktree,
                    paths,
                    f"Implement {config.work_item_name}",
                    runner=runner,
                )
            has_net_changes = _has_net_changes(
                runner, task.worktree, task.starting_commit
            )
            if not has_net_changes:
                status = "no_op"
            elif open_pr:
                pull_request_url = publish_pull_request(
                    task,
                    title=config.work_item_name,
                    body=_pull_request_body(result, selected_output),
                    runner=runner,
                )
                status = "pr_opened"
            else:
                status = "completed_local"
            result = replace(
                result,
                status=(
                    "pr_opened"
                    if status == "pr_opened"
                    else "no_op"
                    if status == "no_op"
                    else "completed"
                ),
                pull_request_url=pull_request_url,
                publication={
                    "branch_push": "pushed"
                    if open_pr and status == "pr_opened"
                    else "not_requested",
                    "pull_request": "opened"
                    if status == "pr_opened"
                    else "not_requested",
                },
            )
    except KeyboardInterrupt:
        status = "interrupted"
        failure = "Headless feature execution was interrupted."
    except Exception as error:
        status = (
            "failed_publication"
            if task is not None and open_pr and result
            else "failed"
        )
        failure = f"{type(error).__name__}: {error}"

    if result is None:
        selected_output.mkdir(parents=True, exist_ok=True)
        result_data: dict[str, Any] = {
            "status": status,
            "task_id": config.task_id or config.work_item_name,
            "request": config.feature_description,
            "branch": task.branch if task else None,
            "worktree": str(task.worktree) if task else None,
            "starting_commit": task.starting_commit if task else None,
            "allowed_paths": list(config.allowed_paths),
            "uncertainty_decisions": [],
            "publication": {"status": "failed" if open_pr else "not_requested"},
            "operational_failures": [{"stage": "initialization", "message": failure}],
        }
        write_json_artifact(selected_output, "run-result.json", result_data)
    else:
        result = _write_run_result(
            selected_output,
            replace(
                result,
                status=status,
                publication={
                    **(result.publication or {}),
                    "status": status,
                    "pull_request_url": pull_request_url,
                },
            ),
        )
        result_data = result.to_data()
    metadata_path = selected_output / "run-metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    metadata.update(
        {
            "request": config.feature_description,
            "allowed_paths": list(config.allowed_paths),
            "submission_base": task.starting_commit if task else None,
            "branch": task.branch if task else None,
            "worktree": str(task.worktree) if task else None,
            "publication_requested": open_pr,
            "preflight": preflight or {"passed": True, "checks": []},
        }
    )
    write_json_artifact(selected_output, "run-metadata.json", metadata)
    if failure:
        write_json_artifact(
            selected_output,
            "failure.json",
            {
                "stage": "publication"
                if status == "failed_publication"
                else "execution",
                "message": failure,
            },
        )
    report_json, report_markdown = _write_task_report(
        selected_output, result_data, metadata
    )
    return AgentFeatureTaskResult(
        status,
        task,
        result,
        report_json,
        report_markdown,
        pull_request_url,
        failure,
    )


def _changed_paths(runner: Any, worktree: Path) -> tuple[str, ...]:
    result = runner(
        [
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        ],
        cwd=worktree,
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0:
        raise PowdrrExecutionError("Could not enumerate feature changes.")
    output = result.stdout
    if isinstance(output, str):
        output = output.encode()
    paths = []
    for record in output.split(b"\0"):
        if record:
            paths.append(record[3:].decode("utf-8", errors="surrogateescape"))
    return tuple(sorted(set(paths)))


def _has_net_changes(runner: Any, worktree: Path, base: str) -> bool:
    result = runner(
        ["git", "diff", "--quiet", base, "HEAD", "--"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise PowdrrExecutionError("Could not inspect the final task diff.")
    return result.returncode == 1


def _pull_request_body(result: FeatureEndpointResult, output_root: Path) -> str:
    decisions_path = output_root / "uncertainty-decisions.json"
    try:
        decisions_doc = json.loads(decisions_path.read_text(encoding="utf-8"))
        decisions = decisions_doc.get("decisions", [])
    except (OSError, json.JSONDecodeError):
        decisions = []
    decision_lines = [
        f"- **{item.get('uncertainty', item.get('id', 'Unspecified uncertainty'))}:** "
        f"{item.get('selected_default', item.get('default', 'default unavailable'))}"
        for item in decisions
        if isinstance(item, dict)
    ]
    validation = result.validation.to_data() if result.validation else None
    validation_status = (
        validation.get("status", "unavailable") if validation else "unavailable"
    )
    return "\n".join(
        [
            "## Headless implementation summary",
            "",
            (
                f"Implemented **{result.task_id or result.branch}** through the "
                "reviewed feature workflow."
            ),
            "",
            "## Validation",
            "",
            (
                f"Validation status: `{validation_status}`. Review passed: "
                f"`{result.review.get('passed')}`."
            ),
            "",
            "## Uncertainty decisions",
            "",
            *(decision_lines or ["No uncertainty defaults were recorded."]),
            "",
            (
                f"Full local reports: `{output_root / 'report.json'}` and "
                f"`{output_root / 'report.md'}`. The uncertainty decisions and "
                "validation summary are included above."
            ),
        ]
    )


def _write_task_report(
    output_root: Path, result: dict[str, Any], metadata: dict[str, Any]
) -> tuple[Path, Path]:
    from powdrr_lift.workrr.run_artifacts import write_run_report

    return write_run_report(output_root, result=result, metadata=metadata)


__all__ = ["AgentFeatureTaskResult", "run_agent_feature_task"]
