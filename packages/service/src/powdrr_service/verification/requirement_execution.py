"""Pinned requirement-level execution receipts for deterministic coverage."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

RECEIPT_SCHEMA = "requirement-execution-receipt-v1"
ReceiptOutcome = Literal["passed", "failed", "blocked", "not_run"]
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_COMMIT = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
_STABLE_ID = re.compile(r"[A-Za-z][A-Za-z0-9:._/-]{0,199}")
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "requirement_id",
        "requirement_digest",
        "source_commit",
        "candidate_commit",
        "oracle_id",
        "oracle_digest",
        "command_digest",
        "outcome",
        "run_id",
        "artifact_refs",
    }
)


@dataclass(frozen=True, slots=True)
class RequirementExecutionReceipt:
    """Immutable observed-run record bound to one exact requirement and oracle."""

    _json: str

    def to_data(self) -> dict[str, Any]:
        return json.loads(self._json)

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> RequirementExecutionReceipt:
        if set(value) != _RECEIPT_FIELDS:
            raise ValueError("requirement execution receipt fields are invalid")
        if value.get("schema_version") != RECEIPT_SCHEMA:
            raise ValueError("unsupported requirement execution receipt schema")
        for field in ("requirement_id", "oracle_id", "run_id"):
            item = value.get(field)
            if not isinstance(item, str) or not _STABLE_ID.fullmatch(item):
                raise ValueError(f"requirement execution receipt has invalid {field}")
        for field in ("source_commit", "candidate_commit"):
            item = value.get(field)
            if not isinstance(item, str) or not _COMMIT.fullmatch(item):
                raise ValueError(f"requirement execution receipt has invalid {field}")
        for field in ("requirement_digest", "oracle_digest", "command_digest"):
            item = value.get(field)
            if not isinstance(item, str) or not _DIGEST.fullmatch(item):
                raise ValueError(f"requirement execution receipt has invalid {field}")
        outcome = value.get("outcome")
        if outcome not in {"passed", "failed", "blocked", "not_run"}:
            raise ValueError("requirement execution receipt has invalid outcome")
        artifacts = value.get("artifact_refs")
        if (
            not isinstance(artifacts, list)
            or not artifacts
            or any(not isinstance(item, str) or not item.strip() for item in artifacts)
            or len(set(artifacts)) != len(artifacts)
        ):
            raise ValueError("requirement execution receipt requires unique artifacts")
        unsigned = {key: item for key, item in value.items() if key != "receipt_id"}
        expected_id = "receipt:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
        if value.get("receipt_id") != expected_id:
            raise ValueError("requirement execution receipt ID is invalid")
        return cls(json.dumps(dict(value), sort_keys=True, ensure_ascii=False))

    @classmethod
    def observed(
        cls,
        *,
        requirement_id: str,
        requirement_digest: str,
        source_commit: str,
        candidate_commit: str,
        oracle_id: str,
        oracle_digest: str,
        command_digest: str,
        outcome: ReceiptOutcome,
        run_id: str,
        artifact_refs: Sequence[str],
    ) -> RequirementExecutionReceipt:
        """Build a receipt from an observed command/oracle execution result."""
        unsigned = {
            "schema_version": RECEIPT_SCHEMA,
            "requirement_id": requirement_id,
            "requirement_digest": requirement_digest,
            "source_commit": source_commit,
            "candidate_commit": candidate_commit,
            "oracle_id": oracle_id,
            "oracle_digest": oracle_digest,
            "command_digest": command_digest,
            "outcome": outcome,
            "run_id": run_id,
            "artifact_refs": list(artifact_refs),
        }
        return cls.from_data(
            {
                **unsigned,
                "receipt_id": "receipt:"
                + hashlib.sha256(_canonical(unsigned)).hexdigest(),
            }
        )

    def proves_coverage(
        self,
        *,
        requirement_id: str,
        requirement_digest: str,
        source_commit: str,
        candidate_commit: str,
    ) -> bool:
        data = self.to_data()
        return (
            data["requirement_id"] == requirement_id
            and data["requirement_digest"] == requirement_digest
            and data["source_commit"] == source_commit
            and data["candidate_commit"] == candidate_commit
            and data["outcome"] == "passed"
        )


def requirement_coverage_digest(
    *,
    requirement_id: str,
    statement: str,
    kind: str,
    source_refs: Sequence[str],
) -> str:
    """Bind a receipt to accepted requirement wording and its cited anchors."""
    payload = {
        "requirement_id": requirement_id,
        "statement": statement,
        "kind": kind,
        "source_refs": list(source_refs),
    }
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


__all__ = [
    "RECEIPT_SCHEMA",
    "RequirementExecutionReceipt",
    "requirement_coverage_digest",
]
