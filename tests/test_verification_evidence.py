from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from powdrr_lift.structrr.verification_obligations import VerificationObligation
from powdrr_lift.workrr.verification_evidence import (
    EvidenceStatus,
    VerificationEvidenceRunner,
)
from powdrr_lift.workrr.verification_provider import (
    ProviderExecutionResult,
    ProviderInventory,
    VerificationProviderRegistry,
    VerificationProviderRequest,
)


def _obligation() -> VerificationObligation:
    return VerificationObligation(
        obligation_id="sha256:obligation",
        contract_id="verify.sample",
        contract_fingerprint="sha256:contract",
        intent_refs=("intent.sample",),
        provider="fake",
        selector="tests/test_sample.py::test_exact",
        profile="fake",
        expectation="pass",
        applicability={"mode": "affected_closure"},
        applicability_explanation="direct intent",
        protected_inputs=("src/**",),
        verifier_fingerprint="sha256:verifier",
        provider_inventory_fingerprint="sha256:inventory",
    )


def test_evidence_runner_persists_identity_and_output_artifacts(tmp_path: Path) -> None:
    class FakeProvider:
        name = "fake"

        def detect(self, root: Path, profiles: Any) -> bool:
            return True

        def inventory(self, root: Path, profile: Any) -> ProviderInventory:
            return ProviderInventory("fake", "fake", (_obligation().selector,))

        def execute(
            self, request: VerificationProviderRequest
        ) -> ProviderExecutionResult:
            return ProviderExecutionResult(
                request.provider,
                request.selector,
                request.profile,
                "passed",
                stdout="structured output",
                duration_ms=12,
                provider_version="fake-1",
            )

        def normalize(self, result: ProviderExecutionResult) -> dict[str, Any]:
            return {"status": result.status}

    progress: list[dict[str, Any]] = []
    runner = VerificationEvidenceRunner(
        VerificationProviderRegistry((FakeProvider(),)),
        progress=lambda payload: progress.append(dict(payload)),
    )
    evidence = runner.run(
        (_obligation(),),
        root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        candidate_tree="sha256:candidate",
        inventory=(
            {
                "provider": "fake",
                "profile": "fake",
                "selector": _obligation().selector,
                "command": ["fake", "run"],
                "fingerprint": "sha256:inventory",
            },
        ),
    )

    assert evidence[0].status is EvidenceStatus.PASSED
    assert evidence[0].candidate_tree == "sha256:candidate"
    assert evidence[0].artifact_refs
    record = Path(evidence[0].artifact_refs[0]).parent / "evidence.json"
    assert json.loads(record.read_text(encoding="utf-8"))["status"] == "passed"
    assert [item["event"] for item in progress] == [
        "verification_started",
        "verification_finished",
    ]


def test_missing_inventory_is_not_collected(tmp_path: Path) -> None:
    runner = VerificationEvidenceRunner(VerificationProviderRegistry())
    evidence = runner.run(
        (_obligation(),),
        root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        candidate_tree="sha256:candidate",
        inventory=(),
    )

    assert evidence[0].status is EvidenceStatus.NOT_COLLECTED
    assert evidence[0].error is not None
