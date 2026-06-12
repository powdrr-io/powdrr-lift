from __future__ import annotations

import subprocess
from pathlib import Path

from powdrr_lift import parse_validation_report, validate_change_log_yaml


def test_validate_change_log_yaml_reports_success_when_changes_match(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = """
    version: 1
    change_id: 7
    title: Add application files

    intent:
      problem: Missing files
      goal: Ship the new files

    decisions:
      - id: ADR-100
        summary: Add files directly

    entities:
      - id: AppService
        type: Service

    changes:
      - file: src/app.py
        span:
          start_line: 1
          end_line: 1
        summary: Add app code
        affects:
          - AppService
        rationale: Needed for the feature.
      - file: tests/test_app.py
        span:
          start_line: 1
          end_line: 2
        summary: Add app test
        affects:
          - AppService
        rationale: Needed for the feature.
    """

    report = parse_validation_report(
        validate_change_log_yaml(
            proposed_yaml,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is True
    assert report.issues == []
    assert report.expected_change_files == ["src/app.py", "tests/test_app.py"]
    assert report.proposed_change_files == ["src/app.py", "tests/test_app.py"]


def test_validate_change_log_yaml_reports_missing_changes(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = """
    version: 1
    change_id: 7
    title: Add application files

    intent:
      problem: Missing files
      goal: Ship the new files

    decisions:
      - id: ADR-100
        summary: Add files directly

    changes:
      - file: src/app.py
        span:
          start_line: 1
          end_line: 1
        summary: Add app code
        affects:
          - AppService
        rationale: Needed for the feature.
    """

    report = parse_validation_report(
        validate_change_log_yaml(
            proposed_yaml,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert report.expected_change_files == ["src/app.py", "tests/test_app.py"]
    assert report.proposed_change_files == ["src/app.py"]
    assert report.issues[0].code == "missing_change"
    assert report.issues[0].path == "tests/test_app.py"


def test_validate_change_log_yaml_reports_invalid_yaml(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)

    report = parse_validation_report(
        validate_change_log_yaml(
            "changes: [",
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert report.issues[0].code == "invalid_yaml"


def test_validate_change_log_yaml_rejects_remaining_comments(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)

    report = parse_validation_report(
        validate_change_log_yaml(
            """
            # Remove this comment before submitting.
            version: 1
            change_id: 7
            title: Add application files

            changes:
              - file: src/app.py
                span:
                  start_line: 1
                  end_line: 1
                summary: Add app code
              - file: tests/test_app.py
                span:
                  start_line: 1
                  end_line: 2
                summary: Add app test
            """,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert report.issues[0].code == "instructions_not_removed"


def test_validate_change_log_yaml_ignores_pr_changelog_artifact(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    changelog_path = repo_root / "docs" / "changelogs" / "PR-7-changelog.yaml"
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_path.write_text(
        "version: 1\nchange_id: 7\ntitle: Add application files\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "docs/changelogs/PR-7-changelog.yaml")
    _git(repo_root, "commit", "-m", "Add PR changelog artifact")

    report = parse_validation_report(
        validate_change_log_yaml(
            """
            version: 1
            change_id: 7
            title: Add application files

            changes:
              - file: src/app.py
                span:
                  start_line: 1
                  end_line: 1
                summary: Add app code
              - file: tests/test_app.py
                span:
                  start_line: 1
                  end_line: 2
                summary: Add app test
            """,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is True
    assert report.expected_change_files == ["src/app.py", "tests/test_app.py"]


def _create_repo_with_feature_branch(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    _git(repo_root, "checkout", "-b", "feature/change-log")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo_root / "tests").mkdir()
    (repo_root / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "tests/test_app.py")
    _git(repo_root, "commit", "-m", "Add application files")

    return repo_root


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
