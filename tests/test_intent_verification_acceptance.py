from __future__ import annotations

from pathlib import Path
from typing import Any

from powdrr_lift.structrr.verification_obligations import VerificationObligation
from powdrr_lift.workrr.evidence_reconciliation import (
    reconcile_verification_evidence,
)
from powdrr_lift.workrr.verification_evidence import VerificationEvidenceRunner
from powdrr_lift.workrr.verification_provider import (
    PytestVerificationProvider,
    VerificationProviderRegistry,
)


def _obligation(
    *, verifier_fingerprint: str = "sha256:verifier"
) -> VerificationObligation:
    return VerificationObligation(
        obligation_id="sha256:transcript-obligation",
        contract_id="verify.transcript.persist-across-runs",
        contract_fingerprint="sha256:contract",
        intent_refs=("intent.transcript.persist-across-runs",),
        provider="pytest",
        selector="tests/test_transcript.py::test_transcript_survives_process_exit",
        profile="pytest",
        expectation="pass",
        applicability={"mode": "affected_closure"},
        applicability_explanation="transcript implementation changed",
        protected_inputs=("src/transcript.py",),
        verifier_fingerprint=verifier_fingerprint,
        provider_inventory_fingerprint="sha256:inventory",
    )


def _inventory(obligation: VerificationObligation) -> tuple[dict[str, Any], ...]:
    return (
        {
            "provider": obligation.provider,
            "profile": obligation.profile,
            "selector": obligation.selector,
            "command": ["python", "-m", "pytest", "-q"],
            "fingerprint": "sha256:inventory",
        },
    )


def _runner() -> VerificationEvidenceRunner:
    return VerificationEvidenceRunner(
        VerificationProviderRegistry((PytestVerificationProvider(),)),
        timeout_seconds=30,
    )


def _write_transcript_fixture(root: Path, *, expected: str = "new-value") -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n",
        encoding="utf-8",
    )
    (root / "src" / "transcript.py").write_text(
        "def transcript_value():\n    return 'old'\n", encoding="utf-8"
    )
    (root / "tests" / "test_transcript.py").write_text(
        "from transcript import transcript_value\n\n"
        "def test_transcript_survives_process_exit():\n"
        f"    assert transcript_value() == {expected!r}\n",
        encoding="utf-8",
    )


class _OpenCodeRepairSession:
    """Deterministic stand-in for one OpenCode session and its continuation."""

    session_id = "opencode-transcript-session"

    def __init__(self, implementation: Path) -> None:
        self.implementation = implementation
        self.repair_calls = 0

    def repair(self, issues: list[dict[str, Any]]) -> None:
        assert len(issues) == 1
        assert issues[0]["kind"] == "implementation_failure"
        self.repair_calls += 1
        self.implementation.write_text(
            "def transcript_value():\n    return 'new-value'\n", encoding="utf-8"
        )


def test_transcript_regression_is_repaired_in_same_session_and_revalidated(
    tmp_path: Path,
) -> None:
    """Exercise the Phase 6 failure -> repair -> fresh-pass acceptance story."""
    _write_transcript_fixture(tmp_path)
    obligation = _obligation()
    inventory = _inventory(obligation)
    session = _OpenCodeRepairSession(tmp_path / "src" / "transcript.py")

    first = _runner().run(
        [obligation],
        root=tmp_path,
        artifact_root=tmp_path / "artifacts" / "first",
        candidate_tree="sha256:candidate-before",
        inventory=inventory,
    )
    first_reconciliation = reconcile_verification_evidence(
        [obligation.to_data()],
        [item.to_data() for item in first],
        candidate_tree="sha256:candidate-before",
    )
    assert first[0].status.value == "failed"
    assert first_reconciliation["passed"] is False

    issues = first_reconciliation["issues"]
    session.repair(issues)

    second = _runner().run(
        [obligation],
        root=tmp_path,
        artifact_root=tmp_path / "artifacts" / "second",
        candidate_tree="sha256:candidate-after",
        inventory=inventory,
    )
    second_reconciliation = reconcile_verification_evidence(
        [obligation.to_data()],
        [item.to_data() for item in second],
        candidate_tree="sha256:candidate-after",
    )

    assert session.session_id == "opencode-transcript-session"
    assert session.repair_calls == 1
    assert second[0].status.value == "passed"
    assert second_reconciliation["passed"] is True
    assert second_reconciliation["issues"] == []
    assert first[0].candidate_tree != second[0].candidate_tree
    assert Path(second[0].artifact_refs[0]).parent.joinpath("evidence.json").exists()


def test_deleted_required_test_is_blocked(tmp_path: Path) -> None:
    _write_transcript_fixture(tmp_path)
    obligation = _obligation()
    evidence = _runner().run(
        [obligation],
        root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        candidate_tree="sha256:candidate",
        inventory=(),
    )

    result = reconcile_verification_evidence(
        [obligation.to_data()],
        [item.to_data() for item in evidence],
        candidate_tree="sha256:candidate",
    )

    assert result["passed"] is False
    assert result["issues"][0]["kind"] == "missing_verifier"


def test_skipped_required_test_is_blocked(tmp_path: Path) -> None:
    _write_transcript_fixture(tmp_path)
    test_path = tmp_path / "tests" / "test_transcript.py"
    test_path.write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='not ready')\n"
        "def test_transcript_survives_process_exit():\n"
        "    pass\n",
        encoding="utf-8",
    )
    obligation = _obligation()
    evidence = _runner().run(
        [obligation],
        root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        candidate_tree="sha256:candidate",
        inventory=_inventory(obligation),
    )

    result = reconcile_verification_evidence(
        [obligation.to_data()],
        [item.to_data() for item in evidence],
        candidate_tree="sha256:candidate",
    )

    assert evidence[0].status.value == "skipped"
    assert result["passed"] is False
    assert result["issues"][0]["kind"] == "implementation_failure"


def test_weakened_verifier_and_reused_evidence_are_stale(tmp_path: Path) -> None:
    _write_transcript_fixture(tmp_path)
    original = _obligation(verifier_fingerprint="sha256:original-verifier")
    evidence = _runner().run(
        [original],
        root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        candidate_tree="sha256:old-candidate",
        inventory=_inventory(original),
    )
    changed_contract = _obligation(verifier_fingerprint="sha256:weakened-verifier")

    result = reconcile_verification_evidence(
        [changed_contract.to_data()],
        [item.to_data() for item in evidence],
        candidate_tree="sha256:new-candidate",
    )

    assert result["passed"] is False
    assert result["issues"][0]["kind"] == "stale_evidence"
