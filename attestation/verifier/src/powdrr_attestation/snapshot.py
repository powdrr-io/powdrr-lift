"""Cross-language snapshot and step-definition digests frozen by PR 01."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from dataclasses import dataclass

SNAPSHOT_MAGIC = b"POWDRR-ATTEST-SNAPSHOT-V1\0"


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    path: bytes
    kind: int
    executable: bool
    content: bytes


def snapshot_digest(records: Iterable[SnapshotRecord]) -> str:
    """Encode ordinary-file/symlink records in their protocol byte order."""
    values = list(records)
    if len(values) > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("snapshot record count exceeds uint64")
    ordered = sorted(values, key=lambda record: record.path)
    if len({record.path for record in ordered}) != len(ordered):
        raise ValueError("snapshot paths must be unique")
    digest = hashlib.sha256()
    digest.update(SNAPSHOT_MAGIC)
    digest.update(struct.pack(">Q", len(ordered)))
    for record in ordered:
        if record.kind not in (0, 1) or (record.kind == 1 and record.executable):
            raise ValueError("invalid snapshot record kind or executable flag")
        if (
            not record.path
            or len(record.path) > 0xFFFFFFFF
            or len(record.content) > 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError("snapshot path or content exceeds protocol limit")
        digest.update(struct.pack(">I", len(record.path)))
        digest.update(record.path)
        digest.update(bytes((record.kind, int(record.executable))))
        digest.update(struct.pack(">Q", len(record.content)))
        digest.update(hashlib.sha256(record.content).digest())
    return digest.hexdigest()


def step_definition_digest(policy_bytes: bytes, step_id: str) -> str:
    if "\0" in step_id:
        raise ValueError("step ID cannot contain NUL")
    return hashlib.sha256(policy_bytes + b"\0" + step_id.encode("utf-8")).hexdigest()
