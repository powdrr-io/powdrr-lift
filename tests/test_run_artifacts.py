from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import powdrr_lift.workrr.run_artifacts as run_artifacts
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
    assert report["usage"]["cost"]["status"] == "unknown"
    assert report["artifacts"]["run_telemetry"] == "run-telemetry.json"
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


def test_run_telemetry_aggregates_retries_replays_stages_and_receipts(
    tmp_path: Path,
) -> None:
    write_json_artifact(
        tmp_path,
        "run-metadata.json",
        {
            "run_started_at": "2026-01-01T00:00:00+00:00",
            "task_id": "telemetry-feature",
        },
    )
    events = [
        {
            "record_type": "powdrr_service.procedrr.provider_attempt",
            "trace_id": "question-1",
            "attempt": 1,
            "model": "planner-a",
            "role": "planning",
            "outcome": "error",
            "duration_ms": 12,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_amount": 0.01,
            "cost_currency": "USD",
        },
        {
            "record_type": "powdrr_service.procedrr.provider_attempt",
            "trace_id": "question-1",
            "attempt": 2,
            "model": "planner-a",
            "role": "planning",
            "outcome": "success",
            "duration_ms": 20,
            "prompt_tokens": 12,
            "completion_tokens": 6,
            "cost_amount": 0.02,
            "cost_currency": "USD",
        },
        {
            "record_type": "powdrr_service.procedrr.provider_attempt",
            "trace_id": "batch-1",
            "attempt": 1,
            "model": "decision-b",
            "role": "decision",
            "outcome": "succeeded",
            "transport_kind": "batch",
            "batch_member_count": 2,
            "duration_ms": 30,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "cost_amount": 0.03,
            "cost_currency": "USD",
        },
        {
            "record_type": "powdrr_service.procedrr.step",
            "kind": "judge",
            "path": "steps[0].judge",
        },
        {
            "record_type": "powdrr_service.procedrr.step",
            "kind": "process",
            "name": "verify",
            "duration_ms": 45.5,
        },
        {
            "record_type": "powdrr_service.procedrr.step",
            "kind": "process_failure",
            "name": "review",
            "duration_ms": 5,
            "error_type": "RuntimeError",
        },
        {
            "record_type": "powdrr_service.procedrr.step",
            "kind": "resume",
            "path": "steps[1]",
        },
        {
            "record_type": "powdrr_service.procedrr.step",
            "kind": "operation",
            "tool": "shell",
            "duration_ms": 8,
        },
    ]
    (tmp_path / "procedrr-events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    result = {
        "status": "completed",
        "validation": {
            "results": [
                {"command": ["pytest", "-q"], "receipt_reused": False},
                {"command": ["ruff", "check", "."], "receipt_reused": True},
            ]
        },
    }

    json_path, _ = write_run_report(
        tmp_path,
        result=result,
        finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )

    report = json.loads(json_path.read_text(encoding="utf-8"))
    usage = report["usage"]
    assert usage["total_wall_ms"] == 1000
    assert usage["logical_judgment_prompts"] == 1
    assert usage["provider_calls"] == 2
    assert usage["transport_requests"] == 3
    assert usage["provider_batches"] == {
        "count": 1,
        "member_count": 2,
        "status": "observed",
    }
    assert usage["concurrent_requests"]["status"] == "unknown"
    assert usage["questions_inside_prompt"]["status"] == "unknown"
    assert (
        usage["provider_calls_by_model"]["planning:planner-a"]["failed_attempts"] == 1
    )
    assert usage["stage_wall_ms"]["verify"]["wall_ms"] == 45.5
    assert usage["stage_wall_ms"]["review"]["failed_processes"] == 1
    assert usage["stage_failures"] == 1
    assert usage["stage_wall_ms_overlap"].startswith("nested process stages overlap")
    assert usage["subprocess_executions"] == 2
    assert usage["cache_hits"] == {
        "workflow_replays": 1,
        "validation_receipts": 1,
    }
    assert usage["tokens"] == {
        "prompt": 42,
        "completion": 21,
        "status": "observed",
    }
    assert usage["cost"] == {"amount": 0.06, "currency": "USD", "status": "observed"}
    assert usage["repeated_work"]["provider_retry_attempts"] == 1
    assert (
        json.loads((tmp_path / "run-telemetry.json").read_text())["provider_calls"] == 2
    )


def test_telemetry_artifact_write_failure_does_not_lose_run_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_json_artifact(
        tmp_path,
        "run-metadata.json",
        {"task_id": "telemetry-write-failure", "run_started_at": "invalid"},
    )
    original_write = run_artifacts.write_json_artifact

    def fail_telemetry_write(root: Path, name: str, value: object) -> Path:
        if name == "run-telemetry.json":
            raise OSError("disk full")
        return original_write(root, name, value)

    monkeypatch.setattr(run_artifacts, "write_json_artifact", fail_telemetry_write)

    json_path, markdown_path = write_run_report(
        tmp_path,
        result={"status": "completed", "review": {"passed": True}},
    )

    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["status"] == "completed"
    assert "run_telemetry" not in report["artifacts"]
    assert report["usage"]["telemetry_errors"] == [
        "persist run telemetry: OSError: disk full"
    ]
    assert markdown_path.is_file()
