from pathlib import Path

from powdrr_lift.core.design_graph import (
    build_canonical_design_graph,
    render_design_context,
    validate_proposal,
)


def _write_current_specs(root: Path) -> None:
    directory = root / "docs" / "current" / "demo"
    directory.mkdir(parents=True)
    (directory / "system-specification.yaml").write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: demo-system
        requirements:
          - id: req-log
            description: Persist log entries.
            state: added
        approach:
          - id: app-file
            description: Use a file.
            state: added
        """,
        encoding="utf-8",
    )
    (directory / "architecture-specification.yaml").write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: demo-architecture
        entities:
          - id: log-entry
            type: Artifact
            summary: One log entry.
        entity_relationships:
          - id: writer-writes-entry
            source: writer
            target: log-entry
            relationship: writes
        invariants: []
        """,
        encoding="utf-8",
    )


def test_canonical_graph_normalizes_current_yaml_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    _write_current_specs(tmp_path)

    graph = build_canonical_design_graph(tmp_path, feature_id="demo")

    assert "requirement:req-log" in graph.nodes
    assert graph.nodes["requirement:req-log"].source.endswith(
        "demo/system-specification.yaml"
    )
    assert graph.edges[0].id == "writer-writes-entry"


def test_proposal_resolves_without_mutating_canonical_graph(tmp_path: Path) -> None:
    _write_current_specs(tmp_path)
    canonical = build_canonical_design_graph(tmp_path, feature_id="demo")
    proposal = {
        "base_version": canonical.version,
        "operations": [
            {
                "id": "op-add-writer",
                "kind": "add_node",
                "node": {
                    "id": "writer",
                    "kind": "component",
                    "layer": "architecture",
                    "data": {"summary": "Writes entries."},
                },
            },
            {
                "id": "op-update-entry",
                "kind": "update_node",
                "target": "entity:log-entry",
                "set": {"summary": "A durable log entry."},
            },
        ],
    }

    result = validate_proposal(canonical, proposal)

    assert result.valid is True
    assert result.graph is not None
    assert "component:writer" in result.graph.nodes
    assert canonical.nodes["entity:log-entry"].data["summary"] == "One log entry."


def test_proposal_reports_stale_base_and_rendered_context_is_explicit(
    tmp_path: Path,
) -> None:
    _write_current_specs(tmp_path)
    canonical = build_canonical_design_graph(tmp_path, feature_id="demo")

    result = validate_proposal(
        canonical,
        {"base_version": "stale", "operations": []},
    )
    context = render_design_context(canonical, {"operations": []})

    assert result.valid is False
    assert result.issues[0].code == "stale_proposal"
    assert "canonical_graph:" in context
    assert "proposal:" in context
    assert "resolved_preview:" in context
