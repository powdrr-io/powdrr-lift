"""Workrr orchestration for deterministic subject lookup and C09 binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from powdrr_lift.core.repository_inventory import (
    CandidateSet,
    InventoryError,
    LookupQuery,
    RepositoryInventory,
    StructrrLookupContext,
    aggregate_candidate_relations,
    retrieve_candidates,
)
from powdrr_lift.core.semantic_contract import PartialSemanticContract
from powdrr_lift.core.semantic_decision import (
    SemanticDecision,
    SemanticDecisionProvider,
    SemanticDecisionSpec,
)

CANDIDATE_RELATION_REVISION = "candidate-relation-v1"


def prepare_subject_lookup_query(
    contract: PartialSemanticContract,
    inventory: RepositoryInventory,
    *,
    explicit_names: Sequence[str] = (),
    structrr_context_fingerprint: str = "",
) -> LookupQuery:
    return LookupQuery(
        source_ref=contract.source_ref,
        source_text=contract.proposition_text,
        subject_text=contract.subject.span.text,
        disposition=contract.disposition,
        behavior_family=contract.behavior_family,
        inventory_fingerprint=inventory.fingerprint,
        explicit_names=tuple(explicit_names),
        structrr_context_fingerprint=structrr_context_fingerprint,
    )


def retrieve_subject_candidates(
    query: LookupQuery,
    inventory: RepositoryInventory,
    context: StructrrLookupContext | None = None,
) -> CandidateSet:
    return retrieve_candidates(query, inventory, context)


def prepare_candidate_relation_decisions(
    query: LookupQuery,
    candidate_set: CandidateSet,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for candidate in candidate_set.candidates:
        spec = SemanticDecisionSpec(
            decision_id=f"decision:{query.source_ref}:candidate:{candidate.record.inventory_id}",
            decision_kind="candidate_relation",
            subject_ref=query.source_ref,
            proposition_text=query.source_text,
            source_fingerprint=query.fingerprint,
            candidate_set_fingerprint=candidate.record.evidence_fingerprint,
            contract_revision=CANDIDATE_RELATION_REVISION,
        )
        requests.append(
            {
                "spec": spec.to_data(),
                "question": (
                    "Does this one repository candidate denote the exact "
                    "source subject?"
                ),
                "instructions": [
                    (
                        "Choose matches only when the candidate evidence supports "
                        "the exact source subject."
                    ),
                    (
                        "Choose does_not_match when evidence contradicts the "
                        "source subject."
                    ),
                    (
                        "Choose insufficient_evidence when the candidate cannot "
                        "be confirmed."
                    ),
                    (
                        "Do not compare this candidate with other candidates or "
                        "choose a winner."
                    ),
                ],
                "candidate": candidate.to_data(),
                "allowed_values": [
                    "matches",
                    "does_not_match",
                    "insufficient_evidence",
                ],
            }
        )
    return requests


def bind_candidate_relation_decisions(
    requests: Sequence[Mapping[str, Any]],
    provider_results: Sequence[Mapping[str, Any]],
    *,
    created_at: str | None = None,
) -> list[SemanticDecision]:
    if len(requests) != len(provider_results):
        raise InventoryError("candidate relation result count is invalid")
    timestamp = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    decisions: list[SemanticDecision] = []
    for request, result in zip(requests, provider_results, strict=True):
        raw_spec = request.get("spec")
        if not isinstance(raw_spec, Mapping):
            raise InventoryError("candidate relation request has no spec")
        spec = SemanticDecisionSpec.from_data(raw_spec)
        decisions.append(
            spec.bind(
                provider=SemanticDecisionProvider(kind="planning-llm"),
                provider_result=result,
                evidence_refs=(f"source-proposition:{spec.subject_ref}",),
                created_at=timestamp,
            )
        )
    return decisions


def finalize_subject_binding(
    candidate_set: CandidateSet,
    decisions: Sequence[SemanticDecision],
    *,
    quantifier: str,
    population_refs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if len(decisions) != len(candidate_set.candidates):
        raise InventoryError("candidate relation decision coverage is incomplete")
    by_candidate_id = {
        candidate.record.inventory_id: decision.result.value
        for candidate, decision in zip(candidate_set.candidates, decisions, strict=True)
    }
    if any(value is None for value in by_candidate_id.values()):
        raise InventoryError("candidate relation decision is unresolved")
    return aggregate_candidate_relations(
        candidate_set,
        {key: str(value) for key, value in by_candidate_id.items()},
        quantifier=quantifier,
        population_refs=population_refs,
    )


__all__ = [
    "CANDIDATE_RELATION_REVISION",
    "bind_candidate_relation_decisions",
    "finalize_subject_binding",
    "prepare_candidate_relation_decisions",
    "prepare_subject_lookup_query",
    "retrieve_subject_candidates",
]
