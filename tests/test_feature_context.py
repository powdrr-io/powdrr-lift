from __future__ import annotations

from pathlib import Path

from powdrr_lift.workrr.feature_context import resolve_feature_context


def test_resolver_selects_exact_normalized_feature_and_derives_documents(
    tmp_path: Path,
) -> None:
    proposal = tmp_path / "docs/proposals/GraphQL-Cache"
    current = tmp_path / "docs/current/graphql_cache"
    workflow = tmp_path / "docs/workflows/GraphQL-Cache"
    proposal.mkdir(parents=True)
    current.mkdir(parents=True)
    workflow.mkdir(parents=True)
    (proposal / "system-specification.yaml").write_text("system: proposal\n")
    (current / "implementation-specification.yaml").write_text(
        "implementation: current\n"
    )
    (workflow / "task.yaml").write_text("name: task\n")

    result = resolve_feature_context("graphql cache", tmp_path)

    assert result["status"] == "selected"
    assert result["feature_name"] == "GraphQL-Cache"
    assert result["proposal_directory"] == "docs/proposals/GraphQL-Cache"
    assert result["current_directory"] == "docs/current/graphql_cache"
    assert result["specification_documents"] == [
        "docs/current/graphql_cache/implementation-specification.yaml",
        "docs/proposals/GraphQL-Cache/system-specification.yaml",
    ]
    assert result["workflow_documents"] == ["docs/workflows/GraphQL-Cache/task.yaml"]
    assert all(path.endswith(".yaml") for path in result["missing_documents"])


def test_resolver_reports_ambiguous_best_feature_matches(tmp_path: Path) -> None:
    (tmp_path / "docs/proposals/graphql-client").mkdir(parents=True)
    (tmp_path / "docs/current/graphql-server").mkdir(parents=True)

    result = resolve_feature_context("graphql", tmp_path)

    assert result["status"] == "ambiguous"
    assert result["feature_name"] is None
    assert result["candidate_names"] == ["graphql-client", "graphql-server"]


def test_resolver_reports_missing_feature_without_guessing(tmp_path: Path) -> None:
    result = resolve_feature_context("brand new feature", tmp_path)

    assert result["status"] == "not_found"
    assert result["feature_name"] is None
    assert result["candidate_names"] == []
