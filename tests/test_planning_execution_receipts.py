from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from powdrr_service.planning.evidence import EvidenceError
from powdrr_service.verification.requirement_execution import (
    RequirementExecutionReceipt,
    requirement_coverage_digest,
)

from powdrr_lift.workrr.planning.execution_receipts import (
    MANIFEST_SCHEMA,
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
