from __future__ import annotations

from powdrr_lift.core.verification_contract import VerificationContract
from powdrr_lift.structrr.proposal import ProposalRevision, compile_proposal_revision
from powdrr_lift.structrr.verification_obligations import (
    compile_verification_obligations,
)


def _contract(**overrides: object) -> VerificationContract:
    data: dict[str, object] = {
        "id": "verify.transcript",
        "description": "The transcript survives a completed run.",
        "intent_refs": ["intent.transcript"],
        "provider": "pytest",
        "selector": "tests/test_transcript.py::test_survives",
        "profile": "pytest",
        "expectation": "pass",
        "applicability": {"mode": "affected_closure"},
        "protected_inputs": ["src/transcript/**"],
        "status": "active",
    }
    data.update(overrides)
    return VerificationContract.from_mapping(data)


def _proposal(plan: dict[str, list[dict[str, object]]]) -> ProposalRevision:
    return compile_proposal_revision(
        "transcript",
        {"entity_relationships": []},
        plan,
        acceptance_criteria=("The transcript survives.",),
        must_preserve=(),
        non_goals=(),
        allowed_paths=("src/transcript/**",),
        source_refs=("structrr:baseline.yaml",),
    )


def _inventory(
    selector: str = "tests/test_transcript.py::test_survives",
) -> list[dict[str, str]]:
    return [
        {
            "provider": "pytest",
            "profile": "pytest",
            "selector": selector,
            "fingerprint": "sha256:inventory",
        }
    ]


def test_direct_protected_input_is_selected() -> None:
    result = compile_verification_obligations(
        _proposal(
            {
                "features": [
                    {
                        "id": "transcript",
                        "action": "added",
                        "intent_refs": ["intent.transcript"],
                    }
                ]
            }
        ),
        active_intents=({"clause_id": "intent.transcript"},),
        contracts=(_contract(),),
        provider_inventory=_inventory(),
        anticipated_paths=("src/transcript/store.py",),
    )

    assert result.complete
    assert [item.contract_id for item in result.obligations] == ["verify.transcript"]
    assert "protected intent" in result.obligations[0].applicability_explanation


def test_relationship_closure_selects_transitive_entity_contract() -> None:
    contract = _contract(
        intent_refs=["intent.other"],
        protected_inputs=["entity:storage"],
    )
    result = compile_verification_obligations(
        _proposal(
            {
                "entities": [{"id": "api", "action": "added"}],
            }
        ),
        active_intents=(),
        contracts=(contract,),
        provider_inventory=_inventory(),
        relationships=(
            {"source": "api", "target": "repository"},
            {"source": "repository", "target": "storage"},
        ),
    )

    assert result.complete
    assert result.obligations[0].contract_id == contract.contract_id
    assert "relationship closure" in result.obligations[0].applicability_explanation


def test_unrelated_contract_is_excluded_with_explanation() -> None:
    result = compile_verification_obligations(
        _proposal({"entities": [{"id": "other", "action": "added"}]}),
        active_intents=({"clause_id": "intent.other"},),
        contracts=(_contract(intent_refs=["intent.transcript"]),),
        provider_inventory=_inventory(),
    )

    assert result.complete
    assert not result.obligations
    assert result.excluded_contracts[0]["contract_id"] == "verify.transcript"


def test_missing_selector_blocks_compilation() -> None:
    result = compile_verification_obligations(
        _proposal(
            {
                "features": [
                    {
                        "id": "transcript",
                        "action": "added",
                        "intent_refs": ["intent.transcript"],
                    }
                ]
            }
        ),
        active_intents=({"clause_id": "intent.transcript"},),
        contracts=(_contract(),),
        provider_inventory=_inventory("tests/test_transcript.py::test_other"),
    )

    assert not result.complete
    assert "missing from provider inventory" in result.failures[0]


def test_new_intent_without_contract_blocks() -> None:
    result = compile_verification_obligations(
        _proposal({"invariants": [{"id": "intent.new", "action": "added"}]}),
        contracts=(),
    )

    assert not result.complete
    assert "intent.new" in result.failures[0]


def test_obligation_fingerprint_is_stable() -> None:
    first = compile_verification_obligations(
        _proposal(
            {
                "features": [
                    {
                        "id": "transcript",
                        "action": "added",
                        "intent_refs": ["intent.transcript"],
                    }
                ]
            }
        ),
        active_intents=({"clause_id": "intent.transcript"},),
        contracts=(_contract(),),
        provider_inventory=_inventory(),
    )
    second = compile_verification_obligations(
        _proposal(
            {
                "features": [
                    {
                        "id": "transcript",
                        "action": "added",
                        "intent_refs": ["intent.transcript"],
                    }
                ]
            }
        ),
        active_intents=({"clause_id": "intent.transcript"},),
        contracts=(_contract(),),
        provider_inventory=_inventory(),
    )

    assert first.fingerprint == second.fingerprint
    assert first.obligations[0].obligation_id == second.obligations[0].obligation_id
