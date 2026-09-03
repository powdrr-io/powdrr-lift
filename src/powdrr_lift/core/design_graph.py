"""LLM-facing design graph built on the existing gather_context scope.

Canonical specification documents are never changed here. A proposal stores
the same structural YAML edit primitives used by ``yaml_edit`` and is applied
to in-memory copies before the resulting graph is validated.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core.repo import resolve_repo_root
from powdrr_lift.core.spec_context import (
    GatherContextMatch,
    gather_specification_context,
    supported_context_types,
)


@dataclass(frozen=True, slots=True)
class DesignGraph:
    """A normalized graph plus the gathered source documents behind it."""

    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    documents: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    version: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "nodes": list(self.nodes),
            "edges": list(self.edges),
        }


@dataclass(frozen=True, slots=True)
class DesignValidationIssue:
    code: str
    message: str
    edit: str | None = None

    def to_data(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.edit is not None:
            result["edit"] = self.edit
        return result


@dataclass(frozen=True, slots=True)
class ProposalValidationResult:
    valid: bool
    graph: DesignGraph | None
    issues: tuple[DesignValidationIssue, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [issue.to_data() for issue in self.issues],
            "resolved_preview": self.graph.to_data() if self.graph else None,
        }


def design_proposal_default_output_path(
    feature_id: str, repo_root: str | Path | None = None
) -> Path:
    return (
        resolve_repo_root(repo_root)
        / "docs"
        / "proposals"
        / feature_id
        / "design-proposal.yaml"
    )


def create_design_proposal_template(
    feature_id: str, repo_root: str | Path | None = None
) -> Path:
    graph = build_canonical_design_graph(repo_root, feature_id=feature_id)
    path = design_proposal_default_output_path(feature_id, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema": "https://powdrr.io/schemas/design-proposal-v1",
                "id": feature_id,
                "base_version": graph.version,
                "edits": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def build_canonical_design_graph(
    repo_root: str | Path | None = None,
    *,
    feature_id: str | None = None,
) -> DesignGraph:
    """Build the graph from exactly the documents that gather_context searches."""
    root = resolve_repo_root(repo_root)
    report = gather_specification_context(
        root,
        types=list(supported_context_types()),
        feature_id=feature_id,
    )
    matches = [
        match for match in report.matches if match.specification_type != "proposed"
    ]
    return _graph_from_matches(root, matches)


def validate_proposal(
    canonical: DesignGraph,
    proposal: Mapping[str, Any],
) -> ProposalValidationResult:
    """Apply yaml_edit-compatible proposal edits to copies and validate the graph."""
    issues: list[DesignValidationIssue] = []
    if proposal.get("base_version") not in (None, canonical.version):
        issues.append(
            DesignValidationIssue(
                "stale_proposal",
                "Proposal base_version does not match the canonical graph.",
            )
        )
    edits = proposal.get("edits", [])
    if not isinstance(edits, Sequence) or isinstance(edits, (str, bytes)):
        return ProposalValidationResult(
            False,
            None,
            (*issues, DesignValidationIssue("edits_invalid", "edits must be a list.")),
        )
    documents = {
        path: copy.deepcopy(dict(data)) for path, data in canonical.documents.items()
    }
    seen: set[str] = set()
    for index, raw_edit in enumerate(edits):
        edit = raw_edit if isinstance(raw_edit, Mapping) else {}
        edit_id = str(edit.get("edit_id") or f"edits[{index}]")
        if edit_id in seen:
            issues.append(
                DesignValidationIssue(
                    "duplicate_edit_id", f"Edit id {edit_id!r} is duplicated.", edit_id
                )
            )
        seen.add(edit_id)
        _apply_yaml_edit(edit, edit_id, documents, issues)
    if issues:
        return ProposalValidationResult(False, None, tuple(issues))
    graph = _graph_from_documents(documents)
    issues.extend(_validate_edges(graph))
    return ProposalValidationResult(
        not issues, graph if not issues else None, tuple(issues)
    )


def render_design_context(
    canonical: DesignGraph,
    proposal: Mapping[str, Any] | None = None,
) -> str:
    """Render the canonical graph, editable proposal edits, and derived preview."""
    proposal_data = dict(proposal or {"base_version": canonical.version, "edits": []})
    result = validate_proposal(canonical, proposal_data)
    return yaml.safe_dump(
        {
            "instructions": {
                "canonical_graph": "read_only",
                "proposal": "editable",
                "resolved_preview": "derived",
                "rule": "Edit proposal edits only; never edit canonical documents.",
            },
            "canonical_graph": canonical.to_data(),
            "proposal": proposal_data,
            "resolved_preview": result.graph.to_data() if result.graph else None,
            "validation": result.to_data(),
        },
        sort_keys=False,
        allow_unicode=True,
    )


def _graph_from_matches(
    root: Path, matches: Sequence[GatherContextMatch]
) -> DesignGraph:
    paths = {
        str(Path(match.path).resolve().relative_to(root.resolve())) for match in matches
    }
    documents: dict[str, dict[str, Any]] = {}
    for path in paths:
        loaded = _load_yaml(root / path)
        if loaded is not None:
            documents[path] = loaded
    return _graph_from_documents(documents)


def _graph_from_documents(documents: Mapping[str, Mapping[str, Any]]) -> DesignGraph:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for path, document in sorted(documents.items()):
        for section, values in document.items():
            if not isinstance(values, list):
                continue
            for index, item in enumerate(values):
                if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                    continue
                node = {
                    "id": item["id"],
                    "kind": section,
                    "data": copy.deepcopy(dict(item)),
                    "source": {"file": path, "section": section, "index": index},
                }
                nodes.append(node)
                if section == "entity_relationships":
                    source = item.get("source")
                    target = item.get("target")
                    relation = item.get("relationship")
                    if all(
                        isinstance(value, str) and value.strip()
                        for value in (source, target, relation)
                    ):
                        edges.append(
                            {
                                "id": item["id"],
                                "from": source,
                                "to": target,
                                "relation": relation,
                            }
                        )
    material = {"nodes": nodes, "edges": edges}
    version = hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return DesignGraph(tuple(nodes), tuple(edges), documents, version)


def _apply_yaml_edit(
    edit: Mapping[str, Any],
    edit_id: str,
    documents: dict[str, dict[str, Any]],
    issues: list[DesignValidationIssue],
) -> None:
    path_value = edit.get("file_path")
    if not isinstance(path_value, str) or path_value not in documents:
        issues.append(
            DesignValidationIssue(
                "document_not_found",
                "Edit file_path must identify a gathered canonical document.",
                edit_id,
            )
        )
        return
    operation = edit.get("op")
    document = documents[path_value]
    if operation == "set_value":
        path = edit.get("path")
        if not isinstance(path, list) or not path:
            issues.append(
                DesignValidationIssue(
                    "path_invalid", "set_value requires a non-empty path.", edit_id
                )
            )
            return
        _set_path(document, path, edit.get("value"), edit_id, issues)
    elif operation == "remove_key":
        path = edit.get("path")
        if not isinstance(path, list) or not path:
            issues.append(
                DesignValidationIssue(
                    "path_invalid", "remove_key requires a non-empty path.", edit_id
                )
            )
            return
        _remove_path(document, path, edit_id, issues)
    elif operation in {"upsert_item", "remove_item"}:
        section = edit.get("section")
        if not isinstance(section, str) or not section.strip():
            issues.append(
                DesignValidationIssue(
                    "section_invalid", f"{operation} requires section.", edit_id
                )
            )
            return
        values = document.get(section)
        if values is None and operation == "upsert_item":
            values = document[section] = []
        if not isinstance(values, list):
            issues.append(
                DesignValidationIssue(
                    "section_invalid", f"Section {section!r} is not a list.", edit_id
                )
            )
            return
        item_id = edit.get("id")
        index = next(
            (
                i
                for i, item in enumerate(values)
                if isinstance(item, Mapping) and item.get("id") == item_id
            ),
            None,
        )
        if operation == "remove_item":
            if index is None:
                issues.append(
                    DesignValidationIssue(
                        "item_not_found",
                        f"Item {item_id!r} was not found in {section!r}.",
                        edit_id,
                    )
                )
            else:
                values.pop(index)
        else:
            value = edit.get("value")
            if not isinstance(value, Mapping) or not isinstance(item_id, str):
                issues.append(
                    DesignValidationIssue(
                        "item_invalid",
                        "upsert_item requires id and mapping value.",
                        edit_id,
                    )
                )
            elif index is None:
                values.append({"id": item_id, **copy.deepcopy(dict(value))})
            else:
                values[index] = {"id": item_id, **copy.deepcopy(dict(value))}
    else:
        issues.append(
            DesignValidationIssue(
                "operation_unknown",
                f"Unsupported yaml_edit operation {operation!r}.",
                edit_id,
            )
        )


def _set_path(
    document: dict[str, Any],
    path: list[Any],
    value: Any,
    edit_id: str,
    issues: list[DesignValidationIssue],
) -> None:
    target: Any = document
    for component in path[:-1]:
        if not isinstance(target, Mapping) or component not in target:
            issues.append(
                DesignValidationIssue(
                    "path_not_found", f"Path {path!r} was not found.", edit_id
                )
            )
            return
        target = target[component]
    if not isinstance(target, dict):
        issues.append(
            DesignValidationIssue(
                "path_invalid", f"Path {path!r} does not identify a mapping.", edit_id
            )
        )
        return
    target[path[-1]] = copy.deepcopy(value)


def _remove_path(
    document: dict[str, Any],
    path: list[Any],
    edit_id: str,
    issues: list[DesignValidationIssue],
) -> None:
    target: Any = document
    for component in path[:-1]:
        if not isinstance(target, Mapping) or component not in target:
            issues.append(
                DesignValidationIssue(
                    "path_not_found", f"Path {path!r} was not found.", edit_id
                )
            )
            return
        target = target[component]
    if not isinstance(target, dict) or path[-1] not in target:
        issues.append(
            DesignValidationIssue(
                "path_not_found", f"Path {path!r} was not found.", edit_id
            )
        )
        return
    del target[path[-1]]


def _validate_edges(graph: DesignGraph) -> list[DesignValidationIssue]:
    ids = {str(node["id"]) for node in graph.nodes}
    return [
        DesignValidationIssue(
            "edge_endpoint_not_found",
            f"Edge {edge['id']!r} references a missing endpoint.",
        )
        for edge in graph.edges
        if edge["from"] not in ids or edge["to"] not in ids
    ]


def _load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return dict(value) if isinstance(value, Mapping) else None
