from __future__ import annotations

import json
from dataclasses import replace

import pytest

from powdrr_lift.core.acceptance_contract import (
    AcceptanceCriterion,
    BehavioralContract,
)
from powdrr_lift.core.behavior_contract import CriterionQuality
from powdrr_lift.core.implementation_packet import (
    ImplementationPacket,
    compile_implementation_packet,
)
from powdrr_lift.workrr.acceptance_criterion_compiler import (
    MAX_CRITERION_REQUIREMENTS,
    bind_acceptance_criteria,
    bind_acceptance_criterion_repairs,
    bind_acceptance_criterion_reviews,
    prepare_acceptance_criteria,
    prepare_acceptance_criterion_repairs,
    prepare_acceptance_criterion_reviews,
)


def _contract(
    member_ids: tuple[str, ...], context_ids: tuple[str, ...] = ()
) -> BehavioralContract:
    return BehavioralContract(
        contract_id="behavioral-contract:test",
        operation_description="result mapping: accumulate entries",
        member_requirement_ids=member_ids,
        supporting_context_ids=context_ids,
        role_interpretations={
            item: {"subject": "result mapping", "operation": "accumulate entries"}
            for item in member_ids
        },
        relationships=(),
        unresolved_questions=(),
    )


def _criterion(source_indexes: list[int], *, kind: str = "transformation") -> str:
    return json.dumps(
        {
            "kind": kind,
            "source_indexes": source_indexes,
            "setup": {"initial": []},
            "operation": "process the supplied payload",
            "events": [],
            "assertions": [
                json.dumps(
                    {
                        "observation": "result.items",
                        "relation": "equals",
                        "expected": ["A", "B"],
                        "source_indexes": source_indexes,
                        "basis": "source_derived",
                    }
                )
            ],
            "unresolved_questions": [],
        }
    )


def test_typed_criterion_round_trips_and_fingerprints() -> None:
    contract = _contract(("instruction-001",))
    plan = prepare_acceptance_criteria(
        {
            "schema_version": "behavioral-contract-collection-v1",
            "ledger_fingerprint": "sha256:ledger",
            "covered_requirement_ids": ["instruction-001"],
            "contracts": [contract.to_data()],
        },
        {"instruction-001": "The result contains both entries."},
    )
    result = bind_acceptance_criteria(plan, [{"criteria": [_criterion([0])]}])

    criterion = AcceptanceCriterion.from_data(result["criteria"][0])
    assert criterion.source_refs == ("instruction-001",)
    assert criterion.assertions[0].source_refs == ("instruction-001",)
    assert criterion.quality.criterion_status == "unassessed"
    assert result["requirement_coverage"]["instruction-001"]["status"] == "unassessed"

    provisional = compile_implementation_packet(
        objective="Implement accumulated results.",
        obligations=("Accumulate entries across payloads.",),
        required_tests=({"description": "observe the combined result"},),
        allowed_paths=("src/", "tests/"),
        validation_profiles=("pytest",),
        acceptance_criteria=result["criteria"],
    )
    assert "Reviewed observable acceptance criteria:" not in provisional.render()
    reviewed = replace(
        criterion,
        quality=CriterionQuality(criterion_status="checkable"),
        fingerprint="",
    )
    reviewed = replace(reviewed, fingerprint=reviewed.calculate_fingerprint())
    packet = compile_implementation_packet(
        objective="Implement accumulated results.",
        obligations=("Accumulate entries across payloads.",),
        required_tests=({"description": "observe the combined result"},),
        allowed_paths=("src/", "tests/"),
        validation_profiles=("pytest",),
        acceptance_criteria=(reviewed.to_data(),),
    )
    restored = ImplementationPacket.from_data(packet.to_data())
    rendered = restored.render()
    assert restored.acceptance_criteria[0].criterion_id == criterion.criterion_id
    assert "Observable acceptance checks:" in rendered
    assert 'Check that result.items equals ["A","B"].' in rendered
    legacy_data = packet.to_data()
    legacy_data["schema_version"] = "implementation-packet-v1"
    legacy_data.pop("acceptance_criteria")
    assert ImplementationPacket.from_data(legacy_data).acceptance_criteria == ()
    malformed_v2 = packet.to_data()
    malformed_v2.pop("acceptance_criteria")
    with pytest.raises(ValueError, match="v2 requires acceptance criteria"):
        ImplementationPacket.from_data(malformed_v2)


def test_criterion_request_partition_preserves_requirements_and_context() -> None:
    member_ids = tuple(f"instruction-{index:03}" for index in range(1, 7))
    contract = _contract(member_ids, ("context-001",))
    plan = prepare_acceptance_criteria(
        {
            "schema_version": "behavioral-contract-collection-v1",
            "ledger_fingerprint": "sha256:ledger",
            "covered_requirement_ids": list(member_ids),
            "contracts": [contract.to_data()],
        },
        {
            **{item: f"Requirement {item}." for item in member_ids},
            "context-001": "Current format is JSON.",
        },
    )

    assert [len(request["candidate_requirements"]) for request in plan["requests"]] == [
        MAX_CRITERION_REQUIREMENTS,
        2,
    ]
    assert all(
        item["classification"] == "context_only_not_a_requirement"
        for request in plan["requests"]
        for item in request["supporting_context"]
    )
    assert [
        item["requirement_id"]
        for request in plan["requests"]
        for item in request["candidate_requirements"]
    ] == list(member_ids)


def test_requirements_without_valid_assertions_remain_source_only() -> None:
    contract = _contract(("instruction-001", "instruction-002"))
    plan = prepare_acceptance_criteria(
        {
            "schema_version": "behavioral-contract-collection-v1",
            "ledger_fingerprint": "sha256:ledger",
            "covered_requirement_ids": list(contract.member_requirement_ids),
            "contracts": [contract.to_data()],
        },
        {item: f"Source for {item}." for item in contract.member_requirement_ids},
    )
    result = bind_acceptance_criteria(plan, [{"criteria": [_criterion([0])]}])

    assert result["requirement_coverage"]["instruction-001"]["status"] == "unassessed"
    assert result["requirement_coverage"]["instruction-002"]["status"] == "source_only"
    assert result["counts"]["source_only"] == 1


def test_review_requires_exact_assertion_evidence_and_adequacy() -> None:
    source = "The result mapping accumulates entries across payloads."
    contract = _contract(("instruction-001",))
    contracts = {
        "schema_version": "behavioral-contract-collection-v1",
        "ledger_fingerprint": "sha256:ledger",
        "covered_requirement_ids": ["instruction-001"],
        "contracts": [contract.to_data()],
    }
    plan = prepare_acceptance_criteria(
        contracts,
        {"instruction-001": source},
    )
    draft = bind_acceptance_criteria(plan, [{"criteria": [_criterion([0])]}])
    review_plan = prepare_acceptance_criterion_reviews(
        draft,
        contracts,
        {"instruction-001": source},
    )
    assertion_id = review_plan["requests"][0]["criterion"]["assertions"][0][
        "assertion_id"
    ]
    result = bind_acceptance_criterion_reviews(
        review_plan,
        [
            {
                "assertion_reviews": [
                    json.dumps(
                        {
                            "assertion_id": assertion_id,
                            "category": "source_supported",
                            "source_evidence": "accumulates entries across payloads",
                            "reason": "The cited words support the accumulation rule.",
                        }
                    )
                ],
                "setup_review": json.dumps(
                    {
                        "category": "illustrative_setup",
                        "source_evidence": source,
                        "reason": (
                            "Values are illustrative inputs without adding product "
                            "constraints."
                        ),
                    }
                ),
                "decision_records": [],
                "adequate": True,
                "plausible_incorrect_behavior": "returns only the latest payload",
                "distinguishes": True,
                "adequacy_reason": "The expected combined result differs.",
            }
        ],
    )

    criterion = AcceptanceCriterion.from_data(result["criteria"][0])
    assert criterion.quality.criterion_status == "checkable"
    assert result["reviews"][0]["assertion_reviews"][0]["evidence_valid"] is True
    assert result["requirement_coverage"]["instruction-001"]["status"] == "checkable"

    invalid_evidence = bind_acceptance_criterion_reviews(
        review_plan,
        [
            {
                "assertion_reviews": [
                    json.dumps(
                        {
                            "assertion_id": assertion_id,
                            "category": "source_supported",
                            "source_evidence": "the system behaves correctly",
                            "reason": "unsupported quote",
                        }
                    )
                ],
                "setup_review": json.dumps(
                    {
                        "category": "illustrative_setup",
                        "source_evidence": source,
                        "reason": (
                            "Values are illustrative inputs without adding product "
                            "constraints."
                        ),
                    }
                ),
                "decision_records": [],
                "adequate": True,
                "plausible_incorrect_behavior": "returns only the latest payload",
                "distinguishes": True,
                "adequacy_reason": "The expected combined result differs.",
            }
        ],
    )
    assert (
        invalid_evidence["criteria"][0]["quality"]["criterion_status"] == "source_only"
    )


def test_assertion_local_repair_retains_supported_claim_and_records_attempt() -> None:
    source = "The result mapping forwards each payload and preserves its contents."
    contract = _contract(("instruction-001",))
    contracts = {
        "schema_version": "behavioral-contract-collection-v1",
        "ledger_fingerprint": "sha256:ledger",
        "covered_requirement_ids": ["instruction-001"],
        "contracts": [contract.to_data()],
    }
    source_by_id = {"instruction-001": source}
    generation = prepare_acceptance_criteria(contracts, source_by_id)
    generated_value = json.loads(_criterion([0]))
    generated_value["assertions"].append(
        json.dumps(
            {
                "observation": "result.cancelled",
                "relation": "equals",
                "expected": True,
                "source_indexes": [0],
                "basis": "source_derived",
            }
        )
    )
    generated_value["assertions"][0] = json.dumps(
        {
            "observation": "result.payload",
            "relation": "equals",
            "expected": "the forwarded payload",
            "source_indexes": [0],
            "basis": "source_derived",
        }
    )
    generated = json.dumps(generated_value)
    draft = bind_acceptance_criteria(generation, [{"criteria": [generated]}])
    review_plan = prepare_acceptance_criterion_reviews(draft, contracts, source_by_id)
    criterion = review_plan["requests"][0]["criterion"]
    forwarding_id, cancellation_id = [
        item["assertion_id"] for item in criterion["assertions"]
    ]
    reviewed = bind_acceptance_criterion_reviews(
        review_plan,
        [
            {
                "assertion_reviews": [
                    json.dumps(
                        {
                            "assertion_id": forwarding_id,
                            "category": "source_supported",
                            "source_evidence": "forwards each payload",
                            "reason": "Forwarding is explicitly required.",
                        }
                    ),
                    json.dumps(
                        {
                            "assertion_id": cancellation_id,
                            "category": "unsupported",
                            "source_evidence": (
                                "the request contains no cancellation rule"
                            ),
                            "reason": "Cancellation behavior is not specified.",
                        }
                    ),
                ],
                "setup_review": json.dumps(
                    {
                        "category": "illustrative_setup",
                        "source_evidence": source,
                        "reason": "The payload is an arbitrary example value.",
                    }
                ),
                "decision_records": [],
                "adequate": False,
                "plausible_incorrect_behavior": "drops payload contents",
                "distinguishes": False,
                "adequacy_reason": (
                    "The unsupported cancellation assertion is not evidence."
                ),
            }
        ],
    )
    repair_state = prepare_acceptance_criterion_repairs(
        reviewed, contracts, source_by_id
    )
    assert repair_state["done"] is False
    repair_request = repair_state["requests"][0]
    assert [
        item["assertion_id"]
        for item in repair_request["failed_criteria"][0]["accepted_assertions"]
    ] == [forwarding_id]
    repair_candidate = json.loads(_criterion([0]))
    repair_candidate["assertions"] = [
        json.dumps(
            {
                "observation": "result.payload",
                "relation": "equals",
                "expected": "the forwarded payload",
                "source_indexes": [0],
                "basis": "source_derived",
            }
        )
    ]
    repaired = bind_acceptance_criterion_repairs(
        {**repair_state, "criterion_collection": reviewed},
        [{"criteria": [json.dumps(repair_candidate)]}],
    )
    repaired_criterion = AcceptanceCriterion.from_data(
        repaired["criterion_collection"]["criteria"][0]
    )
    assert repaired_criterion.quality.repair_attempts == 1
    assert any(
        item.assertion_id == forwarding_id for item in repaired_criterion.assertions
    )
    assert all(
        item.assertion_id != cancellation_id for item in repaired_criterion.assertions
    )
    assert repaired["criterion_collection"]["repair_attempts"][0]["round"] == 1
    repair_review_plan = prepare_acceptance_criterion_reviews(
        repaired["criterion_collection"], contracts, source_by_id
    )
    repair_request = repair_review_plan["requests"][0]
    repair_review = bind_acceptance_criterion_reviews(
        repair_review_plan,
        [
            {
                "assertion_reviews": [
                    json.dumps(
                        {
                            "assertion_id": assertion["assertion_id"],
                            "category": "source_supported",
                            "source_evidence": "forwards each payload",
                            "reason": "The source explicitly requires forwarding.",
                        }
                    )
                    for assertion in repair_request["assertions"]
                ],
                "setup_review": json.dumps(
                    {
                        "category": "illustrative_setup",
                        "source_evidence": source,
                        "reason": "The payload is an arbitrary example value.",
                    }
                ),
                "decision_records": [],
                "adequate": True,
                "plausible_incorrect_behavior": "drops the payload contents",
                "distinguishes": True,
                "adequacy_reason": "The expected forwarded payload differs.",
            }
        ],
    )
    reviewed_criterion = AcceptanceCriterion.from_data(repair_review["criteria"][0])
    assert reviewed_criterion.quality.criterion_status == "checkable"
    assert reviewed_criterion.quality.repair_attempts == 1
    packet = compile_implementation_packet(
        objective="Forward payloads.",
        obligations=("Forward each payload.",),
        required_tests=({"description": "observe the forwarded payload"},),
        allowed_paths=("src/", "tests/"),
        validation_profiles=("pytest",),
        acceptance_criteria=(reviewed_criterion.to_data(),),
    )
    worker_text = packet.render()
    assert 'Check that result.payload equals "the forwarded payload".' in worker_text
    assert "result.cancelled" not in worker_text


def test_repository_claim_without_attached_evidence_is_not_accepted() -> None:
    source = "The result contains both entries."
    contract = _contract(("instruction-001",))
    contracts = {
        "schema_version": "behavioral-contract-collection-v1",
        "ledger_fingerprint": "sha256:ledger",
        "covered_requirement_ids": ["instruction-001"],
        "contracts": [contract.to_data()],
    }
    generation = prepare_acceptance_criteria(contracts, {"instruction-001": source})
    draft = bind_acceptance_criteria(generation, [{"criteria": [_criterion([0])]}])
    review_plan = prepare_acceptance_criterion_reviews(
        draft, contracts, {"instruction-001": source}
    )
    assertion_id = review_plan["requests"][0]["criterion"]["assertions"][0][
        "assertion_id"
    ]
    reviewed = bind_acceptance_criterion_reviews(
        review_plan,
        [
            {
                "assertion_reviews": [
                    json.dumps(
                        {
                            "assertion_id": assertion_id,
                            "category": "repository_supported",
                            "source_evidence": "",
                            "repository_evidence": {
                                "location": "src/example.py:12",
                                "revision_or_fingerprint": "abc123",
                                "excerpt": "some convention",
                            },
                            "reason": "A local convention supports this result.",
                        }
                    )
                ],
                "setup_review": json.dumps(
                    {
                        "category": "illustrative_setup",
                        "source_evidence": source,
                        "reason": "Illustrative values.",
                    }
                ),
                "decision_records": [],
                "adequate": True,
                "plausible_incorrect_behavior": "returns only one entry",
                "distinguishes": True,
                "adequacy_reason": "The expected entries are both observed.",
            }
        ],
    )
    assert reviewed["criteria"][0]["quality"]["criterion_status"] == "source_only"
    assert reviewed["reviews"][0]["assertion_reviews"][0]["category"] == "unsupported"


def test_unresolved_material_choice_is_labeled_assumption() -> None:
    source = "The mapping is updated when a payload arrives."
    contract = _contract(("instruction-001",))
    contracts = {
        "schema_version": "behavioral-contract-collection-v1",
        "ledger_fingerprint": "sha256:ledger",
        "covered_requirement_ids": ["instruction-001"],
        "contracts": [contract.to_data()],
    }
    generation = prepare_acceptance_criteria(contracts, {"instruction-001": source})
    draft = bind_acceptance_criteria(generation, [{"criteria": [_criterion([0])]}])
    review_plan = prepare_acceptance_criterion_reviews(
        draft, contracts, {"instruction-001": source}
    )
    assertion_id = review_plan["requests"][0]["criterion"]["assertions"][0][
        "assertion_id"
    ]
    decision = {
        "question": "Does updating preserve aliases held by callers?",
        "affected_ids": ["instruction-001", assertion_id],
        "alternatives": [
            "mutate the existing mapping",
            "replace it with a fresh mapping",
        ],
        "selected_interpretation": "Use the existing mapping.",
        "source_constraints": ["The mapping is updated when a payload arrives."],
        "basis": "assumption",
        "basis_evidence": (
            "The source and permitted evidence do not settle alias behavior."
        ),
        "confidence": "low",
        "residual_uncertainty": "Alias behavior remains unverified.",
        "dimension_state": "needed_for_implementation",
    }
    reviewed = bind_acceptance_criterion_reviews(
        review_plan,
        [
            {
                "assertion_reviews": [
                    json.dumps(
                        {
                            "assertion_id": assertion_id,
                            "category": "source_supported",
                            "source_evidence": "mapping is updated",
                            "reason": "The behavior is explicit.",
                        }
                    )
                ],
                "setup_review": json.dumps(
                    {
                        "category": "illustrative_setup",
                        "source_evidence": source,
                        "reason": "Arbitrary mapping values.",
                    }
                ),
                "decision_records": [json.dumps(decision)],
                "adequate": True,
                "plausible_incorrect_behavior": "the mapping remains unchanged",
                "distinguishes": True,
                "adequacy_reason": "The updated mapping is observable.",
            }
        ],
    )
    assert reviewed["criteria"][0]["quality"]["criterion_status"] == "unresolved"
    record = reviewed["reviews"][0]["decision_records"][0]
    assert record["dimension_state"] == "needed_for_implementation"
    assert record["evidence_status"] == "assumption"
    assert record["is_assumption"] is True


def test_repair_stops_after_two_rounds_with_source_only_coverage() -> None:
    source = "The result contains both entries."
    contract = _contract(("instruction-001",))
    contracts = {
        "schema_version": "behavioral-contract-collection-v1",
        "ledger_fingerprint": "sha256:ledger",
        "covered_requirement_ids": ["instruction-001"],
        "contracts": [contract.to_data()],
    }
    source_by_id = {"instruction-001": source}
    generation = prepare_acceptance_criteria(contracts, source_by_id)
    draft = bind_acceptance_criteria(generation, [{"criteria": []}])
    reviewed = bind_acceptance_criterion_reviews(
        prepare_acceptance_criterion_reviews(draft, contracts, source_by_id), []
    )
    normative_plan = prepare_acceptance_criterion_repairs(
        reviewed, contracts, source_by_id, uncertainty_policy="normative_default"
    )
    assert normative_plan["requests"][0]["uncertainty_policy"] == "normative_default"
    assert any(
        "evidenced local convention" in item
        for item in normative_plan["requests"][0]["repair_instructions"]
    )
    for repair_round in (1, 2):
        repair_state = prepare_acceptance_criterion_repairs(
            reviewed, contracts, source_by_id
        )
        assert repair_state["done"] is False
        assert repair_state["requests"][0]["repair_round"] == repair_round
        assert repair_state["requests"][0]["uncertainty_policy"] == "clarify"
        repair_draft = bind_acceptance_criterion_repairs(
            {**repair_state, "criterion_collection": reviewed},
            [{"criteria": []}],
        )
        repair_review_plan = prepare_acceptance_criterion_reviews(
            repair_draft["criterion_collection"], contracts, source_by_id
        )
        reviewed = bind_acceptance_criterion_reviews(repair_review_plan, [])

    assert reviewed["counts"]["needs_repair"] == 0
    coverage = reviewed["requirement_coverage"]["instruction-001"]
    assert coverage["status"] == "source_only"
    assert coverage["repair_attempts"] == 2
    assert len(reviewed["repair_attempts"]) == 2


@pytest.mark.parametrize(
    "update, message",
    [
        ({"observation": ""}, "observation"),
        ({"kind": "transformation", "setup": {}}, "observable setup"),
        (
            {"relation": "satisfies", "expected": "works correctly"},
            "concrete predicate",
        ),
        ({"kind": "state_transition", "events": []}, "needs events"),
        ({"kind": "rejection"}, "failure assertion"),
        ({"expected": "result.items"}, "circular"),
    ],
)
def test_typed_criterion_rejects_unobservable_or_circular_forms(
    update: dict[str, object], message: str
) -> None:
    criterion_raw = json.loads(_criterion([0]))
    if "relation" in update or "expected" in update or "observation" in update:
        criterion_raw["assertions"][0] = json.dumps(
            {**json.loads(criterion_raw["assertions"][0]), **update}
        )
    else:
        criterion_raw.update(update)
        if update.get("kind") == "rejection":
            criterion_raw["assertions"][0] = json.dumps(
                {**json.loads(criterion_raw["assertions"][0]), "relation": "equals"}
            )
    contract = _contract(("instruction-001",))
    plan = prepare_acceptance_criteria(
        {
            "schema_version": "behavioral-contract-collection-v1",
            "ledger_fingerprint": "sha256:ledger",
            "covered_requirement_ids": ["instruction-001"],
            "contracts": [contract.to_data()],
        },
        {"instruction-001": "Source requirement."},
    )

    result = bind_acceptance_criteria(plan, [{"criteria": [json.dumps(criterion_raw)]}])
    assert result["criteria"] == []
    assert result["requirement_coverage"]["instruction-001"]["status"] == "source_only"
    assert (
        message
        in result["requirement_coverage"]["instruction-001"]["criterion_quality"][
            "failure_reason"
        ]
    )
