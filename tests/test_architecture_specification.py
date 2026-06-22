from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import build_architecture_specification_validation_report
from powdrr_lift.cli import main
from powdrr_lift.core import architecture_specification_default_output_path

_SYSTEM_SPECIFICATION_YAML = """
version: 1
id: "2026-06-20-skill-distribution-system"
title: "Installable skill set distribution system for coding agents"
requirements:
  - id: req-canonical-skill-bundle
    description: >
      Maintain a canonical set of skill bundles that can be rendered into the
      agent-specific layouts required by Claude Code, Codex, Aider, Cursor,
      Antigravity, and OpenCode.
    state: added
  - id: req-cross-platform-cli-mcp
    description: >
      Provide adjoining CLI and MCP tooling that runs on macOS, Windows, and
      Linux without platform-specific behavior leaks into the user workflow.
    state: added
  - id: req-single-command-install
    description: >
      Make the CLI and MCP installable with a single command on each supported
      platform, including a Homebrew-based install path on macOS.
    state: added
  - id: req-agent-compatibility
    description: >
      Ensure the produced skills work in at least Claude Code, Codex, Aider,
      Cursor, Antigravity, and OpenCode.
    state: added
  - id: req-shared-validation
    description: >
      Validate generated skill packages, installer metadata, and agent-facing
      outputs before publishing them.
    state: added
  - id: req-upgrade-path
    description: >
      Support repeatable updates so teams can refresh skill bundles and the
      accompanying CLI or MCP without hand-editing every agent installation.
    state: added
approach:
  - id: app-canonical-authoring
    description: >
      Author each skill once in a canonical source format, then render the
      agent-specific skill files and metadata from that source.
    state: added
  - id: app-platform-packaging
    description: >
      Package the CLI and MCP as native-installable artifacts for macOS,
      Windows, and Linux, with the macOS path surfaced through Homebrew.
    state: added
  - id: app-agent-adapters
    description: >
      Maintain thin adapters for Claude Code, Codex, Aider, Cursor,
      Antigravity, and OpenCode so each agent can consume the same canonical
      skill content.
    state: added
  - id: app-cli-mcp-parity
    description: >
      Expose the same install, render, and validate actions through both the
      CLI and MCP so agents and operators can use whichever surface is
      available.
    state: added
  - id: app-validation-gates
    description: >
      Gate publishing on validation that checks the rendered skill bundles,
      the cross-platform installer shape, and the agent compatibility matrix.
    state: added
"""


def _write_system_specification(tmp_path: Path) -> None:
    system_spec_path = tmp_path / "docs" / "system" / "system-specification.yaml"
    system_spec_path.parent.mkdir(parents=True, exist_ok=True)
    system_spec_path.write_text(_SYSTEM_SPECIFICATION_YAML, encoding="utf-8")


def test_create_architecture_specification_template_writes_default_file(
    tmp_path: Path,
) -> None:
    output_path = architecture_specification_default_output_path(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "architecture-specification",
                "--entity-type",
                "Service",
                "--entity-type",
                "Skill",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# Allowed entity types:" in template_text
    assert "# - Set `id` to a date-based identifier" in template_text
    assert "# - Write each rationale in English" in template_text
    assert (
        "# - Every entity and every entity relationship rationale must cite at"
        in template_text
    )
    assert "id: null" in template_text
    assert "# - Service" in template_text
    assert "# - Skill" in template_text

    rendered_template = yaml.safe_load(template_text)
    assert [section for section in rendered_template] == [
        "id",
        "title",
        "entities",
        "entity_relationships",
        "invariants",
        "guidance",
    ]


def test_validate_architecture_specification_reports_invalid_types_and_links(
    tmp_path: Path,
) -> None:
    _write_system_specification(tmp_path)

    proposed_spec = """
    version: 1
    id: 2026-06-19
    title: Demo architecture

    entities:
      - id: Alpha
        type: Service
        rationale: Alpha satisfies "req-shared-validation".
      - id: Beta
        type: Widget
        rationale: Beta supports "req-agent-compatibility".

    entity_relationships:
      - id: rel-1
        source: Alpha
        target: Gamma
        relationship: depends_on
        description: Alpha depends on Gamma.
        rationale: This relationship supports "app-cli-mcp-parity".

    invariants:
      - id: inv-1
        description: Keep Alpha stable.
        rationale: Keep the system aligned with "req-shared-validation".
        related:
          entities:
            - Alpha
            - Delta
          entity_relationships:
            - rel-2

    guidance:
      - id: guide-1
        description: Mention Alpha in release notes.
        rationale: Keep the team aligned with "app-validation-gates".
        related:
          entities:
            - Alpha
          entity_relationships: []
    """

    report = build_architecture_specification_validation_report(
        proposed_spec,
        entity_types=["Service", "Skill"],
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert report.allowed_entity_types == ["Service", "Skill"]
    assert [issue.code for issue in report.issues] == [
        "entity_type_not_allowed",
        "unknown_relationship_entity",
        "unknown_related_entity",
        "unknown_related_relationship",
    ]


def test_validate_architecture_specification_requires_quoted_rationale_ids(
    tmp_path: Path,
) -> None:
    _write_system_specification(tmp_path)

    proposed_spec = """
    version: 1
    id: 2026-06-19
    title: Demo architecture

    entities:
      - id: Alpha
        type: Service
        rationale: Alpha supports the system without quoting an id.

    entity_relationships:
      - id: rel-1
        source: Alpha
        target: Alpha
        relationship: depends_on
        description: Alpha depends on itself for the demo.
        rationale: This relationship cites "req-does-not-exist".

    invariants:
      - id: inv-1
        description: Keep Alpha stable.
        rationale: Keep Alpha stable.
        related:
          entities:
            - Alpha
          entity_relationships:
            - rel-1

    guidance:
      - id: guide-1
        description: Mention Alpha in release notes.
        rationale: Keep the team aligned with "app-validation-gates".
        related:
          entities:
            - Alpha
          entity_relationships: []
    """

    report = build_architecture_specification_validation_report(
        proposed_spec,
        entity_types=["Service", "Skill"],
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == [
        "missing_entity_rationale_reference",
        "unknown_relationship_rationale_reference",
    ]


def test_validate_architecture_specification_requires_related_references(
    tmp_path: Path,
) -> None:
    _write_system_specification(tmp_path)

    proposed_spec = """
    version: 1
    id: 2026-06-19
    title: Demo architecture

    entities:
      - id: Alpha
        type: Service
        rationale: Alpha satisfies "req-shared-validation".

    entity_relationships:
      - id: rel-1
        source: Alpha
        target: Alpha
        relationship: depends_on
        description: Alpha depends on itself for the demo.
        rationale: This relationship supports "req-cross-platform-cli-mcp".

    invariants:
      - id: inv-1
        description: Keep Alpha stable.
        rationale: Keep Alpha stable around "req-shared-validation".
        related:
          entities: []
          entity_relationships: []

    guidance:
      - id: guide-1
        description: Mention Alpha in release notes.
        rationale: Keep the team aligned with "app-validation-gates".
        related:
          entities: []
          entity_relationships: []
    """

    report = build_architecture_specification_validation_report(
        proposed_spec,
        entity_types=["Service", "Skill"],
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == [
        "missing_related_reference",
        "missing_related_reference",
    ]


def test_validate_architecture_specification_reports_success_for_valid_spec(
    tmp_path: Path,
) -> None:
    _write_system_specification(tmp_path)

    proposed_spec = """
    version: 1
    id: 2026-06-19
    title: Demo architecture

    entities:
      - id: Alpha
        type: Service
        rationale: Alpha satisfies "req-shared-validation" and "app-validation-gates".
      - id: Beta
        type: Skill
        rationale: Beta supports "req-agent-compatibility" and "app-agent-adapters".

    entity_relationships:
      - id: rel-1
        source: Alpha
        target: Alpha
        relationship: depends_on
        description: Alpha depends on itself for the demo.
        rationale: This relationship supports "req-cross-platform-cli-mcp".

    invariants:
      - id: inv-1
        description: Keep Alpha stable.
        rationale: Keep Alpha stable around "req-shared-validation".
        related:
          entities:
            - Alpha
          entity_relationships:
            - rel-1

    guidance:
      - id: guide-1
        description: Mention Beta in release notes.
        rationale: Keep the team aligned with "app-validation-gates".
        related:
          entities:
            - Beta
          entity_relationships: []
    """

    report = build_architecture_specification_validation_report(
        proposed_spec,
        entity_types=["Service", "Skill"],
        repo_root=tmp_path,
    )

    assert report.validation_successful is True
    assert report.issues == []


def test_cli_validate_architecture_specification_reports_yaml(
    tmp_path: Path,
) -> None:
    _write_system_specification(tmp_path)
    spec_path = tmp_path / "architecture-specification.yaml"
    spec_path.write_text(
        """
        version: 1
        id: 2026-06-19
        title: Demo architecture

        entities:
          - id: Alpha
            type: Service
            rationale: Alpha satisfies "req-shared-validation".
          - id: Beta
            type: Widget
            rationale: Beta supports "req-agent-compatibility".

        entity_relationships:
          - id: rel-1
            source: Alpha
            target: Alpha
            relationship: depends_on
            description: Alpha depends on itself for the demo.
            rationale: This relationship supports "req-cross-platform-cli-mcp".

        invariants:
          - id: inv-1
            description: Keep Alpha stable.
            rationale: Keep Alpha stable around "req-shared-validation".
            related:
              entities:
                - Alpha
              entity_relationships:
                - rel-1

        guidance:
          - id: guide-1
            description: Mention Alpha in release notes.
            rationale: Keep the team aligned with "app-validation-gates".
            related:
              entities:
                - Alpha
              entity_relationships: []
        """,
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "evaluate-architecture-specification",
                "--entity-type",
                "Service",
                "--entity-type",
                "Skill",
                "--repo-root",
                str(tmp_path),
                "--input",
                str(spec_path),
            ]
        )

    assert exit_code == 1
    report = yaml.safe_load(stdout.getvalue())
    assert report["validation_successful"] is False
    assert report["allowed_entity_types"] == ["Service", "Skill"]
    assert {issue["code"] for issue in report["issues"]} == {
        "entity_type_not_allowed",
    }
