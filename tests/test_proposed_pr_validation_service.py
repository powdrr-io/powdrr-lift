from __future__ import annotations

from powdrr_service.specifications.proposed_pr_coverage import ProposedPRSourceDocument
from powdrr_service.specifications.proposed_pr_validation import (
    build_proposed_pr_dependency_graph,
    build_proposed_pr_specification_validation_report,
)


def test_service_validates_single_proposed_pr_from_supplied_repository_evidence() -> (
    None
):
    report = build_proposed_pr_specification_validation_report(
        """
        id: pr-new
        feature_ids: [feature-current]
        dependent_prs: [pr-base]
        intent:
          problem: Add capability.
          goal: Add capability.
          reasoning: Keep the repo aligned.
        acceptance_criteria: [{id: ac-one, description: It works.}]
        expected_tests: []
        required_test_cases: []
        expected_outcomes: []
        non_goals: []
        risks: []
        """,
        available_feature_ids=("feature-current",),
        existing_pr_specifications=(
            ProposedPRSourceDocument(
                "proposals/base/proposed-pr-specification.yaml",
                "id: pr-base\ndependent_prs: []",
            ),
        ),
        dependency_pr_specifications=(
            ProposedPRSourceDocument(
                "proposals/base/proposed-pr-specification.yaml",
                "id: pr-base\ndependent_prs: []",
            ),
        ),
        feature_coverage_source=None,
        effect_sources=(),
    )

    assert report.validation_successful is True
    assert report.proposed_pr_id == "pr-new"
    assert report.available_feature_ids == ["feature-current"]
    assert report.known_pr_ids == ["pr-base"]
    assert report.issues == []


def test_service_checks_multi_pr_ids_dependencies_and_feature_ownership() -> None:
    report = build_proposed_pr_specification_validation_report(
        """
        id: feature
        feature_ids: [feature-current]
        proposed_prs:
          - id: pr-first
            dependent_prs: [pr-second]
            intent: Implement first.
            justification: Required first step.
          - id: pr-second
            dependent_prs: [pr-first]
            intent: Implement second.
            justification: Required second step.
        features:
          - id: feature-current
            action: added
            proposed_pr_id: undeclared
        """,
        available_feature_ids=("feature-current",),
        existing_pr_specifications=(),
        dependency_pr_specifications=(),
        feature_coverage_source=None,
        effect_sources=(),
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "proposed_pr_dependency_cycle",
        "unknown_proposed_pr_id",
    }


def test_service_dependency_graph_reconstructs_supplied_documents() -> None:
    graph = build_proposed_pr_dependency_graph(
        (
            ProposedPRSourceDocument(
                "proposal.yaml",
                """
                id: feature
                proposed_prs:
                  - id: pr-base
                    dependent_prs: []
                  - id: pr-app
                    dependent_prs: [pr-base]
                """,
            ),
            ProposedPRSourceDocument("invalid.yaml", "not: [valid"),
        )
    )

    assert graph == {"pr-base": (), "pr-app": ("pr-base",)}


def test_service_validator_has_no_agent_imports() -> None:
    import ast
    from pathlib import Path

    module = (
        Path(__file__).parents[1]
        / "packages/service/src/powdrr_service/specifications/proposed_pr_validation.py"
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported_roots = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_roots.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert "powdrr_lift" not in imported_roots
