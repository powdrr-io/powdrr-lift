"""Workrr orchestration for deterministic subject lookup and C09 binding."""

from __future__ import annotations

import json
import re
from collections import Counter
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
    alias_targets = {
        targets[0].casefold()
        for name in explicit_names
        if context is not None
        and len(targets := context.symbol_aliases.get(name, ())) == 1
    }
    relationship_sources = explicit_names | alias_targets
    import_targets = {
        relation.partition("|")[2].casefold()
        for name in relationship_sources
        if context is not None
        for relation in context.relationships.get(name, ())
        if relation.startswith("imports|")
    }
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
            or record.qualified_name.casefold() in alias_targets
            or record.qualified_name.casefold() in import_targets
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
        candidates = narrow_qualified_candidates(query, candidates)
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


def narrow_qualified_candidates(
    query: LookupQuery, candidate_set: CandidateSet
) -> CandidateSet:
    """Keep exact qualified-name matches when source text identifies one."""
    qualified_hints = tuple(
        name.casefold()
        for name in extract_explicit_repository_names(query.subject_text)
        if "." in name
    )
    matching = tuple(
        candidate
        for candidate in candidate_set.candidates
        if any(
            candidate.record.qualified_name.casefold() == name
            or candidate.record.qualified_name.casefold().endswith("." + name)
            for name in qualified_hints
        )
    )
    if not matching:
        return candidate_set
    return CandidateSet(
        candidate_set.query_fingerprint,
        candidate_set.inventory_fingerprint,
        matching,
        retrieval_status=candidate_set.retrieval_status,
    )


def prepare_candidate_relation_decisions(
    query: LookupQuery,
    candidate_set: CandidateSet,
    context: StructrrLookupContext | None = None,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    name_counts = Counter(
        candidate.record.canonical_name.casefold()
        for candidate in candidate_set.candidates
    )
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
        request: dict[str, Any] = {
            "spec": spec.to_data(),
            "question": (
                "Does this one repository candidate denote the exact source subject?"
            ),
            "instructions": [
                (
                    "Choose matches only when the candidate evidence supports "
                    "the exact source subject."
                ),
                ("Choose does_not_match when evidence contradicts the source subject."),
                (
                    "Choose insufficient_evidence when the candidate cannot "
                    "be confirmed."
                ),
                (
                    "Do not compare this candidate with other candidates or "
                    "choose a winner."
                ),
                (
                    "A generic name shared by multiple candidates is not enough "
                    "to mark every candidate as a match. Use qualified source "
                    "names and repository relationships; abstain when they do "
                    "not identify this candidate uniquely."
                ),
            ],
            "candidate": candidate.to_data(),
            "allowed_values": [
                "matches",
                "does_not_match",
                "insufficient_evidence",
            ],
        }
        if context is not None:
            relationship_evidence = context.relationships.get(
                candidate.record.qualified_name.casefold(), ()
            )
            request["repository_relationships"] = list(
                sorted(
                    relationship_evidence,
                    key=lambda item: (
                        {
                            "imports": 0,
                            "referenced_by_test": 1,
                            "imported_by": 2,
                            "contained_by": 3,
                            "contains": 4,
                        }.get(item.partition("|")[0], 4),
                        item,
                    ),
                )[:16]
            )
        if name_counts[candidate.record.canonical_name.casefold()] > 1:
            request["shared_candidate_name"] = True
        requests.append(request)
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
    subject_text: str = "",
) -> dict[str, Any]:
    if len(decisions) != len(candidate_set.candidates):
        raise InventoryError("candidate relation decision coverage is incomplete")
    by_candidate_id = {
        candidate.record.inventory_id: decision.result.value
        for candidate, decision in zip(candidate_set.candidates, decisions, strict=True)
    }
    qualified_hints = tuple(
        name.casefold()
        for name in extract_explicit_repository_names(subject_text)
        if "." in name
    )
    uniquely_qualified = [
        candidate.record.inventory_id
        for candidate in candidate_set.candidates
        if any(
            candidate.record.qualified_name.casefold() == name
            or candidate.record.qualified_name.casefold().endswith("." + name)
            for name in qualified_hints
        )
    ]
    if len(uniquely_qualified) == 1:
        selected_id = uniquely_qualified[0]
        by_candidate_id = {
            candidate_id: (
                "matches" if candidate_id == selected_id else "does_not_match"
            )
            for candidate_id in by_candidate_id
        }
    else:
        shared_names = Counter(
            candidate.record.canonical_name.casefold()
            for candidate in candidate_set.candidates
        )
        for candidate in candidate_set.candidates:
            candidate_id = candidate.record.inventory_id
            if (
                shared_names[candidate.record.canonical_name.casefold()] > 1
                and by_candidate_id[candidate_id] == "matches"
            ):
                by_candidate_id[candidate_id] = "insufficient_evidence"
            elif by_candidate_id[candidate_id] is None:
                by_candidate_id[candidate_id] = "insufficient_evidence"
    result = aggregate_candidate_relations(
        candidate_set,
        {key: str(value) for key, value in by_candidate_id.items()},
        quantifier=quantifier,
        population_refs=population_refs,
    )
    uncertain_candidate_ids = [
        candidate_id
        for candidate_id, value in by_candidate_id.items()
        if value == "insufficient_evidence"
    ]
    if uncertain_candidate_ids:
        result["uncertain_candidate_ids"] = uncertain_candidate_ids
    return result


def replay_subject_binding_events(events_path: Path) -> dict[str, Any]:
    """Replay deterministic binding over saved candidate sets and judge results."""
    grouped_plans: dict[str, Mapping[str, Any]] = {}
    grouped_results: dict[str, list[Mapping[str, Any]]] = {}
    scope_pattern = re.compile(r"^(.*?\.for_each\[0\]\[\d+\])")
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InventoryError(f"cannot read Procedrr events: {events_path}") from exc
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InventoryError(
                f"Procedrr event line {line_number} is not valid JSON"
            ) from exc
        if not isinstance(record, Mapping):
            continue
        path_value = record.get("path")
        if not isinstance(path_value, str):
            continue
        scope_match = scope_pattern.match(path_value)
        if scope_match is None:
            continue
        scope = scope_match.group(1)
        if record.get("bind") == "repository_binding_plan":
            value = record.get("output")
            if isinstance(value, Mapping):
                grouped_plans[scope] = value
        elif record.get("output") == "candidate_relation_result":
            value = record.get("value")
            if isinstance(value, Mapping):
                grouped_results.setdefault(scope, []).append(value)

    bindings: list[dict[str, Any]] = []
    for scope, plan in grouped_plans.items():
        raw_query = plan.get("query")
        raw_candidates = plan.get("candidates")
        raw_requests = plan.get("requests")
        if not (
            isinstance(raw_query, Mapping)
            and isinstance(raw_candidates, Mapping)
            and isinstance(raw_requests, list)
            and all(isinstance(item, Mapping) for item in raw_requests)
        ):
            raise InventoryError(f"saved repository binding plan is malformed: {scope}")
        query = LookupQuery.from_data(raw_query)
        candidates = CandidateSet.from_data(raw_candidates)
        requests = [dict(item) for item in raw_requests]
        results = grouped_results.get(scope, [])
        if len(requests) != len(results):
            raise InventoryError(
                f"saved candidate result count is incomplete: {query.source_ref}"
            )
        original_decisions = bind_candidate_relation_decisions(requests, results)
        decisions_by_id = {
            str(
                request.get("candidate", {}).get("record", {}).get("inventory_id")
            ): decision
            for request, decision in zip(requests, original_decisions, strict=True)
        }
        narrowed_candidates = narrow_qualified_candidates(query, candidates)
        narrowed_decisions = [
            decisions_by_id[candidate.record.inventory_id]
            for candidate in narrowed_candidates.candidates
        ]
        result = finalize_subject_binding(
            narrowed_candidates,
            narrowed_decisions,
            quantifier="one",
            subject_text=query.subject_text,
        )
        bindings.append(
            {
                "source_ref": query.source_ref,
                "status": result["status"],
                "binding_ref": result.get("binding_ref"),
                "reason_code": result.get("reason_code"),
                "candidate_ids": [
                    candidate.record.inventory_id
                    for candidate in narrowed_candidates.candidates
                ],
                "uncertain_candidate_ids": result.get("uncertain_candidate_ids", []),
            }
        )
    return {
        "schema_version": "repository-binding-replay-v1",
        "events_path": str(events_path),
        "binding_count": len(bindings),
        "bindings": bindings,
    }


__all__ = [
    "CANDIDATE_RELATION_REVISION",
    "add_candidate_source_excerpts",
    "bind_candidate_relation_decisions",
    "finalize_subject_binding",
    "extract_explicit_repository_names",
    "prepare_candidate_relation_decisions",
    "prepare_subject_lookup_query",
    "narrow_qualified_candidates",
    "retrieve_subject_candidates",
    "replay_subject_binding_events",
]
