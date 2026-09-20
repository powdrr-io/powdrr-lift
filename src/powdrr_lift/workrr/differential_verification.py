"""Provider-neutral base/candidate verification comparison."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DifferentialStatus(StrEnum):
    PRESERVED_PASS = "preserved_pass"
    FIXED_EXISTING_FAILURE = "fixed_existing_failure"
    NEW_REGRESSION = "new_regression"
    PERSISTENT_FAILURE = "persistent_failure"
    NOT_COMPARABLE = "not_comparable"


class VerifierChangeKind(StrEnum):
    CONTRACT = "contract"
    SELECTOR = "selector"
    VERIFIER = "verifier"
    INVENTORY = "inventory"


@dataclass(frozen=True, slots=True)
class DifferentialResult:
    obligation_id: str
    status: DifferentialStatus
    base_status: str
    candidate_status: str
    reason: str

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "status": self.status.value,
            "base_status": self.base_status,
            "candidate_status": self.candidate_status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class VerifierChange:
    obligation_id: str
    kind: VerifierChangeKind
    before: str | None
    after: str | None

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "kind": self.kind.value,
            "before": self.before,
            "after": self.after,
        }


def classify_differential_result(
    obligation_id: str,
    base: MappingLike,
    candidate: MappingLike,
    *,
    comparable: bool = True,
) -> DifferentialResult:
    """Classify a candidate outcome against the same obligation on its base."""
    base_status = _status(base)
    candidate_status = _status(candidate)
    if not comparable:
        status = DifferentialStatus.NOT_COMPARABLE
        reason = "base and candidate verifier executions are not comparable"
    elif base_status == "passed" and candidate_status == "passed":
        status = DifferentialStatus.PRESERVED_PASS
        reason = "contract passed on base and candidate"
    elif base_status != "passed" and candidate_status == "passed":
        status = DifferentialStatus.FIXED_EXISTING_FAILURE
        reason = "candidate fixed a failure present on the base"
    elif base_status == "passed" and candidate_status != "passed":
        status = DifferentialStatus.NEW_REGRESSION
        reason = "candidate regressed a contract that passed on the base"
    else:
        status = DifferentialStatus.PERSISTENT_FAILURE
        reason = "contract failed on both base and candidate"
    return DifferentialResult(
        obligation_id=obligation_id,
        status=status,
        base_status=base_status,
        candidate_status=candidate_status,
        reason=reason,
    )


def detect_verifier_changes(
    base: MappingLike, candidate: MappingLike
) -> tuple[VerifierChange, ...]:
    """Return identity changes that require an explicit verifier decision."""
    changes: list[VerifierChange] = []
    obligation_id = str(candidate.get("obligation_id") or base.get("obligation_id"))
    for kind, field in (
        (VerifierChangeKind.CONTRACT, "contract_fingerprint"),
        (VerifierChangeKind.SELECTOR, "selector"),
        (VerifierChangeKind.VERIFIER, "verifier_fingerprint"),
        (VerifierChangeKind.INVENTORY, "provider_inventory_fingerprint"),
    ):
        before = _optional_text(base.get(field))
        after = _optional_text(candidate.get(field))
        if before != after:
            changes.append(VerifierChange(obligation_id, kind, before, after))
    return tuple(changes)


def _status(value: MappingLike) -> str:
    return str(value.get("status") or "missing")


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


MappingLike = Mapping[str, Any]

__all__ = [
    "DifferentialResult",
    "DifferentialStatus",
    "VerifierChange",
    "VerifierChangeKind",
    "classify_differential_result",
    "detect_verifier_changes",
]
