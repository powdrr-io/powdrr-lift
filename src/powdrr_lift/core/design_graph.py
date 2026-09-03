"""Graph projection and durable graph-native design proposals."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core.repo import resolve_repo_root
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    supported_context_types,
)


@dataclass(frozen=True, slots=True)
class DesignGraph:
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    version: str

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
    operation_id: str | None = None

    def to_data(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.message}
        if self.operation_id is not None:
            value["operation_id"] = self.operation_id
        return value


@dataclass(frozen=True, slots=True)
class ProposalValidationResult:
    valid: bool
    graph: DesignGraph | None
    issues: tuple[DesignValidationIssue, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [issue.to_data() for issue in self.issues],
            "resolved_graph": self.graph.to_data() if self.graph else None,
        }


def build_canonical_design_graph(
    repo_root: str | Path | None = None, *, feature_id: str | None = None
) -> DesignGraph:
    """Project the exact current-state scope used by gather_context."""
    root = resolve_repo_root(repo_root)
    report = gather_specification_context(
        root, types=list(supported_context_types()), feature_id=feature_id
    )
    matches = [item for item in report.matches if item.specification_type != "proposed"]
    documents: dict[str, dict[str, Any]] = {}
    for match in matches:
        path = Path(match.path).resolve()
        try:
            relative = str(path.relative_to(root.resolve()))
        except ValueError:
            continue
        if relative in documents:
            continue
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(value, Mapping):
            documents[relative] = dict(value)
    return _graph_from_documents(documents)


def load_design_proposal(
    path: str | Path, repo_root: str | Path | None = None
) -> dict[str, Any]:
    root = resolve_repo_root(repo_root)
    resolved = Path(path) if Path(path).is_absolute() else root / path
    value = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("Design proposal must be a YAML mapping.")
    return dict(value)


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
                "operations": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def validate_proposal(
    canonical: DesignGraph, proposal: Mapping[str, Any]
) -> ProposalValidationResult:
    """Replay graph operations against a copy of the canonical graph."""
    issues: list[DesignValidationIssue] = []
    if proposal.get("base_version") not in (None, canonical.version):
        issues.append(
            DesignValidationIssue(
                "stale_proposal",
                "Proposal base_version does not match the canonical graph.",
            )
        )
    operations = proposal.get("operations", [])
    if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
        return ProposalValidationResult(
            False,
            None,
            (
                *issues,
                DesignValidationIssue(
                    "operations_invalid", "operations must be a list."
                ),
            ),
        )
    nodes = {str(node["key"]): copy.deepcopy(node) for node in canonical.nodes}
    edges = {str(edge["id"]): copy.deepcopy(edge) for edge in canonical.edges}
    seen: set[str] = set()
    for index, raw in enumerate(operations):
        operation = raw if isinstance(raw, Mapping) else {}
        operation_id = str(operation.get("id") or f"operations[{index}]")
        if operation_id in seen:
            issues.append(
                DesignValidationIssue(
                    "duplicate_operation_id",
                    f"Operation id {operation_id!r} is duplicated.",
                    operation_id,
                )
            )
        seen.add(operation_id)
        _apply_operation(operation, operation_id, nodes, edges, issues)
    if issues:
        return ProposalValidationResult(False, None, tuple(issues))
    graph = _graph_from_records(nodes, edges, canonical.version)
    issues.extend(_validate_edges(graph))
    return ProposalValidationResult(
        not issues, graph if not issues else None, tuple(issues)
    )


def discover_design(
    canonical: DesignGraph,
    seeds: Sequence[str],
    *,
    depth: int = 1,
    limit: int = 100,
) -> DesignGraph:
    """Return a bounded connected graph slice for incremental discovery."""
    if depth < 0 or limit < 1:
        raise ValueError("depth must be non-negative and limit must be positive.")
    known_ids = {str(node["id"]) for node in canonical.nodes}
    selected = {seed for seed in seeds if seed in known_ids}
    frontier = set(selected)
    for _ in range(depth):
        next_frontier: set[str] = set()
        for edge in canonical.edges:
            if edge["from"] in frontier or edge["to"] in frontier:
                next_frontier.update((str(edge["from"]), str(edge["to"])))
        next_frontier -= selected
        selected.update(next_frontier)
        frontier = next_frontier
        if len(selected) >= limit:
            break
    selected = set(sorted(selected)[:limit])
    nodes = {
        str(node["key"]): node for node in canonical.nodes if node["id"] in selected
    }
    edges = {
        str(edge["id"]): edge
        for edge in canonical.edges
        if edge["from"] in selected and edge["to"] in selected
    }
    return _graph_from_records(nodes, edges, canonical.version)


def render_design_context(
    canonical: DesignGraph, proposal: Mapping[str, Any] | None = None
) -> str:
    proposal_data = dict(
        proposal or {"base_version": canonical.version, "operations": []}
    )
    result = validate_proposal(canonical, proposal_data)
    return yaml.safe_dump(
        {
            "instructions": {
                "canonical_graph": "read_only",
                "proposal": "editable_graph_operations",
                "resolved_graph": "derived",
                "rule": (
                    "Modify proposal operations only; never modify the canonical graph."
                ),
            },
            "canonical_graph": canonical.to_data(),
            "proposal": proposal_data,
            "resolved_graph": result.graph.to_data() if result.graph else None,
            "validation": result.to_data(),
        },
        sort_keys=False,
        allow_unicode=True,
    )


def _apply_operation(
    operation: Mapping[str, Any],
    operation_id: str,
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    issues: list[DesignValidationIssue],
) -> None:
    kind = operation.get("op")
    if kind == "add_node":
        node = operation.get("node")
        if (
            not isinstance(node, Mapping)
            or not isinstance(node.get("id"), str)
            or not isinstance(node.get("type"), str)
        ):
            issues.append(
                DesignValidationIssue(
                    "node_invalid",
                    "add_node requires node.id and node.type.",
                    operation_id,
                )
            )
            return
        key = str(node["id"])
        if key in nodes:
            issues.append(
                DesignValidationIssue(
                    "node_exists", f"Node {key!r} already exists.", operation_id
                )
            )
            return
        nodes[key] = {
            "key": key,
            "id": key,
            "kind": node["type"],
            "data": copy.deepcopy(dict(node)),
            "source": {"proposal": operation_id},
        }
    elif kind == "update_node":
        node_id = operation.get("node_id")
        node = nodes.get(str(node_id))
        changes = operation.get("set")
        if node is None:
            issues.append(
                DesignValidationIssue(
                    "node_missing", f"Node {node_id!r} was not found.", operation_id
                )
            )
        elif not isinstance(changes, Mapping):
            issues.append(
                DesignValidationIssue(
                    "changes_invalid", "update_node requires set.", operation_id
                )
            )
        else:
            node["data"].update(copy.deepcopy(dict(changes)))
    elif kind == "remove_node":
        node_id = str(operation.get("node_id"))
        if node_id not in nodes:
            issues.append(
                DesignValidationIssue(
                    "node_missing", f"Node {node_id!r} was not found.", operation_id
                )
            )
        else:
            del nodes[node_id]
    elif kind in {"add_edge", "update_edge"}:
        edge = operation.get("edge", operation)
        if not isinstance(edge, Mapping):
            issues.append(
                DesignValidationIssue(
                    "edge_invalid",
                    "Edge operation requires an edge mapping.",
                    operation_id,
                )
            )
            return
        edge_id, source, target, relation = (
            edge.get("id"),
            edge.get("source"),
            edge.get("target"),
            edge.get("relationship"),
        )
        if not all(
            isinstance(value, str) and value.strip()
            for value in (edge_id, source, target, relation)
        ):
            issues.append(
                DesignValidationIssue(
                    "edge_invalid",
                    "Edges require id, source, target, and relationship.",
                    operation_id,
                )
            )
            return
        edge_id = str(edge_id)
        source = str(source)
        target = str(target)
        relation = str(relation)
        if kind == "add_edge" and edge_id in edges:
            issues.append(
                DesignValidationIssue(
                    "edge_exists", f"Edge {edge_id!r} already exists.", operation_id
                )
            )
        elif kind == "update_edge" and edge_id not in edges:
            issues.append(
                DesignValidationIssue(
                    "edge_missing", f"Edge {edge_id!r} was not found.", operation_id
                )
            )
        else:
            edges[edge_id] = {
                "id": edge_id,
                "from": source,
                "to": target,
                "relation": relation,
            }
    elif kind == "remove_edge":
        edge_id = str(operation.get("edge_id"))
        if edge_id not in edges:
            issues.append(
                DesignValidationIssue(
                    "edge_missing", f"Edge {edge_id!r} was not found.", operation_id
                )
            )
        else:
            del edges[edge_id]
    else:
        issues.append(
            DesignValidationIssue(
                "operation_unknown",
                f"Unsupported graph operation {kind!r}.",
                operation_id,
            )
        )


def _graph_from_documents(documents: Mapping[str, Mapping[str, Any]]) -> DesignGraph:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    for path, document in documents.items():
        for section, values in document.items():
            if not isinstance(values, list):
                continue
            for index, item in enumerate(values):
                if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                    continue
                key = str(item["id"])
                nodes.setdefault(
                    key,
                    {
                        "key": key,
                        "id": key,
                        "kind": section,
                        "data": copy.deepcopy(dict(item)),
                        "source": {"file": path, "section": section, "index": index},
                    },
                )
                if section == "entity_relationships" and all(
                    isinstance(item.get(name), str)
                    for name in ("source", "target", "relationship")
                ):
                    edges[str(item["id"])] = {
                        "id": item["id"],
                        "from": item["source"],
                        "to": item["target"],
                        "relation": item["relationship"],
                    }
    return _graph_from_records(nodes, edges)


def _graph_from_records(
    nodes: Mapping[str, dict[str, Any]],
    edges: Mapping[str, dict[str, Any]],
    version: str | None = None,
) -> DesignGraph:
    node_values = tuple(sorted(nodes.values(), key=lambda item: item["key"]))
    edge_values = tuple(sorted(edges.values(), key=lambda item: item["id"]))
    if version is None:
        material = {"nodes": node_values, "edges": edge_values}
        version = hashlib.sha256(
            json.dumps(material, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
    return DesignGraph(node_values, edge_values, version)


def _validate_edges(graph: DesignGraph) -> list[DesignValidationIssue]:
    node_ids = {str(node["id"]) for node in graph.nodes}
    return [
        DesignValidationIssue(
            "edge_endpoint_missing", f"Edge {edge['id']!r} references a missing node."
        )
        for edge in graph.edges
        if edge["from"] not in node_ids or edge["to"] not in node_ids
    ]
