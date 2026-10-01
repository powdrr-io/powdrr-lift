"""Workrr orchestration for deterministic subject lookup and C09 binding."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
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
    inferred_names = extract_explicit_repository_names(contract.proposition_text)
    return LookupQuery(
        source_ref=contract.source_ref,
        source_text=contract.proposition_text,
        subject_text=contract.subject.span.text,
        disposition=contract.disposition,
        behavior_family=contract.behavior_family,
        inventory_fingerprint=inventory.fingerprint,
        explicit_names=tuple(dict.fromkeys((*explicit_names, *inferred_names))),
        structrr_context_fingerprint=structrr_context_fingerprint,
    )


def retrieve_subject_candidates(
    query: LookupQuery,
    inventory: RepositoryInventory,
    context: StructrrLookupContext | None = None,
) -> CandidateSet:
    explicit_names = {name.casefold() for name in query.explicit_names}
    subject_text = query.subject_text.casefold().strip()
    subject_terms = frozenset(query.normalized_terms)
    accepted_aliases = {
        alias.casefold()
        for alias in (
            context.aliases.get(query.subject_text.casefold(), ()) if context else ()
        )
    }
    relevant_records = tuple(
        record
        for record in inventory.records
        if (
            record.canonical_name.casefold() in explicit_names
            or record.qualified_name.casefold() in explicit_names
            or any(
                record.qualified_name.casefold().endswith("." + name)
                for name in explicit_names
            )
            or record.canonical_name.casefold() == subject_text
            or record.qualified_name.casefold() == subject_text
            or record.canonical_name.casefold() in accepted_aliases
            or frozenset(record.normalized_terms) == subject_terms
        )
    )
    lookup_inventory = RepositoryInventory(
        inventory.commit_ref,
        inventory.structrr_revision,
        inventory.adapter_revisions,
        relevant_records,
    )
    try:
        candidates = retrieve_candidates(query, lookup_inventory, context)
        if candidates.inventory_fingerprint != inventory.fingerprint:
            candidates = CandidateSet(
                candidates.query_fingerprint,
                inventory.fingerprint,
                candidates.candidates,
                retrieval_status=candidates.retrieval_status,
            )
        return candidates
    except InventoryError as exc:
        if str(exc) != "candidate_overflow":
            raise
        return CandidateSet(
            query.fingerprint,
            inventory.fingerprint,
            (),
            retrieval_status="candidate_overflow",
        )


def extract_explicit_repository_names(source_text: str) -> tuple[str, ...]:
    """Extract code-like names from source text for deterministic retrieval."""
    names: set[str] = set()
    generic = {"call", "get", "set", "run", "open", "main", "__init__"}

    def add_name(value: str, *, allow_generic: bool = False) -> None:
        if re.fullmatch(r"[A-Za-z_]\w*", value) is None:
            return
        if value.casefold() in generic and not allow_generic:
            return
        names.add(value)

    for quoted in re.findall(r"`([^`]+)`", source_text):
        for dotted_name in re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", quoted):
            names.add(dotted_name)
            add_name(dotted_name.rsplit(".", 1)[-1])
        for identifier in re.findall(r"(?<![\w.])[A-Za-z_]\w*(?![\w.])", quoted):
            add_name(identifier, allow_generic=True)
    for dotted_name in re.findall(
        r"(?<![\w.])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?!\w)", source_text
    ):
        names.add(dotted_name)
        add_name(dotted_name.rsplit(".", 1)[-1])
    for call_name in re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", source_text):
        add_name(call_name)
    for identifier in re.findall(
        r"(?<![\w])[A-Za-z_]\w*_[A-Za-z0-9_]+(?![\w])", source_text
    ):
        add_name(identifier)
    for identifier in re.findall(r"\b[A-Z][A-Z0-9_]*\d+[A-Z0-9_]*\b", source_text):
        add_name(identifier, allow_generic=True)
    return tuple(sorted(names, key=lambda name: (name.casefold(), name)))


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


def add_candidate_source_excerpts(
    requests: Sequence[Mapping[str, Any]],
    repository_root: Path,
    *,
    max_lines: int = 16,
    max_characters: int = 1600,
) -> list[dict[str, Any]]:
    """Attach bounded source excerpts for exact candidate review."""
    root = repository_root.resolve()
    enriched: list[dict[str, Any]] = []
    for request in requests:
        copied = dict(request)
        raw_candidate = request.get("candidate")
        if not isinstance(raw_candidate, Mapping):
            enriched.append(copied)
            continue
        candidate = dict(raw_candidate)
        raw_record = candidate.get("record")
        if isinstance(raw_record, Mapping):
            record = dict(raw_record)
            path_value = record.get("path")
            span = record.get("span")
            if isinstance(path_value, str) and isinstance(span, Mapping):
                try:
                    start_line = int(span["start_line"])
                    end_line = int(span["end_line"])
                    source_path = (root / path_value).resolve(strict=True)
                    if (
                        source_path.is_relative_to(root)
                        and source_path.is_file()
                        and start_line > 0
                        and end_line >= start_line
                    ):
                        lines = source_path.read_text(encoding="utf-8").splitlines()
                        excerpt = "\n".join(
                            lines[
                                start_line - 1 : min(
                                    end_line, start_line + max_lines - 1
                                )
                            ]
                        )
                        excerpt = excerpt[:max_characters]
                        if excerpt:
                            candidate["source_excerpt"] = {
                                "path": path_value,
                                "start_line": start_line,
                                "text": excerpt,
                            }
                except (
                    OSError,
                    RuntimeError,
                    UnicodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    pass
        copied["candidate"] = candidate
        enriched.append(copied)
    return enriched


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
    "add_candidate_source_excerpts",
    "bind_candidate_relation_decisions",
    "finalize_subject_binding",
    "extract_explicit_repository_names",
    "prepare_candidate_relation_decisions",
    "prepare_subject_lookup_query",
    "retrieve_subject_candidates",
]
