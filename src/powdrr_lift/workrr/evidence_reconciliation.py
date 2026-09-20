"""Deterministic acceptance of per-contract verification evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.workrr.verification_evidence import EvidenceStatus


def reconcile_verification_evidence(
    obligations: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    *,
    candidate_tree: str,
) -> dict[str, Any]:
    """Map every obligation to exactly one fresh, passing evidence record."""
    by_obligation: dict[str, list[Mapping[str, Any]]] = {}
    for record in evidence:
        obligation_id = record.get("obligation_id")
        if isinstance(obligation_id, str):
            by_obligation.setdefault(obligation_id, []).append(record)

    issues: list[dict[str, Any]] = []
    accepted: list[str] = []
    for obligation in obligations:
        obligation_id = str(obligation.get("obligation_id", ""))
        matches = by_obligation.get(obligation_id, [])
        if len(matches) != 1:
            issues.append(
                _issue(
                    obligation,
                    "missing_evidence" if not matches else "duplicate_evidence",
                    "expected exactly one evidence record for the obligation",
                )
            )
            continue
        record = matches[0]
        mismatch = _identity_mismatch(obligation, record, candidate_tree)
        if mismatch is not None:
            issues.append(_issue(obligation, "stale_evidence", mismatch))
            continue
        status = record.get("status")
        if status != EvidenceStatus.PASSED.value:
            issues.append(
                _issue(
                    obligation,
                    _issue_kind(status),
                    f"verification evidence status is {status!r}",
                    evidence=record,
                )
            )
            continue
        accepted.append(obligation_id)

    return {
        "passed": not issues,
        "issues": issues,
        "accepted_obligation_ids": accepted,
        "candidate_tree": candidate_tree,
    }


def _identity_mismatch(
    obligation: Mapping[str, Any],
    evidence: Mapping[str, Any],
    candidate_tree: str,
) -> str | None:
    expected = {
        "candidate_tree": candidate_tree,
        "contract_id": obligation.get("contract_id"),
        "contract_fingerprint": obligation.get("contract_fingerprint"),
        "verifier_fingerprint": obligation.get("verifier_fingerprint"),
        "provider": obligation.get("provider"),
        "selector": obligation.get("selector"),
        "profile": obligation.get("profile"),
    }
    for field, value in expected.items():
        if value and evidence.get(field) != value:
            return f"evidence {field} does not match the current obligation"
    return None


def _issue(
    obligation: Mapping[str, Any],
    kind: str,
    message: str,
    *,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "obligation_id": obligation.get("obligation_id"),
        "contract_id": obligation.get("contract_id"),
        "provider": obligation.get("provider"),
        "selector": obligation.get("selector"),
        "profile": obligation.get("profile"),
        "message": message,
    }
    if evidence is not None:
        result["evidence"] = dict(evidence)
    return result


def _issue_kind(status: object) -> str:
    if status in {"not_collected", "missing"}:
        return "missing_verifier"
    if status in {"errored", "timed_out"}:
        return "verifier_execution_error"
    return "implementation_failure"


__all__ = ["reconcile_verification_evidence"]
