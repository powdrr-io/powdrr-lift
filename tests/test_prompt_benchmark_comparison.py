from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.cli import main
from powdrr_lift.workrr.deepswe_design_evaluation import _content_fingerprint
from powdrr_lift.workrr.prompt_benchmark_comparison import (
    PromptBenchmarkComparisonError,
    compare_prompt_benchmark,
)


def _prompt_report() -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "prompt-evaluation-report-v1",
        "task_id": "case-17",
        "worker_prompts": [{"request_id": "request-17"}],
        "summary": {
            "prompt_score": 82,
            "gate": "fail",
            "critical_failures": ["returns-result"],
        },
        "findings": [
            {
                "criterion_id": "returns-result",
                "importance": "critical",
                "decision": "supported",
                "validation_test_ids": ["api.test_pass", "api.test_fail"],
            },
            {
                "criterion_id": "handles-timeout",
                "importance": "important",
                "decision": "missing",
                "validation_test_ids": ["api.test_blocked", "api.test_not_run"],
            },
        ],
    }
    report["fingerprint"] = _content_fingerprint(report)
    return report


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    prompt_report_path = tmp_path / "prompt-evaluation.json"
    prompt_report_path.write_text(json.dumps(_prompt_report()), encoding="utf-8")
    run_dir = tmp_path / "benchmark-run-17"
    run_dir.mkdir()
    (run_dir / "run-metadata.json").write_text(
        json.dumps(
            {
                "powdrr_version": "0.2.test",
                "powdrr_revision": "revision-17",
                "opencode_version": "opencode-test",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run-result.json").write_text(
        json.dumps(
            {
                "task_id": "case-17",
                "effective_profile": {"worker": "mini", "model": "model-17"},
                "attempt": {"provider": "mini"},
                "validation": {
                    "schema_version": "coding-agent-validation-report-v1",
                    "attempt_id": "attempt-17",
                    "request_id": "request-17",
                    "status": "blocked",
                    "error": None,
                    "results": [
                        {
                            "profile": "api.test_pass",
                            "test_id": "api.test_pass",
                            "status": "passed",
                            "returncode": 0,
                        },
                        {
                            "profile": "api.test_fail",
                            "test_id": "api.test_fail",
                            "status": "failed",
                            "returncode": 1,
                            "actual": "assert expected 1, got 2",
                            "evidence_path": "artifacts/test-output.txt",
                        },
                        {
                            "profile": "api.test_blocked",
                            "test_id": "api.test_blocked",
                            "status": "blocked",
                            "returncode": None,
                            "error": "container unavailable",
                        },
                        {
                            "profile": "unmapped.failure",
                            "status": "failed",
                            "returncode": 1,
                            "stderr": "unexpected failure",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return prompt_report_path, run_dir


def test_comparison_normalizes_statuses_and_maps_declared_tests(
    tmp_path: Path,
) -> None:
    prompt_report_path, run_dir = _write_fixture(tmp_path)

    report = compare_prompt_benchmark(
        prompt_report_path=prompt_report_path,
        benchmark_run_path=run_dir,
    )
    replay = compare_prompt_benchmark(
        prompt_report_path=prompt_report_path,
        benchmark_run_path=run_dir,
    )

    assert report["schema_version"] == "prompt-benchmark-comparison-v1"
    assert report["prompt_report"]["score"] == 82
    assert report["prompt_report"]["gate"] == "fail"
    assert report["prompt_report"]["critical_failure_count"] == 1
    assert (
        report["prompt_report"]["fingerprint"]
        == json.loads(prompt_report_path.read_text(encoding="utf-8"))["fingerprint"]
    )
    assert report["summary"] == {
        "validation_unit_count": 5,
        "passed_count": 1,
        "failed_count": 2,
        "not_run_count": 1,
        "infrastructure_error_count": 1,
        "executed_count": 3,
        "validation_pass_rate": 0.3333,
        "unmapped_failure_count": 1,
        "unresolved_count": 2,
    }
    assert report["benchmark"]["run_id"] == "benchmark-run-17"
    assert report["benchmark"]["request_id"] == "request-17"
    assert report["benchmark"]["provenance"] == {
        "powdrr_version": "0.2.test",
        "powdrr_revision": "revision-17",
        "opencode_version": "opencode-test",
        "worker_profile": {"worker": "mini", "model": "model-17"},
        "worker_provider": "mini",
    }
    assert report["criteria"][0]["interpretation"] == "worker_or_validation_gap_review"
    assert report["criteria"][0]["validation_results"][1]["failure_evidence"] == (
        "assert expected 1, got 2"
    )
    assert report["criteria"][1]["interpretation"] == "inconclusive_validation"
    assert report["unmapped_failures"][0]["test_id"] == "unmapped.failure"
    assert report["fingerprint"].startswith("sha256:")
    assert replay["fingerprint"] == report["fingerprint"]


def test_comparison_rejects_prompt_report_changes(tmp_path: Path) -> None:
    prompt_report_path, run_dir = _write_fixture(tmp_path)
    prompt_report = json.loads(prompt_report_path.read_text(encoding="utf-8"))
    prompt_report["summary"]["prompt_score"] = 100
    prompt_report_path.write_text(json.dumps(prompt_report), encoding="utf-8")

    with pytest.raises(PromptBenchmarkComparisonError, match="fingerprint"):
        compare_prompt_benchmark(
            prompt_report_path=prompt_report_path,
            benchmark_run_path=run_dir,
        )


def test_comparison_rejects_mismatched_request_identity(tmp_path: Path) -> None:
    prompt_report_path, run_dir = _write_fixture(tmp_path)
    run_result_path = run_dir / "run-result.json"
    run_result = json.loads(run_result_path.read_text(encoding="utf-8"))
    run_result["validation"]["request_id"] = "different-request"
    run_result_path.write_text(json.dumps(run_result), encoding="utf-8")

    with pytest.raises(PromptBenchmarkComparisonError, match="request ID"):
        compare_prompt_benchmark(
            prompt_report_path=prompt_report_path,
            benchmark_run_path=run_dir,
        )


def test_standalone_validation_report_uses_profile_as_mapping_id(
    tmp_path: Path,
) -> None:
    prompt_report_path = tmp_path / "prompt-evaluation.json"
    prompt_report_path.write_text(json.dumps(_prompt_report()), encoding="utf-8")
    validation_path = tmp_path / "validation.json"
    validation_path.write_text(
        json.dumps(
            {
                "schema_version": "coding-agent-validation-report-v1",
                "attempt_id": "attempt-17",
                "request_id": "request-17",
                "status": "passed",
                "results": [
                    {"profile": "api.test_pass", "status": "passed", "returncode": 0}
                ],
            }
        ),
        encoding="utf-8",
    )

    report = compare_prompt_benchmark(
        prompt_report_path=prompt_report_path,
        benchmark_run_path=validation_path,
    )

    assert report["benchmark"]["run_id"] == "attempt-17"
    assert report["validation_results"][0]["test_id"] == "api.test_pass"


def test_powdrr_run_report_json_is_supported(tmp_path: Path) -> None:
    prompt_report_path, run_dir = _write_fixture(tmp_path)
    run_result_path = run_dir / "run-result.json"
    run_result = json.loads(run_result_path.read_text(encoding="utf-8"))
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "schema_version": "powdrr-run-report-v1",
                "task_id": "case-17",
                "effective_profile": {"worker": "mini"},
                "validation": run_result["validation"],
            }
        ),
        encoding="utf-8",
    )
    run_result_path.unlink()

    report = compare_prompt_benchmark(
        prompt_report_path=prompt_report_path,
        benchmark_run_path=run_dir,
    )

    assert report["benchmark"]["task_id"] == "case-17"
    assert report["benchmark"]["validation_report_path"].endswith("report.json")
    assert report["benchmark"]["provenance"]["worker_profile"] == {"worker": "mini"}


def test_compare_command_writes_report_and_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt_report_path, run_dir = _write_fixture(tmp_path)

    exit_code = main(
        [
            "compare-prompt-benchmark",
            "--prompt-report",
            str(prompt_report_path),
            "--benchmark-run",
            str(run_dir),
        ]
    )

    report_path = run_dir / "prompt-benchmark-comparison.json"
    written = json.loads(report_path.read_text(encoding="utf-8"))
    output = capsys.readouterr().out
    assert exit_code == 0
    assert written["schema_version"] == "prompt-benchmark-comparison-v1"
    assert "prompt score 82/100; gate fail; critical failures 1" in output
    assert "validation pass rate 33.3%" in output
    assert "unresolved 2" in output
    assert "unmapped failures 1" in output
