"""Agent-owned bootstrap task lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.agent_runtime import (
    AgentWorktree,
    commit_paths,
    prepare_agent_worktree,
    publish_pull_request,
)
from powdrr_lift.core.entity_taxonomy import load_entity_taxonomy
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.structrr.bootstrap import (
    BootstrapResult,
    bootstrap_structrr,
    validate_bootstrap_document,
)
from powdrr_lift.workrr.run_artifacts import (
    write_json_artifact,
    write_run_report,
)

_VALIDATION_BUDGET_SECONDS = 1800.0
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class BootstrapTaskConfig:
    repo_root: Path
    task_name: str = "repository-bootstrap"
    base_ref: str = "HEAD"
    remote: str = "origin"
    base_branch: str | None = None
    open_pr: bool = False
    output_root: Path | None = None
    taxonomy_path: Path = Path("software_development_entity_taxonomy.md")
    run_id: str | None = None
    validation_timeout_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class BootstrapTaskResult:
    status: str
    task: AgentWorktree | None
    bootstrap: BootstrapResult | None
    changed_paths: tuple[str, ...]
    pull_request_url: str | None
    report_json_path: Path | None
    report_markdown_path: Path | None
    error: str | None = None
    failure_stage: str | None = None
    taxonomy_provenance: str | None = None
    validation_status: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "branch": self.task.branch if self.task else None,
            "worktree": str(self.task.worktree) if self.task else None,
            "starting_commit": self.task.starting_commit if self.task else None,
            "base_branch": self.task.base_branch if self.task else None,
            "bootstrap_path": (
                str(self.bootstrap.output_path) if self.bootstrap else None
            ),
            "manifest_path": (
                str(self.bootstrap.manifest_path) if self.bootstrap else None
            ),
            "changed_paths": list(self.changed_paths),
            "pull_request_url": self.pull_request_url,
            "report_json_path": (
                str(self.report_json_path) if self.report_json_path else None
            ),
            "report_markdown_path": (
                str(self.report_markdown_path) if self.report_markdown_path else None
            ),
            "error": self.error,
            "failure_stage": self.failure_stage,
            "taxonomy_provenance": self.taxonomy_provenance,
            "validation_status": self.validation_status,
        }


def run_bootstrap_task(
    config: BootstrapTaskConfig,
    *,
    runner: Any = subprocess.run,
) -> BootstrapTaskResult:
    """Generate, validate, and optionally publish onboarding artifacts."""
    repo_root = config.repo_root.resolve()
    task: AgentWorktree | None = None
    bootstrap: BootstrapResult | None = None
    changed_paths: tuple[str, ...] = ()
    pull_request_url: str | None = None
    output_root: Path | None = config.output_root
    failure_stage = "initialization"
    taxonomy_provenance: str | None = None
    validation_status: str | None = None
    try:
        task = prepare_agent_worktree(
            repo_root,
            config.task_name,
            base_ref=config.base_ref,
            remote=config.remote,
            base_branch=config.base_branch,
            publish=config.open_pr,
            run_id=config.run_id,
            runner=runner,
        )
        run_id = task.worktree.name
        output_root = output_root or _run_output_root(runner, repo_root, run_id)
        output_root.mkdir(parents=True, exist_ok=True)
        failure_stage = "bootstrap"
        selected_taxonomy = config.taxonomy_path
        copied_taxonomy = False
        if not selected_taxonomy.is_absolute():
            selected_taxonomy = task.worktree / selected_taxonomy
        if not selected_taxonomy.is_file():
            if config.taxonomy_path != Path("software_development_entity_taxonomy.md"):
                raise PowdrrExecutionError(
                    f"Requested taxonomy file does not exist: {selected_taxonomy}"
                )
            packaged_taxonomy = (
                files("powdrr_lift.resources")
                .joinpath("software_development_entity_taxonomy.md")
                .read_text(encoding="utf-8")
            )
            selected_taxonomy.write_text(packaged_taxonomy, encoding="utf-8")
            copied_taxonomy = True
        taxonomy_provenance = (
            "packaged_default" if copied_taxonomy else "project_taxonomy"
        )
        snapshot_path = task.worktree / "docs/structrr/current/baseline-bootstrap.yaml"
        manifest_path = snapshot_path.with_suffix(".manifest.json")
        previous_snapshot = (
            snapshot_path.read_bytes() if snapshot_path.is_file() else None
        )
        previous_manifest = (
            manifest_path.read_bytes() if manifest_path.is_file() else None
        )
        previous_manifest_data: Any = None
        if previous_manifest is not None:
            try:
                previous_manifest_data = json.loads(previous_manifest)
            except json.JSONDecodeError:
                previous_manifest_data = None
        bootstrap = bootstrap_structrr(
            task.worktree,
            output_path=Path("docs/structrr/current/baseline-bootstrap.yaml"),
            title=f"Bootstrap Structrr for {repo_root.name}",
            taxonomy_path=selected_taxonomy,
            include_untracked=copied_taxonomy,
            repository_name=repo_root.name,
        )
        if not bootstrap.validation.successful:
            issues = [
                f"{issue.code}: {issue.message}"
                for issue in bootstrap.validation.issues
            ]
            raise PowdrrExecutionError(
                "Structrr bootstrap validation failed: " + "; ".join(issues)
            )
        baseline_result = _run_validation_baseline(
            task.worktree,
            bootstrap.document,
            runner=runner,
            timeout_seconds=config.validation_timeout_seconds,
        )
        validation_status = str(baseline_result["status"])
        if baseline_result["checks"]:
            report = validate_bootstrap_document(
                bootstrap.document,
                root=task.worktree,
                taxonomy=load_entity_taxonomy(task.worktree, selected_taxonomy),
            )
            if not report.successful:
                issues = [f"{issue.code}: {issue.message}" for issue in report.issues]
                raise PowdrrExecutionError(
                    "Validation results made the Structrr snapshot invalid: "
                    + "; ".join(issues)
                )
            bootstrap = replace(bootstrap, validation=report)
            bootstrap.output_path.write_text(
                yaml.safe_dump(
                    bootstrap.document, sort_keys=False, allow_unicode=False
                ),
                encoding="utf-8",
            )
        if (
            previous_snapshot is not None
            and previous_snapshot == bootstrap.output_path.read_bytes()
            and previous_manifest is not None
            and isinstance(previous_manifest_data, dict)
            and bootstrap.source_manifest is not None
            and previous_manifest_data.get("product_digest")
            == bootstrap.source_manifest.product_digest
        ):
            manifest_path.write_bytes(previous_manifest)
        candidate_paths = [bootstrap.output_path, bootstrap.manifest_path]
        if copied_taxonomy:
            candidate_paths.append(selected_taxonomy)
        changed_paths = tuple(
            sorted(
                path.relative_to(task.worktree).as_posix()
                for path in candidate_paths
                if path is not None
            )
        )
        has_commit = commit_paths(
            task.worktree,
            changed_paths,
            "Bootstrap repository structure",
            runner=runner,
        )
        if validation_status in {
            "failed",
            "blocked",
            "timed_out",
        }:
            status = "validation_failed"
            failure_stage = "validation"
        elif config.open_pr and validation_status != "passed":
            status = "validation_incomplete"
            failure_stage = "validation"
        elif config.open_pr and has_commit:
            failure_stage = "publication"
            pull_request_url = publish_pull_request(
                task,
                title=f"Bootstrap project context for {repo_root.name}",
                body=_render_bootstrap_pr_body(
                    bootstrap, changed_paths, copied_taxonomy
                ),
                runner=runner,
            )
            status = "pr_opened"
        elif validation_status == "no_checks":
            status = "completed_unverified"
            failure_stage = "validation"
        elif not has_commit:
            status = "no_op"
        else:
            status = "completed_local"
        result = BootstrapTaskResult(
            status,
            task,
            bootstrap,
            changed_paths,
            pull_request_url,
            None,
            None,
            failure_stage=(
                failure_stage
                if status
                in {
                    "validation_failed",
                    "validation_incomplete",
                    "completed_unverified",
                }
                else None
            ),
            taxonomy_provenance=taxonomy_provenance,
            validation_status=validation_status,
        )
        return _persist_result(
            result, output_root, publication_requested=config.open_pr
        )
    except Exception as error:
        if output_root is None:
            try:
                output_root = _run_output_root(
                    runner, repo_root, config.run_id or "bootstrap-failed"
                )
            except Exception:
                output_root = None
        result = BootstrapTaskResult(
            "failed",
            task,
            bootstrap,
            changed_paths,
            pull_request_url,
            None,
            None,
            f"{type(error).__name__}: {error}",
            failure_stage,
            taxonomy_provenance,
            validation_status,
        )
        return _persist_result(
            result, output_root, publication_requested=config.open_pr
        )


def _persist_result(
    result: BootstrapTaskResult,
    output_root: Path | None,
    *,
    publication_requested: bool,
) -> BootstrapTaskResult:
    if output_root is None:
        return result
    output_root.mkdir(parents=True, exist_ok=True)
    result = replace(
        result,
        report_json_path=output_root / "report.json",
        report_markdown_path=output_root / "report.md",
    )
    data = result.to_data()
    write_json_artifact(output_root, "run-result.json", data)
    metadata = {
        "schema_version": "powdrr-run-metadata-v1",
        "task_id": result.task.worktree.name if result.task else "bootstrap",
        "request": "Generate and validate a Structrr onboarding snapshot.",
        "submission_base": result.task.starting_commit if result.task else None,
        "allowed_paths": list(result.changed_paths),
        "publication_requested": publication_requested,
        "effective_profile": {
            "kind": "deterministic-bootstrap",
            "taxonomy_provenance": result.taxonomy_provenance,
        },
    }
    write_json_artifact(output_root, "run-metadata.json", metadata)
    if result.error:
        write_json_artifact(
            output_root,
            "failure.json",
            {
                "schema_version": "powdrr-run-failure-v1",
                "stage": result.failure_stage or "initialization",
                "error_type": "BootstrapTaskError",
                "message": result.error,
                "artifact_paths": list(result.changed_paths),
            },
        )
    report_result = {
        "status": result.status,
        "branch": data["branch"],
        "worktree": data["worktree"],
        "attempt": {"changed_paths": list(result.changed_paths)},
        "validation": _validation_report_data(result),
        "publication": {
            "status": "opened"
            if result.pull_request_url
            else "not_requested"
            if result.status == "completed_local"
            else "no_op"
            if result.status == "no_op"
            else "failed"
            if result.status
            in {
                "failed",
                "validation_failed",
                "validation_incomplete",
                "completed_unverified",
            }
            else "not_reached",
            "pull_request_url": result.pull_request_url,
        },
        "validation_status": result.validation_status,
    }
    report_json, report_markdown = write_run_report(
        output_root,
        result=report_result,
        metadata=metadata,
    )
    return replace(
        result,
        report_json_path=report_json,
        report_markdown_path=report_markdown,
    )


def _validation_report_data(result: BootstrapTaskResult) -> dict[str, Any]:
    bootstrap = result.bootstrap
    checks = bootstrap.document.get("validation_inventory", []) if bootstrap else []
    return {
        "snapshot_successful": bool(bootstrap and bootstrap.validation.successful),
        "snapshot_issues": [
            {"code": issue.code, "message": issue.message}
            for issue in bootstrap.validation.issues
        ]
        if bootstrap
        else [],
        "baseline_status": result.validation_status,
        "checks": [
            {
                "profile": check.get("profile"),
                "command": check.get("command", []),
                "status": check.get("baseline", {}).get("status")
                if isinstance(check.get("baseline"), dict)
                else None,
            }
            for check in checks
            if isinstance(check, dict)
        ]
        if isinstance(checks, list)
        else [],
    }


def _run_validation_baseline(
    worktree: Path,
    document: dict[str, Any],
    *,
    runner: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run discovered local argv checks and store bounded baseline outcomes."""
    inventory = document.get("validation_inventory", [])
    if not isinstance(inventory, list):
        return {"status": "blocked", "checks": 0}

    applicable = [
        check
        for check in inventory
        if isinstance(check, dict)
        and isinstance(check.get("applicability"), dict)
        and check["applicability"].get("local", True) is not False
    ]
    if not applicable:
        return {"status": "no_checks", "checks": 0}

    deadline = time.monotonic() + _VALIDATION_BUDGET_SECONDS
    statuses: list[str] = []
    for check in applicable:
        command = check.get("command", [])
        execution = check.get("execution", {})
        cwd_value = execution.get("cwd", ".") if isinstance(execution, dict) else "."
        status: str
        returncode: int | None = None
        stdout = ""
        stderr = ""
        error: str | None = None
        cwd = (worktree / str(cwd_value)).resolve()
        remaining_seconds = deadline - time.monotonic()
        try:
            cwd.relative_to(worktree.resolve())
        except ValueError:
            cwd = worktree
            status = "blocked"
            error = "validation working directory escapes the task worktree"
        else:
            if remaining_seconds <= 0:
                status = "timed_out"
                error = "aggregate validation time budget was exhausted"
            elif (
                not isinstance(command, list)
                or not command
                or not all(isinstance(item, str) and item for item in command)
                or not isinstance(execution, dict)
                or execution.get("kind") != "argv"
            ):
                status = "blocked"
                error = "validation check has no supported argv command"
            elif not cwd.is_dir():
                status = "blocked"
                error = f"validation working directory does not exist: {cwd_value}"
            else:
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not any(
                        marker in key.upper()
                        for marker in (
                            "TOKEN",
                            "KEY",
                            "SECRET",
                            "PASSWORD",
                            "CREDENTIAL",
                            "AUTH",
                            "PRIVATE_KEY",
                        )
                    )
                }
                environment.pop("VIRTUAL_ENV", None)
                environment.pop("PYTHONPATH", None)
                environment.pop("UV_NO_SYNC", None)
                environment.pop("UV_PROJECT_ENVIRONMENT", None)
                if command[0] == "uv":
                    environment["UV_PROJECT_ENVIRONMENT"] = str(worktree / ".venv")
                try:
                    completed = _run_validation_command(
                        runner,
                        command,
                        cwd=cwd,
                        env=environment,
                        timeout_seconds=min(timeout_seconds, remaining_seconds),
                    )
                    returncode = completed.returncode
                    stdout = completed.stdout or ""
                    stderr = completed.stderr or ""
                    status, error = _command_outcome(returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    status = "timed_out"
                    error = f"validation command exceeded {timeout_seconds:g}s"
                except FileNotFoundError as command_error:
                    status = "blocked"
                    error = str(command_error)
                except OSError as command_error:
                    status = "blocked"
                    error = str(command_error)

        observation = {
            "kind": "bootstrap_baseline",
            "status": status,
            "command": command if isinstance(command, list) else [],
            "returncode": returncode,
            "stdout_sha256": _output_digest(stdout),
            "stderr_sha256": _output_digest(stderr),
            "error": error,
        }
        check["confirmation"] = {
            "level": "execution",
            "observations": [observation],
        }
        check["baseline"] = {"status": status, "observation": observation}
        statuses.append(status)

    if "timed_out" in statuses:
        overall = "timed_out"
    elif "failed" in statuses:
        overall = "failed"
    elif "blocked" in statuses:
        overall = "blocked"
    else:
        overall = "passed"
    return {"status": overall, "checks": len(statuses)}


def _output_digest(value: object) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_validation_command(
    runner: Any,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    kwargs = {
        "cwd": cwd,
        "env": env,
        "capture_output": True,
        "text": True,
        "timeout": timeout_seconds,
        "check": False,
    }
    if runner is not subprocess.run or os.name != "posix":
        return runner(command, **kwargs)

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as timeout_error:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(
                timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=stdout or timeout_error.stdout,
            stderr=stderr or timeout_error.stderr,
        ) from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _command_outcome(
    returncode: int, stdout: str, stderr: str
) -> tuple[str, str | None]:
    if returncode == 0:
        return "passed", None
    output = f"{stdout}\n{stderr}".lower()
    if any(
        phrase in output
        for phrase in (
            "command not found",
            "executable not found",
            "no module named",
            "no such file or directory",
            "could not find",
            "failed to spawn",
            "not installed",
        )
    ):
        return "blocked", "a required validation tool or dependency is unavailable"
    return "failed", f"validation command exited with status {returncode}"


def _render_bootstrap_pr_body(
    result: BootstrapResult,
    changed_paths: tuple[str, ...],
    packaged_taxonomy: bool,
) -> str:
    manifest = result.source_manifest.to_data() if result.source_manifest else {}
    raw_checks = result.document.get("validation_inventory", [])
    checks = (
        [
            item
            for item in raw_checks
            if isinstance(item, dict) and isinstance(item.get("profile"), str)
        ]
        if isinstance(raw_checks, list)
        else []
    )
    checks_text = (
        "\n".join(
            f"- `{item['profile']}` (discovered from {item.get('source', 'unknown')})"
            for item in checks
        )
        if checks
        else "- No repository validation commands were discovered."
    )
    outcomes = [
        f"- `{item['profile']}`: `{item.get('baseline', {}).get('status', 'not_run')}`"
        for item in checks
    ]
    baseline_text = (
        "All discovered local checks passed.\n\n" + "\n".join(outcomes)
        if checks
        else "No local validation checks were discovered; bootstrap is unverified."
    )
    extraction_errors = (
        manifest.get("extraction_errors", []) if isinstance(manifest, dict) else []
    )
    coverage_complete = manifest.get("coverage_complete") is True
    limitations_text = (
        "\n".join(f"- {item}" for item in extraction_errors)
        if extraction_errors
        else "- File extraction was reported complete."
        if coverage_complete
        else "- Coverage completeness was unavailable."
    )
    return "\n".join(
        [
            "## Bootstrap summary",
            "",
            (
                f"Indexed {len(result.evidence_files)} tracked source and "
                "specification files."
            ),
            f"Validated {len(result.document['entities'])} entities, "
            f"{len(result.document['entity_relationships'])} relationships, and "
            f"{len(result.document['files'])} source anchors.",
            "",
            "## Taxonomy",
            "",
            "Added the packaged default taxonomy."
            if packaged_taxonomy
            else "Used the project taxonomy.",
            "",
            "## Coverage limitations",
            "",
            limitations_text,
            "",
            "## Validation/check discovery",
            "",
            checks_text,
            "",
            "Baseline execution:",
            "",
            baseline_text,
            "",
            "## Generated files",
            "",
            *(f"- `{path}`" for path in changed_paths),
            "",
            "Review and merge this onboarding PR before requesting implementation.",
            "",
            "## Defaults",
            "",
            "No product behavior defaults were introduced. The packaged taxonomy "
            "was used only because the project did not provide one."
            if packaged_taxonomy
            else "No product behavior or taxonomy defaults were introduced.",
            "",
            "The CLI prints the locations of the full local JSON and Markdown "
            "reports after the run.",
        ]
    )


def _run_output_root(runner: Any, repo_root: Path, run_id: str) -> Path:
    result = runner(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PowdrrExecutionError("Cannot locate Git storage for run artifacts.")
    return Path(result.stdout.strip()).resolve() / "powdrr-lift" / "runs" / run_id
