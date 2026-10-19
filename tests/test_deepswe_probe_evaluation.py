from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.deepswe_probe_evaluation import (
    DeepSWEProbeEvaluationError,
    _discover_test_ids,
    evaluate_deepswe_probes,
)


class _Judge:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del response_schema
        self.messages.append(messages)
        return self.response


def _setup(tmp_path: Path) -> tuple[Path, Path, str, str]:
    task_dir = tmp_path / "task"
    run_dir = tmp_path / "run"
    (task_dir / "tests").mkdir(parents=True)
    (run_dir / "postrun-probes").mkdir(parents=True)
    instruction = "Add a function that returns the normalized value."
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")
    (task_dir / "task.toml").write_text(
        '[metadata]\ntask_id = "sample-task"\n', encoding="utf-8"
    )
    verifier_patch = """\
diff --git a/tests/test_feature.py b/tests/test_feature.py
new file mode 100644
--- /dev/null
+++ b/tests/test_feature.py
@@ -0,0 +1,4 @@
+def test_returns_normalized_value():
+    assert normalize(" X ") == "x"
+
+def test_rejects_empty_value():
+    assert normalize("") is None
"""
    (task_dir / "tests" / "test.patch").write_text(verifier_patch, encoding="utf-8")
    probes = [
        {
            "probe_id": "probe-1",
            "scenario_id": "scenario-1",
            "oracle_basis": "explicit",
            "imports": ["sample"],
            "test_source": "assert normalize(' X ') == 'x'",
        },
        {
            "probe_id": "probe-2",
            "scenario_id": "scenario-2",
            "oracle_basis": "inferred",
            "imports": ["sample"],
            "test_source": "assert normalize(' x ') == 'x'",
        },
        {
            "probe_id": "probe-3",
            "scenario_id": "scenario-3",
            "oracle_basis": "unsupported",
            "imports": ["sample"],
            "test_source": "assert normalize(None) == ''",
        },
    ]
    suite = {
        "schema_version": "instruction-derived-probe-suite-v2",
        "generation_status": "generated",
        "source_instruction": instruction,
        "probes": probes,
        "scenarios_without_executable_probe": [],
    }
    (run_dir / "postrun-probes" / "probe-suite.json").write_text(
        json.dumps(suite), encoding="utf-8"
    )
    return task_dir, run_dir, instruction, verifier_patch


def _assessment(
    probe_id: str,
    *,
    coverage: str,
    matching_test_ids: list[str],
    instruction_support: str,
    probe_evidence: str,
    verifier_evidence: str = "",
) -> dict[str, Any]:
    return {
        "probe_id": probe_id,
        "coverage": coverage,
        "matching_test_ids": matching_test_ids,
        "instruction_support": instruction_support,
        "probe_evidence": probe_evidence,
        "verifier_evidence": verifier_evidence,
        "rationale": "The observable values and expected outcome match.",
    }


def test_evaluator_reports_probe_precision_recall_and_unsupported_probes(
    tmp_path: Path,
) -> None:
    task_dir, run_dir, _, verifier_patch = _setup(tmp_path)
    first_test = "tests/test_feature.py::test_returns_normalized_value"
    second_test = "tests/test_feature.py::test_rejects_empty_value"
    judge = _Judge(
        {
            "test_cases": [
                {
                    "test_id": first_test,
                    "behavior": "Whitespace is trimmed and the value lowercased.",
                    "evaluable": True,
                    "verifier_evidence": 'assert normalize(" X ") == "x"',
                },
                {
                    "test_id": second_test,
                    "behavior": "An empty value returns None.",
                    "evaluable": True,
                    "verifier_evidence": 'assert normalize("") is None',
                },
            ],
            "probe_assessments": [
                _assessment(
                    "probe-1",
                    coverage="exact",
                    matching_test_ids=[first_test],
                    instruction_support="explicit",
                    probe_evidence="assert normalize(' X ') == 'x'",
                    verifier_evidence='assert normalize(" X ") == "x"',
                ),
                _assessment(
                    "probe-2",
                    coverage="none",
                    matching_test_ids=[],
                    instruction_support="inferred",
                    probe_evidence="assert normalize(' x ') == 'x'",
                ),
                _assessment(
                    "probe-3",
                    coverage="none",
                    matching_test_ids=[],
                    instruction_support="unsupported",
                    probe_evidence="assert normalize(None) == ''",
                ),
            ],
        }
    )

    report = evaluate_deepswe_probes(
        task_dir=task_dir,
        run_dir=run_dir,
        judge=judge,
        judge_id="fixture/judge",
    )

    assert report["task_id"] == "sample-task"
    assert report["ground_truth"]["availability"] == "individual_tests"
    assert report["test_coverage"] == {first_test: "exact", second_test: "missed"}
    assert report["summary"]["exact_case_recall"] == 0.5
    assert report["summary"]["any_case_recall"] == 0.5
    assert report["summary"]["exact_probe_precision"] == pytest.approx(1 / 3)
    assert report["summary"]["unsupported_probe_count"] == 1
    assert report["summary"]["verifier_unmatched_probe_count"] == 2
    inventory_payload = json.loads(judge.messages[0][1]["content"])
    match_payload = json.loads(judge.messages[1][1]["content"])
    assert inventory_payload["verifier_test_patch"] == verifier_patch
    assert "verifier_test_patch" not in match_payload


def test_evaluator_rejects_probe_suite_from_different_instruction(
    tmp_path: Path,
) -> None:
    task_dir, run_dir, _, _ = _setup(tmp_path)
    suite_path = run_dir / "postrun-probes" / "probe-suite.json"
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    suite["source_instruction"] = "different task"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")

    with pytest.raises(DeepSWEProbeEvaluationError, match="different instruction"):
        evaluate_deepswe_probes(
            task_dir=task_dir,
            run_dir=run_dir,
            judge=_Judge({}),
        )


def test_evaluator_reports_unavailable_suite_without_claiming_zero_recall(
    tmp_path: Path,
) -> None:
    task_dir, run_dir, _, _ = _setup(tmp_path)
    suite_path = run_dir / "postrun-probes" / "probe-suite.json"
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    suite.update(
        {
            "generation_status": "unavailable",
            "generation_error": "invalid generated Python",
            "probes": [],
            "scenarios_without_executable_probe": ["scenario-1"],
        }
    )
    suite_path.write_text(json.dumps(suite), encoding="utf-8")
    report = evaluate_deepswe_probes(
        task_dir=task_dir,
        run_dir=run_dir,
        judge=None,
    )

    assert report["evaluation_status"] == "no_executable_probes"
    assert report["summary"]["verifier_case_count"] == 2
    assert report["summary"]["unjudgeable_verifier_case_count"] == 2
    assert report["summary"]["exact_case_recall"] is None
    assert report["probe_suite"]["generation_error"] == "invalid generated Python"


def test_test_id_discovery_supports_common_verifier_languages() -> None:
    patch = """\
diff --git a/pkg/example_test.go b/pkg/example_test.go
--- a/pkg/example_test.go
+++ b/pkg/example_test.go
@@ -1,2 +1,3 @@
+func TestValue(t *testing.T) {
diff --git a/web/example.test.ts b/web/example.test.ts
--- a/web/example.test.ts
+++ b/web/example.test.ts
@@ -1,2 +1,3 @@
+it("normalizes values", () => {})
"""

    assert _discover_test_ids(patch) == [
        "pkg/example_test.go::TestValue",
        "web/example.test.ts::normalizes values",
    ]
