"""Bounded per-obligation review and targeted repair contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class ObligationReviewError(ValueError):
    """Raised when review evidence or a bounded review result is invalid."""


@dataclass(frozen=True, slots=True)
class ObligationEvidencePacket:
    """The smallest evidence packet needed to review one obligation."""

    obligation_id: str
    description: str
    evidence_refs: tuple[str, ...]
    changed_paths: tuple[str, ...]
    diff_fingerprint: str

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data = {
            "obligation_id": self.obligation_id,
            "description": self.description,
            "evidence_refs": list(self.evidence_refs),
            "changed_paths": list(self.changed_paths),
            "diff_fingerprint": self.diff_fingerprint,
        }
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ObligationEvidencePacket:
        refs = raw.get("evidence_refs")
        paths = raw.get("changed_paths")
        if not isinstance(refs, list) or not isinstance(paths, list):
            raise ObligationReviewError("evidence packet references are malformed")
        packet = cls(
            obligation_id=_required_text(raw, "obligation_id"),
            description=_required_text(raw, "description"),
            evidence_refs=tuple(str(item) for item in refs),
            changed_paths=tuple(str(item) for item in paths),
            diff_fingerprint=_required_text(raw, "diff_fingerprint"),
        )
        if raw.get("fingerprint") != packet.fingerprint:
            raise ObligationReviewError("evidence packet fingerprint is stale")
        return packet


@dataclass(frozen=True, slots=True)
class ObligationReviewResult:
    """Powdrr-bound review result; the model supplies no identity fields."""

    obligation_id: str
    verdict: str
    explanation: str
    evidence_refs: tuple[str, ...]

    def validate_against(self, packet: ObligationEvidencePacket) -> None:
        if self.obligation_id != packet.obligation_id:
            raise ObligationReviewError("review result is bound to another obligation")
        if self.verdict not in {"preserved", "altered", "unknown"}:
            raise ObligationReviewError("review verdict is not allowed")
        if not self.explanation.strip():
            raise ObligationReviewError("review explanation must not be empty")
        if set(self.evidence_refs) != set(packet.evidence_refs):
            raise ObligationReviewError(
                "review evidence references do not match packet"
            )

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "verdict": self.verdict,
            "explanation": self.explanation,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class ObligationRepairPacket:
    """A repair request scoped to one failed obligation and its evidence."""

    obligation_id: str
    request: str
    evidence_refs: tuple[str, ...]
    changed_paths: tuple[str, ...]
    attempt: int = 1

    @property
    def repair_id(self) -> str:
        return f"repair:{self.obligation_id}:{self.attempt}"

    def to_data(self) -> dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "obligation_id": self.obligation_id,
            "request": self.request,
            "evidence_refs": list(self.evidence_refs),
            "changed_paths": list(self.changed_paths),
            "attempt": self.attempt,
        }


def compile_obligation_evidence_packets(
    obligations: Sequence[Mapping[str, Any]],
    *,
    evidence_refs: Sequence[str],
    changed_paths: Sequence[str],
    diff_fingerprint: str,
) -> tuple[ObligationEvidencePacket, ...]:
    """Compile one bounded evidence packet for each canonical obligation."""
    if not diff_fingerprint.strip():
        raise ObligationReviewError("diff fingerprint must not be empty")
    packets: list[ObligationEvidencePacket] = []
    for index, raw in enumerate(obligations, start=1):
        if not isinstance(raw, Mapping):
            raise ObligationReviewError(f"obligation {index} is malformed")
        obligation_id = raw.get("obligation_id") or raw.get("clause_id")
        description = raw.get("description") or raw.get("statement")
        if not isinstance(obligation_id, str) or not obligation_id.strip():
            obligation_id = f"obligation:{index:03d}"
        if not isinstance(description, str) or not description.strip():
            raise ObligationReviewError(f"obligation {obligation_id} lacks description")
        packets.append(
            ObligationEvidencePacket(
                obligation_id=obligation_id,
                description=description.strip(),
                evidence_refs=tuple(dict.fromkeys(str(item) for item in evidence_refs)),
                changed_paths=tuple(dict.fromkeys(str(item) for item in changed_paths)),
                diff_fingerprint=diff_fingerprint,
            )
        )
    if not packets:
        raise ObligationReviewError(
            "at least one obligation evidence packet is required"
        )
    return tuple(packets)


def bind_obligation_review(
    packet: ObligationEvidencePacket, response: Mapping[str, Any]
) -> ObligationReviewResult:
    """Bind semantic model fields to the compiler-selected packet identity."""
    if set(response) - {"verdict", "explanation"}:
        raise ObligationReviewError("review response contains structural fields")
    result = ObligationReviewResult(
        obligation_id=packet.obligation_id,
        verdict=_required_text(response, "verdict"),
        explanation=_required_text(response, "explanation"),
        evidence_refs=packet.evidence_refs,
    )
    result.validate_against(packet)
    return result


def compile_targeted_repair_packets(
    packets: Sequence[ObligationEvidencePacket],
    reviews: Sequence[ObligationReviewResult],
    *,
    attempt: int = 1,
) -> tuple[ObligationRepairPacket, ...]:
    """Create repairs only for failed obligations, preserving packet scope."""
    by_id = {item.obligation_id: item for item in packets}
    repairs: list[ObligationRepairPacket] = []
    for review in reviews:
        if review.verdict == "preserved":
            continue
        packet = by_id.get(review.obligation_id)
        if packet is None:
            raise ObligationReviewError(
                f"review references unknown obligation {review.obligation_id}"
            )
        repairs.append(
            ObligationRepairPacket(
                obligation_id=packet.obligation_id,
                request=(
                    f"Repair only obligation {packet.obligation_id}: "
                    f"{packet.description} Review finding: {review.explanation}"
                ),
                evidence_refs=packet.evidence_refs,
                changed_paths=packet.changed_paths,
                attempt=attempt,
            )
        )
    return tuple(repairs)


def aggregate_obligation_reviews(
    packets: Sequence[ObligationEvidencePacket],
    reviews: Sequence[ObligationReviewResult],
) -> dict[str, Any]:
    """Require exactly one current review per packet and report targeted failures."""
    expected = [item.obligation_id for item in packets]
    actual = [item.obligation_id for item in reviews]
    failures: list[str] = []
    if actual != expected:
        failures.append("obligation review results are missing or out of order")
    for review in reviews:
        if review.verdict != "preserved":
            failures.append(
                f"obligation review did not preserve {review.obligation_id}"
            )
    return {
        "passed": not failures,
        "failures": failures,
        "failed_obligation_ids": [
            review.obligation_id for review in reviews if review.verdict != "preserved"
        ],
    }


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ObligationReviewError(f"review {key} must be non-empty")
    return value.strip()


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "ObligationEvidencePacket",
    "ObligationRepairPacket",
    "ObligationReviewError",
    "ObligationReviewResult",
    "aggregate_obligation_reviews",
    "bind_obligation_review",
    "compile_obligation_evidence_packets",
    "compile_targeted_repair_packets",
]
