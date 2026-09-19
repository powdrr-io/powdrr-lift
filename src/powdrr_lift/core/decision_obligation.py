"""Typed, replayable single-decision obligations.

The model may explain a decision, but it cannot define the decision's shape or
its valid outcomes.  These immutable records are the boundary between a
compiled Procedrr worklist and evidence returned by a judge.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DecisionOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    CLARIFICATION = "clarification"


@dataclass(frozen=True, slots=True)
class DecisionSpecification:
    decision_id: str
    family: str
    subject: str
    predicate: str
    input_fingerprint: str
    predicate_version: str = "decision-v1"
    required: bool = True
    consequences: tuple[tuple[str, str], ...] = ()
    evidence_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "decision_id",
            "family",
            "subject",
            "predicate",
            "input_fingerprint",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not self.consequences:
            object.__setattr__(
                self,
                "consequences",
                (
                    (DecisionOutcome.PASS.value, "satisfy"),
                    (DecisionOutcome.FAIL.value, "block"),
                    (DecisionOutcome.UNKNOWN.value, "hold"),
                    (DecisionOutcome.CLARIFICATION.value, "ask"),
                ),
            )
        object.__setattr__(
            self,
            "evidence_requirements",
            tuple(dict.fromkeys(self.evidence_requirements)),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "family": self.family,
            "subject": self.subject,
            "predicate": self.predicate,
            "input_fingerprint": self.input_fingerprint,
            "predicate_version": self.predicate_version,
            "required": self.required,
            "consequences": {key: value for key, value in self.consequences},
            "evidence_requirements": list(self.evidence_requirements),
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> DecisionSpecification:
        consequences = raw.get("consequences", {})
        if not isinstance(consequences, Mapping):
            raise ValueError("decision consequences must be an object")
        evidence_requirements = raw.get("evidence_requirements", [])
        if not isinstance(evidence_requirements, list) or not all(
            isinstance(item, str) and item.strip() for item in evidence_requirements
        ):
            raise ValueError("decision evidence_requirements must be a list of strings")
        return cls(
            decision_id=_required_string(raw, "decision_id"),
            family=_required_string(raw, "family"),
            subject=_required_string(raw, "subject"),
            predicate=_required_string(raw, "predicate"),
            input_fingerprint=_required_string(raw, "input_fingerprint"),
            predicate_version=_required_string(raw, "predicate_version"),
            required=raw.get("required") is True,
            consequences=tuple(
                (str(key), _required_string(consequences, str(key)))
                for key in sorted(consequences)
            ),
            evidence_requirements=tuple(evidence_requirements),
        )


@dataclass(frozen=True, slots=True)
class DecisionResult:
    decision_id: str
    outcome: DecisionOutcome
    explanation: str
    predicate_version: str
    subject: str
    input_fingerprint: str
    evidence_fingerprint: str
    evidence_refs: tuple[str, ...] = ()

    def validate_against(self, specification: DecisionSpecification) -> None:
        if self.decision_id != specification.decision_id:
            raise ValueError("decision result references a different decision")
        if self.predicate_version != specification.predicate_version:
            raise ValueError("decision result uses a stale predicate version")
        if self.subject != specification.subject:
            raise ValueError("decision result references a different subject")
        if self.input_fingerprint != specification.input_fingerprint:
            raise ValueError("decision result uses stale decision inputs")
        if not self.explanation.strip():
            raise ValueError("decision result requires an explanation")
        if not self.evidence_fingerprint.strip():
            raise ValueError("decision result requires evidence identity")
        if set(self.evidence_refs) != set(specification.evidence_requirements):
            raise ValueError(
                "decision result evidence references do not match requirements"
            )
        expected_evidence_fingerprint = evidence_fingerprint(
            self.input_fingerprint, self.evidence_refs
        )
        if self.evidence_fingerprint != expected_evidence_fingerprint:
            raise ValueError(
                "decision result evidence fingerprint does not match references"
            )
        allowed = {outcome for outcome, _ in specification.consequences}
        if self.outcome.value not in allowed:
            raise ValueError(f"decision outcome is not allowed: {self.outcome.value}")

    def to_data(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "outcome": self.outcome.value,
            "explanation": self.explanation,
            "predicate_version": self.predicate_version,
            "subject": self.subject,
            "input_fingerprint": self.input_fingerprint,
            "evidence_fingerprint": self.evidence_fingerprint,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> DecisionResult:
        return cls(
            decision_id=_required_string(raw, "decision_id"),
            outcome=DecisionOutcome(_required_string(raw, "outcome")),
            explanation=_required_string(raw, "explanation"),
            predicate_version=_required_string(raw, "predicate_version"),
            subject=_required_string(raw, "subject"),
            input_fingerprint=_required_string(raw, "input_fingerprint"),
            evidence_fingerprint=_required_string(raw, "evidence_fingerprint"),
            evidence_refs=_string_tuple(raw, "evidence_refs"),
        )


@dataclass(frozen=True, slots=True)
class DecisionWorklist:
    specifications: tuple[DecisionSpecification, ...]
    fingerprint: str

    @classmethod
    def compile(
        cls, specifications: tuple[DecisionSpecification, ...]
    ) -> DecisionWorklist:
        ordered = tuple(sorted(specifications, key=lambda item: item.decision_id))
        payload = json.dumps(
            [item.to_data() for item in ordered],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(ordered, f"sha256:{hashlib.sha256(payload).hexdigest()}")

    def to_data(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "specifications": [item.to_data() for item in self.specifications],
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> DecisionWorklist:
        raw_specs = raw.get("specifications")
        if not isinstance(raw_specs, list):
            raise ValueError("decision worklist specifications must be a list")
        worklist = cls.compile(
            tuple(
                DecisionSpecification.from_data(item)
                for item in raw_specs
                if isinstance(item, Mapping)
            )
        )
        if raw.get("fingerprint") != worklist.fingerprint:
            raise ValueError("decision worklist fingerprint does not match content")
        return worklist


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _string_tuple(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(dict.fromkeys(value))


def evidence_fingerprint(input_fingerprint: str, evidence_refs: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "input_fingerprint": input_fingerprint,
            "evidence_refs": sorted(evidence_refs),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "DecisionOutcome",
    "DecisionResult",
    "DecisionSpecification",
    "DecisionWorklist",
    "evidence_fingerprint",
]
