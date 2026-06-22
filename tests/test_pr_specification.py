from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift import build_pr_specification_validation_report
from powdrr_lift.cli import main
from powdrr_lift.core import pr_specification_default_output_path


def _write_implementation_specification(repo_root: Path) -> Path:
    implementation_specification_path = (
        repo_root / "docs" / "implementation" / "implementation-specification.yaml"
    )
    implementation_specification_path.parent.mkdir(parents=True, exist_ok=True)
    implementation_specification_path.write_text(
        """
        version: 1
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
    pr_specification_path = repo_root / "docs" / "prs" / "PR-123-specification.yaml"
    pr_specification_path.parent.mkdir(parents=True, exist_ok=True)
    pr_specification_path.write_text(
        """
        version: 1
        id: pr-123
        feature_ids:
          - feature-a
        intent:
          goal: Existing PR spec.
          reasoning: Make sure ids are unique.
        """,
        encoding="utf-8",
    )
    return pr_specification_path


def test_create_pr_specification_template_writes_default_file(tmp_path: Path) -> None:
    _write_implementation_specification(tmp_path)
    output_path = pr_specification_default_output_path(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "pr-specification",
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
    assert "Delete these instructions when you are done." in template_text
    assert "id: null" in template_text

    rendered_template = yaml.safe_load(template_text)
    assert [section for section in rendered_template] == [
        "id",
        "feature_ids",
        "intent",
        "acceptance_criteria",
        "expected_tests",
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
      goal: Add a new capability.
      reasoning: Keep the repo aligned.

    acceptance_criteria:
      - id: ac-1
        description: Acceptance criteria one.
    expected_tests:
      - id: ac-1
        description: Expected test one.
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
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert report.proposed_pr_id == "pr-123"
    assert report.available_feature_ids == ["feature-a", "feature-b"]
    assert report.known_pr_ids == ["pr-123"]
    assert {issue.code for issue in report.issues} == {
        "duplicate_proposed_pr_id",
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
      goal: Add a new capability.
      reasoning: Keep the repo aligned.

    acceptance_criteria:
      - id: ac-1
        description: Acceptance criteria one.
    expected_tests:
      - id: test-1
        description: Expected test one.
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
    # - Fill in `intent.goal` and `intent.reasoning`.
    # - Delete these instructions when you are done.
    # - Add acceptance criteria, expected tests, expected outcomes,
    #   non-goals, and risks as concrete lists with `id` and
    #   `description`.
    #
    # Current feature ids:
    # - feature-a (feature, docs/implementation/implementation-specification.yaml)
    id: pr-789
    feature_ids:
      - feature-a

    intent:
      goal: Add a new capability.
      reasoning: Keep the repo aligned.

    acceptance_criteria:
      - id: ac-1
        description: Acceptance criteria one.
    expected_tests:
      - id: test-1
        description: Expected test one.
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
      goal: Add a new capability.
      reasoning: Keep the repo aligned.

    acceptance_criteria:
      - id: ac-1
        description: Acceptance criteria one.
    expected_tests:
      - id: test-1
        description: Expected test one.
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
        repo_root=tmp_path,
    )

    assert report.validation_successful is False
    assert {issue.code for issue in report.issues} == {
        "risks_description_missing",
    }


def test_cli_validate_pr_specification_reports_yaml(tmp_path: Path) -> None:
    _write_implementation_specification(tmp_path)
    spec_path = tmp_path / "pr-specification.yaml"
    spec_path.write_text(
        """
        version: 1
        id: pr-456

        feature_ids:
          - feature-a

        intent:
          goal: Add a new capability.
          reasoning: Keep the repo aligned.

        acceptance_criteria:
          - id: ac-1
            description: Acceptance criteria one.
        expected_tests:
          - id: test-1
            description: Expected test one.
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
