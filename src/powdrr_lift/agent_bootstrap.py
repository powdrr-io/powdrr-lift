"""Agent-owned bootstrap task lifecycle."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from powdrr_lift.agent_runtime import (
    AgentWorktree,
    commit_paths,
    prepare_agent_worktree,
    publish_pull_request,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.structrr.bootstrap import BootstrapResult, bootstrap_structrr
from powdrr_lift.workrr.run_artifacts import (
    write_json_artifact,
    write_run_report,
)


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
        if not has_commit:
            status = "no_op"
        elif config.open_pr:
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
            failure_stage=None,
            taxonomy_provenance=taxonomy_provenance,
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
        "validation": (
            result.bootstrap.validation.__dict__
            if result.bootstrap and hasattr(result.bootstrap.validation, "__dict__")
            else {
                "successful": bool(
                    result.bootstrap and result.bootstrap.validation.successful
                ),
                "issues": [
                    {"code": issue.code, "message": issue.message}
                    for issue in result.bootstrap.validation.issues
                ]
                if result.bootstrap
                else [],
            }
        ),
        "publication": {
            "status": "opened"
            if result.pull_request_url
            else "not_requested"
            if result.status == "completed_local"
            else "no_op"
            if result.status == "no_op"
            else "failed"
            if result.status == "failed"
            else "not_reached",
            "pull_request_url": result.pull_request_url,
        },
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
