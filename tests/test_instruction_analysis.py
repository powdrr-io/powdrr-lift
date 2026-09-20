from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.workrr.instruction_analysis import (
    analyze_instruction_file,
    run_instruction_analysis_flow,
)


class FakePlanningClient:
    def complete_json(self, messages: list[dict[str, str]], **_: Any) -> dict[str, Any]:
        assert "instruction" in messages[-1]["content"]
        return {
            "plan": {
                "features": [
                    {
                        "id": "instruction-analysis",
                        "description": "Add the analysis endpoint.",
                        "action": "added",
                        "intent_effect": "Preserve the read-only planning boundary.",
                    }
                ],
                "acceptance_criteria": [
                    {"id": "returns-report", "description": "A report is returned."}
                ],
            }
        }


def test_analyze_instruction_compiles_packet_without_writing_repo(
    tmp_path: Path,
) -> None:
    current = tmp_path / "docs" / "structrr" / "current"
    current.mkdir(parents=True)
    baseline = current / "baseline-001.yaml"
    baseline.write_text(
        yaml.safe_dump(
            {
                "schema": "https://powdrr.io/schema/changelog-v2",
                "active_intent": [
                    {
                        "clause_id": "readonly-boundary",
                        "intent_id": "intent-1",
                        "kind": "invariant",
                        "statement": "Analysis does not edit the repository.",
                        "source_ref": "baseline",
                        "version": 1,
                        "active": True,
                    }
                ],
                "entities": [],
            }
        ),
        encoding="utf-8",
    )
    instruction = tmp_path / "request.txt"
    instruction.write_text("Add a read-only analysis endpoint.", encoding="utf-8")

    before = sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")
    )
    report = analyze_instruction_file(
        instruction,
        repo_root=tmp_path,
        work_item_name="Instruction Analysis",
        planning_client=FakePlanningClient(),
    )
    after = sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")
    )

    assert before == after
    assert report["status"] == "proposal_only"
    assert report["implementation_authorized"] is False
    assert report["proposal_revision"]["operations"]
    assert report["feature_obligations"][0]["plan_refs"] == [
        "acceptance_criteria.returns-report"
    ]
    assert report["proposal_review"]["passed"] is True
    json.dumps(report)


def test_analyze_instruction_runs_through_procedrr(
    tmp_path: Path,
) -> None:
    current = tmp_path / "docs" / "structrr" / "current"
    current.mkdir(parents=True)
    (current / "baseline-001.yaml").write_text(
        yaml.safe_dump({"entities": [], "active_intent": []}), encoding="utf-8"
    )
    instruction = tmp_path / "request.txt"
    instruction.write_text("Add the endpoint.", encoding="utf-8")

    report = run_instruction_analysis_flow(
        instruction,
        repo_root=tmp_path,
        work_item_name="Procedrr Analysis",
        planning_client=FakePlanningClient(),
    )

    assert report["status"] == "proposal_only"
    assert report["implementation_authorized"] is False
