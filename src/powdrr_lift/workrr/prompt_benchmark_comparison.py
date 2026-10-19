"""Compare an immutable prompt evaluation with final benchmark validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.deepswe_design_evaluation import _content_fingerprint

COMPARISON_SCHEMA_VERSION = "prompt-benchmark-comparison-v1"
_VALIDATION_REPORT_SCHEMA = "coding-agent-validation-report-v1"
_RUN_REPORT_SCHEMA = "powdrr-run-report-v1"
_NORMALIZED_STATUSES = (
    "passed",
    "failed",
    "not_run",
    "infrastructure_error",
)
_PROMPT_DECISIONS = {"supported", "missing", "contradicted", "ambiguous"}


class PromptBenchmarkComparisonError(ValueError):
    """An input prompt report or benchmark validation artifact is invalid."""


def compare_prompt_benchmark(
    *, prompt_report_path: Path, benchmark_run_path: Path
) -> dict[str, Any]:
    """Create a separate comparison report without modifying prompt evaluation."""
    prompt_report_path = prompt_report_path.resolve()
    benchmark_run_path = benchmark_run_path.resolve()
    prompt_report = _read_json(prompt_report_path)
    _validate_prompt_report(prompt_report)

    validation_document, validation_path, run_id, task_id, run_artifact = (
        _load_validation(benchmark_run_path)
    )
    if task_id is not None and task_id != prompt_report.get("task_id"):
        raise PromptBenchmarkComparisonError(
            "benchmark task ID does not match the prompt evaluation"
        )
    validation_request_id = validation_document.get("request_id")
    prompt_request_ids = {
        item.get("request_id")
        for item in prompt_report.get("worker_prompts", [])
        if isinstance(item, Mapping) and isinstance(item.get("request_id"), str)
    }
    if (
        validation_request_id is not None
        and validation_request_id not in prompt_request_ids
    ):
        raise PromptBenchmarkComparisonError(
            "benchmark validation request ID does not match a captured prompt request"
        )

    raw_results = validation_document.get("results", [])
    normalized_results = _normalize_results(raw_results)
    if raw_results and validation_request_id is None:
        raise PromptBenchmarkComparisonError(
            "benchmark validation results have no request ID for prompt provenance"
        )
    has_infrastructure_result = any(
        item["status"] == "infrastructure_error" for item in normalized_results
    )
    if (
        validation_document.get("status") == "blocked" and not has_infrastructure_result
    ) or (not normalized_results and validation_document.get("error")):
        normalized_results.append(
            {
                "test_id": "__validation__",
                "profile": "validation-report",
                "status": "infrastructure_error",
                "returncode": None,
                "command": [],
                "failure_evidence": _truncate(
                    str(validation_document.get("error") or "validation was blocked")
                ),
                "evidence_path": None,
            }
        )
    findings = prompt_report.get("findings")
    assert isinstance(findings, list)
    mappings: dict[str, list[str]] = {}
    for finding in findings:
        assert isinstance(finding, Mapping)
        criterion_id = finding["criterion_id"]
        test_ids = finding.get("validation_test_ids", [])
        if not isinstance(test_ids, list) or not all(
            isinstance(item, str) and item.strip() for item in test_ids
        ):
            raise PromptBenchmarkComparisonError(
                f"validation test mapping is malformed for criterion {criterion_id}"
            )
        mappings[str(criterion_id)] = list(dict.fromkeys(test_ids))

    results_by_id: dict[str, list[dict[str, Any]]] = {}
    for result in normalized_results:
        results_by_id.setdefault(result["test_id"], []).append(result)
    mapped_ids = {item for values in mappings.values() for item in values}
    for test_id in sorted(mapped_ids - results_by_id.keys()):
        not_run: dict[str, Any] = {
            "test_id": test_id,
            "profile": None,
            "status": "not_run",
            "returncode": None,
            "command": [],
            "failure_evidence": None,
            "evidence_path": None,
            "source_location": None,
        }
        normalized_results.append(not_run)
        results_by_id[test_id] = [not_run]
    criteria: list[dict[str, Any]] = []
    for finding in findings:
        assert isinstance(finding, Mapping)
        criterion_id = str(finding["criterion_id"])
        test_ids = mappings[criterion_id]
        criterion_results: list[dict[str, Any]] = []
        for test_id in test_ids:
            matching = results_by_id.get(test_id)
            if matching:
                criterion_results.extend(dict(item) for item in matching)
            else:
                criterion_results.append(
                    {
                        "test_id": test_id,
                        "profile": None,
                        "status": "not_run",
                        "returncode": None,
                        "command": [],
                        "failure_evidence": None,
                        "evidence_path": None,
                        "source_location": None,
                    }
                )
        criteria.append(
            {
                "criterion_id": criterion_id,
                "prompt_decision": finding.get("decision"),
                "prompt_importance": finding.get("importance"),
                "prompt_dimension": finding.get("dimension"),
                "validation_test_ids": test_ids,
                "validation_results": criterion_results,
                "validation_summary": _summarize_results(criterion_results),
                "interpretation": _interpret_finding(
                    finding.get("decision"), criterion_results, test_ids
                ),
            }
        )

    unmapped_failures = [
        dict(item)
        for item in normalized_results
        if item["status"] == "failed" and item["test_id"] not in mapped_ids
    ]
    summary = _summarize_results(normalized_results)
    run_root = (
        benchmark_run_path if benchmark_run_path.is_dir() else benchmark_run_path.parent
    )
    run_metadata = _read_optional_json(run_root / "run-metadata.json")
    attempt = run_artifact.get("attempt")
    attempt = attempt if isinstance(attempt, Mapping) else {}
    effective_profile = run_artifact.get("effective_profile")
    provenance = {
        key: run_metadata[key]
        for key in (
            "powdrr_version",
            "powdrr_revision",
            "powdrr_install_spec",
            "opencode_version",
        )
        if run_metadata.get(key) is not None
    }
    if isinstance(effective_profile, Mapping):
        provenance["worker_profile"] = dict(effective_profile)
    if isinstance(attempt.get("provider"), str):
        provenance["worker_provider"] = attempt["provider"]
    comparison: dict[str, Any] = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "prompt_report": {
            "fingerprint": prompt_report["fingerprint"],
            "score": prompt_report["summary"]["prompt_score"],
            "gate": prompt_report["summary"]["gate"],
            "critical_failure_count": len(
                prompt_report["summary"]["critical_failures"]
            ),
            "path": str(prompt_report_path),
        },
        "benchmark": {
            "run_id": run_id,
            "task_id": task_id,
            "request_id": validation_request_id,
            "provenance": provenance,
            "validation_report_path": str(validation_path),
            "validation_report_fingerprint": _file_fingerprint(validation_path),
            "validation_report_status": validation_document.get("status"),
            "validation_error": validation_document.get("error"),
        },
        "summary": {
            **summary,
            "unmapped_failure_count": len(unmapped_failures),
            "unresolved_count": summary["not_run_count"]
            + summary["infrastructure_error_count"],
        },
        "validation_results": normalized_results,
        "criteria": criteria,
        "unmapped_failures": unmapped_failures,
    }
    comparison["fingerprint"] = _content_fingerprint(comparison)
    return comparison


def _validate_prompt_report(report: Mapping[str, Any]) -> None:
    if report.get("schema_version") != "prompt-evaluation-report-v1":
        raise PromptBenchmarkComparisonError("unsupported prompt evaluation report")
    fingerprint = report.get("fingerprint")
    unsigned = dict(report)
    unsigned.pop("fingerprint", None)
    if (
        not isinstance(fingerprint, str)
        or _content_fingerprint(unsigned) != fingerprint
    ):
        raise PromptBenchmarkComparisonError(
            "prompt evaluation report fingerprint is missing or invalid"
        )
    task_id = report.get("task_id")
    prompts = report.get("worker_prompts")
    findings = report.get("findings")
    summary = report.get("summary")
    if (
        not isinstance(task_id, str)
        or not isinstance(prompts, list)
        or not isinstance(findings, list)
        or not findings
        or not isinstance(summary, Mapping)
        or not isinstance(summary.get("prompt_score"), int)
        or not isinstance(summary.get("gate"), str)
        or not isinstance(summary.get("critical_failures"), list)
    ):
        raise PromptBenchmarkComparisonError("prompt evaluation report is incomplete")
    for finding in findings:
        if not isinstance(finding, Mapping) or not isinstance(
            finding.get("criterion_id"), str
        ):
            raise PromptBenchmarkComparisonError(
                "prompt evaluation report has a malformed criterion finding"
            )
        if finding.get("decision") not in _PROMPT_DECISIONS:
            raise PromptBenchmarkComparisonError(
                "prompt evaluation report has an unsupported criterion decision"
            )


def _load_validation(
    path: Path,
) -> tuple[dict[str, Any], Path, str, str | None, dict[str, Any]]:
    input_path = path
    run_id = path.name if path.is_dir() else path.parent.name
    if path.is_dir():
        run_result_path = path / "run-result.json"
        report_path = path / "report.json"
        if run_result_path.is_file():
            path = run_result_path
        elif report_path.is_file():
            path = report_path
        else:
            raise PromptBenchmarkComparisonError(
                "benchmark run directory has no run-result.json or report.json"
            )
    document = _read_json(path)
    schema = document.get("schema_version")
    if schema == _VALIDATION_REPORT_SCHEMA:
        validation_value: Any = document
        task_id = None
    elif schema == _RUN_REPORT_SCHEMA or "validation" in document:
        validation_value = document.get("validation")
        task_id = document.get("task_id")
        if validation_value is None:
            validation_value = {
                "schema_version": _VALIDATION_REPORT_SCHEMA,
                "status": "not_run",
                "results": [],
                "error": None,
            }
        if not isinstance(validation_value, Mapping):
            raise PromptBenchmarkComparisonError(
                "benchmark run validation report is malformed"
            )
    else:
        raise PromptBenchmarkComparisonError(
            "unsupported benchmark run or validation report schema"
        )
    if not isinstance(validation_value, Mapping):
        raise PromptBenchmarkComparisonError("benchmark validation report is malformed")
    validation = dict(validation_value)
    if validation.get("schema_version") != _VALIDATION_REPORT_SCHEMA:
        raise PromptBenchmarkComparisonError(
            "unsupported coding-agent validation report schema"
        )
    if task_id is not None and not isinstance(task_id, str):
        task_id = None
    request_id = validation.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise PromptBenchmarkComparisonError("validation request_id is malformed")
    if not input_path.is_dir() and isinstance(validation.get("attempt_id"), str):
        run_id = str(validation["attempt_id"])
    validation["request_id"] = request_id
    return dict(validation), path, run_id, task_id, document


def _normalize_results(raw_results: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        raise PromptBenchmarkComparisonError("validation results must be a list")
    results: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, Mapping):
            raise PromptBenchmarkComparisonError("validation result is malformed")
        profile = item.get("profile")
        test_id = item.get("test_id") or profile
        if not isinstance(test_id, str) or not test_id.strip():
            raise PromptBenchmarkComparisonError(
                "validation result must include test_id or profile"
            )
        status = _normalize_status(item.get("status"))
        evidence = item.get("actual") or item.get("error") or item.get("stderr")
        if evidence is None and status == "failed":
            evidence = item.get("stdout")
        raw_command = item.get("command", [])
        if not isinstance(raw_command, (list, tuple)) or not all(
            isinstance(part, str) for part in raw_command
        ):
            raise PromptBenchmarkComparisonError(
                f"validation command is malformed for {test_id}"
            )
        results.append(
            {
                "test_id": test_id,
                "profile": profile if isinstance(profile, str) else None,
                "status": status,
                "returncode": item.get("returncode"),
                "command": list(raw_command),
                "failure_evidence": _truncate(evidence)
                if isinstance(evidence, str)
                else None,
                "evidence_path": item.get("evidence_path")
                if isinstance(item.get("evidence_path"), str)
                else None,
                "source_location": item.get("source_location")
                if isinstance(item.get("source_location"), str)
                else None,
            }
        )
    return results


def _normalize_status(value: Any) -> str:
    if not isinstance(value, str):
        raise PromptBenchmarkComparisonError("validation result status is missing")
    if value in _NORMALIZED_STATUSES:
        return value
    if value == "timed_out" or value == "blocked":
        return "infrastructure_error"
    raise PromptBenchmarkComparisonError(
        f"unsupported validation result status: {value}"
    )


def _summarize_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in _NORMALIZED_STATUSES}
    for result in results:
        status = result.get("status")
        if status not in counts:
            raise PromptBenchmarkComparisonError(
                f"invalid normalized validation status: {status}"
            )
        counts[status] += 1
    executed_count = counts["passed"] + counts["failed"]
    return {
        "validation_unit_count": len(results),
        "passed_count": counts["passed"],
        "failed_count": counts["failed"],
        "not_run_count": counts["not_run"],
        "infrastructure_error_count": counts["infrastructure_error"],
        "executed_count": executed_count,
        "validation_pass_rate": (
            round(counts["passed"] / executed_count, 4) if executed_count else None
        ),
    }


def _interpret_finding(
    decision: Any,
    results: Sequence[Mapping[str, Any]],
    test_ids: Sequence[str],
) -> str:
    if not test_ids:
        return "rubric_mapping_missing"
    statuses = {result.get("status") for result in results}
    if "not_run" in statuses or "infrastructure_error" in statuses or not statuses:
        return "inconclusive_validation"
    if statuses == {"passed"}:
        if decision == "supported":
            return "prompt_and_validation_agree"
        if decision in {"missing", "contradicted"}:
            return "prompt_defect_without_observed_validation_failure"
        return "inconclusive_prompt_finding"
    if "failed" in statuses:
        if decision == "supported":
            return "worker_or_validation_gap_review"
        if decision in {"missing", "contradicted"}:
            return "prompt_generation_or_rubric_review"
        return "inconclusive_prompt_finding"
    return "inconclusive_validation"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PromptBenchmarkComparisonError(
            f"could not read JSON artifact {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise PromptBenchmarkComparisonError(f"JSON artifact must be an object: {path}")
    return value


def _read_optional_json(path: Path) -> dict[str, Any]:
    return _read_json(path) if path.is_file() else {}


def _file_fingerprint(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _truncate(value: str, limit: int = 1200) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


__all__ = [
    "PromptBenchmarkComparisonError",
    "compare_prompt_benchmark",
]
