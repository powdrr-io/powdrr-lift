from powdrr_lift.workrr.differential_verification import (
    DifferentialStatus,
    VerifierChangeKind,
    classify_differential_result,
    detect_verifier_changes,
)


def test_differential_classifies_regression_and_repair_outcomes() -> None:
    assert (
        classify_differential_result(
            "obligation",
            {"status": "passed"},
            {"status": "failed"},
        ).status
        is DifferentialStatus.NEW_REGRESSION
    )
    assert (
        classify_differential_result(
            "obligation",
            {"status": "failed"},
            {"status": "passed"},
        ).status
        is DifferentialStatus.FIXED_EXISTING_FAILURE
    )
    assert (
        classify_differential_result(
            "obligation",
            {"status": "failed"},
            {"status": "failed"},
            comparable=False,
        ).status
        is DifferentialStatus.NOT_COMPARABLE
    )


def test_verifier_changes_are_explicitly_identified() -> None:
    changes = detect_verifier_changes(
        {
            "obligation_id": "obligation",
            "contract_fingerprint": "old-contract",
            "selector": "tests/test_old.py::test_case",
            "verifier_fingerprint": "old-verifier",
            "provider_inventory_fingerprint": "old-inventory",
        },
        {
            "obligation_id": "obligation",
            "contract_fingerprint": "new-contract",
            "selector": "tests/test_new.py::test_case",
            "verifier_fingerprint": "new-verifier",
            "provider_inventory_fingerprint": "new-inventory",
        },
    )

    assert {change.kind for change in changes} == {
        VerifierChangeKind.CONTRACT,
        VerifierChangeKind.SELECTOR,
        VerifierChangeKind.VERIFIER,
        VerifierChangeKind.INVENTORY,
    }
