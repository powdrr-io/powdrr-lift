"""Pinned requirement-level execution receipts for deterministic coverage."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

RECEIPT_SCHEMA = "requirement-execution-receipt-v2"
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
    source_group_ids: Sequence[str],
    source_validation_groups: Sequence[tuple[str, str]],
    source_logic: Sequence[Mapping[str, Any]],
) -> str:
    """Bind a receipt to the full typed requirement used by coverage assessment."""
    payload = {
        "schema": "requirement-coverage-binding-v2",
        "requirement_id": requirement_id,
        "statement": statement,
        "kind": kind,
        "source_refs": list(source_refs),
        "source_group_ids": list(source_group_ids),
        "source_validation_groups": [
            {"id": group_id, "relation": relation}
            for group_id, relation in source_validation_groups
        ],
        "source_logic": [dict(expression) for expression in source_logic],
    }
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def receipt_from_verification_evidence(
    *,
    evidence: Mapping[str, Any],
    obligation: Mapping[str, Any],
    requirement_id: str,
    requirement_digest: str,
    source_commit: str,
    candidate_commit: str,
    expected_candidate_tree: str,
    run_id: str,
    evidence_artifact_ref: str,
) -> RequirementExecutionReceipt:
    """Adapt an observed verifier record using an explicit requirement binding.

    Callers must supply the requirement linkage and commit identities from the
    execution session. The adapter checks those identities against the verifier
    record and refuses to infer requirement coverage from story or intent refs.
    """
    if evidence.get("obligation_id") != obligation.get("obligation_id"):
        raise ValueError("verification evidence does not match its obligation")
    if evidence.get("candidate_tree") != expected_candidate_tree:
        raise ValueError("verification evidence is for a different candidate tree")
    for field in (
        "contract_id",
        "provider",
        "selector",
        "profile",
        "contract_fingerprint",
        "verifier_fingerprint",
    ):
        if evidence.get(field) != obligation.get(field):
            raise ValueError(f"verification evidence has mismatched {field}")
    command_digest = evidence.get("command_fingerprint")
    if not isinstance(command_digest, str) or not _DIGEST.fullmatch(command_digest):
        raise ValueError("verification evidence has an invalid command fingerprint")
    provider = obligation.get("provider")
    selector = obligation.get("selector")
    profile = obligation.get("profile")
    if not all(
        isinstance(item, str) and item for item in (provider, selector, profile)
    ):
        raise ValueError("verification obligation lacks an oracle identity")
    oracle_data = {
        "contract_fingerprint": obligation.get("contract_fingerprint"),
        "provider": provider,
        "profile": profile,
        "selector": selector,
        "verifier_fingerprint": obligation.get("verifier_fingerprint"),
    }
    oracle_digest = "sha256:" + hashlib.sha256(_canonical(oracle_data)).hexdigest()
    oracle_id = "oracle:" + hashlib.sha256(_canonical(oracle_data)).hexdigest()
    raw_status = evidence.get("status")
    outcome = getattr(raw_status, "value", raw_status)
    outcome_map: dict[str, ReceiptOutcome] = {
        "passed": "passed",
        "failed": "failed",
        "blocked": "blocked",
        "errored": "blocked",
        "not_collected": "not_run",
    }
    if not isinstance(outcome, str) or outcome not in outcome_map:
        raise ValueError("verification evidence has an unsupported outcome")
    raw_refs = evidence.get("artifact_refs")
    if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
        raise ValueError("verification evidence artifact references are invalid")
    artifact_refs = [
        item for item in raw_refs if isinstance(item, str) and item.strip()
    ]
    if not isinstance(evidence_artifact_ref, str) or not evidence_artifact_ref.strip():
        raise ValueError("verification evidence artifact reference is required")
    artifact_refs.append(evidence_artifact_ref)
    return RequirementExecutionReceipt.observed(
        requirement_id=requirement_id,
        requirement_digest=requirement_digest,
        source_commit=source_commit,
        candidate_commit=candidate_commit,
        oracle_id=oracle_id,
        oracle_digest=oracle_digest,
        command_digest=command_digest,
        outcome=outcome_map[outcome],
        run_id=run_id,
        artifact_refs=tuple(dict.fromkeys(artifact_refs)),
    )


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
    "receipt_from_verification_evidence",
]
