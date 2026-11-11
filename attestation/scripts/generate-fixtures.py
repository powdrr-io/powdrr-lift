#!/usr/bin/env python3
"""Regenerate deterministic protocol fixtures (the key is test-only)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from powdrr_attestation.snapshot import SnapshotRecord, snapshot_digest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
PAYLOAD_TYPE = "application/vnd.in-toto+json"


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main() -> None:
    policy = (
        b"version: 1\nfreshness:\n  max_age_seconds: 86400\n"
        b"environment:\n  toolchain: macos-14-arm64\n  network: allowed\n"
        b"defaults:\n  timeout_seconds: 3600\nsteps:\n"
        b"  - id: setup\n    argv: [make, bootstrap]\n"
        b"  - id: tests\n    argv: [make, test]\n    timeout_seconds: 7200\n"
        b"  - id: quality\n    shell: ./scripts/check-quality.sh\n"
    )
    write(FIXTURES / "valid/policy.yaml", policy)
    write(
        FIXTURES / "valid/policy-formatted.yaml",
        policy.replace(
            b"  max_age_seconds: 86400", b"  max_age_seconds: 86400  # 24 hours"
        ),
    )

    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_id = "sha256:" + hashlib.sha256(public).hexdigest()
    registry = (
        "\n".join(
            [
                "version: 1",
                "users:",
                "  octocat:",
                "    github_user_id: 583231",
                "    providers:",
                "      - provider_id: laptop-test",
                f"        key_id: {key_id}",
                "        algorithm: ed25519",
                f"        public_key: {base64.b64encode(public).decode()}",
                "        status: active",
                "        not_before: '2026-01-01T00:00:00Z'",
                "        not_after: null",
            ]
        )
        + "\n"
    ).encode()
    write(FIXTURES / "valid/attestors.yaml", registry)

    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "powdrr-lift", "digest": {"sha1": "a" * 40}}],
        "predicateType": "https://powdrr.io/attestation/local-validation/v1",
        "predicate": {
            "repository": {"id": 123456, "full_name": "powdrr/powdrr-lift"},
            "principal": {"github_user_id": 583231},
            "provider": {
                "id": "laptop-test",
                "version": "0.1.0-test",
                "key_id": key_id,
                "security_profile": "macos-worker-v1",
            },
            "source": {
                "object_format": "sha1",
                "commit": "a" * 40,
                "tree": "b" * 40,
                "snapshot_sha256": "c" * 64,
            },
            "policy": {
                "path": ".github/local-validation.yaml",
                "trusted_ref": "refs/heads/main",
                "resolved_commit": "d" * 40,
                "sha256": hashlib.sha256(policy).hexdigest(),
                "candidate_sha256": None,
            },
            "environment": {
                "os": "macos",
                "os_version": "14.0",
                "architecture": "arm64",
                "toolchain_id": "macos-14-arm64",
                "toolchain_sha256": "e" * 64,
                "network": "allowed",
                "execution_identity": "dedicated-worker",
            },
            "run": {
                "id": "00000000-0000-4000-8000-000000000001",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "duration_ms": 1000,
                "result": "passed",
                "cleanup": "complete",
            },
            "steps": [
                {
                    "id": step_id,
                    "definition_sha256": hashlib.sha256(
                        policy + b"\0" + step_id.encode()
                    ).hexdigest(),
                    "status": "passed",
                    "exit_code": 0,
                    "signal": None,
                    "duration_ms": 100,
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                }
                for step_id in ("setup", "tests", "quality")
            ],
        },
    }
    payload = json.dumps(statement, separators=(",", ":"), ensure_ascii=False).encode()
    pae = b"DSSEv1 " + str(len(PAYLOAD_TYPE)).encode() + b" " + PAYLOAD_TYPE.encode()
    pae += b" " + str(len(payload)).encode() + b" " + payload
    signature = private.sign(pae)
    envelope = {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode(),
        "signatures": [{"keyid": key_id, "sig": base64.b64encode(signature).decode()}],
    }
    write(FIXTURES / "crypto/statement.json", payload + b"\n")
    write(FIXTURES / "crypto/pae.hex", pae.hex().encode() + b"\n")
    write(
        FIXTURES / "crypto/envelope.json",
        json.dumps(envelope, indent=2).encode() + b"\n",
    )
    write(FIXTURES / "crypto/ed25519-public-key.hex", public.hex().encode() + b"\n")
    write(
        FIXTURES / "crypto/ed25519-private-seed.TEST-ONLY.hex",
        bytes(range(32)).hex().encode() + b"\n",
    )
    write(FIXTURES / "crypto/ed25519-signature.hex", signature.hex().encode() + b"\n")

    records = [
        SnapshotRecord(b"empty.txt", 0, False, b""),
        SnapshotRecord(b"bin/tool", 0, True, b"#!/bin/sh\n"),
        SnapshotRecord(b"link", 1, False, b"bin/tool"),
        SnapshotRecord("caf\u00e9.txt".encode(), 0, False, "ol\u00e1".encode()),
    ]
    write(
        FIXTURES / "snapshot-vectors.json",
        json.dumps(
            {
                "records": [
                    {
                        "path_hex": r.path.hex(),
                        "kind": r.kind,
                        "executable": r.executable,
                        "content_hex": r.content.hex(),
                    }
                    for r in records
                ],
                "digest": snapshot_digest(records),
                "reverse_order_digest": snapshot_digest(reversed(records)),
                "policy_sha256": hashlib.sha256(policy).hexdigest(),
                "step_definition_sha256": {
                    step_id: hashlib.sha256(
                        policy + b"\0" + step_id.encode()
                    ).hexdigest()
                    for step_id in ("setup", "tests", "quality")
                },
            },
            indent=2,
        ).encode()
        + b"\n",
    )


if __name__ == "__main__":
    main()
