from __future__ import annotations

import pytest

from powdrr_lift.core.feature_obligation import (
    FeatureObligationError,
    compile_feature_design,
)
from powdrr_lift.core.instruction_ledger import compile_instruction_ledger
from powdrr_lift.core.semantic_contract import (
    BoundSourceExtraction,
    PartialSemanticContract,
    UnresolvedSemanticField,
)
from powdrr_lift.core.semantic_decision import (
    ExactSourceSpan,
    SemanticDecisionProvider,
)


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


def test_feature_design_compiler_owns_ids_references_and_test_name_hints() -> None:
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
    assert design.test_contracts[0].name_hint == "test_implement_behavior_1"
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


def test_workflow_clause_is_terminal_even_when_model_calls_it_a_feature() -> None:
    ledger = compile_instruction_ledger(
        "feature",
        "Implement state data. Create a branch from main and commit everything.",
    )
    semantic = [
        {
            "design": {
                "kind": "feature",
                "description": "Implement state data.",
                "acceptance_criterion": "State data works.",
                "expected_test": "Test state data.",
            }
        },
        {
            "design": {
                "kind": "feature",
                "description": "Create a branch from main and commit everything.",
                "acceptance_criterion": "The branch and commit exist.",
                "expected_test": "Verify the commit.",
            }
        },
    ]

    design = compile_feature_design(ledger, "feature", semantic)

    assert design.projections[1].kind == "nonactionable"
    assert [item.clause_id for item in design.obligations] == ["instruction-001"]


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


def test_missing_persistence_behavior_is_actionable_when_model_calls_it_non_goal() -> (
    None
):
    ledger = compile_instruction_ledger(
        "feature",
        "When fit runs with dataset.features configured, the selected raw feature "
        "schema is not persisted.",
    )
    semantic = [
        {
            "design": {
                "kind": "non_goal",
                "description": "Persist the selected raw feature schema after fit.",
                "acceptance_criterion": "The selected schema is persisted after fit.",
                "expected_test": "Verify the selected schema is persisted after fit.",
            }
        }
    ]

    design = compile_feature_design(ledger, "feature", semantic)

    assert design.projections[0].kind == "feature"
    assert design.obligations[0].projection.kind == "feature"


def test_source_contract_restores_an_included_clause_mislabeled_as_context() -> None:
    ledger = compile_instruction_ledger(
        "feature", "States lack data ownership. Add data ownership support."
    )
    semantic = _semantic(2)
    for index, clause in enumerate(ledger.clauses):
        text = clause.text
        span = ExactSourceSpan(clause.clause_id, 0, len(text), text)
        subject = BoundSourceExtraction(
            extraction_id=f"subject-{index}",
            extraction_kind="subject",
            subject_ref=clause.clause_id,
            input_fingerprint="input",
            provider=SemanticDecisionProvider(kind="deterministic-rule"),
            span=span,
            created_at="test",
        )
        behavior = BoundSourceExtraction(
            extraction_id=f"behavior-{index}",
            extraction_kind="behavior",
            subject_ref=clause.clause_id,
            input_fingerprint="input",
            provider=SemanticDecisionProvider(kind="deterministic-rule"),
            span=span,
            created_at="test",
        )
        contract = PartialSemanticContract(
            contract_id=f"contract:{clause.clause_id}",
            source_ref=clause.clause_id,
            source_fingerprint=clause.fingerprint,
            proposition_text=text,
            routing="include",
            disposition="feature",
            polarity="required",
            requirement_strength="unspecified",
            quantifier="unspecified",
            subject=subject,
            behavior=behavior,
            behavior_family="other",
            preconditions=(),
            exceptions=(),
            explicit_result=None,
            temporal_scope="unspecified",
            source_predicate="not_stated",
            semantic_dimensions=(),
            field_provenance=(),
            unresolved=(UnresolvedSemanticField("predicate", "source_underspecified"),),
        )
        semantic[index]["partial_contract"] = contract.to_data()
    semantic[0]["design"] = {
        "kind": "context",
        "description": "Background context.",
        "acceptance_criterion": "No obligation.",
        "expected_test": "No test.",
    }

    design = compile_feature_design(ledger, "feature", semantic)

    assert design.projections[0].kind == "feature"
    assert [item.clause_id for item in design.obligations] == [
        "instruction-001",
        "instruction-002",
    ]


def test_source_contract_fingerprint_must_match_the_instruction_clause() -> None:
    ledger = compile_instruction_ledger("feature", "Add data ownership support.")
    semantic = _semantic(1)
    semantic[0]["partial_contract"] = {
        "schema_version": "partial-semantic-contract-v3",
        "contract_id": "contract:instruction-001",
        "source_ref": "instruction-001",
        "source_fingerprint": "sha256:stale",
        "proposition_text": "Add data ownership support.",
    }

    with pytest.raises(FeatureObligationError, match="source semantic contract"):
        compile_feature_design(ledger, "feature", semantic)


def test_required_rejection_is_compiled_as_an_actionable_obligation() -> None:
    ledger = compile_instruction_ledger(
        "state-data", "DataVar rejects simultaneous default and factory."
    )
    semantic = [
        {
            "design": {
                "kind": "non_goal",
                "description": "Reject simultaneous default and factory.",
                "acceptance_criterion": "The invalid combination raises an error.",
                "expected_test": "Assert construction fails for both arguments.",
            }
        }
    ]

    design = compile_feature_design(ledger, "state-data", semantic)

    assert design.projections[0].kind == "feature"
    assert design.obligations[0].projection.kind == "feature"
    assert design.test_contracts[0].obligation_id == "obligation:instruction-001"
