from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from science.deepswe.test_case_prediction.evaluation import (
    prepare_review,
    score_reviews,
)
from science.deepswe.test_case_prediction.predictor import (
    build_messages,
    validate_predictions,
)
from science.deepswe.test_case_prediction.records import collect_task_record


def test_collector_merges_repeated_f2p_cases_and_excludes_base_tests(
    tmp_path: Path,
) -> None:
    task_dir = _task_dir(tmp_path)
    reports = [
        _write_ctrf(
            tmp_path / "run-a" / "ctrf.json",
            [
                {"name": "[p2p] tests.test_existing", "status": "passed"},
                {"name": "[f2p] tests.test_new_behavior", "status": "failed"},
            ],
        ),
        _write_ctrf(
            tmp_path / "run-b" / "ctrf.json",
            [{"name": "[f2p] tests.test_new_behavior", "status": "passed"}],
        ),
    ]

    record = collect_task_record(task_dir, verifier_reports=tuple(reports))

    assert record["task_id"] == "example-task"
    assert record["ground_truth"]["availability"] == "individual_tests"
    assert record["ground_truth"]["cases"] == [
        {
            "id": "tests.test_new_behavior",
            "test_name": "tests.test_new_behavior",
            "behavior_group_id": "tests.test_new_behavior",
            "observed_statuses": ["failed", "passed"],
            "behavior_status": "needs_review",
        }
    ]
    assert record["provenance"]["verifier_report_count"] == 2
    assert "solution" not in record


def test_task_record_marks_missing_feature_markers_as_unavailable_labels(
    tmp_path: Path,
) -> None:
    record = collect_task_record(
        _task_dir(tmp_path),
        verifier_reports=(
            _write_ctrf(
                tmp_path / "run" / "ctrf.json",
                [{"name": "tests.test_existing", "status": "passed"}],
            ),
        ),
    )

    assert record["ground_truth"]["availability"] == "aggregate_or_no_f2p_marker"
    assert record["ground_truth"]["cases"] == []


def test_task_record_reads_declared_verifier_command(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    (task_dir / "task.toml").write_text(
        '[metadata]\ntask_id = "example-task"\n'
        '[verifier]\ncommand = "pytest -q tests/test_feature.py"\n'
    )

    record = collect_task_record(task_dir)

    assert record["input"]["validation"] == [
        {
            "name": "verifier-command-1",
            "command": ["pytest", "-q", "tests/test_feature.py"],
            "source": "task.toml verifier command",
        }
    ]


def test_predictor_messages_include_only_preimplementation_input() -> None:
    record = {
        "task_id": "example-task",
        "input": {
            "instruction": "Add the requested behavior.",
            "validation": [{"name": "pytest", "command": ["pytest", "-q"]}],
        },
        "ground_truth": {"cases": [{"test_name": "hidden_test_name"}]},
        "provenance": {"ground_truth_sources": ["secret/path"]},
    }

    messages = build_messages(record)
    serialized = "\n".join(message["content"] for message in messages)

    assert "Add the requested behavior." in serialized
    assert "pytest" in serialized
    assert "hidden_test_name" not in serialized
    assert "secret/path" not in serialized


def test_prediction_validator_requires_valid_clause_and_inference_reason() -> None:
    instruction = "Expose the result through fetch_items()."
    response = {
        "cases": [
            {
                "subject": "fetch_items",
                "given": "valid inputs",
                "when": "the method runs",
                "then": "it returns items",
                "category": "normal",
                "instruction_clause_ids": ["instruction-001"],
                "basis": "explicit",
                "confidence": 0.8,
                "inference_rationale": "",
            }
        ]
    }

    predictions = validate_predictions("task", instruction, response)

    assert predictions["cases"][0]["id"] == "pred-001"
    assert predictions["cases"][0]["instruction_evidence"] == [instruction]
    response["cases"][0]["instruction_clause_ids"] = ["instruction-999"]
    with pytest.raises(ValueError, match="unknown instruction clause"):
        validate_predictions("task", instruction, response)
    response["cases"][0]["instruction_clause_ids"] = ["instruction-001"]
    response["cases"][0]["basis"] = "inferred"
    with pytest.raises(ValueError, match="requires a rationale"):
        validate_predictions("task", instruction, response)


def test_review_scoring_reports_exact_and_partial_coverage_at_k() -> None:
    record = _scoring_record()
    predictions = {
        "task_id": "example-task",
        "cases": [
            {"id": "pred-001"},
            {"id": "pred-002"},
            {"id": "pred-003"},
        ],
    }
    review = prepare_review(record, predictions)
    for disposition in review["ground_truth_dispositions"]:
        disposition["behavior_status"] = "known"
        disposition["rationale"] = "report identifies observable behavior"
    judgments = {
        ("pred-001", "test-a"): "exact",
        ("pred-001", "test-b"): "no_match",
        ("pred-002", "test-a"): "partial",
        ("pred-002", "test-b"): "partial",
        ("pred-003", "test-a"): "no_match",
        ("pred-003", "test-b"): "no_match",
    }
    for pair in review["pairs"]:
        pair["judgment"] = judgments[(pair["prediction_id"], pair["ground_truth_id"])]
        pair["rationale"] = "reviewed behavior and outcome"

    report = score_reviews([(record, predictions, review)], cutoffs=(1, 2, 5))
    metrics = report["tasks"][0]

    assert metrics["matched_exact"] == 1
    assert metrics["matched_partial"] == 1
    assert metrics["unsupported_predictions"] == 1
    assert metrics["exact_precision"] == pytest.approx(1 / 3)
    assert metrics["coverage_recall"] == 1.0
    assert metrics["at_k"]["1"] == {
        "prediction_count": 1,
        "matched_predictions": 1,
        "matched_ground_truth": 1,
    }
    assert metrics["at_k"]["2"]["matched_ground_truth"] == 2


def test_review_scoring_rejects_unreviewed_pairs() -> None:
    record = _scoring_record()
    predictions = {"task_id": "example-task", "cases": [{"id": "pred-001"}]}
    review = prepare_review(record, predictions)
    for disposition in review["ground_truth_dispositions"]:
        disposition["behavior_status"] = "known"
        disposition["rationale"] = "report identifies observable behavior"

    with pytest.raises(ValueError, match="still has unreviewed"):
        score_reviews([(record, predictions, review)])


def test_unknown_ground_truth_is_excluded_from_semantic_metrics() -> None:
    record = _scoring_record()
    predictions = {"task_id": "example-task", "cases": [{"id": "pred-001"}]}
    review = prepare_review(record, predictions)
    review["ground_truth_dispositions"] = [
        {
            "ground_truth_id": "test-a",
            "behavior_status": "known",
            "behavior_group_id": "test-a",
            "rationale": "test identifier describes the asserted behavior",
        },
        {
            "ground_truth_id": "test-b",
            "behavior_status": "unknown",
            "rationale": "test identifier does not reveal the asserted behavior",
        },
    ]
    review["pairs"][0].update(
        {"judgment": "exact", "rationale": "same behavior and expected outcome"}
    )

    metrics = score_reviews([(record, predictions, review)])["tasks"][0]

    assert metrics["ground_truth_count"] == 1
    assert metrics["ground_truth_behavior_unknown_count"] == 1
    assert metrics["coverage_recall"] == 1.0


def test_one_prediction_covers_parameterized_tests_of_the_same_behavior() -> None:
    record = _scoring_record()
    record["ground_truth"]["cases"][0]["behavior_group_id"] = "test-group"
    record["ground_truth"]["cases"][1]["behavior_group_id"] = "test-group"
    predictions = {"task_id": "example-task", "cases": [{"id": "pred-001"}]}
    review = prepare_review(record, predictions)
    for disposition in review["ground_truth_dispositions"]:
        disposition["behavior_status"] = "known"
        disposition["rationale"] = "same behavior in two parameterized variants"
    for pair in review["pairs"]:
        pair.update({"judgment": "exact", "rationale": "same behavior and outcome"})

    metrics = score_reviews([(record, predictions, review)])["tasks"][0]

    assert metrics["matched_exact"] == 2
    assert metrics["exact_prediction_count"] == 1
    assert metrics["exact_precision"] == 1.0
    assert metrics["exact_recall"] == 1.0


def _task_dir(root: Path) -> Path:
    task_dir = root / "tasks" / "example-task"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Implement example behavior.\n")
    (task_dir / "task.toml").write_text(
        '[task]\nname = "org/example-task"\n'
        '[metadata]\ntask_id = "example-task"\nrepository_url = '
        '"https://example.invalid/repo"\n'
    )
    return task_dir


def _write_ctrf(path: Path, tests: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"results": {"tests": tests}}),
        encoding="utf-8",
    )
    return path


def _scoring_record() -> dict[str, Any]:
    return {
        "schema_version": "deepswe-test-prediction-task-v1",
        "task_id": "example-task",
        "repository_id": "example-repo",
        "input": {"instruction": "Implement behavior", "validation": []},
        "ground_truth": {
            "availability": "individual_tests",
            "cases": [
                {"id": "test-a", "test_name": "test_a", "behavior_group_id": "test_a"},
                {"id": "test-b", "test_name": "test_b", "behavior_group_id": "test_b"},
            ],
        },
        "provenance": {},
    }
