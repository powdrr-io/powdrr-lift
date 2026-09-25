"""Provider-neutral contracts for one bounded semantic decision.

Providers return only a result value or an unresolved reason. Powdrr owns the
decision identity, input fingerprint, provenance, and evidence binding.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SEMANTIC_DECISION_SCHEMA_VERSION = "semantic-decision-v1"
SEMANTIC_DECISION_SPEC_SCHEMA_VERSION = "semantic-decision-spec-v1"

DECISION_VALUES: dict[str, frozenset[str]] = {
    "disposition": frozenset(
        {
            "entity",
            "feature",
            "interface",
            "invariant",
            "guidance",
            "non_goal",
            "nonactionable",
        }
    ),
    "polarity": frozenset({"required", "prohibited", "permitted", "descriptive"}),
    "quantifier": frozenset({"one", "some", "every", "unspecified"}),
    "requirement_strength": frozenset(
        {"must", "should", "may", "descriptive", "unspecified"}
    ),
    "behavior_family": frozenset(
        {
            "create",
            "read",
            "update",
            "delete",
            "list",
            "search",
            "validate",
            "transform",
            "serialize",
            "deserialize",
            "round_trip",
            "persist",
            "retrieve",
            "compare",
            "invoke",
            "emit",
            "receive",
            "authorize",
            "authenticate",
            "retry",
            "render",
            "configure",
            "other",
        }
    ),
    "has_precondition": frozenset({"present", "absent"}),
    "has_exception": frozenset({"present", "absent"}),
    "has_explicit_result": frozenset({"present", "absent"}),
    "temporal_scope": frozenset(
        {"current", "future", "current_and_future", "event_bound", "unspecified"}
    ),
    "source_predicate": frozenset(
        {"explicit", "implied_by_registered_term", "not_stated"}
    ),
    "candidate_relation": frozenset(
        {"matches", "does_not_match", "insufficient_evidence"}
    ),
    "entailment": frozenset({"entailed", "contradicted", "not_stated"}),
    "proposition_coverage": frozenset(
        {
            "fully_represented",
            "partly_represented",
            "not_represented",
            "invented",
        }
    ),
    "nonactionable_exclusion_safety": frozenset(
        {"process_only", "product_semantics_present", "mixed"}
    ),
    "contract_observation_relation": frozenset(
        {
            "same_observation",
            "distinct_observations",
            "ordered_phases",
            "precedence",
            "mutual_exclusion",
            "preservation_boundary",
            "independent",
        }
    ),
}

UNRESOLVED_REASON_CODES = frozenset(
    {
        "source_ambiguous",
        "source_underspecified",
        "no_candidate",
        "multiple_candidates",
        "repository_evidence_missing",
        "unsupported_concept",
        "classifier_abstained",
        "invalid_response",
        "conflicting_evidence",
    }
)

PROVIDER_KINDS = frozenset(
    {"planning-llm", "local-classifier", "deterministic-rule", "human"}
)


class SemanticDecisionError(ValueError):
    """Raised when a decision specification or bound result is invalid."""


@dataclass(frozen=True, slots=True)
class ExactSourceSpan:
    """An exact, compiler-resolved span in one immutable proposition."""

    source_ref: str
    start: int
    end: int
    text: str

    def __post_init__(self) -> None:
        if not self.source_ref.strip():
            raise SemanticDecisionError("source_ref must not be empty")
        if self.start < 0 or self.end <= self.start:
            raise SemanticDecisionError("source span must be non-empty")
        if not self.text:
            raise SemanticDecisionError("source span text must not be empty")

    @property
    def evidence_ref(self) -> str:
        return f"source-span:{self.source_ref}:{self.start}-{self.end}"

    def to_data(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ExactSourceSpan:
        _require_exact_keys(raw, {"source_ref", "start", "end", "text"})
        start = raw.get("start")
        end = raw.get("end")
        if not isinstance(start, int) or isinstance(start, bool):
            raise SemanticDecisionError("source span start must be an integer")
        if not isinstance(end, int) or isinstance(end, bool):
            raise SemanticDecisionError("source span end must be an integer")
        return cls(
            source_ref=_required_string(raw, "source_ref"),
            start=start,
            end=end,
            text=_required_string(raw, "text"),
        )


def resolve_exact_source_span(
    *,
    source_ref: str,
    source: str,
    quote: str,
    occurrence: int | None = None,
) -> ExactSourceSpan:
    """Resolve a provider quotation without normalization or fuzzy matching."""
    if not quote:
        raise SemanticDecisionError("source quote must not be empty")
    starts: list[int] = []
    offset = 0
    while True:
        start = source.find(quote, offset)
        if start < 0:
            break
        starts.append(start)
        offset = start + 1
    if not starts:
        raise SemanticDecisionError("quote is not an exact source substring")
    if occurrence is None:
        if len(starts) != 1:
            raise SemanticDecisionError(
                "occurrence is required for a repeated source quote"
            )
        selected = 1
    else:
        selected = occurrence
    if selected < 1 or selected > len(starts):
        raise SemanticDecisionError("occurrence is outside the exact match set")
    start = starts[selected - 1]
    return ExactSourceSpan(
        source_ref=source_ref,
        start=start,
        end=start + len(quote),
        text=quote,
    )


@dataclass(frozen=True, slots=True)
class SemanticDecisionProvider:
    kind: str
    name: str | None = None
    model_revision: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in PROVIDER_KINDS:
            raise SemanticDecisionError("semantic decision provider kind is invalid")
        if self.name is not None and not self.name.strip():
            raise SemanticDecisionError("provider name must be non-empty when present")
        if self.model_revision is not None and not self.model_revision.strip():
            raise SemanticDecisionError(
                "provider model revision must be non-empty when present"
            )

    def to_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "model_revision": self.model_revision,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SemanticDecisionProvider:
        _require_exact_keys(raw, {"kind", "name", "model_revision"})
        return cls(
            kind=_required_string(raw, "kind"),
            name=_optional_string(raw, "name"),
            model_revision=_optional_string(raw, "model_revision"),
        )


@dataclass(frozen=True, slots=True)
class SemanticDecisionResult:
    status: str
    value: str | None = None
    reason_code: str | None = None

    def validate(self, decision_kind: str) -> None:
        allowed_values = DECISION_VALUES.get(decision_kind)
        if allowed_values is None:
            raise SemanticDecisionError(f"unsupported decision kind {decision_kind!r}")
        if self.status == "resolved":
            if self.value not in allowed_values:
                raise SemanticDecisionError(
                    f"value is not allowed for decision kind {decision_kind}"
                )
            if self.reason_code is not None:
                raise SemanticDecisionError(
                    "a resolved semantic decision cannot have a reason code"
                )
            return
        if self.status != "unresolved":
            raise SemanticDecisionError("semantic decision status is invalid")
        if self.value is not None:
            raise SemanticDecisionError(
                "an unresolved semantic decision cannot have a value"
            )
        if self.reason_code not in UNRESOLVED_REASON_CODES:
            raise SemanticDecisionError("unresolved reason code is invalid")

    def to_data(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "status": self.status,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_provider_data(
        cls, decision_kind: str, raw: Mapping[str, Any]
    ) -> SemanticDecisionResult:
        allowed_keys = {"status", "value", "reason_code"}
        if set(raw) - allowed_keys:
            raise SemanticDecisionError(
                "semantic provider result contains compiler-owned fields"
            )
        result = cls(
            status=_required_string(raw, "status"),
            value=_optional_string(raw, "value"),
            reason_code=_optional_string(raw, "reason_code"),
        )
        result.validate(decision_kind)
        return result


@dataclass(frozen=True, slots=True)
class SemanticDecisionSpec:
    """Compiler-owned identity and exact input for one classifier activation."""

    decision_id: str
    decision_kind: str
    subject_ref: str
    proposition_text: str
    source_fingerprint: str
    contract_revision: str
    candidate_set_fingerprint: str | None = None
    accepted_definition_revision: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_id", self.decision_id),
            ("subject_ref", self.subject_ref),
            ("proposition_text", self.proposition_text),
            ("source_fingerprint", self.source_fingerprint),
            ("contract_revision", self.contract_revision),
        ):
            if not value.strip():
                raise SemanticDecisionError(f"{name} must not be empty")
        if self.decision_kind not in DECISION_VALUES:
            raise SemanticDecisionError("semantic decision kind is invalid")
        for optional_name, optional_value in (
            ("candidate_set_fingerprint", self.candidate_set_fingerprint),
            ("accepted_definition_revision", self.accepted_definition_revision),
        ):
            if optional_value is not None and not optional_value.strip():
                raise SemanticDecisionError(
                    f"{optional_name} must be non-empty when present"
                )

    @property
    def input_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema_version": SEMANTIC_DECISION_SPEC_SCHEMA_VERSION,
                "decision_kind": self.decision_kind,
                "proposition_text": self.proposition_text,
                "source_fingerprint": self.source_fingerprint,
                "candidate_set_fingerprint": self.candidate_set_fingerprint,
                "accepted_definition_revision": self.accepted_definition_revision,
                "contract_revision": self.contract_revision,
            }
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": SEMANTIC_DECISION_SPEC_SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "decision_kind": self.decision_kind,
            "subject_ref": self.subject_ref,
            "proposition_text": self.proposition_text,
            "source_fingerprint": self.source_fingerprint,
            "candidate_set_fingerprint": self.candidate_set_fingerprint,
            "accepted_definition_revision": self.accepted_definition_revision,
            "contract_revision": self.contract_revision,
            "input_fingerprint": self.input_fingerprint,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SemanticDecisionSpec:
        _require_exact_keys(
            raw,
            {
                "schema_version",
                "decision_id",
                "decision_kind",
                "subject_ref",
                "proposition_text",
                "source_fingerprint",
                "candidate_set_fingerprint",
                "accepted_definition_revision",
                "contract_revision",
                "input_fingerprint",
            },
        )
        if raw.get("schema_version") != SEMANTIC_DECISION_SPEC_SCHEMA_VERSION:
            raise SemanticDecisionError("unsupported semantic decision spec schema")
        spec = cls(
            decision_id=_required_string(raw, "decision_id"),
            decision_kind=_required_string(raw, "decision_kind"),
            subject_ref=_required_string(raw, "subject_ref"),
            proposition_text=_required_string(raw, "proposition_text"),
            source_fingerprint=_required_string(raw, "source_fingerprint"),
            candidate_set_fingerprint=_optional_string(
                raw, "candidate_set_fingerprint"
            ),
            accepted_definition_revision=_optional_string(
                raw, "accepted_definition_revision"
            ),
            contract_revision=_required_string(raw, "contract_revision"),
        )
        if raw.get("input_fingerprint") != spec.input_fingerprint:
            raise SemanticDecisionError("semantic decision spec fingerprint is stale")
        return spec

    def bind(
        self,
        *,
        provider: SemanticDecisionProvider,
        provider_result: Mapping[str, Any],
        evidence_refs: Sequence[str],
        created_at: str,
    ) -> SemanticDecision:
        if not created_at.strip():
            raise SemanticDecisionError("created_at must not be empty")
        evidence = tuple(dict.fromkeys(evidence_refs))
        if not evidence or any(not item.strip() for item in evidence):
            raise SemanticDecisionError(
                "a semantic decision requires non-empty evidence references"
            )
        result = SemanticDecisionResult.from_provider_data(
            self.decision_kind, provider_result
        )
        return SemanticDecision(
            decision_id=self.decision_id,
            decision_kind=self.decision_kind,
            subject_ref=self.subject_ref,
            input_fingerprint=self.input_fingerprint,
            provider=provider,
            result=result,
            evidence_refs=evidence,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    decision_id: str
    decision_kind: str
    subject_ref: str
    input_fingerprint: str
    provider: SemanticDecisionProvider
    result: SemanticDecisionResult
    evidence_refs: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_id", self.decision_id),
            ("subject_ref", self.subject_ref),
            ("input_fingerprint", self.input_fingerprint),
            ("created_at", self.created_at),
        ):
            if not value.strip():
                raise SemanticDecisionError(f"{name} must not be empty")
        if not self.evidence_refs or any(
            not item.strip() for item in self.evidence_refs
        ):
            raise SemanticDecisionError(
                "a semantic decision requires non-empty evidence references"
            )
        self.result.validate(self.decision_kind)

    def validate_current(self, spec: SemanticDecisionSpec) -> None:
        if self.decision_id != spec.decision_id:
            raise SemanticDecisionError("semantic decision identity is stale")
        if self.decision_kind != spec.decision_kind:
            raise SemanticDecisionError("semantic decision kind is stale")
        if self.subject_ref != spec.subject_ref:
            raise SemanticDecisionError("semantic decision subject is stale")
        if self.input_fingerprint != spec.input_fingerprint:
            raise SemanticDecisionError("semantic decision input fingerprint is stale")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": SEMANTIC_DECISION_SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "decision_kind": self.decision_kind,
            "subject_ref": self.subject_ref,
            "input_fingerprint": self.input_fingerprint,
            "provider": self.provider.to_data(),
            "result": self.result.to_data(),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SemanticDecision:
        _require_exact_keys(
            raw,
            {
                "schema_version",
                "decision_id",
                "decision_kind",
                "subject_ref",
                "input_fingerprint",
                "provider",
                "result",
                "evidence_refs",
                "created_at",
            },
        )
        if raw.get("schema_version") != SEMANTIC_DECISION_SCHEMA_VERSION:
            raise SemanticDecisionError("unsupported semantic decision schema")
        provider_raw = raw.get("provider")
        result_raw = raw.get("result")
        evidence_raw = raw.get("evidence_refs")
        if not isinstance(provider_raw, Mapping) or not isinstance(result_raw, Mapping):
            raise SemanticDecisionError("semantic decision is incomplete")
        _require_exact_keys(result_raw, {"value", "status", "reason_code"})
        if not isinstance(evidence_raw, list) or not all(
            isinstance(item, str) and item.strip() for item in evidence_raw
        ):
            raise SemanticDecisionError("semantic decision evidence is malformed")
        decision_kind = _required_string(raw, "decision_kind")
        result = SemanticDecisionResult(
            status=_required_string(result_raw, "status"),
            value=_optional_string(result_raw, "value"),
            reason_code=_optional_string(result_raw, "reason_code"),
        )
        return cls(
            decision_id=_required_string(raw, "decision_id"),
            decision_kind=decision_kind,
            subject_ref=_required_string(raw, "subject_ref"),
            input_fingerprint=_required_string(raw, "input_fingerprint"),
            provider=SemanticDecisionProvider.from_data(provider_raw),
            result=result,
            evidence_refs=tuple(evidence_raw),
            created_at=_required_string(raw, "created_at"),
        )


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SemanticDecisionError(f"{key} must be a non-empty string")
    return value


def _require_exact_keys(raw: Mapping[str, Any], expected: set[str]) -> None:
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SemanticDecisionError(
            f"semantic artifact fields are invalid; missing={missing}, extra={extra}"
        )


def _optional_string(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SemanticDecisionError(f"{key} must be a non-empty string or null")
    return value


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "DECISION_VALUES",
    "PROVIDER_KINDS",
    "SEMANTIC_DECISION_SCHEMA_VERSION",
    "SEMANTIC_DECISION_SPEC_SCHEMA_VERSION",
    "UNRESOLVED_REASON_CODES",
    "ExactSourceSpan",
    "SemanticDecision",
    "SemanticDecisionError",
    "SemanticDecisionProvider",
    "SemanticDecisionResult",
    "SemanticDecisionSpec",
    "resolve_exact_source_span",
]
