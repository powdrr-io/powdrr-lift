from __future__ import annotations

from pathlib import Path

from powdrr_lift.core.spec_context import (
    gather_specification_context,
    proposed_pr_id_exists,
)


def test_proposed_pr_id_exists_uses_gather_context_discovery(
    tmp_path: Path,
) -> None:
    proposal_path = (
        tmp_path
        / "docs"
        / "proposals"
        / "demo-feature"
        / "proposed-pr-specification.yaml"
    )
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        "id: demo-pr\nfeature_ids: []\n",
        encoding="utf-8",
    )

    assert proposed_pr_id_exists(tmp_path, "demo-pr") is True
    assert proposed_pr_id_exists(tmp_path, "missing-pr") is False


def test_gather_context_filters_tools_by_entity_type_and_labels(
    tmp_path: Path,
) -> None:
    spec_path = tmp_path / "docs" / "specs" / "demo"
    spec_path.mkdir(parents=True)
    (spec_path / "architecture-specification.yaml").write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: 2026-08-11
        entities: []
        modules: []
        tools:
          - id: python-tests
            labels: [pr-prep, python]
            template: pytest -q
          - id: javascript-tests
            labels: [pr-prep, javascript]
            template: npm test
        entity_relationships: []
        invariants: []
        guidance: []
        """,
        encoding="utf-8",
    )

    report = gather_specification_context(
        tmp_path,
        types=["tools"],
        filters={"entity_type": ["Tool"], "labels": ["pr-prep", "python"]},
    )

    assert report.filters == {
        "entity_type": ["Tool"],
        "labels": ["pr-prep", "python"],
    }
    assert [match.item["id"] for match in report.matches] == ["python-tests"]


def test_gather_context_discovers_project_structure_artifact(tmp_path: Path) -> None:
    project_structure_path = tmp_path / "docs" / "project_structure"
    project_structure_path.mkdir(parents=True)
    (project_structure_path / "project-structure.yaml").write_text(
        "modules:\n- id: app\ntools:\n- id: tests\n",
        encoding="utf-8",
    )

    report = gather_specification_context(
        tmp_path,
        types=["modules", "tools"],
    )

    assert {match.path for match in report.matches} == {
        str(project_structure_path / "project-structure.yaml")
    }
    assert {match.section for match in report.matches} == {"modules", "tools"}


def test_gather_context_scopes_current_and_exact_feature_proposal(
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "docs" / "current" / "system.yaml"
    current_path.parent.mkdir(parents=True)
    current_path.write_text(
        "requirements:\n- id: current\n  description: current requirement\n",
        encoding="utf-8",
    )
    proposal_path = tmp_path / "docs" / "proposed" / "feature-a" / "proposal.yaml"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        "requirements:\n- id: proposed\n  description: proposed requirement\n",
        encoding="utf-8",
    )
    other_proposal_path = tmp_path / "docs" / "proposed" / "feature-b" / "proposal.yaml"
    other_proposal_path.parent.mkdir(parents=True)
    other_proposal_path.write_text(
        "requirements:\n- id: unrelated\n  description: unrelated requirement\n",
        encoding="utf-8",
    )

    report = gather_specification_context(
        tmp_path,
        types=["requirements"],
        feature_id="feature-a",
    )

    assert {match.item["id"] for match in report.matches} == {"current", "proposed"}
    assert {match.path for match in report.matches} == {
        str(current_path),
        str(proposal_path),
    }
    assert {
        match.path
        for match in gather_specification_context(
            tmp_path,
            types=["requirements"],
        ).matches
    } == {str(current_path)}
    assert {match.specification_type for match in report.matches} == {
        "current-state",
        "proposed",
    }


def test_gather_context_resolves_proposed_pr_document_in_explicit_feature(
    tmp_path: Path,
) -> None:
    proposal_path = (
        tmp_path
        / "docs"
        / "proposals"
        / "interaction-file-logging"
        / "proposed-pr-specification.yaml"
    )
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        "id: pr-interaction-capture-17\n"
        "feature_ids: [feature-interaction-capture]\n"
        "acceptance_criteria:\n"
        "  - id: ac-log-file-created\n",
        encoding="utf-8",
    )

    report = gather_specification_context(
        tmp_path,
        types=["proposed_prs"],
        keywords=["pr-interaction-capture-17"],
        feature_id="interaction-file-logging",
    )

    assert len(report.matches) == 1
    assert report.matches[0].path == str(proposal_path)
    assert report.matches[0].section == "proposed_prs"
    assert report.matches[0].item["id"] == "pr-interaction-capture-17"

    assert not gather_specification_context(
        tmp_path,
        types=["proposed_prs"],
        keywords=["different-pr"],
        feature_id="interaction-file-logging",
    ).matches


def test_gather_context_reads_yaml_and_yml_from_current_and_one_proposal_root(
    tmp_path: Path,
) -> None:
    current_root = tmp_path / "docs" / "current" / "feature"
    current_root.mkdir(parents=True)
    (current_root / "current.yml").write_text(
        "requirements:\n- id: current-requirement\n", encoding="utf-8"
    )

    canonical_proposal = tmp_path / "docs" / "proposals" / "feature"
    canonical_proposal.mkdir(parents=True)
    (canonical_proposal / "proposal.yml").write_text(
        "requirements:\n- id: canonical-proposal\n", encoding="utf-8"
    )
    legacy_proposal = tmp_path / "docs" / "proposed" / "feature"
    legacy_proposal.mkdir(parents=True)
    (legacy_proposal / "proposal.yaml").write_text(
        "requirements:\n- id: legacy-proposal\n", encoding="utf-8"
    )

    report = gather_specification_context(
        tmp_path,
        types=["requirements"],
        feature_id="feature",
    )

    assert {match.item["id"] for match in report.matches} == {
        "current-requirement",
        "canonical-proposal",
    }
