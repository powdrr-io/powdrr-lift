"""Prepare and bind bounded cross-requirement behavioral contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.core.acceptance_contract import (
    RELATIONSHIP_KINDS,
    AcceptanceContractError,
    BehavioralContract,
    ContractRelationship,
    validate_contracts,
)
from powdrr_lift.core.instruction_ledger import InstructionLedger
from powdrr_lift.core.semantic_contract import PartialSemanticContract

MAX_CONTRACT_GROUP_SIZE = 16
MAX_CONTEXT_ITEMS = 8


def prepare_behavioral_contracts(
    ledger_data: Mapping[str, Any],
    semantic_designs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Propose bounded requirement groups from explicit source/role relations."""
    ledger = InstructionLedger.from_data(dict(ledger_data))
    if len(semantic_designs) != len(ledger.clauses):
        raise AcceptanceContractError("semantic designs do not match ledger clauses")
    requirement_ids: list[str] = []
    context_ids: list[str] = []
    role_by_id: dict[str, Mapping[str, Any]] = {}
    for clause, design in zip(ledger.clauses, semantic_designs, strict=True):
        if not isinstance(design, Mapping):
            raise AcceptanceContractError("semantic design entry is malformed")
        contract_raw = design.get("partial_contract")
        if not isinstance(contract_raw, Mapping):
            nested = design.get("design")
            contract_raw = (
                nested.get("partial_contract") if isinstance(nested, Mapping) else None
            )
        if not isinstance(contract_raw, Mapping):
            raise AcceptanceContractError(
                f"source semantic contract is missing for {clause.clause_id}"
            )
        contract = PartialSemanticContract.from_data(contract_raw)
        if (
            contract.source_ref != clause.clause_id
            or contract.source_fingerprint != clause.fingerprint
            or contract.proposition_text != clause.text
        ):
            raise AcceptanceContractError(
                f"semantic contract is stale for {clause.clause_id}"
            )
        if contract.routing in {
            "include",
            "include_prohibition",
        } and contract.disposition not in {
            "context",
            "nonactionable",
        }:
            requirement_ids.append(clause.clause_id)
            role = contract.source_interpretation
            role_by_id[clause.clause_id] = (
                {"source_text": clause.text, **role.to_data()}
                if role is not None
                else {
                    "source_text": clause.text,
                    "source_refs": [clause.clause_id],
                    "unresolved_fields": ["operation|source_interpretation_missing"],
                    "meaning_status": "unresolved",
                }
            )
        elif contract.disposition == "context":
            context_ids.append(clause.clause_id)

    clauses_by_id = {item.clause_id: item for item in ledger.clauses}
    diagnostic_spans = {
        item.source_clause_id: item.source_span for item in ledger.split_diagnostics
    }
    candidate_sets = _candidate_groups(ledger, requirement_ids, role_by_id)
    requests: list[dict[str, Any]] = []
    for group_index, candidate_ids in enumerate(candidate_sets, start=1):
        for partition_index, member_ids in enumerate(
            _partition(candidate_ids, MAX_CONTRACT_GROUP_SIZE), start=1
        ):
            if len(member_ids) < 2:
                continue
            contexts = _nearby_context(
                ledger,
                member_ids,
                context_ids,
                max_items=MAX_CONTEXT_ITEMS,
            )
            shared_constraints = _cross_partition_constraints(
                ledger, candidate_ids, member_ids
            )
            member_set = set(member_ids)
            scope_relations = [
                relation.to_data()
                for clause_id in member_ids
                for relation in (
                    *clauses_by_id[clause_id].semantic_relations,
                    *clauses_by_id[clause_id].modifier_attachments,
                )
                if member_set.intersection(relation.child_clause_ids)
            ]
            validation_relations = [
                {
                    "requirement_id": clause_id,
                    "group_id": clauses_by_id[clause_id].validation_group_id,
                    "relation": clauses_by_id[clause_id].validation_relation,
                }
                for clause_id in member_ids
                if clauses_by_id[clause_id].validation_group_id is not None
            ]
            parent_texts = []
            parent_ids = dict.fromkeys(
                clauses_by_id[item].parent_clause_id
                for item in member_ids
                if clauses_by_id[item].parent_clause_id is not None
            )
            for parent_id in parent_ids:
                if parent_id is None:
                    continue
                span = diagnostic_spans.get(parent_id)
                if span is not None:
                    parent_texts.append(ledger.source.text[span[0] : span[1]])
            request_id = f"behavioral-group-{group_index:03d}-{partition_index:03d}"
            requests.append(
                {
                    "request_id": request_id,
                    "parent_source_texts": parent_texts,
                    "scope_relations": scope_relations,
                    "validation_relations": validation_relations,
                    "candidate_requirements": [
                        {
                            "local_index": index,
                            "requirement_id": clause_id,
                            "source_text": clauses_by_id[clause_id].text,
                            "role_interpretation": dict(role_by_id[clause_id]),
                            "parent_clause_id": clauses_by_id[
                                clause_id
                            ].parent_clause_id,
                            "validation_group_id": clauses_by_id[
                                clause_id
                            ].validation_group_id,
                            "validation_relation": clauses_by_id[
                                clause_id
                            ].validation_relation,
                        }
                        for index, clause_id in enumerate(member_ids)
                    ],
                    "context_items": [
                        {
                            "local_index": index,
                            "context_id": clause_id,
                            "source_text": clauses_by_id[clause_id].text,
                        }
                        for index, clause_id in enumerate(contexts)
                    ],
                    "shared_constraints": shared_constraints,
                    "instructions": [
                        (
                            "Confirm candidate grouping only when the source "
                            "establishes an explicit operation, subject, source "
                            "relation, or validation relation."
                        ),
                        (
                            "Shared words or subsystem alone do not justify grouping. "
                            "Keep independent requirements separate unless a source "
                            "relationship links their behavior."
                        ),
                        (
                            "Context items can support interpretation but can never "
                            "be selected as contract members."
                        ),
                        (
                            "For each relationship, cite exact source words and list "
                            "only the requirement indexes it constrains."
                        ),
                        (
                            "Do not infer that a relation applies to every transport, "
                            "mode, or API path."
                        ),
                    ],
                }
            )
    return {
        "ledger_fingerprint": ledger.fingerprint,
        "requirement_ids": requirement_ids,
        "context_ids": context_ids,
        "source_text_by_id": {item.clause_id: item.text for item in ledger.clauses},
        "role_interpretations": role_by_id,
        "requests": requests,
        "candidate_group_count": len(requests),
    }


def bind_behavioral_contracts(
    plan: Mapping[str, Any],
    provider_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    requests = plan.get("requests")
    requirement_ids = plan.get("requirement_ids")
    context_ids = plan.get("context_ids")
    source_text_by_id = plan.get("source_text_by_id")
    role_interpretations = plan.get("role_interpretations")
    if (
        not isinstance(requests, list)
        or len(requests) != len(provider_results)
        or not isinstance(requirement_ids, list)
        or not isinstance(context_ids, list)
        or not isinstance(source_text_by_id, Mapping)
        or not isinstance(role_interpretations, Mapping)
    ):
        raise AcceptanceContractError("behavioral contract binding plan is malformed")
    contracts: list[BehavioralContract] = []
    covered: set[str] = set()
    for request, result in zip(requests, provider_results, strict=True):
        if not isinstance(request, Mapping) or not isinstance(result, Mapping):
            raise AcceptanceContractError("behavioral group result is malformed")
        candidates = request.get("candidate_requirements")
        context_items = request.get("context_items")
        if not isinstance(candidates, list) or not isinstance(context_items, list):
            raise AcceptanceContractError("behavioral group candidates are malformed")
        candidate_ids = [
            _required_text(item, "requirement_id")
            for item in candidates
            if isinstance(item, Mapping)
        ]
        context_candidate_ids = [
            _required_text(item, "context_id")
            for item in context_items
            if isinstance(item, Mapping)
        ]
        try:
            selected_indexes = _indexes(
                result.get("member_indexes"), len(candidate_ids)
            )
            selected_ids = tuple(candidate_ids[index] for index in selected_indexes)
            selected_context_indexes = _indexes(
                result.get("context_indexes", []), len(context_candidate_ids)
            )
            selected_context_ids = tuple(
                context_candidate_ids[index] for index in selected_context_indexes
            )
            relationships = _bind_relationships(
                result.get("relationships"),
                selected_indexes,
                candidate_ids,
                plan["source_text_by_id"],
                selected_context_ids,
            )
            questions = _string_list(
                result.get("unresolved_questions"), "unresolved_questions"
            )
            if isinstance(result.get("relationships"), list) and len(
                relationships
            ) < len(result["relationships"]):
                questions = (
                    *questions,
                    "One or more unsupported relationship edges were discarded.",
                )
            if len(selected_ids) < 2:
                if not questions:
                    contracts.extend(
                        _build_contract((item,), (), role_interpretations, (), (), ())
                        for item in candidate_ids
                    )
                    covered.update(candidate_ids)
                    continue
                raise AcceptanceContractError(
                    "confirmed group needs at least two members"
                )
            constraints = _string_list(
                request.get("shared_constraints", []), "shared_constraints"
            )
            contract = _build_contract(
                selected_ids,
                selected_context_ids,
                role_interpretations,
                relationships,
                questions,
                constraints,
            )
        except AcceptanceContractError as error:
            # A failed grouping decision cannot drop requirements. Retain each
            # candidate as an explicit singleton contract with a visible question.
            contract_list = [
                _build_contract(
                    (item,),
                    (),
                    role_interpretations,
                    (),
                    (f"Candidate group unresolved: {error}",),
                    (),
                )
                for item in candidate_ids
            ]
            covered.update(candidate_ids)
        else:
            contract_list = [contract]
            covered.update(selected_ids)
            contract_list.extend(
                _build_contract((item,), (), role_interpretations, (), (), ())
                for item in candidate_ids
                if item not in selected_ids and item not in covered
            )
            covered.update(candidate_ids)
        contracts.extend(contract_list)
    for requirement_id in requirement_ids:
        if requirement_id not in covered:
            contracts.append(
                _build_contract((requirement_id,), (), role_interpretations, (), (), ())
            )
            covered.add(requirement_id)
    validate_contracts(
        contracts,
        requirement_ids=requirement_ids,
        context_ids=context_ids,
        source_text_by_id=source_text_by_id,
    )
    data = [item.to_data() for item in contracts]
    return {
        "schema_version": "behavioral-contract-collection-v1",
        "ledger_fingerprint": plan.get("ledger_fingerprint"),
        "contracts": data,
        "covered_requirement_ids": sorted(covered),
    }


def _candidate_groups(
    ledger: InstructionLedger,
    requirement_ids: Sequence[str],
    roles: Mapping[str, Mapping[str, Any]],
) -> list[tuple[str, ...]]:
    requirement_set = set(requirement_ids)
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    parent_buckets: dict[str, list[str]] = defaultdict(list)
    validation_buckets: dict[str, list[str]] = defaultdict(list)
    for clause in ledger.clauses:
        if clause.clause_id not in requirement_set:
            continue
        if clause.parent_clause_id:
            parent_buckets[clause.parent_clause_id].append(clause.clause_id)
        if clause.validation_group_id:
            validation_buckets[clause.validation_group_id].append(clause.clause_id)
        role = roles.get(clause.clause_id, {})
        subject = _normalize(role.get("subject"))
        operation = _normalize(role.get("operation"))
        if subject and operation:
            buckets[(subject, operation)].append(clause.clause_id)
    groups: set[tuple[str, ...]] = set()
    for values in (
        *parent_buckets.values(),
        *validation_buckets.values(),
        *buckets.values(),
    ):
        ordered = tuple(dict.fromkeys(values))
        if len(ordered) > 1:
            groups.add(ordered)
    for clause in ledger.clauses:
        if clause.clause_id not in requirement_set:
            continue
        for relation in clause.semantic_relations:
            referenced = tuple(
                item for item in relation.child_clause_ids if item in requirement_set
            )
            if len(referenced) > 1:
                groups.add(referenced)
    return sorted(groups, key=lambda group: (min(group), len(group), group))


def _partition(items: Sequence[str], maximum: int) -> tuple[tuple[str, ...], ...]:
    if len(items) <= maximum:
        return (tuple(items),) if items else ()
    chunks: list[tuple[str, ...]] = []
    index = 0
    stride = maximum - 1
    while index < len(items):
        chunk = tuple(items[index : index + maximum])
        if chunk:
            chunks.append(chunk)
        index += stride
    return tuple(chunks)


def _nearby_context(
    ledger: InstructionLedger,
    member_ids: Sequence[str],
    context_ids: Sequence[str],
    *,
    max_items: int,
) -> tuple[str, ...]:
    clause_positions = {item.clause_id: item.ordinal for item in ledger.clauses}
    members = [clause_positions[item] for item in member_ids]
    lower, upper = min(members), max(members)
    candidates = [
        item for item in context_ids if lower - 1 <= clause_positions[item] <= upper + 1
    ]
    return tuple(candidates[:max_items])


def _cross_partition_constraints(
    ledger: InstructionLedger,
    complete_group: Sequence[str],
    partition: Sequence[str],
) -> list[str]:
    ids = set(complete_group)
    local = set(partition)
    constraints: list[str] = []
    for clause in ledger.clauses:
        for relation in clause.semantic_relations:
            targets = set(relation.child_clause_ids).intersection(ids)
            if targets.intersection(local) and not targets.issubset(local):
                constraints.append(
                    f"{relation.relation_type}:{relation.label}; "
                    f"members={','.join(sorted(targets))}; evidence={relation.evidence}"
                )
    return constraints


def _bind_relationships(
    raw: Any,
    selected_indexes: Sequence[int],
    candidate_ids: Sequence[str],
    source_text_by_id: Mapping[str, Any],
    context_ids: Sequence[str],
) -> tuple[ContractRelationship, ...]:
    if not isinstance(raw, list):
        raise AcceptanceContractError("relationships must be a list")
    selected = set(selected_indexes)
    edges: list[ContractRelationship] = []
    for item in raw:
        try:
            if isinstance(item, str):
                decoded = json.loads(item)
                if not isinstance(decoded, Mapping):
                    continue
                item = decoded
            elif isinstance(item, Mapping):
                pass
            else:
                continue
            if set(item) != {"kind", "source_evidence", "target_indexes"}:
                continue
            kind = _required_text(item, "kind")
            evidence = _required_text(item, "source_evidence")
            targets_raw = item.get("target_indexes")
            targets = _indexes(targets_raw, len(candidate_ids))
            if (
                kind not in RELATIONSHIP_KINDS
                or not targets
                or not set(targets).issubset(selected)
            ):
                continue
            target_ids = tuple(candidate_ids[index] for index in targets)
            if not any(
                evidence in source_text_by_id.get(item_id, "")
                for item_id in (*target_ids, *context_ids)
                if isinstance(source_text_by_id.get(item_id), str)
            ):
                continue
            edges.append(
                ContractRelationship(
                    kind=kind,
                    source_evidence=evidence,
                    target_requirement_ids=target_ids,
                )
            )
        except (AcceptanceContractError, json.JSONDecodeError, TypeError):
            continue
    return tuple(edges)


def _build_contract(
    members: Sequence[str],
    contexts: Sequence[str],
    roles: Mapping[str, Mapping[str, Any]],
    relationships: Sequence[ContractRelationship],
    questions: Sequence[str],
    shared_constraints: Sequence[str],
) -> BehavioralContract:
    member_tuple = tuple(members)
    role_values = {item: dict(roles.get(item, {})) for item in member_tuple}
    operations = tuple(
        dict.fromkeys(_role_text(value, "operation") for value in role_values.values())
    )
    operations = tuple(item for item in operations if item)
    subjects = tuple(
        dict.fromkeys(_role_text(value, "subject") for value in role_values.values())
    )
    subjects = tuple(item for item in subjects if item)
    description = (
        f"{', '.join(subjects)}: {operations[0]}"
        if len(operations) == 1 and subjects
        else operations[0]
        if len(operations) == 1
        else "Related operations: " + "; ".join(operations)
        if operations
        else next(
            (
                _role_text(value, "source_text")
                for value in role_values.values()
                if _role_text(value, "source_text")
            ),
            "Source requirements need operation interpretation",
        )
    )
    role_questions = [
        (
            f"Clarify {field} for {member_id}: source interpretation is unresolved "
            f"({reason})."
        )
        for member_id, value in role_values.items()
        if value.get("meaning_status") == "unresolved"
        for field, reason in _unresolved_pairs(value.get("unresolved_fields"))
    ]
    identity = json.dumps(
        {
            "members": member_tuple,
            "contexts": tuple(contexts),
            "relationships": [item.to_data() for item in relationships],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return BehavioralContract(
        contract_id=f"behavioral-contract:{digest}",
        operation_description=description,
        member_requirement_ids=member_tuple,
        supporting_context_ids=tuple(contexts),
        role_interpretations=role_values,
        relationships=tuple(relationships),
        unresolved_questions=tuple(dict.fromkeys((*questions, *role_questions))),
        shared_constraints=tuple(shared_constraints),
    )


def _indexes(raw: Any, size: int) -> tuple[int, ...]:
    if not isinstance(raw, list) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in raw
    ):
        raise AcceptanceContractError("member indexes must be integers")
    if any(value < 0 or value >= size for value in raw):
        raise AcceptanceContractError("member index is out of range")
    if len(set(raw)) != len(raw):
        raise AcceptanceContractError("member indexes contain duplicates")
    return tuple(raw)


def _string_list(raw: Any, name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise AcceptanceContractError(f"{name} must be a list of non-empty strings")
    return tuple(item.strip() for item in raw)


def _normalize(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\W+", " ", value.casefold()).strip()


def _required_text(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceContractError(f"{name} must be a non-empty string")
    return value.strip()


def _role_text(role: Mapping[str, Any], field: str) -> str:
    value = role.get(field)
    return value.strip() if isinstance(value, str) else ""


def _unresolved_pairs(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        (field.strip(), reason.strip())
        for item in raw
        if isinstance(item, str) and "|" in item
        for field, reason in [item.split("|", 1)]
        if field.strip() and reason.strip()
    )


__all__ = [
    "MAX_CONTEXT_ITEMS",
    "MAX_CONTRACT_GROUP_SIZE",
    "bind_behavioral_contracts",
    "prepare_behavioral_contracts",
]
