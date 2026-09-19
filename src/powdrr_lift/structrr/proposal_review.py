"""Proposal-time review receipts for the Structrr/Procedrr boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_lift.core.decision_obligation import (
    DecisionOutcome,
    DecisionResult,
    DecisionWorklist,
)
from powdrr_lift.structrr.gate_compiler import (
    compile_proposal_worklist,
    evaluate_structural_proposal_gate,
)
from powdrr_lift.structrr.proposal import ProposalRevision

PROPOSAL_REVIEW_RECEIPT_SCHEMA_VERSION = "proposal-review-receipt-v1"


@dataclass(frozen=True, slots=True)
class ProposalReviewReceipt:
    proposal_fingerprint: str
    worklist_fingerprint: str
    decision_results: tuple[DecisionResult, ...]
    accepted: bool
    schema_version: str = PROPOSAL_REVIEW_RECEIPT_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_fingerprint": self.proposal_fingerprint,
            "worklist_fingerprint": self.worklist_fingerprint,
            "decision_results": [item.to_data() for item in self.decision_results],
            "accepted": self.accepted,
        }

    def assert_current(
        self, proposal: ProposalRevision, worklist: DecisionWorklist
    ) -> None:
        if self.proposal_fingerprint != proposal.fingerprint:
            raise ValueError("proposal review receipt references a stale proposal")
        if self.worklist_fingerprint != worklist.fingerprint:
            raise ValueError("proposal review receipt references a stale worklist")
        results_by_id = {item.decision_id: item for item in self.decision_results}
        missing = [
            item.decision_id
            for item in worklist.specifications
            if item.required and item.decision_id not in results_by_id
        ]
        if missing:
            raise ValueError(f"proposal review receipt is incomplete: {missing}")
        for specification in worklist.specifications:
            result = results_by_id.get(specification.decision_id)
            if result is not None:
                result.validate_against(specification)
        expected_acceptance = all(
            results_by_id[item.decision_id].outcome is DecisionOutcome.PASS
            for item in worklist.specifications
            if item.required
        )
        if self.accepted != expected_acceptance:
            raise ValueError("proposal review receipt acceptance is inconsistent")


def compile_review_worklist(
    proposal: ProposalRevision,
    *,
    active_intent_clause_ids: Sequence[str] = (),
) -> DecisionWorklist:
    """Compile the exact proposal review worklist used by every gate."""

    return compile_proposal_worklist(
        proposal, active_intent_clause_ids=active_intent_clause_ids
    )


def build_structural_review_receipt(
    proposal: ProposalRevision,
    *,
    active_intent_clause_ids: Sequence[str] = (),
) -> ProposalReviewReceipt:
    """Create a receipt for facts that can be checked without a model.

    Semantic decisions are represented as failed/unknown obligations until a
    Procedrr judge supplies a result.  This prevents a structural check from
    masquerading as complete intent review.
    """

    worklist, failures = evaluate_structural_proposal_gate(
        proposal, active_intent_clause_ids=active_intent_clause_ids
    )
    structural_failure = "; ".join(failures)
    results: list[DecisionResult] = []
    for specification in worklist.specifications:
        if (
            specification.family
            in {
                "source-coverage",
                "intent-operation-validity",
                "acceptance-verifier-completeness",
            }
            and not structural_failure
        ):
            outcome = DecisionOutcome.PASS
            explanation = "deterministic structural check passed"
        else:
            outcome = (
                DecisionOutcome.FAIL if structural_failure else DecisionOutcome.UNKNOWN
            )
            explanation = structural_failure or "requires a Procedrr semantic review"
        results.append(
            DecisionResult(
                decision_id=specification.decision_id,
                outcome=outcome,
                explanation=explanation,
                predicate_version=specification.predicate_version,
                subject=specification.subject,
                input_fingerprint=specification.input_fingerprint,
                evidence_fingerprint=worklist.fingerprint,
            )
        )
    receipt = ProposalReviewReceipt(
        proposal_fingerprint=proposal.fingerprint,
        worklist_fingerprint=worklist.fingerprint,
        decision_results=tuple(results),
        accepted=False,
    )
    receipt.assert_current(proposal, worklist)
    return receipt


def load_review_receipt(path: Path) -> ProposalReviewReceipt:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("proposal review receipt must contain an object")
    if raw.get("schema_version") != PROPOSAL_REVIEW_RECEIPT_SCHEMA_VERSION:
        raise ValueError("unsupported proposal review receipt schema version")
    raw_results = raw.get("decision_results")
    if not isinstance(raw_results, list):
        raise ValueError("proposal review receipt decision_results must be a list")
    results = tuple(_result_from_data(item) for item in raw_results)
    accepted = raw.get("accepted")
    if not isinstance(accepted, bool):
        raise ValueError("proposal review receipt accepted must be a boolean")
    return ProposalReviewReceipt(
        proposal_fingerprint=_string(raw, "proposal_fingerprint"),
        worklist_fingerprint=_string(raw, "worklist_fingerprint"),
        decision_results=results,
        accepted=accepted,
    )


def write_review_receipt(path: Path, receipt: ProposalReviewReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_data(), indent=2, sort_keys=True) + "\n")


def _result_from_data(raw: Any) -> DecisionResult:
    if not isinstance(raw, Mapping):
        raise ValueError("decision result must contain an object")
    return DecisionResult(
        decision_id=_string(raw, "decision_id"),
        outcome=DecisionOutcome(_string(raw, "outcome")),
        explanation=_string(raw, "explanation"),
        predicate_version=_string(raw, "predicate_version"),
        subject=_string(raw, "subject"),
        input_fingerprint=_string(raw, "input_fingerprint"),
        evidence_fingerprint=_string(raw, "evidence_fingerprint"),
    )


def _string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


__all__ = [
    "PROPOSAL_REVIEW_RECEIPT_SCHEMA_VERSION",
    "ProposalReviewReceipt",
    "build_structural_review_receipt",
    "compile_review_worklist",
    "load_review_receipt",
    "write_review_receipt",
]
