from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from powdrr_lift import parse_change_log, parse_validation_report
from powdrr_lift.cli import main


def test_cli_init_writes_template(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    output_path = tmp_path / "change-log.template.yaml"

    with redirect_stdout(io.StringIO()) as stdout:
        exit_code = main(
            [
                "init",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--output",
                str(output_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    change_log = parse_change_log(output_path.read_text(encoding="utf-8"))
    assert [change.file for change in change_log.changes] == [
        "src/app.py",
        "tests/test_app.py",
    ]


def test_cli_init_uses_pr_changelog_path(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "init",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--pr-number",
                "123",
            ]
        )

    output_path = repo_root / "docs" / "changelogs" / "PR-123-changelog.yaml"
    assert exit_code == 0
    assert output_path.exists()
    assert "Next: fill out the template" in stdout.getvalue()
    assert "docs/changelogs/PR-123-changelog.yaml" in stdout.getvalue()


def test_cli_evaluate_reports_validation_failure(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = tmp_path / "proposed-change-log.yaml"
    proposed_yaml.write_text(
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
        """,
        encoding="utf-8",
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(
            [
                "evaluate-pr-against-changelog",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--input",
                str(proposed_yaml),
            ]
        )

    assert exit_code == 1
    report = parse_validation_report(stdout.getvalue())
    assert report.validation_successful is False
    assert report.issues[0].code == "missing_change"


def test_cli_evaluate_uses_pr_changelog_path(tmp_path: Path) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    changelog_path = repo_root / "docs" / "changelogs" / "PR-123-changelog.yaml"
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_path.write_text(
        """
        version: 1
        change_id: 123
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
        encoding="utf-8",
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(
            [
                "evaluate-pr-against-changelog",
                "feature/change-log",
                "--repo-root",
                str(repo_root),
                "--pr-number",
                "123",
            ]
        )

    assert exit_code == 0
    report = parse_validation_report(stdout.getvalue())
    assert report.validation_successful is True
    assert "Next: include docs/changelogs/PR-123-changelog.yaml in the PR." in (
        stderr.getvalue()
    )


def test_cli_blame_ui_invokes_local_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    captured: dict[str, object] = {}

    def _fake_serve_blame_ui(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("powdrr_lift.cli.serve_blame_ui", _fake_serve_blame_ui)

    exit_code = main(
        [
            "blame-ui",
            "feature/change-log",
            "--repo-root",
            str(repo_root),
            "--parent-branch",
            "main",
            "--file",
            "src/app.py",
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "repo_root": repo_root,
        "branch_name": "feature/change-log",
        "parent_branch": "main",
        "selected_file": "src/app.py",
        "host": "0.0.0.0",
        "port": 8123,
    }


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
