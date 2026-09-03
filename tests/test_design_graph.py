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

    assert any(node["id"] == "req-log" for node in graph.nodes)
    assert any(
        node["source"]["file"].endswith("demo/system-specification.yaml")
        for node in graph.nodes
    )
    assert graph.edges[0]["id"] == "writer-writes-entry"


def test_proposal_resolves_without_mutating_canonical_graph(tmp_path: Path) -> None:
    _write_current_specs(tmp_path)
    canonical = build_canonical_design_graph(tmp_path, feature_id="demo")
    proposal = {
        "base_version": canonical.version,
        "edits": [
            {
                "edit_id": "op-add-writer",
                "file_path": "docs/current/demo/architecture-specification.yaml",
                "op": "upsert_item",
                "section": "entities",
                "id": "writer",
                "value": {"type": "Component", "summary": "Writes entries."},
            },
            {
                "edit_id": "op-update-entry",
                "file_path": "docs/current/demo/architecture-specification.yaml",
                "op": "upsert_item",
                "section": "entities",
                "id": "log-entry",
                "value": {
                    "type": "Artifact",
                    "summary": "A durable log entry.",
                },
            },
        ],
    }

    result = validate_proposal(canonical, proposal)

    assert result.valid is True
    assert result.graph is not None
    assert any(node["id"] == "writer" for node in result.graph.nodes)
    assert any(
        node["data"].get("summary") == "A durable log entry."
        for node in result.graph.nodes
    )
    assert any(
        node["data"].get("summary") == "One log entry." for node in canonical.nodes
    )


def test_proposal_reports_stale_base_and_rendered_context_is_explicit(
    tmp_path: Path,
) -> None:
    _write_current_specs(tmp_path)
    canonical = build_canonical_design_graph(tmp_path, feature_id="demo")

    result = validate_proposal(
        canonical,
        {"base_version": "stale", "edits": []},
    )
    context = render_design_context(canonical, {"edits": []})

    assert result.valid is False
    assert result.issues[0].code == "stale_proposal"
    assert "canonical_graph:" in context
    assert "proposal:" in context
    assert "resolved_preview:" in context
