"""Normalized current-design graph and semantic proposal overlays."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.core.repo import resolve_repo_root

_SECTIONS = {
    "requirements": "requirement",
    "approach": "approach",
    "entities": "entity",
    "modules": "module",
    "tools": "tool",
    "entity_relationships": "relationship",
    "invariants": "invariant",
    "guidance": "guidance",
    "features": "feature",
    "decisions": "decision",
}
_FILES = {
    "system-specification.yaml",
    "system-specification.yml",
    "system-map-specification.yaml",
    "system-map-specification.yml",
    "architecture-specification.yaml",
    "architecture-specification.yml",
    "implementation-specification.yaml",
    "implementation-specification.yml",
    "feature-pr-specification.yaml",
    "feature-pr-specification.yml",
}


@dataclass(frozen=True, slots=True)
class DesignNode:
    id: str
    kind: str
    layer: str
    data: Mapping[str, Any]
    source: str
    section: str

    @property
    def key(self) -> str:
        return f"{self.layer}:{self.kind}:{self.id}"


@dataclass(frozen=True, slots=True)
class DesignEdge:
    id: str
    source: str
    target: str
    relation: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DesignGraph:
    nodes: Mapping[str, DesignNode]
    edges: tuple[DesignEdge, ...]
    version: str

    def to_data(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "layer": n.layer,
                    "data": copy.deepcopy(dict(n.data)),
                    "source": {"file": n.source, "section": n.section},
                }
                for n in sorted(self.nodes.values(), key=lambda item: item.key)
            ],
            "edges": [
                {
                    "id": e.id,
                    "from": e.source,
                    "to": e.target,
                    "relation": e.relation,
                    **dict(e.data),
                }
                for e in sorted(self.edges, key=lambda item: item.id)
            ],
        }


@dataclass(frozen=True, slots=True)
class DesignValidationIssue:
    code: str
    message: str
    operation: str | None = None

    def to_data(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.operation is not None:
            result["operation"] = self.operation
        return result


@dataclass(frozen=True, slots=True)
class ProposalValidationResult:
    valid: bool
    graph: DesignGraph | None
    issues: tuple[DesignValidationIssue, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [item.to_data() for item in self.issues],
            "resolved_preview": self.graph.to_data() if self.graph else None,
        }


def design_proposal_default_output_path(
    feature_id: str, repo_root: str | Path | None = None
) -> Path:
    root = resolve_repo_root(repo_root)
    return root / "docs" / "proposals" / feature_id / "design-proposal.yaml"


def create_design_proposal_template(
    feature_id: str, repo_root: str | Path | None = None
) -> Path:
    """Create an empty proposal pinned to the current canonical graph."""
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


def build_canonical_design_graph(
    repo_root: str | Path | None = None, *, feature_id: str | None = None
) -> DesignGraph:
    """Load current YAML documents into a normalized, read-only graph."""
    root = resolve_repo_root(repo_root)
    current = root / "docs" / "current"
    if feature_id is not None:
        feature_id = feature_id.strip()
        if not feature_id or Path(feature_id).name != feature_id:
            raise ValueError("feature_id must be a directory name.")
        current /= feature_id
    nodes: dict[str, DesignNode] = {}
    relationship_items: list[Mapping[str, Any]] = []
    paths = (
        sorted(p for p in current.rglob("*") if p.is_file() and p.name in _FILES)
        if current.is_dir()
        else []
    )
    for path in paths:
        raw = _load_yaml(path)
        if raw is None:
            continue
        layer = _layer(path.name)
        for section, kind in _SECTIONS.items():
            values = raw.get(section)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                continue
            for item in values:
                if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                    continue
                node = DesignNode(
                    item["id"].strip(),
                    kind,
                    layer,
                    copy.deepcopy(dict(item)),
                    str(path.relative_to(root)),
                    section,
                )
                nodes.setdefault(node.key, node)
                if section == "entity_relationships":
                    relationship_items.append(item)
    edges = tuple(_edges(relationship_items))
    material = {
        "nodes": [(key, node.data) for key, node in sorted(nodes.items())],
        "edges": [
            {
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "relation": edge.relation,
                "data": edge.data,
            }
            for edge in edges
        ],
    }
    version = hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return DesignGraph(nodes, edges, version)


def validate_proposal(
    canonical: DesignGraph, proposal: Mapping[str, Any]
) -> ProposalValidationResult:
    """Validate an overlay and return its derived graph without mutation."""
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
    nodes = dict(canonical.nodes)
    edges = {edge.id: edge for edge in canonical.edges}
    seen: set[str] = set()
    for index, raw in enumerate(operations):
        operation = raw if isinstance(raw, Mapping) else {}
        op_id = operation.get("id")
        label = str(op_id or f"operations[{index}]")
        if not isinstance(op_id, str) or not op_id.strip():
            issues.append(
                DesignValidationIssue(
                    "operation_id_missing", "Every operation needs an id.", label
                )
            )
        elif op_id in seen:
            issues.append(
                DesignValidationIssue(
                    "duplicate_operation_id",
                    f"Operation id {op_id!r} is duplicated.",
                    label,
                )
            )
        else:
            seen.add(op_id)
        _apply(operation, label, nodes, edges, issues)
    if issues:
        return ProposalValidationResult(False, None, tuple(issues))
    graph = DesignGraph(
        nodes,
        tuple(sorted(edges.values(), key=lambda item: item.id)),
        canonical.version,
    )
    issues.extend(_reference_issues(graph))
    return ProposalValidationResult(
        not issues, graph if not issues else None, tuple(issues)
    )


def render_design_context(
    canonical: DesignGraph, proposal: Mapping[str, Any] | None = None
) -> str:
    """Render clearly separated canonical, editable, and derived sections."""
    proposal_data = dict(
        proposal or {"base_version": canonical.version, "operations": []}
    )
    result = validate_proposal(canonical, proposal_data)
    return yaml.safe_dump(
        {
            "instructions": {
                "canonical_graph": "read_only",
                "proposal": "editable",
                "resolved_preview": "derived",
                "rule": (
                    "Modify proposal operations only; never modify the canonical graph."
                ),
            },
            "canonical_graph": canonical.to_data(),
            "proposal": proposal_data,
            "resolved_preview": result.graph.to_data() if result.graph else None,
            "validation": result.to_data(),
        },
        sort_keys=False,
        allow_unicode=True,
    )


def _apply(
    operation: Mapping[str, Any],
    label: str,
    nodes: dict[str, DesignNode],
    edges: dict[str, DesignEdge],
    issues: list[DesignValidationIssue],
) -> None:
    kind = operation.get("kind")
    if kind == "add_node":
        raw = operation.get("node")
        if not isinstance(raw, Mapping) or not all(
            isinstance(raw.get(key), str) and raw[key].strip() for key in ("id", "kind")
        ):
            issues.append(
                DesignValidationIssue(
                    "node_shape_invalid",
                    "add_node requires node.id and node.kind.",
                    label,
                )
            )
            return
        node = DesignNode(
            raw["id"].strip(),
            raw["kind"].strip(),
            str(raw.get("layer", "proposal")),
            copy.deepcopy(dict(raw.get("data", raw))),
            "proposal",
            "proposal",
        )
        if node.key in nodes:
            issues.append(
                DesignValidationIssue(
                    "node_already_exists", f"Node {node.key!r} already exists.", label
                )
            )
        else:
            nodes[node.key] = node
    elif kind == "update_node":
        target_node = _find(nodes, operation.get("target"))
        changes = operation.get("set")
        if target_node is None:
            issues.append(
                DesignValidationIssue(
                    "node_not_found", "Node target was not found.", label
                )
            )
        elif not isinstance(changes, Mapping):
            issues.append(
                DesignValidationIssue(
                    "node_changes_invalid", "update_node requires a set mapping.", label
                )
            )
        else:
            nodes[target_node.key] = DesignNode(
                target_node.id,
                target_node.kind,
                target_node.layer,
                {**target_node.data, **copy.deepcopy(dict(changes))},
                target_node.source,
                target_node.section,
            )
    elif kind == "remove_node":
        target_node = _find(nodes, operation.get("target"))
        if target_node is None:
            issues.append(
                DesignValidationIssue(
                    "node_not_found", "Node target was not found.", label
                )
            )
        else:
            nodes.pop(target_node.key)
    elif kind in {"add_edge", "update_edge"}:
        raw = operation.get("edge", operation) if kind == "add_edge" else operation
        if not isinstance(raw, Mapping):
            issues.append(
                DesignValidationIssue(
                    "edge_shape_invalid", "Edges require a mapping.", label
                )
            )
            return
        values = (
            raw.get("id"),
            raw.get("from", raw.get("source")),
            raw.get("to", raw.get("target")),
            raw.get("relation"),
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            issues.append(
                DesignValidationIssue(
                    "edge_shape_invalid",
                    "Edges require id, from, to, and relation.",
                    label,
                )
            )
            return
        edge_id, source, target, relation = (cast(str, value) for value in values)
        if kind == "add_edge" and edge_id in edges:
            issues.append(
                DesignValidationIssue(
                    "edge_already_exists", f"Edge {edge_id!r} already exists.", label
                )
            )
        elif kind == "update_edge" and edge_id not in edges:
            issues.append(
                DesignValidationIssue(
                    "edge_not_found", f"Edge {edge_id!r} was not found.", label
                )
            )
        else:
            edges[edge_id] = DesignEdge(edge_id, source, target, relation, {})
    elif kind == "remove_edge":
        edge_target = operation.get("target")
        if not isinstance(edge_target, str) or edge_target not in edges:
            issues.append(
                DesignValidationIssue(
                    "edge_not_found", "Edge target was not found.", label
                )
            )
        else:
            edges.pop(edge_target)
    else:
        issues.append(
            DesignValidationIssue(
                "operation_kind_unknown",
                f"Unsupported proposal operation {kind!r}.",
                label,
            )
        )


def _find(nodes: Mapping[str, DesignNode], target: object) -> DesignNode | None:
    if not isinstance(target, str):
        return None
    if target in nodes:
        return nodes[target]
    parts = target.split(":", 1)
    if len(parts) == 2:
        matches = [
            node
            for node in nodes.values()
            if node.kind == parts[0] and node.id == parts[1]
        ]
    else:
        matches = [node for node in nodes.values() if node.id == target]
    return matches[0] if len(matches) == 1 else None


def _reference_issues(graph: DesignGraph) -> list[DesignValidationIssue]:
    return [
        DesignValidationIssue(
            "edge_endpoint_not_found",
            f"Edge {edge.id!r} references a missing endpoint.",
        )
        for edge in graph.edges
        if _find(graph.nodes, edge.source) is None
        or _find(graph.nodes, edge.target) is None
    ]


def _edges(items: Sequence[Mapping[str, Any]]) -> list[DesignEdge]:
    result = []
    for item in items:
        values = (
            item.get("id"),
            item.get("source"),
            item.get("target"),
            item.get("relationship"),
        )
        if all(isinstance(value, str) and value.strip() for value in values):
            edge_id, source, target, relation = (cast(str, value) for value in values)
            result.append(
                DesignEdge(
                    edge_id,
                    source,
                    target,
                    relation,
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"id", "source", "target", "relationship"}
                    },
                )
            )
    return result


def _load_yaml(path: Path) -> Mapping[str, Any] | None:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return value if isinstance(value, Mapping) else None


def _layer(filename: str) -> str:
    return (
        "system"
        if filename.startswith("system")
        else "architecture"
        if filename.startswith("architecture")
        else "implementation"
        if filename.startswith("implementation")
        else "feature"
    )
