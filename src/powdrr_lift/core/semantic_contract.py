"""Source-anchored partial semantic contracts and exact extraction records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_lift.core.semantic_decision import (
    ExactSourceSpan,
    SemanticDecision,
    SemanticDecisionError,
    SemanticDecisionProvider,
    resolve_exact_source_span,
)

PARTIAL_SEMANTIC_CONTRACT_SCHEMA_VERSION = "partial-semantic-contract-v1"
SOURCE_EXTRACTION_SCHEMA_VERSION = "source-extraction-v1"
SOURCE_EXTRACTION_SPEC_SCHEMA_VERSION = "source-extraction-spec-v1"

SOURCE_EXTRACTION_KINDS = frozenset(
    {"subject", "behavior", "precondition", "exception", "explicit_result"}
)


class SemanticContractError(ValueError):
    """Raised when source semantics cannot be compiled without invention."""


@dataclass(frozen=True, slots=True)
class SourceExtractionSpec:
    """Compiler-owned request for one exact quotation from a proposition."""

    extraction_id: str
    extraction_kind: str
    subject_ref: str
    proposition_text: str
    source_fingerprint: str
    contract_revision: str

    def __post_init__(self) -> None:
        for name, value in (
            ("extraction_id", self.extraction_id),
            ("subject_ref", self.subject_ref),
            ("proposition_text", self.proposition_text),
            ("source_fingerprint", self.source_fingerprint),
            ("contract_revision", self.contract_revision),
        ):
            if not value.strip():
                raise SemanticContractError(f"{name} must not be empty")
        if self.extraction_kind not in SOURCE_EXTRACTION_KINDS:
            raise SemanticContractError("source extraction kind is invalid")

    @property
    def input_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema_version": SOURCE_EXTRACTION_SPEC_SCHEMA_VERSION,
                "extraction_kind": self.extraction_kind,
                "proposition_text": self.proposition_text,
                "source_fingerprint": self.source_fingerprint,
                "contract_revision": self.contract_revision,
            }
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_EXTRACTION_SPEC_SCHEMA_VERSION,
            "extraction_id": self.extraction_id,
            "extraction_kind": self.extraction_kind,
            "subject_ref": self.subject_ref,
            "proposition_text": self.proposition_text,
            "source_fingerprint": self.source_fingerprint,
            "contract_revision": self.contract_revision,
            "input_fingerprint": self.input_fingerprint,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SourceExtractionSpec:
        expected = {
            "schema_version",
            "extraction_id",
            "extraction_kind",
            "subject_ref",
            "proposition_text",
            "source_fingerprint",
            "contract_revision",
            "input_fingerprint",
        }
        _require_exact_keys(raw, expected)
        if raw.get("schema_version") != SOURCE_EXTRACTION_SPEC_SCHEMA_VERSION:
            raise SemanticContractError("unsupported source extraction spec schema")
        spec = cls(
            extraction_id=_required_string(raw, "extraction_id"),
            extraction_kind=_required_string(raw, "extraction_kind"),
            subject_ref=_required_string(raw, "subject_ref"),
            proposition_text=_required_string(raw, "proposition_text"),
            source_fingerprint=_required_string(raw, "source_fingerprint"),
            contract_revision=_required_string(raw, "contract_revision"),
        )
        if raw.get("input_fingerprint") != spec.input_fingerprint:
            raise SemanticContractError("source extraction spec fingerprint is stale")
        return spec

    def bind(
        self,
        *,
        provider: SemanticDecisionProvider,
        provider_result: Mapping[str, Any],
        created_at: str,
    ) -> BoundSourceExtraction:
        if set(provider_result) - {"quote", "occurrence"}:
            raise SemanticContractError(
                "source extractor result contains compiler-owned fields"
            )
        quote = _required_string(provider_result, "quote")
        occurrence_raw = provider_result.get("occurrence")
        if occurrence_raw is None:
            occurrence = None
        elif isinstance(occurrence_raw, int) and not isinstance(occurrence_raw, bool):
            occurrence = occurrence_raw
        else:
            raise SemanticContractError("source quote occurrence must be an integer")
        try:
            span = resolve_exact_source_span(
                source_ref=self.subject_ref,
                source=self.proposition_text,
                quote=quote,
                occurrence=occurrence,
            )
        except SemanticDecisionError as exc:
            raise SemanticContractError(str(exc)) from exc
        if self.extraction_kind == "subject" and quote.casefold().strip() in {
            "a",
            "an",
            "the",
            "all",
            "every",
            "some",
        }:
            raise SemanticContractError(
                "subject extraction cannot contain only a determiner or quantifier"
            )
        if not created_at.strip():
            raise SemanticContractError("created_at must not be empty")
        return BoundSourceExtraction(
            extraction_id=self.extraction_id,
            extraction_kind=self.extraction_kind,
            subject_ref=self.subject_ref,
            input_fingerprint=self.input_fingerprint,
            provider=provider,
            span=span,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class BoundSourceExtraction:
    extraction_id: str
    extraction_kind: str
    subject_ref: str
    input_fingerprint: str
    provider: SemanticDecisionProvider
    span: ExactSourceSpan
    created_at: str

    def __post_init__(self) -> None:
        for name, value in (
            ("extraction_id", self.extraction_id),
            ("subject_ref", self.subject_ref),
            ("input_fingerprint", self.input_fingerprint),
            ("created_at", self.created_at),
        ):
            if not value.strip():
                raise SemanticContractError(f"{name} must not be empty")
        if self.extraction_kind not in SOURCE_EXTRACTION_KINDS:
            raise SemanticContractError("source extraction kind is invalid")
        if self.span.source_ref != self.subject_ref:
            raise SemanticContractError(
                "source extraction span references another subject"
            )

    def validate_current(self, spec: SourceExtractionSpec) -> None:
        if (
            self.extraction_id != spec.extraction_id
            or self.extraction_kind != spec.extraction_kind
            or self.subject_ref != spec.subject_ref
        ):
            raise SemanticContractError("source extraction identity is stale")
        if self.input_fingerprint != spec.input_fingerprint:
            raise SemanticContractError("source extraction input fingerprint is stale")
        if spec.proposition_text[self.span.start : self.span.end] != self.span.text:
            raise SemanticContractError("source extraction span is stale")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_EXTRACTION_SCHEMA_VERSION,
            "extraction_id": self.extraction_id,
            "extraction_kind": self.extraction_kind,
            "subject_ref": self.subject_ref,
            "input_fingerprint": self.input_fingerprint,
            "provider": self.provider.to_data(),
            "span": self.span.to_data(),
            "evidence_refs": [self.span.evidence_ref],
            "created_at": self.created_at,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> BoundSourceExtraction:
        expected = {
            "schema_version",
            "extraction_id",
            "extraction_kind",
            "subject_ref",
            "input_fingerprint",
            "provider",
            "span",
            "evidence_refs",
            "created_at",
        }
        _require_exact_keys(raw, expected)
        if raw.get("schema_version") != SOURCE_EXTRACTION_SCHEMA_VERSION:
            raise SemanticContractError("unsupported source extraction schema")
        provider_raw = raw.get("provider")
        span_raw = raw.get("span")
        if not isinstance(provider_raw, Mapping) or not isinstance(span_raw, Mapping):
            raise SemanticContractError("source extraction is incomplete")
        span = ExactSourceSpan.from_data(span_raw)
        evidence_refs = raw.get("evidence_refs")
        if evidence_refs != [span.evidence_ref]:
            raise SemanticContractError("source extraction evidence is stale")
        return cls(
            extraction_id=_required_string(raw, "extraction_id"),
            extraction_kind=_required_string(raw, "extraction_kind"),
            subject_ref=_required_string(raw, "subject_ref"),
            input_fingerprint=_required_string(raw, "input_fingerprint"),
            provider=SemanticDecisionProvider.from_data(provider_raw),
            span=span,
            created_at=_required_string(raw, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class UnresolvedSemanticField:
    field: str
    reason_code: str

    def to_data(self) -> dict[str, str]:
        return {"field": self.field, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class PartialSemanticContract:
    """Canonical source-only meaning before repository and ontology binding."""

    contract_id: str
    source_ref: str
    source_fingerprint: str
    proposition_text: str
    disposition: str
    polarity: str
    requirement_strength: str
    quantifier: str
    subject: BoundSourceExtraction
    behavior: BoundSourceExtraction
    behavior_family: str
    preconditions: tuple[BoundSourceExtraction, ...]
    exceptions: tuple[BoundSourceExtraction, ...]
    explicit_result: BoundSourceExtraction | None
    temporal_scope: str
    source_predicate: str
    field_provenance: tuple[tuple[str, str], ...]
    unresolved: tuple[UnresolvedSemanticField, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("contract_id", self.contract_id),
            ("source_ref", self.source_ref),
            ("source_fingerprint", self.source_fingerprint),
            ("proposition_text", self.proposition_text),
        ):
            if not value.strip():
                raise SemanticContractError(f"{name} must not be empty")
        if self.subject.extraction_kind != "subject":
            raise SemanticContractError("partial contract subject has the wrong kind")
        if self.behavior.extraction_kind != "behavior":
            raise SemanticContractError("partial contract behavior has the wrong kind")
        if any(item.extraction_kind != "precondition" for item in self.preconditions):
            raise SemanticContractError(
                "partial contract precondition has the wrong kind"
            )
        if any(item.extraction_kind != "exception" for item in self.exceptions):
            raise SemanticContractError("partial contract exception has the wrong kind")
        if (
            self.explicit_result is not None
            and self.explicit_result.extraction_kind != "explicit_result"
        ):
            raise SemanticContractError("partial contract result has the wrong kind")
        if any(
            item.subject_ref != self.source_ref
            for item in (
                self.subject,
                self.behavior,
                *self.preconditions,
                *self.exceptions,
                *((self.explicit_result,) if self.explicit_result else ()),
            )
        ):
            raise SemanticContractError(
                "partial contract contains foreign source spans"
            )

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": PARTIAL_SEMANTIC_CONTRACT_SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "source_ref": self.source_ref,
            "source_fingerprint": self.source_fingerprint,
            "proposition_text": self.proposition_text,
            "disposition": self.disposition,
            "polarity": self.polarity,
            "requirement_strength": self.requirement_strength,
            "quantifier": self.quantifier,
            "subject": {
                "source_span": self.subject.span.to_data(),
                "source_text": self.subject.span.text,
                "binding_status": "unresolved",
                "binding_refs": [],
            },
            "behavior": {
                "source_span": self.behavior.span.to_data(),
                "source_text": self.behavior.span.text,
                "family": self.behavior_family,
                "ontology_status": "unresolved",
                "ontology_ref": None,
            },
            "preconditions": [item.span.to_data() for item in self.preconditions],
            "exceptions": [item.span.to_data() for item in self.exceptions],
            "explicit_result": (
                self.explicit_result.span.to_data() if self.explicit_result else None
            ),
            "temporal_scope": {"value": self.temporal_scope, "derivation": "source"},
            "predicate": {
                "status": "unresolved",
                "source_classification": self.source_predicate,
                "ontology_ref": None,
            },
            "field_provenance": dict(self.field_provenance),
            "unresolved": [item.to_data() for item in self.unresolved],
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> PartialSemanticContract:
        """Restore the persisted source-only view for downstream compiler gates."""
        if raw.get("schema_version") != PARTIAL_SEMANTIC_CONTRACT_SCHEMA_VERSION:
            raise SemanticContractError("unsupported partial contract schema")

        def extraction(
            kind: str, span_raw: Mapping[str, Any], index: int = 0
        ) -> BoundSourceExtraction:
            return BoundSourceExtraction(
                extraction_id=f"persisted:{raw.get('source_ref')}:{kind}:{index}",
                extraction_kind=kind,
                subject_ref=_required_string(raw, "source_ref"),
                input_fingerprint="persisted-contract",
                provider=SemanticDecisionProvider(kind="deterministic-rule"),
                span=ExactSourceSpan.from_data(span_raw),
                created_at="persisted",
            )

        subject_raw = raw.get("subject")
        behavior_raw = raw.get("behavior")
        if not isinstance(subject_raw, Mapping) or not isinstance(
            behavior_raw, Mapping
        ):
            raise SemanticContractError("partial contract source fields are incomplete")
        unresolved_raw = raw.get("unresolved")
        provenance_raw = raw.get("field_provenance")
        if not isinstance(unresolved_raw, list) or not isinstance(
            provenance_raw, Mapping
        ):
            raise SemanticContractError(
                "partial contract completion metadata is invalid"
            )
        explicit_raw = raw.get("explicit_result")
        precondition_raw = raw.get("preconditions")
        exception_raw = raw.get("exceptions")
        if not isinstance(precondition_raw, list) or not isinstance(
            exception_raw, list
        ):
            raise SemanticContractError("partial contract modifiers are invalid")
        temporal_raw = raw.get("temporal_scope")
        if not isinstance(temporal_raw, Mapping):
            raise SemanticContractError("partial contract temporal scope is invalid")
        return cls(
            contract_id=_required_string(raw, "contract_id"),
            source_ref=_required_string(raw, "source_ref"),
            source_fingerprint=_required_string(raw, "source_fingerprint"),
            proposition_text=_required_string(raw, "proposition_text"),
            disposition=_required_string(raw, "disposition"),
            polarity=_required_string(raw, "polarity"),
            requirement_strength=_required_string(raw, "requirement_strength"),
            quantifier=_required_string(raw, "quantifier"),
            subject=extraction("subject", subject_raw.get("source_span", {})),
            behavior=extraction("behavior", behavior_raw.get("source_span", {})),
            behavior_family=_required_string(behavior_raw, "family"),
            preconditions=tuple(
                extraction("precondition", item, index)
                for index, item in enumerate(precondition_raw)
                if isinstance(item, Mapping)
            ),
            exceptions=tuple(
                extraction("exception", item, index)
                for index, item in enumerate(exception_raw)
                if isinstance(item, Mapping)
            ),
            explicit_result=(
                extraction("explicit_result", explicit_raw)
                if isinstance(explicit_raw, Mapping)
                else None
            ),
            temporal_scope=_required_string(temporal_raw, "value"),
            source_predicate=_required_string(
                raw.get("predicate", {}), "source_classification"
            )
            if isinstance(raw.get("predicate"), Mapping)
            else "not_stated",
            field_provenance=tuple(
                (str(key), str(value)) for key, value in provenance_raw.items()
            ),
            unresolved=tuple(
                UnresolvedSemanticField(
                    field=_required_string(item, "field"),
                    reason_code=_required_string(item, "reason_code"),
                )
                for item in unresolved_raw
                if isinstance(item, Mapping)
            ),
        )


def compile_partial_semantic_contract(
    *,
    source_ref: str,
    source_fingerprint: str,
    proposition_text: str,
    decisions: Sequence[SemanticDecision],
    extractions: Sequence[BoundSourceExtraction],
) -> PartialSemanticContract:
    """Compile bound single-field results without interpreting their prose."""
    decision_by_kind = _unique_by_kind(decisions)
    extraction_by_kind = _extractions_by_kind(extractions)
    required_decisions = {
        "disposition",
        "polarity",
        "quantifier",
        "requirement_strength",
        "behavior_family",
        "has_precondition",
        "has_exception",
        "has_explicit_result",
        "temporal_scope",
        "source_predicate",
        "nonactionable_exclusion_safety",
    }
    missing = required_decisions - set(decision_by_kind)
    if missing:
        raise SemanticContractError(
            f"partial contract is missing semantic decisions: {sorted(missing)}"
        )
    unresolved_decisions = [
        kind
        for kind, decision in decision_by_kind.items()
        if kind in required_decisions and decision.result.status != "resolved"
    ]
    if unresolved_decisions:
        raise SemanticContractError(
            "partial contract cannot compile unresolved source decisions: "
            f"{sorted(unresolved_decisions)}"
        )
    disposition = _decision_value(decision_by_kind, "disposition")
    exclusion_safety = _decision_value(
        decision_by_kind, "nonactionable_exclusion_safety"
    )
    if disposition == "nonactionable" and exclusion_safety != "process_only":
        raise SemanticContractError(
            "nonactionable disposition lacks independent process-only confirmation"
        )
    if disposition != "nonactionable" and exclusion_safety == "process_only":
        raise SemanticContractError(
            "actionable disposition conflicts with process-only confirmation"
        )
    subject = _one_extraction(extraction_by_kind, "subject")
    behavior = _one_extraction(extraction_by_kind, "behavior")
    preconditions = tuple(extraction_by_kind.get("precondition", ()))
    exceptions = tuple(extraction_by_kind.get("exception", ()))
    explicit_results = tuple(extraction_by_kind.get("explicit_result", ()))
    _validate_modifier_presence(
        decision_by_kind,
        "has_precondition",
        "precondition",
        preconditions,
    )
    _validate_modifier_presence(
        decision_by_kind,
        "has_exception",
        "exception",
        exceptions,
    )
    _validate_modifier_presence(
        decision_by_kind,
        "has_explicit_result",
        "explicit_result",
        explicit_results,
    )
    if len(explicit_results) > 1:
        raise SemanticContractError("partial contract has multiple explicit results")
    unresolved = [
        UnresolvedSemanticField("subject.binding_refs", "repository_evidence_missing"),
        UnresolvedSemanticField("behavior.ontology_ref", "no_candidate"),
        UnresolvedSemanticField("predicate", "source_underspecified"),
    ]
    return PartialSemanticContract(
        contract_id=f"contract:{source_ref}",
        source_ref=source_ref,
        source_fingerprint=source_fingerprint,
        proposition_text=proposition_text,
        disposition=disposition,
        polarity=_decision_value(decision_by_kind, "polarity"),
        requirement_strength=_decision_value(decision_by_kind, "requirement_strength"),
        quantifier=_decision_value(decision_by_kind, "quantifier"),
        subject=subject,
        behavior=behavior,
        behavior_family=_decision_value(decision_by_kind, "behavior_family"),
        preconditions=preconditions,
        exceptions=exceptions,
        explicit_result=explicit_results[0] if explicit_results else None,
        temporal_scope=_decision_value(decision_by_kind, "temporal_scope"),
        source_predicate=_decision_value(decision_by_kind, "source_predicate"),
        field_provenance=tuple(
            (kind, decision.decision_id)
            for kind, decision in sorted(decision_by_kind.items())
            if kind in required_decisions
        )
        + tuple(
            (kind, item.extraction_id)
            for kind, items in sorted(extraction_by_kind.items())
            for item in items
        ),
        unresolved=tuple(unresolved),
    )


def _decision_value(decisions: Mapping[str, SemanticDecision], kind: str) -> str:
    value = decisions[kind].result.value
    if value is None:
        raise SemanticContractError(f"semantic decision {kind} is unresolved")
    return value


def _unique_by_kind(
    decisions: Sequence[SemanticDecision],
) -> dict[str, SemanticDecision]:
    result: dict[str, SemanticDecision] = {}
    for decision in decisions:
        if decision.decision_kind in result:
            raise SemanticContractError(
                f"duplicate semantic decision {decision.decision_kind}"
            )
        result[decision.decision_kind] = decision
    return result


def _extractions_by_kind(
    extractions: Sequence[BoundSourceExtraction],
) -> dict[str, list[BoundSourceExtraction]]:
    result: dict[str, list[BoundSourceExtraction]] = {}
    for extraction in extractions:
        result.setdefault(extraction.extraction_kind, []).append(extraction)
    return result


def _one_extraction(
    extractions: Mapping[str, Sequence[BoundSourceExtraction]], kind: str
) -> BoundSourceExtraction:
    candidates = tuple(extractions.get(kind, ()))
    if len(candidates) != 1:
        raise SemanticContractError(
            f"partial contract requires exactly one {kind} extraction"
        )
    return candidates[0]


def _validate_modifier_presence(
    decisions: Mapping[str, SemanticDecision],
    decision_kind: str,
    extraction_kind: str,
    extractions: Sequence[BoundSourceExtraction],
) -> None:
    expected = _decision_value(decisions, decision_kind)
    if expected == "present" and not extractions:
        raise SemanticContractError(
            f"{decision_kind} is present but no {extraction_kind} span was extracted"
        )
    if expected == "absent" and extractions:
        raise SemanticContractError(
            f"{decision_kind} is absent but a {extraction_kind} span was extracted"
        )


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SemanticContractError(f"{key} must be a non-empty string")
    return value


def _require_exact_keys(raw: Mapping[str, Any], expected: set[str]) -> None:
    if set(raw) != expected:
        raise SemanticContractError("semantic artifact fields are invalid")


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "PARTIAL_SEMANTIC_CONTRACT_SCHEMA_VERSION",
    "SOURCE_EXTRACTION_KINDS",
    "SOURCE_EXTRACTION_SCHEMA_VERSION",
    "SOURCE_EXTRACTION_SPEC_SCHEMA_VERSION",
    "BoundSourceExtraction",
    "PartialSemanticContract",
    "SemanticContractError",
    "SourceExtractionSpec",
    "UnresolvedSemanticField",
    "compile_partial_semantic_contract",
]
