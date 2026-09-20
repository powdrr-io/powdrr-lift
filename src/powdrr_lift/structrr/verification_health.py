"""Deterministic verification coverage and evidence health auditing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class VerificationPolicyMode(StrEnum):
    OBSERVE = "observe"
    REQUIRED_FOR_NEW = "required_for_new"
    ENFORCE = "enforce"


class HealthFindingKind(StrEnum):
    INTENT_WITHOUT_CONTRACT = "intent_without_contract"
    INCOMPLETE_CONTRACT = "incomplete_contract"
    MISSING_INVENTORY = "missing_inventory"
    ORPHAN_CONTRACT = "orphan_contract"
    STALE_EVIDENCE = "stale_evidence"
    UNAUTHORIZED_VERIFIER_CHANGE = "unauthorized_verifier_change"
    EXPIRED_WAIVER = "expired_waiver"


@dataclass(frozen=True, slots=True)
class VerificationHealthFinding:
    kind: HealthFindingKind
    subject_id: str
    message: str
    blocking: bool

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "subject_id": self.subject_id,
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class VerificationHealthReport:
    mode: VerificationPolicyMode
    findings: tuple[VerificationHealthFinding, ...]

    @property
    def passed(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    def to_data(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "passed": self.passed,
            "findings": [finding.to_data() for finding in self.findings],
        }


def audit_verification_health(
    *,
    intents: tuple[dict[str, Any], ...] = (),
    contracts: tuple[dict[str, Any], ...] = (),
    inventory: tuple[dict[str, Any], ...] = (),
    evidence: tuple[dict[str, Any], ...] = (),
    candidate_tree: str | None = None,
    verifier_changes: tuple[dict[str, Any], ...] = (),
    waivers: tuple[dict[str, Any], ...] = (),
    mode: VerificationPolicyMode = VerificationPolicyMode.REQUIRED_FOR_NEW,
    today: str | None = None,
) -> VerificationHealthReport:
    """Audit coverage without interpreting intent or invoking a model."""
    findings: list[VerificationHealthFinding] = []
    active_intents = {
        str(item.get("clause_id") or item.get("intent_id")): item
        for item in intents
        if item.get("status", "active") == "active"
    }
    active_contracts = {
        str(item.get("id")): item
        for item in contracts
        if item.get("status", "active") == "active"
    }
    protected = {
        str(intent)
        for contract in active_contracts.values()
        for intent in contract.get("intent_refs", [])
    }
    for intent_id, intent in active_intents.items():
        if intent_id not in protected:
            findings.append(
                _finding(
                    HealthFindingKind.INTENT_WITHOUT_CONTRACT,
                    intent_id,
                    "active intent has no verification contract",
                    mode=mode,
                    item=intent,
                )
            )
    inventory_keys = {
        (item.get("provider"), item.get("profile"), item.get("selector"))
        for item in inventory
    }
    for contract_id, contract in active_contracts.items():
        required = ("intent_refs", "provider", "selector", "profile", "expectation")
        if any(not contract.get(field) for field in required):
            findings.append(
                _finding(
                    HealthFindingKind.INCOMPLETE_CONTRACT,
                    contract_id,
                    "active verification contract is incomplete",
                    mode=mode,
                    item=contract,
                    structural=True,
                )
            )
        elif (
            contract.get("provider"),
            contract.get("profile"),
            contract.get("selector"),
        ) not in inventory_keys:
            findings.append(
                _finding(
                    HealthFindingKind.MISSING_INVENTORY,
                    contract_id,
                    "active contract has no exact provider inventory match",
                    mode=mode,
                    item=contract,
                )
            )
    for contract in contracts:
        contract_id = str(contract.get("id", ""))
        if (
            contract_id
            and contract_id not in active_contracts
            and contract.get("status")
            not in {
                "superseded",
                "retired",
            }
        ):
            findings.append(
                _finding(
                    HealthFindingKind.ORPHAN_CONTRACT,
                    contract_id,
                    "contract does not protect an active contract record",
                    mode=mode,
                    item=contract,
                )
            )
    if candidate_tree:
        for record in evidence:
            if record.get("candidate_tree") != candidate_tree:
                findings.append(
                    _finding(
                        HealthFindingKind.STALE_EVIDENCE,
                        str(record.get("obligation_id", "")),
                        "evidence was produced for a different candidate tree",
                        mode=mode,
                        item=record,
                    )
                )
    for change in verifier_changes:
        if not change.get("authorized"):
            findings.append(
                _finding(
                    HealthFindingKind.UNAUTHORIZED_VERIFIER_CHANGE,
                    str(change.get("obligation_id", "")),
                    "verifier identity changed without an accepted decision",
                    mode=mode,
                    item=change,
                )
            )
    for waiver in waivers:
        expires = waiver.get("expires_on")
        if today and isinstance(expires, str) and expires < today:
            findings.append(
                _finding(
                    HealthFindingKind.EXPIRED_WAIVER,
                    str(waiver.get("id", "")),
                    "verification waiver has expired",
                    mode=mode,
                    item=waiver,
                    structural=True,
                )
            )
    return VerificationHealthReport(mode, tuple(findings))


def _finding(
    kind: HealthFindingKind,
    subject_id: str,
    message: str,
    *,
    mode: VerificationPolicyMode,
    item: dict[str, Any],
    structural: bool = False,
) -> VerificationHealthFinding:
    new_or_changed = bool(item.get("new") or item.get("materially_altered"))
    blocking = (
        structural
        or mode is VerificationPolicyMode.ENFORCE
        or (mode is VerificationPolicyMode.REQUIRED_FOR_NEW and new_or_changed)
    )
    return VerificationHealthFinding(kind, subject_id, message, blocking)


__all__ = [
    "HealthFindingKind",
    "VerificationHealthFinding",
    "VerificationHealthReport",
    "VerificationPolicyMode",
    "audit_verification_health",
]
