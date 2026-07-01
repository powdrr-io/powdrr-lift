from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import build_pr_specification_validation_report
from powdrr_lift.cli import main
from powdrr_lift.core import (
    pr_specification_default_output_path,
    search_proposed_pr_specifications,
    show_proposed_pr_specification,
)


def _write_implementation_specification(repo_root: Path) -> Path:
    implementation_specification_path = (
        repo_root
        / "docs"
        / "specs"
        / "powdrr-lift"
        / "implementation-specification.yaml"
    )
    implementation_specification_path.parent.mkdir(parents=True, exist_ok=True)
    implementation_specification_path.write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        architecture_id: 2026-06-19
        features:
          - id: feature-a
            description: Add the first feature.
            functional_requirements:
              - Implement the first behavior.
          - id: feature-b
            description: Add the second feature.
            functional_requirements:
              - Implement the second behavior.
        """,
        encoding="utf-8",
    )
    return implementation_specification_path


def _write_existing_pr_specification(repo_root: Path) -> Path:
    pr_specification_path = (
        repo_root / "docs" / "specs" / "PR-123" / "proposed-pr-specification.yaml"
    )
    pr_specification_path.parent.mkdir(parents=True, exist_ok=True)
    pr_specification_path.write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: pr-123
        feature_ids:
          - feature-a
        intent:
          problem: Existing PR spec.
          goal: Existing PR spec.
          reasoning: Make sure ids are unique.
        """,
        encoding="utf-8",
    )
    return pr_specification_path


def _write_proposed_pr_specification(
    repo_root: Path,
    pr_number: int,
    *,
    proposed_pr_id: str,
    feature_id: str,
    goal: str,
    reasoning: str,
) -> Path:
    proposal_path = (
        repo_root
        / "docs"
        / "proposals"
        / f"PR-{pr_number}-proposed-pr-specification.yaml"
    )
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(
        f"""
        schema: https://powdrr.io/schemas/proposed-pr-specification-v1
        id: {proposed_pr_id}
        feature_ids:
          - {feature_id}
        intent:
          problem: {goal}
          goal: {goal}
          reasoning: {reasoning}
        acceptance_criteria:
          - id: ac-{pr_number}
            description: Acceptance criteria {pr_number}.
        expected_tests:
          - id: test-{pr_number}
            description: Expected tests {pr_number}.
        required_test_cases:
          - id: rtc-{pr_number}
            description: Required test case {pr_number}.
        expected_outcomes:
          - id: outcome-{pr_number}
            description: Expected outcome {pr_number}.
        non_goals:
          - id: ng-{pr_number}
            description: Non-goal {pr_number}.
        risks:
          - id: risk-{pr_number}
            description: Risk {pr_number}.
        """,
        encoding="utf-8",
    )
    return proposal_path


def test_create_pr_specification_template_writes_default_file(tmp_path: Path) -> None:
    _write_implementation_specification(tmp_path)
    output_path = pr_specification_default_output_path("PR-456", tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "pr-specification",
                "--work-item-name",
                "PR-456",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# PR specification template." in template_text
    assert "# - feature-a" in template_text
    assert "# - feature-b" in template_text
    assert (
        "# - Delete these instructions and replace with a comment saying that"
        in template_text
    )
    assert "schema: https://powdrr.io/schemas/specification-v1" in template_text
    assert "id: null" in template_text
    assert "Fill in `intent.problem`, `intent.goal`, and `intent.reasoning`." in (
        template_text
    )

    rendered_template = yaml.safe_load(template_text)
    assert [section for section in rendered_template] == [
        "schema",
        "id",
        "feature_ids",
        "intent",
        "acceptance_criteria",
        "expected_tests",
        "required_test_cases",
        "expected_outcomes",
        "non_goals",
        "risks",
    ]


def test_validate_pr_specification_reports_errors(tmp_path: Path) -> None:
    _write_implementation_specification(tmp_path)
    _write_existing_pr_specification(tmp_path)
    proposed_spec = """
    version: 1
    id: pr-123

    feature_ids:
      - feature-a
      - feature-a
      - feature-missing

    intent:
      problem: Add a new capability.
      goal: Add a new capability.
      reasoning: Keep the repo aligned.

    acceptance_criteria:
      - id: ac-1
        description: Acceptance criteria one.
    expected_tests:
      - id: ac-1
        description: Expected test one.
    required_test_cases:
      - id: rtc-1
        description: Required test case one.
    expected_outcomes:
      - id: outcome-1
        description: Expected outcome one.
    non_goals:
      - id: ng-1
        description: Non-goal one.
    risks:
      - id: risk-1
        description: Risk one.
    """

    report = build_pr_specification_validation_report(
        proposed_spec,
        work_item_name="PR-123",
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert report.proposed_pr_id == "pr-123"
    assert report.available_feature_ids == ["feature-a", "feature-b"]
    assert report.known_pr_ids == []
    assert {issue.code for issue in report.issues} == {
        "duplicate_feature_id",
        "unknown_feature_id",
        "duplicate_detail_id",
    }


def test_validate_pr_specification_reports_success_for_valid_spec(
    tmp_path: Path,
) -> None:
    _write_implementation_specification(tmp_path)
    proposed_spec = """
    version: 1
    id: pr-456

    feature_ids:
      - feature-a
      - feature-b

    intent:
      problem: Add a new capability.
      goal: Add a new capability.
      reasoning: Keep the repo aligned.

    acceptance_criteria:
      - id: ac-1
        description: Acceptance criteria one.
    expected_tests:
      - id: test-1
        description: Expected test one.
    required_test_cases:
      - id: rtc-1
        description: Required test case one.
    expected_outcomes:
      - id: outcome-1
        description: Expected outcome one.
    non_goals:
      - id: ng-1
        description: Non-goal one.
    risks:
      - id: risk-1
        description: Risk one.
    """

    report = build_pr_specification_validation_report(
        proposed_spec,
        work_item_name="PR-456",
        repo_root=tmp_path,
    )

    assert report.validation_successful is True
    assert report.issues == []


def test_validate_pr_specification_allows_sparse_spec(tmp_path: Path) -> None:
    _write_implementation_specification(tmp_path)
    proposed_spec = """
    version: 1
    id: pr-789

    intent:
      problem: Add a new capability.
      goal: Add a new capability.
      reasoning: Keep the repo aligned.
    """

    report = build_pr_specification_validation_report(
        proposed_spec,
        work_item_name="PR-789",
        repo_root=tmp_path,
    )

    assert report.validation_successful is True
    assert report.issues == []


def test_validate_pr_specification_rejects_template_boilerplate(
    tmp_path: Path,
) -> None:
    _write_implementation_specification(tmp_path)
    proposed_spec = """
    # PR specification template.
    #
    # Instructions:
    # - Create one template per proposed PR.
    # - Set `id` to a globally unique proposed PR id.
    # - Reference one or more current feature ids from the codebase state
    #   listed below.
    # - Fill in `intent.problem`, `intent.goal`, and `intent.reasoning`.
    # - Delete these instructions and replace with a comment saying that
    #   this file is read-only and should never be editted by a tool or
    #   agent.
    # - Add acceptance criteria, expected tests, expected outcomes,
    #   required test cases, non-goals, and risks as concrete lists with `id` and
    #   `description`.
    #
    # Current feature ids:
    # - feature-a (feature, docs/specs/powdrr-lift/implementation-specification.yaml)
    id: pr-789
    feature_ids:
      - feature-a

    intent:
      problem: Add a new capability.
      goal: Add a new capability.
      reasoning: Keep the repo aligned.

    acceptance_criteria:
      - id: ac-1
        description: Acceptance criteria one.
    expected_tests:
      - id: test-1
        description: Expected test one.
    required_test_cases:
      - id: rtc-1
        description: Required test case one.
    expected_outcomes:
      - id: outcome-1
        description: Expected outcome one.
    non_goals:
      - id: ng-1
        description: Non-goal one.
    risks:
      - id: risk-1
        description: Risk one.
    """

    report = build_pr_specification_validation_report(
        proposed_spec,
        work_item_name="PR-456",
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "template_boilerplate_not_removed",
    }


def test_validate_pr_specification_rejects_missing_detail_description(
    tmp_path: Path,
) -> None:
    _write_implementation_specification(tmp_path)
    proposed_spec = """
    version: 1
    id: pr-790

    feature_ids:
      - feature-a

    intent:
      problem: Add a new capability.
      goal: Add a new capability.
      reasoning: Keep the repo aligned.

    acceptance_criteria:
      - id: ac-1
        description: Acceptance criteria one.
    expected_tests:
      - id: test-1
        description: Expected test one.
    required_test_cases:
      - id: rtc-1
        description: Required test case one.
    expected_outcomes:
      - id: outcome-1
        description: Expected outcome one.
    non_goals:
      - id: ng-1
        description: Non-goal one.
    risks:
      - id: risk-1
        description: ""
    """

    report = build_pr_specification_validation_report(
        proposed_spec,
        work_item_name="PR-456",
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "risks_description_missing",
    }


def test_cli_validate_pr_specification_reports_yaml(tmp_path: Path) -> None:
    _write_implementation_specification(tmp_path)
    spec_path = (
        tmp_path / "docs" / "specs" / "PR-456" / "proposed-pr-specification.yaml"
    )
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        """
        schema: https://powdrr.io/schemas/specification-v1
        id: pr-456

        feature_ids:
          - feature-a

        intent:
          problem: Add a new capability.
          goal: Add a new capability.
          reasoning: Keep the repo aligned.

        acceptance_criteria:
          - id: ac-1
            description: Acceptance criteria one.
        expected_tests:
          - id: test-1
            description: Expected test one.
        required_test_cases:
          - id: rtc-1
            description: Required test case one.
        expected_outcomes:
          - id: outcome-1
            description: Expected outcome one.
        non_goals:
          - id: ng-1
            description: Non-goal one.
        risks:
          - id: risk-1
            description: Risk one.
        """,
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "evaluate-pr-specification",
                "--work-item-name",
                "PR-456",
                "--repo-root",
                str(tmp_path),
                "--input",
                str(spec_path),
            ]
        )

    assert exit_code == 0
    report = yaml.safe_load(stdout.getvalue())
    assert report["validation_successful"] is True
    assert report["proposed_pr_id"] == "pr-456"


def test_search_proposed_pr_specifications_ranks_matching_results(
    tmp_path: Path,
) -> None:
    _write_implementation_specification(tmp_path)
    first_path = _write_proposed_pr_specification(
        tmp_path,
        40,
        proposed_pr_id="pr-40",
        feature_id="feature-a",
        goal="Introduce current-state and diff document families.",
        reasoning="This supports the document family proposal.",
    )
    _write_proposed_pr_specification(
        tmp_path,
        41,
        proposed_pr_id="pr-41",
        feature_id="feature-b",
        goal="Add synthesis workflows.",
        reasoning="This supports the synthesis proposal.",
    )

    report = search_proposed_pr_specifications(
        "document families",
        repo_root=tmp_path,
    )

    assert report.query == "document families"
    assert [result.pr_number for result in report.results][:1] == [40]
    assert report.results[0].path == first_path
    assert "intent.goal" in report.results[0].matched_fields


def test_show_proposed_pr_specification_returns_file_contents(
    tmp_path: Path,
) -> None:
    _write_implementation_specification(tmp_path)
    proposal_path = _write_proposed_pr_specification(
        tmp_path,
        42,
        proposed_pr_id="pr-42",
        feature_id="feature-a",
        goal="Add review workflows.",
        reasoning="This supports the review proposal.",
    )

    assert show_proposed_pr_specification(42, repo_root=tmp_path) == (
        proposal_path.read_text(encoding="utf-8")
    )


def test_cli_search_and_show_proposed_pr_specification(
    tmp_path: Path,
) -> None:
    _write_implementation_specification(tmp_path)
    _write_proposed_pr_specification(
        tmp_path,
        43,
        proposed_pr_id="pr-43",
        feature_id="feature-a",
        goal="Package the platform.",
        reasoning="This supports the packaging proposal.",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "search-proposed-prs",
                "packaging",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    search_report = yaml.safe_load(stdout.getvalue())
    assert search_report["results"][0]["pr_number"] == 43

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "show-proposed-pr",
                "43",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert "pr-43" in stdout.getvalue()
