"""Compile Structrr proposal data into a deterministic decision worklist."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.core.decision_obligation import (
    DecisionSpecification,
    DecisionWorklist,
)
from powdrr_lift.structrr.proposal import ProposalRevision


def compile_proposal_worklist(
    proposal: ProposalRevision,
    *,
    active_intent_clause_ids: Sequence[str] = (),
    evidence_fingerprints: Mapping[str, str] | None = None,
) -> DecisionWorklist:
    """Create the complete ordered proposal-review worklist.

    Every item has one subject and one predicate.  The compiler is deliberately
    mechanical: adding an operation or acceptance criterion adds obligations;
    it never removes a required gate because a model claimed it was covered.
    """

    evidence_fingerprints = evidence_fingerprints or {}
    specs: list[DecisionSpecification] = []

    def evidence_refs(labels: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            f"{label}@{evidence_fingerprints[label]}"
            if label in evidence_fingerprints
            else label
            for label in labels
        )

    for source_ref in proposal.source_refs:
        specs.append(
            _spec(
                proposal,
                "source-coverage",
                source_ref,
                "the proposal source is present and addressable",
                evidence_requirements=evidence_refs((source_ref,)),
            )
        )
    for operation in proposal.operations:
        subject = operation.operation_id
        specs.extend(
            (
                _spec(
                    proposal,
                    "intent-operation-validity",
                    subject,
                    "the operation has one supported action and a stable subject",
                    evidence_requirements=evidence_refs(("proposal", "plan")),
                ),
                _spec(
                    proposal,
                    "architecture-intent-effect",
                    subject,
                    "the operation explicitly declares its intent effect",
                    evidence_requirements=evidence_refs(("proposal", "plan")),
                ),
                _spec(
                    proposal,
                    "affected-intent-disposition",
                    subject,
                    "every affected active intent clause has an explicit disposition",
                    active_intent_clause_ids,
                    evidence_requirements=evidence_refs(
                        (
                            "proposal",
                            "plan",
                            *(f"intent:{item}" for item in active_intent_clause_ids),
                        )
                    ),
                ),
            )
        )
    for index, criterion in enumerate(proposal.acceptance_criteria, start=1):
        specs.append(
            _spec(
                proposal,
                "acceptance-verifier-completeness",
                f"criterion-{index}",
                f"acceptance criterion is verifiable: {criterion}",
                evidence_requirements=evidence_refs(("proposal", "plan")),
            )
        )
    specs.append(
        _spec(
            proposal,
            "resulting-state-consistency",
            proposal.proposal_id,
            "the resulting Structrr state is self-consistent",
            evidence_requirements=evidence_refs(("baseline", "proposal", "plan")),
        )
    )
    return DecisionWorklist.compile(tuple(specs))


def evaluate_structural_proposal_gate(
    proposal: ProposalRevision,
    *,
    active_intent_clause_ids: Sequence[str] = (),
    evidence_fingerprints: Mapping[str, str] | None = None,
) -> tuple[DecisionWorklist, tuple[str, ...]]:
    """Return the worklist and deterministic structural failures.

    Semantic predicates remain obligations for Procedrr/Workrr judges.  This
    function only rejects facts that are mechanically invalid and therefore
    must never be delegated to an LLM.
    """

    worklist = compile_proposal_worklist(
        proposal,
        active_intent_clause_ids=active_intent_clause_ids,
        evidence_fingerprints=evidence_fingerprints,
    )
    failures: list[str] = []
    if not proposal.operations:
        failures.append("proposal must contain at least one operation")
    if not proposal.acceptance_criteria:
        failures.append("proposal must contain at least one acceptance criterion")
    if not proposal.allowed_paths:
        failures.append("proposal must contain at least one allowed path")
    for operation in proposal.operations:
        if operation.action not in {"add", "remove"}:
            failures.append(f"unsupported operation action: {operation.action}")
        if not operation.subject_id.strip():
            failures.append(f"operation has an empty subject: {operation.operation_id}")
        if active_intent_clause_ids and not _has_intent_disposition(operation.content):
            failures.append(
                f"operation {operation.operation_id} omits affected-intent disposition"
            )
    return worklist, tuple(failures)


def _spec(
    proposal: ProposalRevision,
    family: str,
    subject: str,
    predicate: str,
    active_intent_clause_ids: Sequence[str] = (),
    evidence_requirements: Sequence[str] = (),
) -> DecisionSpecification:
    input_data = {
        "proposal": proposal.fingerprint,
        "family": family,
        "subject": subject,
        "predicate": predicate,
        "active_intent_clause_ids": sorted(active_intent_clause_ids),
        "evidence_requirements": list(evidence_requirements),
    }
    encoded = json.dumps(input_data, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return DecisionSpecification(
        decision_id=f"{family}:{subject}",
        family=family,
        subject=subject,
        predicate=predicate,
        input_fingerprint=fingerprint,
        evidence_requirements=tuple(evidence_requirements),
    )


def _has_intent_disposition(content: Mapping[str, Any]) -> bool:
    value = content.get("intent_effect")
    return isinstance(value, str) and bool(value.strip())


__all__ = [
    "compile_proposal_worklist",
    "evaluate_structural_proposal_gate",
]
