from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from powdrr_service.planning.evidence import EvidenceError
from powdrr_service.verification.requirement_execution import (
    RequirementExecutionReceipt,
    requirement_coverage_digest,
)

from powdrr_lift.workrr.planning.execution_receipts import (
    MANIFEST_SCHEMA,
    RECEIPT_ARTIFACT_NAME,
    discover_requirement_execution_receipts,
    load_requirement_execution_receipts,
)


def _manifest_data(commit: str = "a" * 40) -> dict[str, Any]:
    requirement_digest = requirement_coverage_digest(
        requirement_id="req-0001",
        statement="Implement expected behavior.",
        kind="functional",
        source_refs=("anchor-0001",),
    )
    receipt = RequirementExecutionReceipt.observed(
        requirement_id="req-0001",
        requirement_digest=requirement_digest,
        source_commit=commit,
        candidate_commit=commit,
        oracle_id="oracle:sample",
        oracle_digest="sha256:" + "b" * 64,
        command_digest="sha256:" + "c" * 64,
        outcome="passed",
        run_id="github:run/456",
        artifact_refs=("artifact:run/456/result.json",),
    ).to_data()
    excerpt = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    evidence = {
        "id": "evidence-0001",
        "base_commit": commit,
        "path": "artifacts/requirement-receipt.json",
        "start_line": 1,
        "end_line": 1,
        "content_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "kind": "execution_receipt",
        "symbol": None,
    }
    return {
        "schema_version": MANIFEST_SCHEMA,
        "base_commit": commit,
        "receipts": [{"evidence": evidence, "receipt": receipt}],
    }


def test_receipt_manifest_loads_only_at_its_pinned_commit(tmp_path: Path) -> None:
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps(_manifest_data()), encoding="utf-8")

    evidence_by_requirement, excerpts = load_requirement_execution_receipts(
        path, pinned_commit="a" * 40
    )

    evidence = evidence_by_requirement["req-0001"][0]
    assert evidence.kind == "execution_receipt"
    assert evidence.id in excerpts
    assert json.loads(excerpts[evidence.id])["requirement_id"] == "req-0001"

    with pytest.raises(EvidenceError, match="different base commit"):
        load_requirement_execution_receipts(path, pinned_commit="d" * 40)


def test_receipt_manifest_rejects_excerpt_digest_mismatch(tmp_path: Path) -> None:
    manifest = _manifest_data()
    receipt_record = manifest["receipts"][0]
    assert isinstance(receipt_record, dict)
    evidence = receipt_record["evidence"]
    assert isinstance(evidence, dict)
    evidence["content_sha256"] = "e" * 64
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceError, match="digest does not match"):
        load_requirement_execution_receipts(path, pinned_commit="a" * 40)


def test_discovery_downloads_only_successful_artifacts_for_exact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "a" * 40
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> Any:
        calls.append(command)
        if command[1:3] == ["run", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 1,
                            "headSha": "f" * 40,
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "databaseId": 2,
                            "headSha": commit,
                            "status": "completed",
                            "conclusion": "failure",
                        },
                        {
                            "databaseId": 3,
                            "headSha": commit,
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ]
                ),
                stderr="",
            )
        destination = Path(command[command.index("--dir") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "receipts.json").write_text(
            json.dumps(_manifest_data(commit)), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.execution_receipts.subprocess.run", run
    )
    evidence_by_requirement, _excerpts = discover_requirement_execution_receipts(
        "owner/repo", pinned_commit=commit
    )

    assert list(evidence_by_requirement) == ["req-0001"]
    assert calls[0][calls[0].index("--commit") + 1] == commit
    assert calls[1][3] == "3"
    assert calls[1][calls[1].index("--name") + 1] == RECEIPT_ARTIFACT_NAME


def test_discovery_returns_no_evidence_when_runs_have_no_receipt_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40

    def run(command: list[str], **_kwargs: Any) -> Any:
        if command[1:3] == ["run", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 4,
                            "headSha": commit,
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                ),
                stderr="",
            )
        return SimpleNamespace(
            returncode=1, stdout="", stderr="no artifacts found for run"
        )

    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.execution_receipts.subprocess.run", run
    )

    assert discover_requirement_execution_receipts(
        "owner/repo", pinned_commit=commit
    ) == ({}, {})
