from powdrr_lift.workrr.evidence_reconciliation import reconcile_verification_evidence


def _obligation() -> dict[str, str]:
    return {
        "obligation_id": "sha256:obligation",
        "contract_id": "verify.sample",
        "contract_fingerprint": "sha256:contract",
        "provider": "pytest",
        "selector": "tests/test_sample.py::test_exact",
        "profile": "pytest",
        "verifier_fingerprint": "sha256:verifier",
    }


def _evidence(
    *, status: str = "passed", candidate_tree: str = "sha256:candidate"
) -> dict[str, str]:
    return {
        **_obligation(),
        "status": status,
        "candidate_tree": candidate_tree,
    }


def test_reconciliation_accepts_one_fresh_passing_record() -> None:
    result = reconcile_verification_evidence(
        [_obligation()], [_evidence()], candidate_tree="sha256:candidate"
    )

    assert result["passed"] is True
    assert result["issues"] == []


def test_reconciliation_reports_only_current_failure_and_stale_identity() -> None:
    second = {
        **_obligation(),
        "obligation_id": "sha256:second",
        "contract_id": "verify.second",
    }
    result = reconcile_verification_evidence(
        [_obligation(), second],
        [_evidence(status="failed")],
        candidate_tree="sha256:candidate",
    )

    assert result["passed"] is False
    assert [issue["kind"] for issue in result["issues"]] == [
        "implementation_failure",
        "missing_evidence",
    ]


def test_reconciliation_rejects_stale_candidate_evidence() -> None:
    result = reconcile_verification_evidence(
        [_obligation()],
        [_evidence(candidate_tree="sha256:old")],
        candidate_tree="sha256:new",
    )

    assert result["issues"][0]["kind"] == "stale_evidence"
