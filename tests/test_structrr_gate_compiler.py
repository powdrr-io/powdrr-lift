from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from powdrr_lift.structrr.gate_compiler import (
    compile_proposal_worklist,
    evaluate_structural_proposal_gate,
)
from powdrr_lift.structrr.proposal import ProposalRevision, compile_proposal_revision
from powdrr_lift.structrr.proposal_review import (
    build_structural_review_receipt,
    load_review_receipt,
    write_review_receipt,
)


def _proposal(*, intent_effect: str | None = "preserve API") -> ProposalRevision:
    content = {"id": "adapter", "action": "added"}
    if intent_effect is not None:
        content["intent_effect"] = intent_effect
    return compile_proposal_revision(
        "adapter",
        {"entities": [{"id": "adapter"}]},
        {"features": [content]},
        acceptance_criteria=("the adapter works",),
        must_preserve=("the public API",),
        non_goals=("no transport redesign",),
        allowed_paths=("src/adapter.py",),
        source_refs=("structrr:baseline.yaml",),
    )


def test_gate_compiler_emits_stable_complete_families() -> None:
    worklist = compile_proposal_worklist(
        _proposal(), active_intent_clause_ids=("intent-1",)
    )
    families = {item.family for item in worklist.specifications}

    assert families == {
        "source-coverage",
        "intent-operation-validity",
        "architecture-intent-effect",
        "affected-intent-disposition",
        "acceptance-verifier-completeness",
        "resulting-state-consistency",
    }
    assert list(worklist.specifications) == sorted(
        worklist.specifications, key=lambda item: item.decision_id
    )


def test_structural_gate_rejects_affected_intent_without_effect() -> None:
    _, failures = evaluate_structural_proposal_gate(
        _proposal(intent_effect=None), active_intent_clause_ids=("intent-1",)
    )

    assert failures == (
        "operation add:features:adapter omits affected-intent disposition",
    )


def test_structural_review_receipt_is_incomplete_until_semantic_review(
    tmp_path: Path,
) -> None:
    proposal = _proposal()
    receipt = build_structural_review_receipt(proposal)
    path = tmp_path / "proposal-review.json"
    write_review_receipt(path, receipt)

    loaded = load_review_receipt(path)
    worklist = compile_proposal_worklist(proposal)
    loaded.assert_current(proposal, worklist)

    assert loaded.accepted is False
    assert any(item.outcome.value == "unknown" for item in loaded.decision_results)

    with pytest.raises(ValueError, match="stale proposal"):
        loaded.assert_current(replace(proposal, proposal_id="other"), worklist)
