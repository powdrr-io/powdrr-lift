"""Prepare, bind, and audit bounded observable acceptance criteria."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from powdrr_lift.core.acceptance_contract import (
    ACCEPTANCE_CRITERION_SCHEMA_VERSION,
    AcceptanceContractError,
    AcceptanceCriterion,
    BehavioralContract,
    CriterionAssertion,
)
from powdrr_lift.core.behavior_contract import CriterionQuality

MAX_CRITERION_REQUIREMENTS = 4
MAX_CRITERIA_PER_REQUEST = 4


def prepare_acceptance_criteria(
    contract_collection: Mapping[str, Any],
    source_text_by_id: Mapping[str, str],
) -> dict[str, Any]:
    """Create one bounded generation request per related requirement partition."""
    raw_contracts = contract_collection.get("contracts")
    requirement_ids = contract_collection.get("covered_requirement_ids")
    if (
        contract_collection.get("schema_version") != "behavioral-contract-collection-v1"
        or not isinstance(contract_collection.get("ledger_fingerprint"), str)
        or not isinstance(requirement_ids, list)
        or not all(isinstance(item, str) for item in requirement_ids)
        or len(set(requirement_ids)) != len(requirement_ids)
        or not isinstance(raw_contracts, list)
        or not isinstance(source_text_by_id, Mapping)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in source_text_by_id.items()
        )
    ):
        raise AcceptanceContractError("acceptance criterion inputs are malformed")
    contracts = tuple(
        BehavioralContract.from_data(item)
        for item in raw_contracts
        if isinstance(item, Mapping)
    )
    if len(contracts) != len(raw_contracts):
        raise AcceptanceContractError("behavioral contract entry is malformed")
    covered = {
        source_id for item in contracts for source_id in item.member_requirement_ids
    }
    if covered != set(requirement_ids):
        raise AcceptanceContractError(
            "behavioral contracts do not cover the declared requirements"
        )
    referenced_ids = {
        source_id
        for item in contracts
        for source_id in (*item.member_requirement_ids, *item.supporting_context_ids)
    }
    if not referenced_ids.issubset(source_text_by_id):
        raise AcceptanceContractError("behavioral contract source text is missing")
    requests: list[dict[str, Any]] = []
    for contract in contracts:
        members = contract.member_requirement_ids
        for offset in range(0, len(members), MAX_CRITERION_REQUIREMENTS):
            partition = members[offset : offset + MAX_CRITERION_REQUIREMENTS]
            local_indexes = {
                source_id: index for index, source_id in enumerate(partition)
            }
            relationships = [
                {
                    "kind": edge.kind,
                    "source_evidence": edge.source_evidence,
                    "target_indexes": [
                        local_indexes[item]
                        for item in edge.target_requirement_ids
                        if item in local_indexes
                    ],
                }
                for edge in contract.relationships
                if set(edge.target_requirement_ids).issubset(partition)
            ]
            requests.append(
                {
                    "request_id": f"criterion-request-{len(requests) + 1:04d}",
                    "contract_id": contract.contract_id,
                    "contract_operation": contract.operation_description,
                    "candidate_requirements": [
                        {
                            "local_index": index,
                            "requirement_id": source_id,
                            "source_text": source_text_by_id.get(source_id, ""),
                            "role_interpretation": dict(
                                contract.role_interpretations[source_id]
                            ),
                        }
                        for index, source_id in enumerate(partition)
                    ],
                    "supporting_context": [
                        {
                            "context_id": context_id,
                            "source_text": source_text_by_id.get(context_id, ""),
                            "classification": "context_only_not_a_requirement",
                        }
                        for context_id in contract.supporting_context_ids
                    ],
                    "relationships": relationships,
                    "shared_constraints": list(contract.shared_constraints),
                    "unresolved_contract_questions": list(
                        contract.unresolved_questions
                    ),
                    "instructions": [
                        "Generate no more than four criteria and four assertions "
                        "per criterion.",
                        "Cover explicitly stated interface and normal behavior "
                        "first, then source-named boundaries and interactions.",
                        "Do not invent requirements from context, examples, "
                        "naming, or conventions.",
                        "Use local source indexes; never return compiler IDs.",
                        "Describe observable behavior, never executable code, "
                        "imports, shell commands, or test selectors.",
                        "Use a concrete observation and expected relation; "
                        "satisfies requires a specific predicate.",
                        "Do not turn an unresolved question into an asserted "
                        "expected behavior.",
                    ],
                }
            )
    return {
        "schema_version": ACCEPTANCE_CRITERION_SCHEMA_VERSION,
        "ledger_fingerprint": contract_collection.get("ledger_fingerprint"),
        "requirement_ids": list(requirement_ids),
        "source_text_by_id": dict(source_text_by_id),
        "contracts": [item.to_data() for item in contracts],
        "requests": requests,
    }


def bind_acceptance_criteria(
    plan: Mapping[str, Any], provider_results: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    requests = plan.get("requests")
    requirement_ids = plan.get("requirement_ids")
    raw_contracts = plan.get("contracts")
    if (
        not isinstance(requests, list)
        or len(requests) != len(provider_results)
        or not isinstance(requirement_ids, list)
        or not all(isinstance(item, str) for item in requirement_ids)
        or not isinstance(raw_contracts, list)
    ):
        raise AcceptanceContractError("acceptance criterion binding plan is malformed")
    contracts = tuple(
        BehavioralContract.from_data(item)
        for item in raw_contracts
        if isinstance(item, Mapping)
    )
    if len(contracts) != len(raw_contracts):
        raise AcceptanceContractError("behavioral contract entry is malformed")
    contract_by_id = {item.contract_id: item for item in contracts}
    criteria: list[AcceptanceCriterion] = []
    errors_by_requirement: dict[str, list[str]] = defaultdict(list)
    for request, result in zip(requests, provider_results, strict=True):
        if not isinstance(request, Mapping) or not isinstance(result, Mapping):
            raise AcceptanceContractError("acceptance criterion result is malformed")
        contract_id = request.get("contract_id")
        contract = (
            contract_by_id.get(contract_id) if isinstance(contract_id, str) else None
        )
        candidates = request.get("candidate_requirements")
        if contract is None or not isinstance(candidates, list):
            raise AcceptanceContractError("criterion request has invalid contract")
        candidate_ids = [
            _required_text(item, "requirement_id")
            for item in candidates
            if isinstance(item, Mapping)
        ]
        if len(candidate_ids) != len(candidates):
            raise AcceptanceContractError("criterion candidates are malformed")
        encoded_criteria = result.get("criteria")
        if not isinstance(encoded_criteria, list):
            raise AcceptanceContractError("criteria must be a list")
        if len(encoded_criteria) > MAX_CRITERIA_PER_REQUEST:
            encoded_criteria = encoded_criteria[:MAX_CRITERIA_PER_REQUEST]
            for source_id in candidate_ids:
                errors_by_requirement[source_id].append(
                    "criterion generation exceeded the per-request criterion limit"
                )
        for raw_criterion in encoded_criteria:
            try:
                value = _decode_object(raw_criterion, "criterion")
                criterion = _bind_criterion(value, contract, candidate_ids)
            except AcceptanceContractError as exc:
                for source_id in candidate_ids:
                    errors_by_requirement[source_id].append(str(exc))
                continue
            criteria.append(criterion)
    criterion_ids_by_requirement: dict[str, set[str]] = defaultdict(set)
    for criterion in criteria:
        for source_id in criterion.source_refs:
            criterion_ids_by_requirement[source_id].add(criterion.criterion_id)
    requirement_coverage = {}
    for source_id in requirement_ids:
        linked = sorted(criterion_ids_by_requirement.get(source_id, set()))
        if linked:
            status = "unassessed"
            quality = CriterionQuality(criterion_status="unassessed")
        else:
            status = "source_only"
            reason = (
                errors_by_requirement[source_id][0]
                if errors_by_requirement.get(source_id)
                else "no valid observable criterion was generated"
            )
            quality = CriterionQuality(
                criterion_status="source_only",
                failure_stage="criterion_generation",
                failure_reason=reason,
            )
        requirement_coverage[source_id] = {
            "criterion_ids": linked,
            "criterion_quality": quality.to_data(),
            "status": status,
        }
    return {
        "schema_version": ACCEPTANCE_CRITERION_SCHEMA_VERSION,
        "ledger_fingerprint": plan.get("ledger_fingerprint"),
        "criteria": [item.to_data() for item in criteria],
        "requirement_coverage": requirement_coverage,
        "counts": {
            "requirements": len(requirement_ids),
            "with_criteria": sum(
                bool(item["criterion_ids"]) for item in requirement_coverage.values()
            ),
            "unassessed": sum(
                item["status"] == "unassessed" for item in requirement_coverage.values()
            ),
            "source_only": sum(
                item["status"] == "source_only"
                for item in requirement_coverage.values()
            ),
        },
    }


def prepare_acceptance_criterion_reviews(
    criterion_collection: Mapping[str, Any],
    contract_collection: Mapping[str, Any],
    source_text_by_id: Mapping[str, str],
) -> dict[str, Any]:
    """Prepare exactly one bounded semantic adequacy review per criterion."""
    criteria_raw = criterion_collection.get("criteria")
    contracts_raw = contract_collection.get("contracts")
    if (
        not isinstance(criteria_raw, list)
        or not isinstance(contracts_raw, list)
        or not isinstance(source_text_by_id, Mapping)
        or criterion_collection.get("ledger_fingerprint")
        != contract_collection.get("ledger_fingerprint")
    ):
        raise AcceptanceContractError("criterion review inputs are malformed")
    criteria = tuple(
        AcceptanceCriterion.from_data(item)
        for item in criteria_raw
        if isinstance(item, Mapping)
    )
    contracts = tuple(
        BehavioralContract.from_data(item)
        for item in contracts_raw
        if isinstance(item, Mapping)
    )
    if len(criteria) != len(criteria_raw) or len(contracts) != len(contracts_raw):
        raise AcceptanceContractError("criterion review artifact is malformed")
    contract_by_id = {item.contract_id: item for item in contracts}
    requests: list[dict[str, Any]] = []
    for criterion in criteria:
        contract = contract_by_id.get(criterion.contract_id)
        if contract is None:
            raise AcceptanceContractError("criterion references an unknown contract")
        source_ids = set(criterion.source_refs)
        requests.append(
            {
                "request_id": f"review-{criterion.criterion_id}",
                "criterion_id": criterion.criterion_id,
                "criterion_fingerprint": criterion.fingerprint,
                "criterion": criterion.to_data(),
                "contract": {
                    "operation_description": contract.operation_description,
                    "role_interpretations": {
                        source_id: dict(contract.role_interpretations[source_id])
                        for source_id in criterion.source_refs
                    },
                    "relationships": [
                        item.to_data()
                        for item in contract.relationships
                        if set(item.target_requirement_ids).intersection(source_ids)
                    ],
                    "shared_constraints": list(contract.shared_constraints),
                    "unresolved_questions": list(contract.unresolved_questions),
                },
                "source_clauses": [
                    {"source_ref": source_id, "text": source_text_by_id[source_id]}
                    for source_id in criterion.source_refs
                ],
                "assertions": [item.to_data() for item in criterion.assertions],
                "supporting_context": [
                    {
                        "classification": "context_only_not_a_requirement",
                        "text": source_text_by_id[context_id],
                    }
                    for context_id in contract.supporting_context_ids
                    if context_id in source_text_by_id
                ],
                "review_question": (
                    "Does each assertion follow from cited source evidence, and "
                    "does at least one distinguish a plausible incorrect behavior?"
                ),
            }
        )
    return {
        "schema_version": "acceptance-criterion-review-plan-v1",
        "ledger_fingerprint": criterion_collection.get("ledger_fingerprint"),
        "criterion_collection": dict(criterion_collection),
        "requests": requests,
    }


def bind_acceptance_criterion_reviews(
    plan: Mapping[str, Any], provider_results: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    requests = plan.get("requests")
    criterion_collection = plan.get("criterion_collection")
    if (
        not isinstance(requests, list)
        or len(requests) != len(provider_results)
        or not isinstance(criterion_collection, Mapping)
        or not isinstance(criterion_collection.get("criteria"), list)
    ):
        raise AcceptanceContractError("criterion review binding plan is malformed")
    criteria = [
        AcceptanceCriterion.from_data(item)
        for item in criterion_collection["criteria"]
        if isinstance(item, Mapping)
    ]
    if len(criteria) != len(criterion_collection["criteria"]):
        raise AcceptanceContractError("criterion review criteria are malformed")
    reviews: list[dict[str, Any]] = []
    reviewed_by_id: dict[str, AcceptanceCriterion] = {}
    for request, result in zip(requests, provider_results, strict=True):
        if not isinstance(request, Mapping) or not isinstance(result, Mapping):
            raise AcceptanceContractError("criterion review result is malformed")
        criterion_id = _required_text(request, "criterion_id")
        original = next(
            (item for item in criteria if item.criterion_id == criterion_id), None
        )
        if (
            original is None
            or request.get("criterion_fingerprint") != original.fingerprint
        ):
            raise AcceptanceContractError("criterion review request is stale")
        source_clauses = request.get("source_clauses")
        raw_assertion_reviews = result.get("assertion_reviews")
        adequate = result.get("adequate")
        distinguishes = result.get("distinguishes")
        if not isinstance(source_clauses, list) or not isinstance(
            raw_assertion_reviews, list
        ):
            source_clauses = []
            raw_assertion_reviews = []
        source_texts: dict[str, str] = {}
        for item in source_clauses:
            if (
                isinstance(item, Mapping)
                and isinstance(item.get("source_ref"), str)
                and isinstance(item.get("text"), str)
            ):
                source_texts[item["source_ref"]] = item["text"]
        assertions_by_id = {item.assertion_id: item for item in original.assertions}
        assertion_reviews: list[dict[str, Any]] = []
        for raw_review in raw_assertion_reviews:
            try:
                review = _decode_object(raw_review, "assertion review")
                assertion_id = _required_text(review, "assertion_id")
                status = review.get("status")
                evidence = _required_text(review, "source_evidence")
                review_reason = _required_text(review, "reason")
                assertion = assertions_by_id.get(assertion_id)
                if assertion is None or status not in {
                    "supported",
                    "contradicted",
                    "unsupported",
                    "unresolved",
                }:
                    continue
                evidence_valid = any(
                    evidence in source_texts.get(source_id, "")
                    for source_id in assertion.source_refs
                )
                assertion_reviews.append(
                    {
                        "assertion_id": assertion_id,
                        "status": status if evidence_valid else "unsupported",
                        "source_evidence": evidence,
                        "reason": review_reason,
                        "evidence_valid": evidence_valid,
                    }
                )
            except AcceptanceContractError:
                continue
        if len(assertion_reviews) != len(original.assertions) or {
            item["assertion_id"] for item in assertion_reviews
        } != set(assertions_by_id):
            assertion_reviews = [
                {
                    "assertion_id": item.assertion_id,
                    "status": "unresolved",
                    "source_evidence": "",
                    "reason": "assertion review was missing or duplicated",
                    "evidence_valid": False,
                }
                for item in original.assertions
            ]
        statuses = {item["status"] for item in assertion_reviews}
        source_status = (
            "supported"
            if statuses == {"supported"}
            else "unresolved"
            if "unresolved" in statuses
            else "contradicted"
            if "contradicted" in statuses
            else "unsupported"
        )
        if not isinstance(adequate, bool):
            adequate = False
        if not isinstance(distinguishes, bool):
            distinguishes = False
        plausible_incorrect = result.get("plausible_incorrect_behavior")
        reason = result.get("adequacy_reason")
        if not isinstance(plausible_incorrect, str) or not plausible_incorrect.strip():
            plausible_incorrect = "No plausible incorrect behavior was identified."
            distinguishes = False
        if not isinstance(reason, str) or not reason.strip():
            reason = "Criterion review did not provide a usable rationale."
        if source_status == "supported" and adequate and distinguishes:
            quality = CriterionQuality(criterion_status="checkable")
        elif source_status == "unresolved":
            quality = CriterionQuality(
                criterion_status="unresolved",
                failure_stage="criterion_review",
                failure_reason=reason,
            )
        else:
            failure_reason = (
                reason
                if source_status in {"contradicted", "unsupported"}
                else "criterion does not distinguish a plausible incorrect behavior"
            )
            quality = CriterionQuality(
                criterion_status="source_only",
                failure_stage="criterion_review",
                failure_reason=failure_reason,
            )
        updated = replace(original, quality=quality, fingerprint="")
        updated = replace(updated, fingerprint=updated.calculate_fingerprint())
        reviewed_by_id[criterion_id] = updated
        reviews.append(
            {
                "criterion_id": criterion_id,
                "criterion_fingerprint": original.fingerprint,
                "source_status": source_status,
                "assertion_reviews": assertion_reviews,
                "adequate": adequate,
                "plausible_incorrect_behavior": plausible_incorrect,
                "distinguishes": distinguishes,
                "adequacy_reason": reason,
                "quality": quality.to_data(),
            }
        )
    reviewed_criteria = [
        reviewed_by_id.get(item.criterion_id, item).to_data() for item in criteria
    ]
    criterion_ids_by_requirement: dict[str, set[str]] = defaultdict(set)
    reviewed_by_id_and_fresh = {
        item.criterion_id: reviewed_by_id.get(item.criterion_id, item)
        for item in criteria
    }
    for criterion in reviewed_by_id_and_fresh.values():
        for source_id in criterion.source_refs:
            criterion_ids_by_requirement[source_id].add(criterion.criterion_id)
    coverage = criterion_collection.get("requirement_coverage")
    if not isinstance(coverage, Mapping):
        raise AcceptanceContractError("criterion requirement coverage is malformed")
    updated_coverage: dict[str, Any] = {}
    for source_id, record in coverage.items():
        if not isinstance(source_id, str) or not isinstance(record, Mapping):
            raise AcceptanceContractError("criterion coverage record is malformed")
        linked = sorted(criterion_ids_by_requirement.get(source_id, set()))
        linked_criteria = [reviewed_by_id_and_fresh[item] for item in linked]
        if not linked_criteria:
            quality = CriterionQuality(
                criterion_status="source_only",
                failure_stage="criterion_generation",
                failure_reason=(
                    record.get("criterion_quality", {}).get("failure_reason")
                    if isinstance(record.get("criterion_quality"), Mapping)
                    else "no valid observable criterion was generated"
                ),
            )
        elif all(
            item.quality.criterion_status == "checkable" for item in linked_criteria
        ):
            quality = CriterionQuality(criterion_status="checkable")
        elif any(
            item.quality.criterion_status == "unresolved" for item in linked_criteria
        ):
            unresolved = next(
                item
                for item in linked_criteria
                if item.quality.criterion_status == "unresolved"
            )
            quality = CriterionQuality(
                criterion_status="unresolved",
                failure_stage="criterion_review",
                failure_reason=unresolved.quality.failure_reason,
            )
        else:
            source_only = next(
                item
                for item in linked_criteria
                if item.quality.criterion_status == "source_only"
            )
            quality = CriterionQuality(
                criterion_status="source_only",
                failure_stage="criterion_review",
                failure_reason=source_only.quality.failure_reason,
            )
        updated_coverage[source_id] = {
            "criterion_ids": linked,
            "criterion_quality": quality.to_data(),
            "status": quality.criterion_status,
        }
    return {
        **dict(criterion_collection),
        "criteria": reviewed_criteria,
        "requirement_coverage": updated_coverage,
        "counts": {
            "requirements": len(updated_coverage),
            "with_criteria": sum(
                bool(item["criterion_ids"]) for item in updated_coverage.values()
            ),
            "checkable": sum(
                item["status"] == "checkable" for item in updated_coverage.values()
            ),
            "source_only": sum(
                item["status"] == "source_only" for item in updated_coverage.values()
            ),
            "unresolved": sum(
                item["status"] == "unresolved" for item in updated_coverage.values()
            ),
        },
        "reviews": reviews,
    }


def _bind_criterion(
    raw: Mapping[str, Any], contract: BehavioralContract, candidate_ids: Sequence[str]
) -> AcceptanceCriterion:
    if set(raw) != {
        "kind",
        "source_indexes",
        "setup",
        "operation",
        "events",
        "assertions",
        "unresolved_questions",
    }:
        raise AcceptanceContractError("criterion has unknown or missing fields")
    source_indexes = _indexes(
        raw.get("source_indexes"), len(candidate_ids), "source_indexes"
    )
    if not source_indexes:
        raise AcceptanceContractError("criterion has no source requirements")
    source_refs = tuple(candidate_ids[index] for index in source_indexes)
    assertion_values = raw.get("assertions")
    if not isinstance(assertion_values, list) or len(assertion_values) > 4:
        raise AcceptanceContractError(
            "criterion assertions must contain at most four items"
        )
    assertions: list[CriterionAssertion] = []
    for index, item in enumerate(assertion_values, start=1):
        assertion_data = _decode_object(item, "assertion")
        if set(assertion_data) != {
            "observation",
            "relation",
            "expected",
            "source_indexes",
            "basis",
        }:
            raise AcceptanceContractError("assertion has unknown or missing fields")
        assertion_indexes = _indexes(
            assertion_data.get("source_indexes"),
            len(candidate_ids),
            "assertion source_indexes",
        )
        if not assertion_indexes or not set(assertion_indexes).issubset(source_indexes):
            raise AcceptanceContractError(
                "assertion references an unrelated requirement"
            )
        assertions.append(
            CriterionAssertion(
                assertion_id=f"assertion-{index:02d}",
                observation=_required_text(assertion_data, "observation"),
                relation=_required_text(assertion_data, "relation"),
                expected=assertion_data.get("expected"),
                source_refs=tuple(candidate_ids[value] for value in assertion_indexes),
                basis=_required_text(assertion_data, "basis"),
            )
        )
    if not assertions:
        raise AcceptanceContractError("criterion needs at least one assertion")
    questions = raw.get("unresolved_questions")
    if not isinstance(questions, list) or not all(
        isinstance(item, str) and item.strip() for item in questions
    ):
        raise AcceptanceContractError("criterion unresolved questions are malformed")
    if any(
        source_id not in contract.member_requirement_ids for source_id in source_refs
    ):
        raise AcceptanceContractError("criterion references a nonmember requirement")
    material = {
        "contract_id": contract.contract_id,
        "kind": raw.get("kind"),
        "source_refs": source_refs,
        "setup": raw.get("setup"),
        "operation": raw.get("operation"),
        "events": raw.get("events"),
        "assertions": [item.to_data() for item in assertions],
        "unresolved_questions": questions,
    }
    digest = hashlib.sha256(
        json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()[:16]
    criterion_id = f"acceptance-criterion:{digest}"
    assertions = [
        CriterionAssertion(
            assertion_id=f"{criterion_id}:assertion-{index:02d}",
            observation=item.observation,
            relation=item.relation,
            expected=item.expected,
            source_refs=item.source_refs,
            basis=item.basis,
        )
        for index, item in enumerate(assertions, start=1)
    ]
    criterion = AcceptanceCriterion(
        criterion_id=criterion_id,
        contract_id=contract.contract_id,
        kind=_required_text(raw, "kind"),
        source_refs=source_refs,
        setup=raw.get("setup"),
        operation=_required_text(raw, "operation"),
        events=_value_tuple(raw.get("events"), "criterion events"),
        assertions=tuple(assertions),
        unresolved_questions=tuple(questions),
        quality=CriterionQuality(criterion_status="unassessed"),
    )
    return replace(criterion, fingerprint=criterion.calculate_fingerprint())


def _decode_object(raw: Any, label: str) -> Mapping[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AcceptanceContractError(f"{label} is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise AcceptanceContractError(f"{label} must be a JSON object")
    return raw


def _indexes(raw: Any, size: int, name: str) -> tuple[int, ...]:
    if not isinstance(raw, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in raw
    ):
        raise AcceptanceContractError(f"{name} must be an integer list")
    if any(item < 0 or item >= size for item in raw) or len(set(raw)) != len(raw):
        raise AcceptanceContractError(f"{name} contains invalid indexes")
    return tuple(raw)


def _value_tuple(raw: Any, name: str) -> tuple[Any, ...]:
    if not isinstance(raw, list):
        raise AcceptanceContractError(f"{name} must be a list")
    return tuple(raw)


def _required_text(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceContractError(f"{name} must be a non-empty string")
    return value.strip()


__all__ = [
    "MAX_CRITERIA_PER_REQUEST",
    "MAX_CRITERION_REQUIREMENTS",
    "bind_acceptance_criterion_reviews",
    "bind_acceptance_criteria",
    "prepare_acceptance_criterion_reviews",
    "prepare_acceptance_criteria",
]
