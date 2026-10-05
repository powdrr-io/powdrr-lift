from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.workrr.run_artifacts import write_json_artifact, write_run_report


def test_run_report_json_and_markdown_share_persisted_evidence(
    tmp_path: Path,
) -> None:
    write_json_artifact(
        tmp_path,
        "run-metadata.json",
        {
            "task_id": "export-feature",
            "request": "Add export support.",
            "submission_base": "abc123",
            "allowed_paths": ["src/export.py"],
            "effective_profile": {"planning": {"model": "planner"}},
            "publication_requested": True,
        },
    )
    write_json_artifact(
        tmp_path,
        "uncertainty-decisions.json",
        {
            "schema_version": "uncertainty-decisions-v1",
            "decisions": [
                {
                    "id": "uncertainty:instruction-001:format",
                    "source_ref": "instruction-001",
                    "source_quote": "Add export support.",
                    "uncertainty": "The serialization format is unspecified.",
                    "selected_default": "Use JSON.",
                    "rationale": "Use the repository's existing artifact format.",
                    "basis": "repository_convention",
                    "basis_reference": "Existing artifacts use JSON.",
                    "confidence": "high",
                }
            ],
        },
    )
    result = {
        "status": "completed",
        "branch": "feature/export",
        "worktree": "/tmp/worktree",
        "attempt": {
            "changed_paths": ["src/export.py"],
            "diff_fingerprint": "diff123",
        },
        "validation": {"status": "passed", "results": []},
        "review": {"passed": True, "findings": []},
        "pull_request_url": "https://example.test/pull/1",
    }

    json_path, markdown_path = write_run_report(tmp_path, result=result)

    report = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert report["schema_version"] == "powdrr-run-report-v1"
    assert report["starting_commit"] == "abc123"
    assert report["request"] == "Add export support."
    assert report["uncertainty_decisions"][0]["selected_default"] == "Use JSON."
    assert report["uncertainty_decisions"][0]["implementation_refs"] == {
        "status": "unavailable"
    }
    assert report["publication"]["status"] == "opened"
    assert report["usage"]["cost"] == "unknown"
    assert report["artifacts"]["report_json"] == "report.json"
    assert "Use JSON." in markdown
    assert "uncertainty:instruction-001:format" in markdown
    assert "https://example.test/pull/1" in markdown
    assert markdown == write_run_report(tmp_path, result=result)[1].read_text(
        encoding="utf-8"
    )


def test_run_report_states_when_no_uncertainty_was_recorded(tmp_path: Path) -> None:
    write_json_artifact(
        tmp_path,
        "run-metadata.json",
        {"task_id": "simple-feature", "request": "Fix a typo."},
    )
    _, markdown_path = write_run_report(
        tmp_path,
        result={"status": "completed", "review": {"passed": True}},
    )

    assert "No uncertainty decisions were recorded." in markdown_path.read_text(
        encoding="utf-8"
    )


def test_run_report_includes_persisted_operational_failure(tmp_path: Path) -> None:
    write_json_artifact(
        tmp_path,
        "failure.json",
        {
            "schema_version": "powdrr-run-failure-v1",
            "stage": "planning",
            "error_type": "PlanningError",
            "message": "Planning stopped.",
        },
    )
    _, markdown_path = write_run_report(
        tmp_path,
        result={"status": "failed", "review": {"passed": False}},
        metadata={"task_id": "failed-feature"},
    )

    markdown = markdown_path.read_text(encoding="utf-8")
    assert "PlanningError" in markdown
    assert "Planning stopped." in markdown
