from __future__ import annotations

from powdrr_lift.core.verification_contract import VerificationContract


def _contract(**overrides: object) -> VerificationContract:
    data: dict[str, object] = {
        "id": "verify.transcript",
        "description": "Transcript survives process exit.",
        "intent_refs": ["intent.transcript"],
        "provider": "pytest",
        "selector": "tests/test_transcript.py::test_survives_exit",
        "profile": "e2e",
        "expectation": "pass",
        "applicability": {"mode": "affected_closure"},
        "protected_inputs": ["src/transcript/**"],
        "status": "active",
    }
    data.update(overrides)
    return VerificationContract.from_mapping(data)


def test_complete_contract_round_trips_and_fingerprints() -> None:
    contract = _contract()

    assert contract.is_complete
    assert not contract.is_legacy
    assert contract.to_data()["intent_refs"] == ["intent.transcript"]
    assert contract.fingerprint.startswith("sha256:")
    assert contract.fingerprint != _contract(selector="tests/test_other.py").fingerprint


def test_legacy_required_test_case_remains_parseable_and_incomplete() -> None:
    contract = VerificationContract.from_mapping(
        {"id": "legacy-test", "description": "Existing required test."}
    )

    assert contract.is_legacy
    assert not contract.is_complete
    assert contract.validation_errors() == ()


def test_partially_migrated_contract_reports_missing_fields() -> None:
    contract = _contract(selector=None, status=None)

    assert not contract.is_complete
    assert "selector is required" in contract.validation_errors()
    assert "status is required" in contract.validation_errors()


def test_contract_rejects_unsupported_expectation_and_status() -> None:
    contract = _contract(expectation="skip", status="retired")

    assert contract.validation_errors() == (
        "expectation must be one of: absent, pass",
        "status must be one of: active, superseded",
    )


def test_contract_rejects_malformed_collection_fields() -> None:
    errors = VerificationContract.mapping_validation_errors(
        {
            "id": "verify.malformed",
            "description": "Malformed contract.",
            "intent_refs": "intent.one",
            "protected_inputs": ["src/**", ""],
            "applicability": "affected_closure",
        }
    )

    assert errors == (
        "intent_refs must be an array of strings",
        "protected_inputs must contain non-empty strings",
        "applicability must be an object",
    )
