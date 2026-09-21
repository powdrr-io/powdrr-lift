from __future__ import annotations

import pytest

from powdrr_lift.core.feature_obligation import (
    FeatureObligationError,
    compile_feature_design,
)
from powdrr_lift.core.instruction_ledger import compile_instruction_ledger


def _semantic(count: int) -> list[dict[str, object]]:
    return [
        {
            "design": {
                "kind": "feature",
                "description": f"Implement behavior {index}.",
                "acceptance_criterion": f"Behavior {index} is observable.",
                "expected_test": f"Test behavior {index}.",
            }
        }
        for index in range(1, count + 1)
    ]


def test_feature_design_compiler_owns_ids_references_and_selectors() -> None:
    ledger = compile_instruction_ledger(
        "state-data", "First behavior. Second behavior."
    )

    design = compile_feature_design(ledger, "state-data", _semantic(2))

    assert [item.design_id for item in design.projections] == [
        "design:instruction-001",
        "design:instruction-002",
    ]
    assert [item.obligation_id for item in design.obligations] == [
        "obligation:instruction-001",
        "obligation:instruction-002",
    ]
    assert [item.test_id for item in design.test_contracts] == [
        "test:obligation:instruction-001",
        "test:obligation:instruction-002",
    ]
    assert design.test_contracts[0].selector == (
        "tests/test_state-data_instruction_001.py::test_instruction_001"
    )
    data = design.to_data()
    assert data["structrr"]["source_refs"] == ["instruction-ledger"]
    assert all("id" not in item for item in data["obligations"])


def test_feature_design_compiler_rejects_missing_clause_projection() -> None:
    ledger = compile_instruction_ledger("feature", "First behavior. Second behavior.")

    with pytest.raises(FeatureObligationError, match="count must match"):
        compile_feature_design(ledger, "feature", _semantic(1))


def test_feature_design_compiler_allows_non_required_clauses() -> None:
    ledger = compile_instruction_ledger(
        "feature", "First behavior. Second behavior. Third behavior."
    )

    design = compile_feature_design(
        ledger,
        "feature",
        _semantic(3),
        required_clause_ids=("instruction-002",),
    )

    assert len(design.projections) == 3
    assert [item.obligation_id for item in design.obligations] == [
        "obligation:instruction-002"
    ]
    assert [item.obligation_id for item in design.test_contracts] == [
        "obligation:instruction-002"
    ]


def test_feature_design_compiler_rejects_model_authored_structural_fields() -> None:
    ledger = compile_instruction_ledger("feature", "One behavior.")
    semantic = _semantic(1)
    semantic[0]["id"] = "invented-by-model"

    design = compile_feature_design(ledger, "feature", semantic)

    assert design.obligations[0].obligation_id == "obligation:instruction-001"


def test_nonactionable_clause_is_trace_only() -> None:
    ledger = compile_instruction_ledger(
        "feature", "Implement state data. Create a branch from main."
    )
    semantic = _semantic(2)
    semantic[1]["design"] = {
        "kind": "nonactionable",
        "description": "Ignore the branch instruction as process metadata.",
        "acceptance_criterion": (
            "No product obligation is created for the branch instruction."
        ),
        "expected_test": "No product test is required.",
    }

    design = compile_feature_design(ledger, "feature", semantic)

    assert [item.clause_id for item in design.projections] == [
        "instruction-001",
        "instruction-002",
    ]
    assert [item.clause_id for item in design.obligations] == ["instruction-001"]
    assert len(design.test_contracts) == 1


def test_non_goal_cannot_invert_a_missing_capability_into_a_prohibition() -> None:
    ledger = compile_instruction_ledger("feature", "States lack data ownership.")
    semantic = [
        {
            "design": {
                "kind": "non_goal",
                "description": "Do not implement data ownership.",
                "acceptance_criterion": "Data ownership is absent.",
                "expected_test": "Verify data ownership is absent.",
            }
        }
    ]

    with pytest.raises(FeatureObligationError, match="explicit product prohibition"):
        compile_feature_design(ledger, "feature", semantic)
