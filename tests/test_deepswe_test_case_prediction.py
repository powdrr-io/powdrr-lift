from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from science.deepswe.test_case_prediction import predictor as predictor_module
from science.deepswe.test_case_prediction.evaluation import (
    prepare_review,
    score_reviews,
)
from science.deepswe.test_case_prediction.predictor import (
    TEST_CASE_PROMPT,
    TEST_CASES_SCHEMA,
    build_messages,
    build_test_cases_response_schema,
    validate_obligations,
    validate_predictions,
    validate_test_count,
)
from science.deepswe.test_case_prediction.records import collect_task_record


def test_collector_extracts_named_cases_from_test_patch(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    patch = task_dir / "tests" / "test.patch"
    patch.parent.mkdir()
    patch.write_text(
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "new file mode 100644\n@@ -0,0 +1,5 @@\n"
        "+def test_widget_returns_value():\n"
        "+    result = widget()\n+    assert result == 3\n"
        "+\n+def test_widget_rejects_none():\n+    with pytest.raises(ValueError):\n"
        "+        widget(None)\n",
        encoding="utf-8",
    )
    record = collect_task_record(task_dir)

    assert record["task_id"] == "example-task"
    assert record["ground_truth"]["availability"] == "patch_test_cases"
    assert [case["test_name"] for case in record["ground_truth"]["cases"]] == [
        "test_widget_rejects_none",
        "test_widget_returns_value",
    ]
    assert record["ground_truth"]["cases"][0]["source_file"] == "tests/test_widget.py"
    assert "assert result == 3" in record["ground_truth"]["cases"][1]["source_excerpt"]
    assert "solution" not in record


def test_test_patch_cases_support_go_and_javascript_names(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    patch = task_dir / "tests" / "test.patch"
    patch.parent.mkdir()
    patch.write_text(
        "diff --git a/widget_test.go b/widget_test.go\n@@ -0,0 +1,2 @@\n"
        "+func TestWidgetWorks(t *testing.T) {\n+}\n"
        "diff --git a/widget.test.ts b/widget.test.ts\n@@ -0,0 +1,2 @@\n"
        "+it('returns a widget', () => {\n+});\n",
        encoding="utf-8",
    )
    record = collect_task_record(task_dir)
    assert [case["test_name"] for case in record["ground_truth"]["cases"]] == [
        "returns a widget",
        "TestWidgetWorks",
    ]


def test_test_patch_cases_preserve_pytest_decorators_and_expand_parameters(
    tmp_path: Path,
) -> None:
    task_dir = _task_dir(tmp_path)
    patch = task_dir / "tests" / "test.patch"
    patch.parent.mkdir()
    patch.write_text(
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "@@ -0,0 +1,8 @@\n"
        "+@pytest.mark.parametrize(('value', 'expected'), [(1, 2), (3, 4)])\n"
        "+def test_doubles(value, expected):\n"
        "+    assert double(value) == expected\n",
        encoding="utf-8",
    )

    record = collect_task_record(task_dir)
    cases = record["ground_truth"]["cases"]

    assert [case["test_name"] for case in cases] == [
        "test_doubles[value=1,expected=2]",
        "test_doubles[value=3,expected=4]",
    ]
    assert {case["behavior_group_id"] for case in cases} == {
        "tests/test_widget.py::test_doubles"
    }
    assert all("@pytest.mark.parametrize" in case["source_excerpt"] for case in cases)


def test_test_patch_cases_expand_named_go_table_cases(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    patch = task_dir / "tests" / "test.patch"
    patch.parent.mkdir()
    patch.write_text(
        "diff --git a/widget_test.go b/widget_test.go\n@@ -0,0 +1,8 @@\n"
        "+func TestWidget(t *testing.T) {\n"
        "+    tests := []struct { name string }{\n"
        '+        {name: "empty input"},\n'
        '+        {name: "valid input"},\n'
        "+    }\n+}\n",
        encoding="utf-8",
    )

    record = collect_task_record(task_dir)

    assert [case["test_name"] for case in record["ground_truth"]["cases"]] == [
        "TestWidget[empty input]",
        "TestWidget[valid input]",
    ]


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
    assert "independently observable contract" in serialized


def test_staged_predictor_validates_count_and_obligation_links() -> None:
    count = validate_test_count(
        {
            "estimated_test_case_count": 12,
            "lower_bound": 9,
            "upper_bound": 16,
            "confidence": 0.7,
            "rationale": "The request introduces multiple distinct behaviors.",
        }
    )
    assert count["estimated_test_case_count"] == 12

    instruction = "Expose the result through fetch_items()."
    obligations = validate_obligations(
        instruction,
        {
            "obligations": [
                {
                    "behavior": "The function returns items.",
                    "instruction_clause_ids": ["instruction-001"],
                    "basis": "explicit",
                    "rationale": "The instruction asks to expose the function.",
                }
            ]
        },
    )
    assert obligations[0]["id"] == "obligation-001"
    assert "exactly the forecast number" in TEST_CASE_PROMPT
    assert "maxItems" not in TEST_CASES_SCHEMA["properties"]["cases"]
    assert build_test_cases_response_schema(12)["properties"]["cases"]["maxItems"] == 12


def test_predict_record_runs_count_obligation_and_case_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        output_limit: int | None = None

        def set_structured_output_token_limit(self, limit: int) -> None:
            self.output_limit = limit

    client = FakeClient()
    calls: list[dict[str, Any]] = []

    def fake_complete_json(
        _client: Any,
        _messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        calls.append(response_schema)
        properties = response_schema["properties"]
        if "estimated_test_case_count" in properties:
            return {
                "estimated_test_case_count": 1,
                "lower_bound": 1,
                "upper_bound": 1,
                "confidence": 0.8,
                "rationale": "One requested behavior.",
            }
        if "obligations" in properties:
            return {
                "obligations": [
                    {
                        "behavior": "fetch_items returns items",
                        "instruction_clause_ids": ["instruction-001"],
                        "basis": "explicit",
                        "rationale": "The instruction names the function.",
                    }
                ]
            }
        return {
            "cases": [
                {
                    "subject": "fetch_items",
                    "given": "valid inputs",
                    "when": "the function runs",
                    "then": "it returns items",
                    "category": "normal",
                    "instruction_clause_ids": ["instruction-001"],
                    "obligation_ids": ["obligation-001"],
                    "basis": "explicit",
                    "confidence": 0.8,
                    "inference_rationale": "",
                }
            ]
        }

    monkeypatch.setattr(
        predictor_module, "resolve_provider_credentials", lambda _: object()
    )
    monkeypatch.setattr(
        predictor_module, "build_workflow_client", lambda *_, **__: client
    )
    monkeypatch.setattr(predictor_module, "complete_json", fake_complete_json)
    record = {
        "task_id": "example-task",
        "input": {"instruction": "Expose fetch_items().", "validation": []},
    }

    predictions = predictor_module.predict_record(
        record, provider="test", model="test-model"
    )

    assert len(calls) == 3
    assert calls[-1]["properties"]["cases"]["maxItems"] == 1
    assert client.output_limit == predictor_module.FULL_SET_STRUCTURED_OUTPUT_TOKENS
    assert predictions["test_count_prediction"]["estimated_test_case_count"] == 1
    assert predictions["cases"][0]["obligation_evidence"][0]["obligation_id"] == (
        "obligation-001"
    )


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


def test_prediction_validator_accepts_complete_sets_larger_than_eight() -> None:
    instruction = "Expose the result through fetch_items()."
    cases = [
        {
            "subject": f"fetch_items case {index}",
            "given": f"input {index}",
            "when": f"case {index} runs",
            "then": f"result {index} is returned",
            "category": "normal",
            "instruction_clause_ids": ["instruction-001"],
            "basis": "explicit",
            "confidence": 0.8,
            "inference_rationale": "",
        }
        for index in range(12)
    ]

    predictions = validate_predictions("task", instruction, {"cases": cases})

    assert len(predictions["cases"]) == 12


def test_prediction_validator_keeps_distinct_inputs_with_same_expected_result() -> None:
    template = {
        "subject": "fetch_items",
        "when": "the method runs",
        "then": "it returns items",
        "category": "normal",
        "instruction_clause_ids": ["instruction-001"],
        "basis": "explicit",
        "confidence": 0.8,
        "inference_rationale": "",
    }
    response = {
        "cases": [
            {**template, "given": "one matching item"},
            {**template, "given": "multiple matching items"},
        ]
    }

    predictions = validate_predictions(
        "task", "Expose the result through fetch_items().", response
    )

    assert len(predictions["cases"]) == 2


def test_review_scoring_reports_full_set_exact_and_partial_coverage() -> None:
    record = _scoring_record()
    predictions = {
        "task_id": "example-task",
        "test_count_prediction": {
            "estimated_test_case_count": 3,
            "lower_bound": 2,
            "upper_bound": 4,
        },
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

    report = score_reviews([(record, predictions, review)])
    metrics = report["tasks"][0]

    assert metrics["matched_exact"] == 1
    assert metrics["matched_partial"] == 1
    assert metrics["unsupported_predictions"] == 1
    assert metrics["exact_precision"] == pytest.approx(1 / 3)
    assert metrics["coverage_recall"] == 1.0
    assert metrics["prediction_count"] == 3
    assert metrics["ground_truth_count"] == 2
    assert "at_k" not in metrics
    assert metrics["test_case_count"] == {
        "estimated_test_case_count": 3,
        "known_ground_truth_case_count": 2,
        "absolute_error": 1,
        "exact_match": False,
        "within_bounds": True,
    }
    assert report["aggregate"]["test_case_count_prediction"] == {
        "task_count": 1,
        "mean_absolute_error": 1.0,
        "exact_count_accuracy": 0.0,
        "interval_coverage": 1.0,
    }


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


def _scoring_record() -> dict[str, Any]:
    return {
        "schema_version": "deepswe-test-prediction-task-v2",
        "task_id": "example-task",
        "repository_id": "example-repo",
        "input": {"instruction": "Implement behavior", "validation": []},
        "ground_truth": {
            "availability": "patch_test_cases",
            "cases": [
                {"id": "test-a", "test_name": "test_a", "behavior_group_id": "test_a"},
                {"id": "test-b", "test_name": "test_b", "behavior_group_id": "test_b"},
            ],
        },
        "provenance": {},
    }
