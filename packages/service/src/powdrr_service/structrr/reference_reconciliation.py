"""Reciprocal Structrr/source applicability checks and decision preparation.

Bootstrap name matches and source comments are observations, not proof that a
semantic relationship should exist. This module only compares representations
and prepares the provider-neutral question for an exact candidate edge.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from powdrr_service.planning.semantic_decision import (
    SemanticDecision,
    SemanticDecisionProvider,
    SemanticDecisionResult,
    SemanticDecisionSpec,
)
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.source_references import SourceReferenceIndex

RECONCILIATION_SCHEMA = "structrr-link-reconciliation-v1"
ELIGIBILITY_POLICY = "structrr-semantic-source-binding-v1"
_ELIGIBLE_RELATIONSHIPS: dict[str, frozenset[str]] = {
    "feature": frozenset({"implements", "checks"}),
    "decision": frozenset({"realizes", "checks"}),
    "assumption": frozenset({"relies_on", "checks"}),
    "invariant": frozenset({"preserves", "checks"}),
}
_CANONICAL_RELATIONSHIP = {
    "feature": "implements",
    "decision": "realizes",
    "assumption": "relies_on",
    "invariant": "preserves",
}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, order=True)
class SourceOwnerIdentity:
    path: str
    language: str
    kind: str
    qualified_name: str

    def to_data(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SourceOwnerIdentity:
        if set(raw) != {"path", "language", "kind", "qualified_name"}:
            raise ValueError("invalid source owner identity")
        if any(not isinstance(raw[key], str) for key in raw):
            raise ValueError("source owner identity fields must be text")
        return cls(raw["path"], raw["language"], raw["kind"], raw["qualified_name"])


@dataclass(frozen=True, slots=True, order=True)
class LogicalSourceEdge:
    reference_id: str
    owner: SourceOwnerIdentity
    relationship: str

    def to_data(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "owner": self.owner.to_data(),
            "relationship": self.relationship,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> LogicalSourceEdge:
        if set(raw) != {"reference_id", "owner", "relationship"}:
            raise ValueError("invalid logical source edge")
        owner = raw.get("owner")
        if not isinstance(owner, Mapping):
            raise ValueError("logical source edge owner must be an object")
        if not all(
            isinstance(raw[key], str) for key in ("reference_id", "relationship")
        ):
            raise ValueError("logical source edge fields must be text")
        return cls(
            raw["reference_id"],
            SourceOwnerIdentity.from_data(owner),
            raw["relationship"],
        )


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    code: str
    message: str
    reference_id: str | None = None
    binding_id: str | None = None
    owner: SourceOwnerIdentity | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "reference_id": self.reference_id,
            "binding_id": self.binding_id,
            "owner": self.owner.to_data() if self.owner else None,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    structrr_edges: tuple[LogicalSourceEdge, ...]
    source_edges: tuple[LogicalSourceEdge, ...]
    structrr_only: tuple[LogicalSourceEdge, ...]
    source_only: tuple[LogicalSourceEdge, ...]
    findings: tuple[ReconciliationFinding, ...]
    source_digests: Mapping[str, str]
    document_digest: str

    @property
    def consistent(self) -> bool:
        return not (self.structrr_only or self.source_only or self.findings)

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": RECONCILIATION_SCHEMA,
            "eligibility_policy": ELIGIBILITY_POLICY,
            "document_digest": self.document_digest,
            "source_digests": dict(sorted(self.source_digests.items())),
            "structrr_edges": [x.to_data() for x in self.structrr_edges],
            "source_edges": [x.to_data() for x in self.source_edges],
            "structrr_only": [x.to_data() for x in self.structrr_only],
            "source_only": [x.to_data() for x in self.source_only],
            "findings": [x.to_data() for x in self.findings],
            "consistent": self.consistent,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationOperation:
    operation: str
    edge: LogicalSourceEdge
    expected_document_digest: str
    expected_source_digest: str

    def to_data(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "edge": self.edge.to_data(),
            "expected_document_digest": self.expected_document_digest,
            "expected_source_digest": self.expected_source_digest,
        }


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    stage: str
    provider: SemanticDecisionProvider
    decision: SemanticDecision
    failure: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "provider": self.provider.to_data(),
            "decision": self.decision.to_data(),
            "failure": self.failure,
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ProviderAttempt:
        if set(raw) != {"stage", "provider", "decision", "failure"}:
            raise ValueError("invalid source-link provider attempt")
        provider_raw, decision_raw = raw.get("provider"), raw.get("decision")
        if not isinstance(provider_raw, Mapping) or not isinstance(
            decision_raw, Mapping
        ):
            raise ValueError("source-link provider attempt is incomplete")
        failure = raw.get("failure")
        if failure is not None and not isinstance(failure, str):
            raise ValueError("provider failure must be text or null")
        return cls(
            raw["stage"],
            SemanticDecisionProvider.from_data(provider_raw),
            SemanticDecision.from_data(decision_raw),
            failure,
        )


@dataclass(frozen=True, slots=True)
class SourceLinkDecisionRun:
    final_decision: SemanticDecision
    primary_specification: SemanticDecisionSpec
    final_specification: SemanticDecisionSpec
    attempts: tuple[ProviderAttempt, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "structrr-source-link-decision-run-v1",
            "final_decision": self.final_decision.to_data(),
            "primary_specification": self.primary_specification.to_data(),
            "final_specification": self.final_specification.to_data(),
            "attempts": [attempt.to_data() for attempt in self.attempts],
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SourceLinkDecisionRun:
        if (
            set(raw)
            != {
                "schema_version",
                "final_decision",
                "primary_specification",
                "final_specification",
                "attempts",
            }
            or raw.get("schema_version") != "structrr-source-link-decision-run-v1"
        ):
            raise ValueError("invalid source-link decision run schema")
        decision_raw = raw.get("final_decision")
        primary_spec_raw = raw.get("primary_specification")
        spec_raw = raw.get("final_specification")
        attempts_raw = raw.get("attempts")
        if (
            not isinstance(decision_raw, Mapping)
            or not isinstance(primary_spec_raw, Mapping)
            or not isinstance(spec_raw, Mapping)
            or not isinstance(attempts_raw, list)
            or any(not isinstance(item, Mapping) for item in attempts_raw)
        ):
            raise ValueError("source-link decision run is incomplete")
        decision = SemanticDecision.from_data(decision_raw)
        primary_specification = SemanticDecisionSpec.from_data(primary_spec_raw)
        specification = SemanticDecisionSpec.from_data(spec_raw)
        decision.validate_current(specification)
        attempts = tuple(ProviderAttempt.from_data(item) for item in attempts_raw)
        if not attempts or len(attempts) > 2 or attempts[-1].decision != decision:
            raise ValueError("source-link decision run has invalid attempt history")
        if attempts[0].stage != "primary":
            raise ValueError("source-link decision run must begin with the primary")
        attempts[0].decision.validate_current(primary_specification)
        if len(attempts) == 1:
            if (
                decision.result.status != "resolved"
                or specification != primary_specification
            ):
                raise ValueError("unresolved primary decision requires one fallback")
        elif (
            attempts[0].decision.result.status != "unresolved"
            or attempts[1].stage != "fallback"
            or attempts[1].provider.kind != "planning-llm"
            or specification
            != _fallback_specification(primary_specification, attempts[0])
        ):
            raise ValueError(
                "fallback is allowed exactly once after unresolved primary"
            )
        return cls(decision, primary_specification, specification, attempts)


def reconcile_source_references(
    *,
    document: Mapping[str, Any],
    definitions: Mapping[str, ReferenceDefinition],
    source_indexes: Mapping[str, SourceReferenceIndex],
    document_digest: str | None = None,
) -> ReconciliationReport:
    """Compare semantic binding edges and parsed source annotations both ways."""
    subjects = document.get("source_subjects", [])
    bindings = document.get("source_bindings", [])
    if not isinstance(subjects, list) or any(
        not isinstance(x, Mapping) for x in subjects
    ):
        raise ValueError("source_subjects must be a list of mappings")
    if not isinstance(bindings, list) or any(
        not isinstance(x, Mapping) for x in bindings
    ):
        raise ValueError("source_bindings must be a list of mappings")

    subjects_by_id = {item.get("id"): item for item in subjects}
    findings: list[ReconciliationFinding] = []
    structrr: list[LogicalSourceEdge] = []
    binding_ids: Counter[str] = Counter()
    for binding in bindings:
        binding_id = binding.get("id")
        if isinstance(binding_id, str):
            binding_ids[binding_id] += 1
        ref_id = binding.get("entity_id")
        # File membership and inferred noncanonical links are structural
        # inventory, so they do not imply a source comment.
        if not isinstance(ref_id, str) or ref_id not in definitions:
            continue
        definition = definitions[ref_id]
        if definition.lifecycle != "active":
            findings.append(
                ReconciliationFinding(
                    "inactive_reference_binding",
                    f"Source binding targets a {definition.lifecycle} reference.",
                    ref_id,
                    binding_id if isinstance(binding_id, str) else None,
                )
            )
            continue
        binding_relationship = binding.get("relationship")
        if binding_relationship == "implemented_by":
            relationship = _CANONICAL_RELATIONSHIP.get(definition.kind)
        elif binding_relationship == "verified_by":
            relationship = "checks"
        else:
            relationship = None
        if relationship not in _ELIGIBLE_RELATIONSHIPS.get(
            definition.kind, frozenset()
        ):
            findings.append(
                ReconciliationFinding(
                    "unsupported_binding_relationship",
                    "Binding relationship "
                    f"{binding_relationship!r} is not eligible for "
                    f"{definition.kind} references.",
                    ref_id,
                    binding_id if isinstance(binding_id, str) else None,
                )
            )
            continue
        subject = subjects_by_id.get(binding.get("subject_id"))
        if subject is None:
            findings.append(
                ReconciliationFinding(
                    "missing_source_subject",
                    "Eligible binding references a missing source subject.",
                    ref_id,
                    binding_id if isinstance(binding_id, str) else None,
                )
            )
            continue
        path, kind, name = (subject.get(k) for k in ("path", "kind", "qualified_name"))
        index = source_indexes.get(path) if isinstance(path, str) else None
        if index is None or not isinstance(kind, str) or not isinstance(name, str):
            findings.append(
                ReconciliationFinding(
                    "unresolved_source_owner",
                    "Eligible binding owner is absent from current source indexes.",
                    ref_id,
                    binding_id if isinstance(binding_id, str) else None,
                )
            )
            continue
        if index.parse_status != "complete":
            findings.append(
                ReconciliationFinding(
                    "source_parse_failed",
                    "Current source cannot resolve the eligible binding owner.",
                    ref_id,
                    binding_id if isinstance(binding_id, str) else None,
                )
            )
            continue
        owners = [
            o
            for o in index.owners
            if o.kind == kind and (kind == "module" or o.qualified_name == name)
        ]
        if len(owners) != 1:
            findings.append(
                ReconciliationFinding(
                    "unresolved_source_owner",
                    "Eligible binding owner does not resolve uniquely "
                    "in current source.",
                    ref_id,
                    binding_id if isinstance(binding_id, str) else None,
                )
            )
            continue
        owner = SourceOwnerIdentity(path, "python", kind, owners[0].qualified_name)
        span = binding.get("span")
        if isinstance(span, Mapping) and (
            span.get("start_line"),
            span.get("end_line"),
        ) != (owners[0].start_line, owners[0].end_line):
            findings.append(
                ReconciliationFinding(
                    "stale_binding_span",
                    "Persisted binding span differs from the current owner location.",
                    ref_id,
                    binding_id if isinstance(binding_id, str) else None,
                    owner,
                )
            )
        if relationship == "checks" and (
            kind not in {"function", "async_function", "class"}
            or not path.startswith(("tests/", "hardening_tests/"))
        ):
            findings.append(
                ReconciliationFinding(
                    "invalid_check_owner",
                    "A verified_by binding must resolve to an executable test owner.",
                    ref_id,
                    binding_id if isinstance(binding_id, str) else None,
                    owner,
                )
            )
            continue
        structrr.append(LogicalSourceEdge(ref_id, owner, str(relationship)))

    for binding_id, count in binding_ids.items():
        if count > 1:
            findings.append(
                ReconciliationFinding(
                    "duplicate_binding_id",
                    f"Binding ID {binding_id!r} occurs {count} times.",
                    binding_id=binding_id,
                )
            )

    source: list[LogicalSourceEdge] = []
    for path, index in sorted(source_indexes.items()):
        for diagnostic in index.diagnostics:
            findings.append(
                ReconciliationFinding(
                    "source_" + diagnostic.code,
                    f"{path}:{diagnostic.line or 0}:{diagnostic.column or 0}: "
                    + diagnostic.message,
                )
            )
        for annotation in index.annotations:
            source_definition = definitions.get(annotation.ref)
            if source_definition is None:
                findings.append(
                    ReconciliationFinding(
                        "unknown_source_reference",
                        "Source annotation does not resolve to a canonical reference.",
                        annotation.ref,
                    )
                )
                continue
            if source_definition.lifecycle != "active":
                findings.append(
                    ReconciliationFinding(
                        "inactive_source_reference",
                        "Source annotation targets a "
                        f"{source_definition.lifecycle} reference.",
                        annotation.ref,
                    )
                )
                continue
            if annotation.owner is None:
                findings.append(
                    ReconciliationFinding(
                        "unresolved_annotation_owner",
                        "Source annotation has no resolved owner.",
                        annotation.ref,
                    )
                )
                continue
            owner = SourceOwnerIdentity(
                path, "python", annotation.owner.kind, annotation.owner.qualified_name
            )
            rels = (
                frozenset({"checks"})
                if path.startswith(("tests/", "hardening_tests/"))
                else frozenset({_CANONICAL_RELATIONSHIP[source_definition.kind]})
                if source_definition.kind in _CANONICAL_RELATIONSHIP
                else frozenset()
            )
            matching = {
                edge.relationship
                for edge in structrr
                if edge.reference_id == annotation.ref and edge.owner == owner
            }
            # A source comment does not encode relationship. Use a registered
            # unique mapping, or require one unambiguous existing binding.
            if len(rels) == 1:
                relationship = next(iter(rels))
            elif len(matching) == 1:
                relationship = next(iter(matching))
            else:
                findings.append(
                    ReconciliationFinding(
                        "ambiguous_relationship",
                        "Source annotation relationship cannot be resolved uniquely.",
                        annotation.ref,
                        owner=owner,
                    )
                )
                continue
            source.append(LogicalSourceEdge(annotation.ref, owner, relationship))

    s_counts, c_counts = Counter(structrr), Counter(source)
    for edge, count in s_counts.items():
        if count > 1:
            findings.append(
                ReconciliationFinding(
                    "duplicate_eligible_edge",
                    f"Structrr represents this edge {count} times.",
                    edge.reference_id,
                    owner=edge.owner,
                )
            )
    for edge, count in c_counts.items():
        if count > 1:
            findings.append(
                ReconciliationFinding(
                    "duplicate_source_edge",
                    f"Source represents this edge {count} times.",
                    edge.reference_id,
                    owner=edge.owner,
                )
            )
    relevant_paths = {
        path for path, index in source_indexes.items() if index.annotations
    }
    relevant_paths.update(edge.owner.path for edge in structrr)
    return ReconciliationReport(
        tuple(sorted(s_counts)),
        tuple(sorted(c_counts)),
        tuple(sorted(s_counts.keys() - c_counts.keys())),
        tuple(sorted(c_counts.keys() - s_counts.keys())),
        tuple(
            sorted(
                findings,
                key=lambda x: (x.code, x.reference_id or "", x.binding_id or ""),
            )
        ),
        {
            path: source_indexes[path].source_sha256
            for path in sorted(relevant_paths)
            if path in source_indexes
        },
        document_digest or _digest(document),
    )


def prepare_source_link_decision(
    *,
    edge: LogicalSourceEdge,
    definition: ReferenceDefinition,
    source_text: str,
    source_fingerprint: str,
    observed_representations: Mapping[str, Any],
    context_text: str,
    contract_revision: str = "source-link-existence-v1",
) -> SemanticDecisionSpec:
    """Create a single-edge existence decision with complete context fingerprinting."""
    if edge.reference_id != definition.id:
        raise ValueError("source edge and canonical definition do not match")
    packet = {
        "edge": edge.to_data(),
        "statement": definition.statement,
        "rationale": definition.rationale,
        "source_ref": definition.source_ref,
        "version": definition.version,
        "lifecycle": definition.lifecycle,
        "source": source_text,
        "source_fingerprint": source_fingerprint,
        "observed_representations": dict(observed_representations),
        "context": context_text,
    }
    return SemanticDecisionSpec(
        decision_id="source-link:" + _digest(edge.to_data()).removeprefix("sha256:"),
        decision_kind="source_link_existence",
        subject_ref=edge.reference_id,
        proposition_text=(
            "Under the supplied accepted intent and source context, should this exact "
            "relationship exist between this Structrr reference and this code owner?"
        ),
        source_fingerprint=source_fingerprint,
        context_text=json.dumps(packet, sort_keys=True, ensure_ascii=False),
        accepted_definition_revision=f"{definition.id}@{definition.version}:{definition.lifecycle}",
        contract_revision=contract_revision,
    )


def compile_reconciliation_operations(
    *,
    report: ReconciliationReport,
    specifications: Mapping[LogicalSourceEdge, SemanticDecisionSpec],
    decisions: Mapping[LogicalSourceEdge, SemanticDecision | SourceLinkDecisionRun],
) -> tuple[ReconciliationOperation, ...]:
    """Compile a deterministic correction table from current bound decisions."""
    operations: list[ReconciliationOperation] = []
    source_present = set(report.source_edges)
    structrr_present = set(report.structrr_edges)
    candidates = set(specifications)
    observed_edges = set(report.structrr_edges) | set(report.source_edges)
    if candidates - observed_edges:
        raise ValueError("decision specification does not identify an observed edge")
    for edge in sorted(candidates):
        spec, decision_record = specifications.get(edge), decisions.get(edge)
        if spec is None or decision_record is None:
            continue
        decision = (
            decision_record.final_decision
            if isinstance(decision_record, SourceLinkDecisionRun)
            else decision_record
        )
        if (
            isinstance(decision_record, SourceLinkDecisionRun)
            and decision_record.primary_specification != spec
        ):
            raise ValueError(
                "source-link decision run was prepared for different inputs"
            )
        decision_spec = (
            decision_record.final_specification
            if isinstance(decision_record, SourceLinkDecisionRun)
            else spec
        )
        decision.validate_current(decision_spec)
        if decision.result.status != "resolved":
            continue
        value = decision.result.value
        if value not in {"exists", "absent"}:
            raise ValueError("source link decision has an unsupported value")
        source_digest = report.source_digests.get(edge.owner.path)
        if source_digest is None:
            raise ValueError("source edge decision has no source digest")
        targets = (
            ("ensure_binding", "ensure_annotation")
            if value == "exists"
            else ("remove_binding", "remove_annotation")
        )
        if value == "exists":
            operations.extend(
                ReconciliationOperation(
                    operation, edge, report.document_digest, source_digest
                )
                for operation in targets
            )
            continue
        for operation, present, should_exist in (
            (targets[0], edge in structrr_present, False),
            (targets[1], edge in source_present, False),
        ):
            if present == should_exist:
                continue
            operations.append(
                ReconciliationOperation(
                    operation, edge, report.document_digest, source_digest
                )
            )
    return tuple(sorted(operations, key=lambda x: (x.edge, x.operation)))


def run_source_link_decision(
    *,
    specification: SemanticDecisionSpec,
    primary: tuple[
        SemanticDecisionProvider, Callable[[SemanticDecisionSpec], Mapping[str, Any]]
    ],
    fallback: tuple[
        SemanticDecisionProvider, Callable[[SemanticDecisionSpec], Mapping[str, Any]]
    ],
    evidence_refs: Sequence[str],
    created_at: str,
    cached_run: SourceLinkDecisionRun | None = None,
) -> SourceLinkDecisionRun:
    """Run primary once and, only if unresolved/invalid/unavailable, fallback once."""
    if specification.decision_kind != "source_link_existence":
        raise ValueError(
            "source-link orchestration requires its dedicated decision kind"
        )
    if not evidence_refs:
        raise ValueError("source-link decisions require evidence references")
    if fallback[0].kind != "planning-llm":
        raise ValueError("source-link fallback provider must be a configured LLM")
    if (
        cached_run is not None
        and cached_run.primary_specification == specification
        and cached_run.attempts[0].provider == primary[0]
        and (
            len(cached_run.attempts) == 1
            or cached_run.attempts[1].provider == fallback[0]
        )
    ):
        # SourceLinkDecisionRun validates the complete per-stage fingerprints.
        # Provider identity includes model revision, so a changed provider cannot
        # reuse a prior classification.
        return SourceLinkDecisionRun.from_data(cached_run.to_data())

    def invoke(
        stage: str,
        spec: SemanticDecisionSpec,
        configured: tuple[
            SemanticDecisionProvider,
            Callable[[SemanticDecisionSpec], Mapping[str, Any]],
        ],
    ) -> ProviderAttempt:
        provider, call = configured
        failure = None
        try:
            response = call(spec)
            if not isinstance(response, Mapping):
                raise ValueError("provider response must be an object")
            result = SemanticDecisionResult.from_provider_data(
                spec.decision_kind, response
            )
            bound = spec.bind(
                provider=provider,
                provider_result=result.to_data(),
                evidence_refs=evidence_refs,
                created_at=created_at,
            )
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            reason = (
                "invalid_response"
                if isinstance(error, (ValueError, TypeError))
                else "classifier_abstained"
            )
            bound = spec.bind(
                provider=provider,
                provider_result={"status": "unresolved", "reason_code": reason},
                evidence_refs=evidence_refs,
                created_at=created_at,
            )
        return ProviderAttempt(stage, provider, bound, failure)

    first = invoke("primary", specification, primary)
    if first.decision.result.status == "resolved":
        return SourceLinkDecisionRun(
            first.decision, specification, specification, (first,)
        )
    fallback_spec = _fallback_specification(specification, first)
    second = invoke("fallback", fallback_spec, fallback)
    return SourceLinkDecisionRun(
        second.decision, specification, fallback_spec, (first, second)
    )


def _fallback_specification(
    specification: SemanticDecisionSpec, first: ProviderAttempt
) -> SemanticDecisionSpec:
    fallback_context = json.dumps(
        {
            "prior_input_fingerprint": specification.input_fingerprint,
            "primary_unresolved": first.decision.result.to_data(),
            "primary_provider": first.provider.to_data(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return replace(
        specification,
        decision_id=specification.decision_id + ":fallback",
        context_text=json.dumps(
            {
                "evidence_packet": specification.context_text,
                "fallback_context": fallback_context,
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        contract_revision=specification.contract_revision + "+single-fallback-v1",
    )


__all__ = [
    "ELIGIBILITY_POLICY",
    "LogicalSourceEdge",
    "ReconciliationFinding",
    "ProviderAttempt",
    "ReconciliationOperation",
    "ReconciliationReport",
    "SourceLinkDecisionRun",
    "SourceOwnerIdentity",
    "compile_reconciliation_operations",
    "prepare_source_link_decision",
    "reconcile_source_references",
    "run_source_link_decision",
]
