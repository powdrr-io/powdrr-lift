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


def test_feature_design_compiler_rejects_model_authored_structural_fields() -> None:
    ledger = compile_instruction_ledger("feature", "One behavior.")
    semantic = _semantic(1)
    semantic[0]["id"] = "invented-by-model"

    design = compile_feature_design(ledger, "feature", semantic)

    assert design.obligations[0].obligation_id == "obligation:instruction-001"
