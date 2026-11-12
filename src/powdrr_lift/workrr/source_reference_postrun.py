"""Reconcile source annotations with the frozen Structrr task contract."""

from __future__ import annotations

import hashlib
import io
import json
import tokenize
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from powdrr_service.decision_classifier import DecisionProviderFactory
from powdrr_service.planning.semantic_decision import (
    SemanticDecisionProvider,
    SemanticDecisionSpec,
)
from powdrr_service.structrr.reference_editing import (
    edit_source_annotations,
    publish_staged_files,
)
from powdrr_service.structrr.reference_reconciliation import (
    LogicalSourceEdge,
    SourceOwnerIdentity,
    prepare_source_link_decision,
    run_source_link_decision,
)
from powdrr_service.structrr.source_references import (
    SourceReferenceIndex,
    index_python_source,
)

_RELATIONSHIP = {
    "feature": "implements",
    "decision": "realizes",
    "assumption": "relies_on",
    "invariant": "preserves",
}


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _edge_for_annotation(
    path: str,
    annotation: Any,
    definitions: Mapping[str, Any],
) -> LogicalSourceEdge | None:
    definition = definitions.get(annotation.ref)
    owner = annotation.owner
    if definition is None or owner is None:
        return None
    relationship = (
        "checks"
        if path.startswith(("tests/", "hardening_tests/"))
        else _RELATIONSHIP.get(definition.kind)
    )
    if relationship is None:
        return None
    return LogicalSourceEdge(
        annotation.ref,
        SourceOwnerIdentity(path, "python", owner.kind, owner.qualified_name),
        relationship,
    )


def _source_text(
    source: bytes, index: SourceReferenceIndex, edge: LogicalSourceEdge
) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    lines = source.decode(encoding).splitlines()
    owner = next(
        (
            item
            for item in index.owners
            if (item.kind, item.qualified_name)
            == (edge.owner.kind, edge.owner.qualified_name)
        ),
        None,
    )
    if owner is None:
        return "\n".join(lines)
    return "\n".join(lines[owner.start_line - 1 : owner.end_line])


def _provider_response(
    provider: Any,
    spec: SemanticDecisionSpec,
) -> Mapping[str, Any]:
    question = (
        "Should this exact Structrr reference relationship exist for this code owner? "
        "Return exists only when accepted intent and source support it; return absent "
        "when they do not; otherwise return unresolved with a permitted reason code."
    )
    packet = {
        "spec": spec.to_data(),
        "question": question,
        "instructions": [
            "Use only the supplied accepted placement, canonical definition, source "
            "owner, and observed representations.",
            "Do not treat a source comment as evidence that the relationship is valid.",
            "Return a resolved result only when the evidence is decisive.",
        ],
        "allowed_values": ["exists", "absent"],
        "source_text": spec.context_text,
    }
    messages = [
        {
            "role": "system",
            "content": "Make one bounded Structrr source-link decision.",
        },
        {"role": "user", "content": json.dumps(packet, sort_keys=True)},
    ]
    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["resolved", "unresolved"]},
            "value": {"type": ["string", "null"], "enum": ["exists", "absent", None]},
            "reason_code": {"type": ["string", "null"]},
        },
        "required": ["status", "value", "reason_code"],
        "additionalProperties": False,
    }
    return provider.complete_json(messages, response_schema=schema)


def reconcile_source_task(run: Any, review_client: Any) -> dict[str, Any]:
    """Decide each observed mismatch, then edit only Python annotation comments."""
    contract = run.handoff.worker_prompt.to_data()["contract"]
    definitions = {entry.reference.id: entry.reference for entry in run.catalog.entries}
    obligations = contract["obligations"]
    preservation = contract["preservation"]
    accepted: set[LogicalSourceEdge] = set()
    selectors: dict[LogicalSourceEdge, Mapping[str, Any]] = {}
    for record in [
        *obligations,
        *(p for p in preservation if p["disposition"] == "preserve"),
    ]:
        selector = record["owner"]
        if selector.get("binding") != "exact":
            continue
        definition = definitions[record["ref"]]
        relationship = (
            "checks"
            if selector["role"] == "test"
            else _RELATIONSHIP.get(definition.kind)
        )
        if relationship is None:
            continue
        edge = LogicalSourceEdge(
            record["ref"],
            SourceOwnerIdentity(
                selector["path"], "python", selector["kind"], selector["qualified_name"]
            ),
            relationship,
        )
        accepted.add(edge)
        selectors[edge] = record

    changed_paths = set(getattr(run._last_attempt, "changed_paths", ()))
    relevant_paths = {edge.owner.path for edge in accepted} | {
        path for path in changed_paths if path.endswith(".py")
    }
    accepted_paths = {edge.owner.path for edge in accepted}
    issues: list[str] = []
    indexes: dict[str, SourceReferenceIndex] = {}
    sources: dict[str, bytes] = {}
    for path in sorted(relevant_paths):
        target = run.workspace_root / path
        if (
            not target.resolve(strict=False).is_relative_to(run.workspace_root)
            or any(
                parent.is_symlink()
                for parent in target.parents
                if parent != run.workspace_root
                and parent.is_relative_to(run.workspace_root)
            )
            or target.is_symlink()
            or not target.is_file()
        ):
            if path in accepted_paths:
                issues.append(f"Required owner {path} is missing or unsafe.")
            continue
        source = target.read_bytes()
        sources[path] = source
        indexes[path] = index_python_source(path, source)

    source_edges: set[LogicalSourceEdge] = set()
    for path, index in indexes.items():
        if index.parse_status != "complete" or index.diagnostics:
            issues.append(
                f"Source annotations or Python ownership are invalid in {path}."
            )
        for annotation in index.annotations:
            annotation_edge = _edge_for_annotation(path, annotation, definitions)
            if annotation_edge is None:
                if annotation.ref in definitions:
                    issues.append(
                        f"Annotation {annotation.ref} in {path} has no unique owner."
                    )
                continue
            source_edges.add(annotation_edge)

    # Consider source-only edges only in files changed by this worker.
    mismatches = sorted((accepted - source_edges) | (source_edges - accepted))
    if issues:
        return {
            "status": "blocked",
            "reason": "invalid_source_index",
            "issues": issues,
            "decisions": [],
        }
    if not mismatches:
        return {
            "status": "passed",
            "decisions": [],
            "operations": [],
            "source_digests": {p: indexes[p].source_sha256 for p in sorted(indexes)},
        }

    fallback_provider = SemanticDecisionProvider(
        "planning-llm",
        name=getattr(review_client, "provider", None) or "workflow-llm",
        model_revision=getattr(review_client, "model", None) or "configured",
    )
    provider_factory = DecisionProviderFactory()
    configured = provider_factory.create(review_client, fail_closed=True)
    primary_provider = (
        SemanticDecisionProvider(
            "decision-provider",
            name=provider_factory.provider_name or "configured",
            model_revision=getattr(configured, "model", None) or "configured",
        )
        if configured is not None
        else fallback_provider
    )

    def primary_call(spec: SemanticDecisionSpec) -> Mapping[str, Any]:
        return _provider_response(
            configured or review_client,
            spec,
        )

    def fallback_call(spec: SemanticDecisionSpec) -> Mapping[str, Any]:
        return _provider_response(review_client, spec)

    decisions: dict[LogicalSourceEdge, Any] = {}
    records: list[dict[str, Any]] = []
    for edge in mismatches:
        definition = definitions.get(edge.reference_id)
        if definition is None:
            continue
        source_index = indexes.get(edge.owner.path)
        source = sources.get(edge.owner.path)
        if source_index is None or source is None:
            issues.append(
                f"Required owner {edge.owner.path} is missing from the candidate."
            )
            continue
        record = selectors.get(edge)
        context = {
            "accepted_placement": dict(record) if record is not None else None,
            "accepted_document_digest": contract["accepted_document_digest"],
            "observed": {
                "structrr": record is not None,
                "source": edge in source_edges,
            },
            "source_owner": edge.owner.to_data(),
        }
        spec = prepare_source_link_decision(
            edge=edge,
            definition=definition,
            source_text=_source_text(source, source_index, edge),
            source_fingerprint=source_index.source_sha256,
            observed_representations=context["observed"],
            context_text=json.dumps(context, sort_keys=True),
        )
        evidence_refs = [
            f"source:{edge.owner.path}:{source_index.source_sha256}",
            f"accepted-document:{contract['accepted_document_digest']}",
        ]
        decision_run = run_source_link_decision(
            specification=spec,
            primary=(
                primary_provider,
                primary_call,
            ),
            fallback=(
                fallback_provider,
                fallback_call,
            ),
            evidence_refs=evidence_refs,
            created_at=datetime.now(UTC).isoformat(),
        )
        decisions[edge] = decision_run
        value = decision_run.final_decision.result.value
        record_data = {"edge": edge.to_data(), **decision_run.to_data()}
        records.append(record_data)
        if decision_run.final_decision.result.status != "resolved":
            issues.append(
                f"Decision for {edge.reference_id} on {edge.owner.path} remained "
                "unresolved."
            )
        elif edge in accepted and value != "exists":
            issues.append(
                f"Accepted placement {edge.reference_id} was contradicted; amend "
                "the reviewed design."
            )
        elif edge not in accepted and value == "exists":
            issues.append(
                f"Source-only link {edge.reference_id} needs a reviewed Structrr "
                "placement."
            )

    receipt: dict[str, Any] = {
        "status": "blocked" if issues else "passed",
        "decisions": records,
        "operations": [],
        "issues": issues,
    }
    if issues:
        return receipt

    ensure: dict[str, dict[SourceOwnerIdentity, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    remove: dict[str, dict[SourceOwnerIdentity, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for edge, decision_run in decisions.items():
        value = decision_run.final_decision.result.value
        if value == "exists" and edge in accepted:
            ensure[edge.owner.path][edge.owner].append(edge.reference_id)
        elif value == "absent" and edge in source_edges and edge not in accepted:
            remove[edge.owner.path][edge.owner].append(edge.reference_id)

    staged: dict[Path, bytes] = {}
    expected: dict[Path, str] = {}
    for path in sorted(set(ensure) | set(remove)):
        target = run.workspace_root / path
        original = sources[path]
        retained: dict[SourceOwnerIdentity, list[str]] = defaultdict(list)
        for edge in source_edges & accepted:
            if edge.owner.path == path:
                retained[edge.owner].append(edge.reference_id)
        updated = edit_source_annotations(
            path=path,
            source=original,
            ensure=ensure.get(path, {}),
            remove=remove.get(path, {}),
            retained_edges=retained,
        )
        if updated != original:
            staged[target] = updated
            expected[target] = _sha256(original)
    if staged:
        receipt["transaction"] = publish_staged_files(
            staged=staged,
            expected_digests=expected,
            receipt_path=run.private_root
            / "source-link-reconciliation.transaction.json",
        )
    final_edges: set[LogicalSourceEdge] = set()
    for path in sorted(indexes):
        target = run.workspace_root / path
        if not target.is_file() or target.is_symlink():
            continue
        final_index = index_python_source(path, target.read_bytes())
        if final_index.parse_status != "complete" or final_index.diagnostics:
            raise ValueError(f"Reconciliation produced invalid annotations in {path}.")
        for annotation in final_index.annotations:
            annotation_edge = _edge_for_annotation(path, annotation, definitions)
            if annotation_edge is not None:
                final_edges.add(annotation_edge)
    if accepted - final_edges or (source_edges - accepted) & final_edges:
        raise ValueError("Reconciliation did not produce the decided annotation set.")
    receipt["operations"] = [
        {
            "operation": "ensure_annotation"
            if edge in accepted
            else "remove_annotation",
            "edge": edge.to_data(),
        }
        for edge in sorted(decisions)
    ]
    receipt["status"] = "passed"
    return receipt
