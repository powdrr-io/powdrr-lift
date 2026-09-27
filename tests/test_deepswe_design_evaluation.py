from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from powdrr_lift.workrr.deepswe_design_evaluation import (
    DeepSWEEvaluationError,
    evaluate_deepswe_design,
)


class FakeJudge:
    def __init__(
        self, *, decision: str = "supported", quote: str | None = None
    ) -> None:
        self.decision = decision
        self.quote = quote
        self.calls: list[list[dict[str, str]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        self.calls.append(messages)
        payload = json.loads(messages[1]["content"])
        candidate_text = payload["candidate_design_projection"]
        quote = self.quote or (
            "Adds value"
            if "Adds value" in candidate_text
            else candidate_text.splitlines()[0]
        )
        return {
            "decision": self.decision,
            "evidence_quote": quote,
            "reason": "The candidate projection represents the behavior.",
        }


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    task_dir = tmp_path / "task"
    run_dir = tmp_path / "run"
    task_dir.mkdir()
    (task_dir / "solution").mkdir()
    (task_dir / "tests").mkdir()
    run_dir.mkdir()
    instruction = (
        "The API adds a value.\nA branch and commit are process instructions.\n"
    )
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")
    (task_dir / "task.toml").write_text(
        '[metadata]\ntask_id = "demo-task"\n', encoding="utf-8"
    )
    (task_dir / "solution" / "solution.patch").write_text(
        "diff --git a/api.py b/api.py\n+def add_value(): pass\n", encoding="utf-8"
    )
    (task_dir / "tests" / "test.patch").write_text(
        "+def test_add_value():\n+    assert add_value() == 1\n", encoding="utf-8"
    )
    ledger = {
        "schema_version": "instruction-ledger-v1",
        "fingerprint": "ledger-fingerprint",
        "source": {"text": instruction},
        "clauses": [
            {
                "clause_id": "instruction-001",
                "text": "The API adds a value.",
                "source_span": {"start": 0, "end": len("The API adds a value.")},
            },
            {
                "clause_id": "instruction-002",
                "text": "A branch and commit are process instructions.",
                "source_span": {
                    "start": instruction.index("A branch"),
                    "end": instruction.index("\n", instruction.index("A branch")),
                },
            },
        ],
    }
    (run_dir / "instruction-ledger.json").write_text(
        json.dumps(ledger), encoding="utf-8"
    )
    design = {
        "schema_version": "feature-design-v2",
        "fingerprint": "design-fingerprint",
        "ledger_fingerprint": "ledger-fingerprint",
        "projections": [
            {
                "clause_id": "instruction-001",
                "kind": "feature",
                "description": "Adds value",
                "acceptance_criterion": "Adds value",
                "expected_test": "Adds value",
            },
            {
                "clause_id": "instruction-002",
                "kind": "nonactionable",
                "description": "Branch and commit instructions are process only",
                "acceptance_criterion": "Do not create a product obligation",
                "expected_test": "No product test is needed",
            },
        ],
    }
    (run_dir / "canonical-feature-design.json").write_text(
        json.dumps(design), encoding="utf-8"
    )
    rubric_path = tmp_path / "rubric.yaml"
    rubric_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "deepswe-design-gold-v1",
                "task_id": "demo-task",
                "criteria": [
                    {
                        "id": "api-adds-value",
                        "importance": "critical",
                        "instruction_excerpt": "The API adds a value.",
                        "expected_behavior": "The API returns an added value.",
                        "solution_symbols": ["add_value"],
                        "verifier_tests": ["test_add_value"],
                    },
                    {
                        "id": "process-only",
                        "importance": "important",
                        "instruction_excerpt": (
                            "A branch and commit are process instructions."
                        ),
                        "expected_behavior": "Process directions are not product work.",
                        "solution_symbols": [],
                        "verifier_tests": [],
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return task_dir, run_dir, rubric_path


def test_evaluation_uses_task_references_and_requires_candidate_evidence(
    tmp_path: Path,
) -> None:
    task_dir, run_dir, rubric_path = _inputs(tmp_path)
    judge = FakeJudge()

    report = evaluate_deepswe_design(
        task_dir=task_dir,
        run_dir=run_dir,
        judge=judge,
        rubric_path=rubric_path,
    )

    assert report["summary"]["passed"] is True
    assert report["summary"]["weighted_coverage"] == 1.0
    assert report["findings"][0]["ledger_clause_ids"] == ["instruction-001"]
    assert report["findings"][1]["decision"] == "supported"
    assert len(judge.calls) == 2
    assert "test_add_value" in judge.calls[0][1]["content"]
    assert "add_value" in judge.calls[0][1]["content"]


def test_missing_canonical_projection_is_a_critical_failure(tmp_path: Path) -> None:
    task_dir, run_dir, rubric_path = _inputs(tmp_path)
    design_path = run_dir / "canonical-feature-design.json"
    design = json.loads(design_path.read_text(encoding="utf-8"))
    design["projections"] = [
        item for item in design["projections"] if item["clause_id"] != "instruction-001"
    ]
    design_path.write_text(json.dumps(design), encoding="utf-8")

    report = evaluate_deepswe_design(
        task_dir=task_dir,
        run_dir=run_dir,
        judge=FakeJudge(),
        rubric_path=rubric_path,
    )

    assert report["findings"][0]["decision"] == "missing"
    assert report["summary"]["critical_failures"] == ["api-adds-value"]
    assert report["summary"]["passed"] is False


def test_reference_task_identity_must_match_rubric(tmp_path: Path) -> None:
    task_dir, run_dir, rubric_path = _inputs(tmp_path)
    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    rubric["task_id"] = "another-task"
    rubric_path.write_text(yaml.safe_dump(rubric), encoding="utf-8")

    with pytest.raises(DeepSWEEvaluationError, match="rubric is for"):
        evaluate_deepswe_design(
            task_dir=task_dir,
            run_dir=run_dir,
            judge=FakeJudge(),
            rubric_path=rubric_path,
        )


def test_judge_must_quote_exact_candidate_text(tmp_path: Path) -> None:
    task_dir, run_dir, rubric_path = _inputs(tmp_path)

    report = evaluate_deepswe_design(
        task_dir=task_dir,
        run_dir=run_dir,
        judge=FakeJudge(quote="invented evidence"),
        rubric_path=rubric_path,
    )

    assert report["findings"][0]["decision"] == "ambiguous"
    assert "not present" in report["findings"][0]["reason"]
    assert report["summary"]["passed"] is False
