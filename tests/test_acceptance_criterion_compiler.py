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
    bind_acceptance_criterion_reviews,
    prepare_acceptance_criteria,
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
    assert "Reviewed observable acceptance criteria:" in rendered
    assert 'result.items equals ["A", "B"]' in rendered
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
                            "status": "supported",
                            "source_evidence": "accumulates entries across payloads",
                            "reason": "The cited words support the accumulation rule.",
                        }
                    )
                ],
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
                            "status": "supported",
                            "source_evidence": "the system behaves correctly",
                            "reason": "unsupported quote",
                        }
                    )
                ],
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
