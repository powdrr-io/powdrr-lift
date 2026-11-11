from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from powdrr_attestation.snapshot import (
    SnapshotRecord,
    snapshot_digest,
    step_definition_digest,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def test_frozen_snapshot_vectors_cover_record_kinds_and_byte_sorting() -> None:
    vector = json.loads((FIXTURES / "snapshot-vectors.json").read_text())
    records = [
        SnapshotRecord(
            bytes.fromhex(record["path_hex"]),
            record["kind"],
            record["executable"],
            bytes.fromhex(record["content_hex"]),
        )
        for record in vector["records"]
    ]
    assert snapshot_digest(records) == vector["digest"]
    assert snapshot_digest(reversed(records)) == vector["reverse_order_digest"]
    assert vector["digest"] == vector["reverse_order_digest"]
    assert {record.kind for record in records} == {0, 1}
    assert any(record.executable for record in records)
    assert any(record.content == b"" for record in records)
    assert any(any(byte >= 128 for byte in record.path) for record in records)


def test_duplicate_paths_and_invalid_symlink_metadata_fail() -> None:
    record = SnapshotRecord(b"same", 0, False, b"x")
    try:
        snapshot_digest([record, record])
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate path accepted")
    try:
        snapshot_digest([SnapshotRecord(b"link", 1, True, b"target")])
    except ValueError as error:
        assert "kind" in str(error)
    else:
        raise AssertionError("executable symlink accepted")


def test_step_digest_binds_exact_raw_policy_bytes_and_id() -> None:
    assert step_definition_digest(b"policy", "tests") == step_definition_digest(
        b"policy", "tests"
    )
    assert step_definition_digest(b"policy", "tests") != step_definition_digest(
        b"policy ", "tests"
    )
    assert step_definition_digest(b"policy", "tests") != step_definition_digest(
        b"policy", "quality"
    )


def test_policy_and_step_digests_match_shared_golden_vectors() -> None:
    vector = json.loads((FIXTURES / "snapshot-vectors.json").read_text())
    policy = (FIXTURES / "valid/policy.yaml").read_bytes()
    assert sha256(policy).hexdigest() == vector["policy_sha256"]
    assert {
        step_id: step_definition_digest(policy, step_id)
        for step_id in ("setup", "tests", "quality")
    } == vector["step_definition_sha256"]
