from __future__ import annotations

from powdrr_service.specifications.proposed_pr_coverage import (
    ProposedPRSourceDocument,
    validate_proposed_pr_coverage_and_effects,
)


def test_service_validates_coverage_and_effects_from_supplied_documents() -> None:
    result = validate_proposed_pr_coverage_and_effects(
        """
        proposed_prs:
          - id: feature-core
            requirements: [req-core]
            acceptance_criteria: [ac-core]
            expected_tests: [test-core]
        features:
          - id: feature-core
            action: added
        """,
        proposed_pr_ids=("feature-core",),
        feature_coverage_source=ProposedPRSourceDocument(
            path="proposals/feature/feature-pr-specification.yaml",
            yaml_content="""
            requirements: [{id: req-core}]
            acceptance_criteria: [{id: ac-core}]
            expected_tests: [{id: test-core}]
            """,
        ),
        effect_sources=(
            ProposedPRSourceDocument(
                path="proposals/feature/implementation-specification.yaml",
                yaml_content="features: [{id: feature-core, action: added}]",
            ),
        ),
    )

    assert result.issues == []


def test_service_reports_uncovered_ids_and_ordered_effect_mismatch() -> None:
    result = validate_proposed_pr_coverage_and_effects(
        """
        proposed_prs:
          - id: feature-core
            requirements: []
        features: [{id: feature-core, action: changed}]
        """,
        proposed_pr_ids=("feature-core",),
        feature_coverage_source=ProposedPRSourceDocument(
            path="feature-pr-specification.yaml",
            yaml_content="requirements: [{id: req-core}]",
        ),
        effect_sources=(
            ProposedPRSourceDocument(
                path="implementation-specification.yaml",
                yaml_content="features: [{id: feature-core, action: added}]",
            ),
        ),
    )

    assert {issue.code for issue in result.issues} == {
        "uncovered_feature_items",
        "v1_effect_mismatch",
    }
    effect_issue = next(
        issue for issue in result.issues if issue.code == "v1_effect_mismatch"
    )
    assert "Expected ordered id/action pairs" in effect_issue.message
    assert effect_issue.path == "proposed_prs.features"


def test_service_distinguishes_unreadable_evidence_from_missing_evidence() -> None:
    result = validate_proposed_pr_coverage_and_effects(
        "proposed_prs: []",
        proposed_pr_ids=(),
        feature_coverage_source=ProposedPRSourceDocument(
            path="feature-pr-specification.yaml", read_error="permission denied"
        ),
        effect_sources=(
            ProposedPRSourceDocument(
                path="implementation-specification.yaml", read_error="permission denied"
            ),
        ),
    )

    assert [issue.code for issue in result.issues] == [
        "invalid_feature_coverage_source",
        "invalid_effect_source",
    ]


def test_service_validator_has_no_agent_imports() -> None:
    import ast
    from pathlib import Path

    module = (
        Path(__file__).parents[1]
        / "packages/service/src/powdrr_service/specifications/proposed_pr_coverage.py"
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
