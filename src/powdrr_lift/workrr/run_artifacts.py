"""Stable metadata and failure artifacts for feature endpoint runs."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class RunFailureStage(StrEnum):
    INITIALIZATION = "initialization"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    VALIDATION = "validation"
    REVIEW = "review"
    PUBLICATION = "publication"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunFailure:
    stage: RunFailureStage
    error_type: str
    message: str
    clause_id: str | None = None
    obligation_id: str | None = None
    contract_id: str | None = None
    model_response_path: str | None = None
    validation_command: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "powdrr-run-failure-v1",
            "stage": self.stage.value,
            "error_type": self.error_type,
            "message": self.message,
            "clause_id": self.clause_id,
            "obligation_id": self.obligation_id,
            "contract_id": self.contract_id,
            "model_response_path": self.model_response_path,
            "validation_command": list(self.validation_command),
            "artifact_paths": list(self.artifact_paths),
        }


@dataclass(frozen=True, slots=True)
class RunReport:
    """Canonical, evidence-based summary of one feature endpoint attempt."""

    status: str
    task_id: str | None
    request: str | None
    starting_commit: str | None
    effective_profile: dict[str, Any]
    branch: str | None
    worktree: str | None
    allowed_paths: tuple[str, ...]
    change_summary: dict[str, Any]
    uncertainty_decisions: tuple[dict[str, Any], ...]
    validation: dict[str, Any] | None
    review_findings: tuple[Any, ...]
    operational_failures: tuple[dict[str, Any], ...]
    publication: dict[str, Any]
    usage: dict[str, Any]
    artifacts: dict[str, str]
    schema_version: str = "powdrr-run-report-v1"

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "task_id": self.task_id,
            "request": self.request,
            "starting_commit": self.starting_commit,
            "effective_profile": self.effective_profile,
            "branch": self.branch,
            "worktree": self.worktree,
            "allowed_paths": list(self.allowed_paths),
            "change_summary": self.change_summary,
            "uncertainty_decisions": list(self.uncertainty_decisions),
            "validation": self.validation,
            "review_findings": list(self.review_findings),
            "operational_failures": list(self.operational_failures),
            "publication": self.publication,
            "usage": self.usage,
            "artifacts": self.artifacts,
        }


def collect_run_metadata(
    *,
    task_id: str,
    repo_root: Path,
    output_root: Path,
    submission_base: str | None = None,
) -> dict[str, Any]:
    """Collect reproducibility metadata without making the run depend on Git."""
    try:
        distribution = importlib.metadata.distribution("powdrr-lift")
        powdrr_version = distribution.version
        direct_url = distribution.read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        powdrr_version = "editable"
        direct_url = None
    return {
        "schema_version": "powdrr-run-metadata-v1",
        "task_id": task_id,
        "powdrr_version": powdrr_version,
        "powdrr_install_spec": os.environ.get("POWDRR_INSTALL_SPEC"),
        "powdrr_revision": os.environ.get("POWDRR_REVISION"),
        "powdrr_direct_url": direct_url,
        "opencode_version": _command_version("opencode", repo_root),
        "repo_root": str(repo_root),
        "output_root": str(output_root),
        "submission_base": submission_base,
    }


def write_json_artifact(output_root: Path, name: str, value: Any) -> Path:
    path = output_root / name
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def write_run_report(
    output_root: Path,
    *,
    result: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Write canonical JSON and deterministic Markdown from persisted evidence."""
    metadata = metadata or _read_json_mapping(output_root / "run-metadata.json")
    decisions_doc = _read_json_mapping(output_root / "uncertainty-decisions.json")
    failure_doc = _read_json_mapping(output_root / "failure.json")
    decisions = decisions_doc.get("decisions", [])
    decision_records = (
        [dict(item) for item in decisions if isinstance(item, dict)]
        if isinstance(decisions, list)
        else []
    )
    for decision in decision_records:
        decision.setdefault("implementation_refs", {"status": "unavailable"})
        decision.setdefault("verification_refs", {"status": "unavailable"})
    failure = (failure_doc,) if failure_doc else ()
    review = result.get("review")
    review_map = review if isinstance(review, dict) else {}
    review_findings = _review_findings(review_map)
    validation = result.get("validation")
    validation_data = validation if isinstance(validation, dict) else None
    attempt = result.get("attempt")
    attempt_map = attempt if isinstance(attempt, dict) else {}
    request = metadata.get("request")
    allowed_paths = metadata.get("allowed_paths", [])
    if not isinstance(allowed_paths, list):
        allowed_paths = []
    result_status = str(result.get("status", "unknown"))
    publication_requested = metadata.get("publication_requested") is True
    pull_request_url = result.get("pull_request_url")
    reported_publication = result.get("publication")
    if isinstance(reported_publication, dict):
        publication = dict(reported_publication)
        publication.setdefault(
            "pull_request_url",
            pull_request_url if isinstance(pull_request_url, str) else None,
        )
    elif isinstance(pull_request_url, str) and pull_request_url:
        publication = {"status": "opened", "pull_request_url": pull_request_url}
    elif failure_doc.get("stage") == RunFailureStage.PUBLICATION.value:
        publication = {"status": "failed", "pull_request_url": None}
    elif publication_requested and result_status in {"failed", "interrupted"}:
        publication = {"status": "not_reached", "pull_request_url": None}
    elif publication_requested:
        publication = {"status": "not_completed", "pull_request_url": None}
    else:
        publication = {"status": "not_requested", "pull_request_url": None}
    paths = attempt_map.get("changed_paths", [])
    change_summary = {
        "changed_paths": paths if isinstance(paths, list) else [],
        "count": len(paths) if isinstance(paths, list) else 0,
        "diff_fingerprint": attempt_map.get("diff_fingerprint"),
    }
    artifacts: dict[str, str] = {
        "run_result": "run-result.json",
        "run_metadata": "run-metadata.json",
        "uncertainty_decisions": "uncertainty-decisions.json",
        "report_json": "report.json",
        "report_markdown": "report.md",
    }
    if failure_doc:
        artifacts["failure"] = "failure.json"
    for artifact_path in sorted(output_root.rglob("*")):
        if not artifact_path.is_file():
            continue
        relative = artifact_path.relative_to(output_root).as_posix()
        if relative in {"report.json", "report.md"}:
            continue
        artifacts.setdefault(f"evidence:{relative}", relative)
    report = RunReport(
        status=result_status,
        task_id=(
            str(metadata["task_id"]) if metadata.get("task_id") is not None else None
        ),
        request=request if isinstance(request, str) else None,
        starting_commit=(
            str(metadata["submission_base"])
            if metadata.get("submission_base") is not None
            else None
        ),
        effective_profile=(
            dict(metadata["effective_profile"])
            if isinstance(metadata.get("effective_profile"), dict)
            else {}
        ),
        branch=(str(result["branch"]) if result.get("branch") is not None else None),
        worktree=(
            str(result["worktree"]) if result.get("worktree") is not None else None
        ),
        allowed_paths=tuple(str(item) for item in allowed_paths),
        change_summary=change_summary,
        uncertainty_decisions=tuple(decision_records),
        validation=validation_data,
        review_findings=review_findings,
        operational_failures=failure,
        publication=publication,
        usage={"status": "unknown", "cost": "unknown"},
        artifacts=artifacts,
    )
    json_path = output_root / "report.json"
    markdown_path = output_root / "report.md"
    _atomic_write(
        json_path, json.dumps(report.to_data(), indent=2, sort_keys=True) + "\n"
    )
    _atomic_write(markdown_path, render_run_report_markdown(report))
    return json_path, markdown_path


def render_run_report_markdown(report: RunReport) -> str:
    """Render a stable human-readable view of a canonical run report."""
    lines = [
        f"# Run report: {report.task_id or 'untitled run'}",
        "",
        f"- **Status:** {report.status}",
        f"- **Starting commit:** {report.starting_commit or 'unavailable'}",
        f"- **Branch:** {report.branch or 'unavailable'}",
        f"- **Worktree:** {report.worktree or 'unavailable'}",
        "",
        "## Request",
        "",
        report.request or "Request unavailable.",
        "",
        "## Effective profile",
        "",
        _markdown_json(report.effective_profile),
        "",
        "## Scope and changes",
        "",
        f"Allowed paths: {', '.join(report.allowed_paths) or 'unavailable'}.",
        f"Changed paths ({report.change_summary.get('count', 0)}):",
    ]
    changed_paths = report.change_summary.get("changed_paths", [])
    if isinstance(changed_paths, list) and changed_paths:
        lines.extend(f"- `{path}`" for path in changed_paths)
    else:
        lines.append("- No changed paths were recorded.")
    lines.extend(["", "## Uncertainty decisions", ""])
    if report.uncertainty_decisions:
        for decision in report.uncertainty_decisions:
            lines.extend(
                [
                    f"### {decision.get('id', 'unknown decision')}",
                    "",
                    f"- **Originating phase:** "
                    f"{decision.get('originating_phase', 'unavailable')}",
                    f"- **Where:** {decision.get('source_ref', 'unavailable')} — "
                    f"{decision.get('source_quote', 'source quote unavailable')}",
                    f"- **Repository location:** "
                    f"{_markdown_inline_json(decision.get('repository_location'))}",
                    f"- **Uncertainty:** {decision.get('uncertainty', 'unavailable')}",
                    f"- **Default:** {decision.get('selected_default', 'unavailable')}",
                    f"- **Rationale:** {decision.get('rationale', 'unavailable')}",
                    f"- **Basis:** {decision.get('basis', 'unavailable')} — "
                    f"{decision.get('basis_reference', 'unavailable')}",
                    f"- **Confidence:** {decision.get('confidence', 'unavailable')}",
                    f"- **Revision history:** "
                    f"{_markdown_inline_json(decision.get('revision_history', []))}",
                    f"- **Implementation references:** "
                    f"{_markdown_inline_json(decision.get('implementation_refs'))}",
                    f"- **Verification references:** "
                    f"{_markdown_inline_json(decision.get('verification_refs'))}",
                    "",
                ]
            )
    else:
        lines.extend(["No uncertainty decisions were recorded.", ""])
    lines.extend(["## Validation", "", _markdown_json(report.validation), ""])
    lines.extend(["## Review findings", ""])
    lines.extend(
        [f"- {_markdown_json(item)}" for item in report.review_findings]
        or ["- No review findings were recorded."]
    )
    lines.extend(["", "## Operational failures", ""])
    lines.extend(
        [f"- {_markdown_json(item)}" for item in report.operational_failures]
        or ["- No operational failures were recorded."]
    )
    lines.extend(
        [
            "",
            "## Publication",
            "",
            _markdown_json(report.publication),
            "",
            "## Usage",
            "",
            _markdown_json(report.usage),
            "",
            "## Artifacts",
            "",
        ]
    )
    lines.extend(
        f"- `{name}`: `{path}`" for name, path in sorted(report.artifacts.items())
    )
    return "\n".join(lines) + "\n"


def _read_json_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _review_findings(review: dict[str, Any]) -> tuple[Any, ...]:
    findings: list[Any] = []
    for key in ("findings", "potential_issues", "issues"):
        value = review.get(key)
        if isinstance(value, list):
            findings.extend(value)
    return tuple(findings)


def _markdown_json(value: Any) -> str:
    if value is None:
        return "Unavailable."
    return (
        "```json\n" + json.dumps(value, indent=2, sort_keys=True, default=str) + "\n```"
    )


def _markdown_inline_json(value: Any) -> str:
    if value is None:
        return "unavailable"
    rendered = json.dumps(value, sort_keys=True, default=str).replace("`", "\\u0060")
    return f"`{rendered}`"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = stream.name
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            Path(temporary_path).unlink(missing_ok=True)
        raise


def _command_version(command: str, cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            [command, "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = (result.stdout or result.stderr).strip()
    return version or None


__all__ = [
    "RunReport",
    "RunFailure",
    "RunFailureStage",
    "collect_run_metadata",
    "render_run_report_markdown",
    "write_run_report",
    "write_json_artifact",
]
