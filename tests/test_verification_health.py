from powdrr_lift.structrr.verification_health import (
    HealthFindingKind,
    VerificationPolicyMode,
    audit_verification_health,
)


def test_required_for_new_reports_legacy_debt_without_blocking() -> None:
    report = audit_verification_health(
        intents=({"clause_id": "intent.legacy", "status": "active"},),
        contracts=(),
        mode=VerificationPolicyMode.REQUIRED_FOR_NEW,
    )

    assert report.passed is True
    assert report.findings[0].kind is HealthFindingKind.INTENT_WITHOUT_CONTRACT


def test_required_for_new_blocks_new_uncovered_intent() -> None:
    # A new intent is explicit repository state, not an inferred model claim.
    report = audit_verification_health(
        intents=({"clause_id": "intent.new", "status": "active", "new": True},),
        contracts=(),
        mode=VerificationPolicyMode.REQUIRED_FOR_NEW,
    )

    assert report.passed is False
    assert report.findings[0].blocking is True


def test_enforce_reports_stale_evidence_and_unapproved_verifier_change() -> None:
    report = audit_verification_health(
        intents=({"clause_id": "intent.transcript", "status": "active"},),
        contracts=(
            {
                "id": "verify.transcript",
                "intent_refs": ["intent.transcript"],
                "provider": "pytest",
                "profile": "pytest",
                "selector": "tests/test_transcript.py::test_case",
                "expectation": "pass",
                "status": "active",
            },
        ),
        inventory=(),
        evidence=({"obligation_id": "obligation", "candidate_tree": "old"},),
        candidate_tree="new",
        verifier_changes=({"obligation_id": "obligation"},),
        mode=VerificationPolicyMode.ENFORCE,
    )

    assert report.passed is False
    assert {finding.kind for finding in report.findings} == {
        HealthFindingKind.MISSING_INVENTORY,
        HealthFindingKind.STALE_EVIDENCE,
        HealthFindingKind.UNAUTHORIZED_VERIFIER_CHANGE,
    }


def test_expired_waiver_is_always_blocking() -> None:
    report = audit_verification_health(
        waivers=({"id": "waiver-1", "expires_on": "2026-01-01"},),
        mode=VerificationPolicyMode.OBSERVE,
        today="2026-09-20",
    )

    assert report.passed is False
    assert report.findings[0].kind is HealthFindingKind.EXPIRED_WAIVER
