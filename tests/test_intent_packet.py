from __future__ import annotations

import pytest

from powdrr_lift.core.execution_plan import ExecutionUnit
from powdrr_lift.core.intent_packet import IntentPacket


def test_packet_fingerprint_is_independent_of_mapping_order() -> None:
    first = IntentPacket(
        "operation-1",
        required_operations=({"change": {"id": "x", "action": "add"}},),
        must_preserve=("the invariant",),
    )
    second = IntentPacket(
        "operation-1",
        required_operations=({"change": {"action": "add", "id": "x"}},),
        must_preserve=("the invariant",),
    )

    assert first.fingerprint == second.fingerprint
    assert first.to_data()["fingerprint"] == first.fingerprint


def test_packet_round_trip_rejects_tampering() -> None:
    packet = IntentPacket("operation-1", must_preserve=("keep this",))
    data = packet.to_data()
    data["must_preserve"] = ["do not keep this"]

    with pytest.raises(ValueError, match="fingerprint"):
        IntentPacket.from_data(data)


def test_packet_round_trip_preserves_the_request_contract() -> None:
    from powdrr_lift.workrr.coding_agent import ImplementationRequest

    request = ImplementationRequest.from_execution_unit(
        ExecutionUnit(
            unit_id="operation-1",
            objective="Add the adapter.",
            paths=("src/adapter.py",),
            acceptance_criteria=("the adapter is bounded",),
        ),
        request_id="request-1",
        base_commit="abc",
        plan_fingerprint="plan-1",
    )

    assert ImplementationRequest.from_data(request.to_data()) == request


def test_packet_compiles_one_execution_unit_with_explainable_context() -> None:
    from powdrr_lift.workrr.coding_agent import ImplementationRequest

    request = ImplementationRequest.from_execution_unit(
        ExecutionUnit(
            unit_id="operation-1",
            objective="Add the bounded adapter.",
            paths=("src/adapter.py",),
            acceptance_criteria=("the adapter rejects unknown inputs",),
            planned_additions=(
                {"section": "features", "id": "adapter", "action": "added"},
            ),
            non_goals=("Do not redesign the transport.",),
            source_refs=("structrr:entities/adapter",),
        ),
        request_id="request-1",
        base_commit="abc",
        plan_fingerprint="plan-1",
        context_refs=("plan:structrr-diff.yaml",),
    )

    assert request.intent_packet is not None
    assert request.intent_packet.operation_id == "operation-1"
    assert request.intent_packet.source_refs == (
        "structrr:entities/adapter",
        "plan:structrr-diff.yaml",
    )
    assert "Operation-scoped intent packet: operation-1" in request.prompt
    assert "Do not redesign the transport." in request.prompt
    assert "the adapter rejects unknown inputs" in request.prompt


def test_repair_prompt_keeps_original_packet_contract() -> None:
    from powdrr_lift.workrr.coding_agent import ImplementationRequest

    request = ImplementationRequest.from_execution_unit(
        ExecutionUnit(
            unit_id="operation-1",
            objective="Add the bounded adapter.",
            paths=("src/adapter.py",),
            acceptance_criteria=("the adapter rejects unknown inputs",),
        ),
        request_id="request-1",
        base_commit="abc",
        plan_fingerprint="plan-1",
    )

    prompt = request.repair_prompt(
        {"finding_id": "F-1", "expected": "reject unknown inputs"}
    )

    assert "F-1" in prompt
    assert "the adapter rejects unknown inputs" in prompt
    assert "Do not re-plan the feature" in prompt
