"""Reconcile source annotations with the frozen Structrr task contract."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from powdrr_service.structrr.reference_editing import (
    edit_source_annotations,
    publish_staged_files,
)
from powdrr_service.structrr.reference_reconciliation import (
    LogicalSourceEdge,
    SourceOwnerIdentity,
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


def reconcile_source_task(run: Any, _review_client: Any) -> dict[str, Any]:
    """Reconcile exact accepted edges, editing only Python annotation comments."""
    contract = run.handoff.worker_prompt.to_data()["contract"]
    definitions = {entry.reference.id: entry.reference for entry in run.catalog.entries}
    obligations = contract["obligations"]
    preservation = contract["preservation"]
    accepted: set[LogicalSourceEdge] = set()
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

    changed_paths = set(getattr(run._last_attempt, "changed_paths", ()))
    changed_python_paths = {path for path in changed_paths if path.endswith(".py")}
    relevant_paths = {edge.owner.path for edge in accepted} | changed_python_paths
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
                issues.append(
                    f"Annotation {annotation.ref} in {path} has no known reference "
                    "and unique owner."
                )
                continue
            source_edges.add(annotation_edge)

    for edge in accepted:
        owner_index = indexes.get(edge.owner.path)
        if (
            owner_index is None
            or sum(
                (owner.kind, owner.qualified_name)
                == (edge.owner.kind, edge.owner.qualified_name)
                for owner in owner_index.owners
            )
            != 1
        ):
            issues.append(
                f"Accepted owner {edge.owner.qualified_name} in {edge.owner.path} "
                "does not resolve uniquely."
            )

    if issues:
        return {
            "status": "blocked",
            "reason": "invalid_source_index",
            "issues": issues,
            "decisions": [],
        }
    # The frozen contract authorizes adding accepted edges. Removing a source-only
    # edge is authorized only in a file changed by this worker.
    to_ensure = accepted - source_edges
    to_remove = {
        edge
        for edge in source_edges - accepted
        if edge.owner.path in changed_python_paths
    }
    if not to_ensure and not to_remove:
        return {
            "status": "passed",
            "decisions": [],
            "operations": [],
            "source_digests": {p: indexes[p].source_sha256 for p in sorted(indexes)},
        }
    receipt: dict[str, Any] = {
        "status": "passed",
        "decisions": [
            {
                "edge": edge.to_data(),
                "decision": "add" if edge in to_ensure else "remove",
                "basis": "accepted_contract"
                if edge in to_ensure
                else "worker_changed_source",
            }
            for edge in sorted(to_ensure | to_remove)
        ],
        "operations": [],
        "issues": [],
    }

    ensure: dict[str, dict[SourceOwnerIdentity, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    remove: dict[str, dict[SourceOwnerIdentity, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for edge in to_ensure:
        ensure[edge.owner.path][edge.owner].append(edge.reference_id)
    for edge in to_remove:
        remove[edge.owner.path][edge.owner].append(edge.reference_id)

    staged: dict[Path, bytes] = {}
    expected: dict[Path, str] = {}
    for path in sorted(set(ensure) | set(remove)):
        target = run.workspace_root / path
        original = sources[path]
        retained: dict[SourceOwnerIdentity, list[str]] = defaultdict(list)
        for edge in source_edges:
            if edge.owner.path == path and edge not in to_remove:
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
    if accepted - final_edges or to_remove & final_edges:
        raise ValueError("Reconciliation did not produce the decided annotation set.")
    receipt["operations"] = [
        {
            "operation": "ensure_annotation"
            if edge in to_ensure
            else "remove_annotation",
            "edge": edge.to_data(),
        }
        for edge in sorted(to_ensure | to_remove)
    ]
    receipt["status"] = "passed"
    return receipt
