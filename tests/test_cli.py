from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

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
    with redirect_stdout(stdout):
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
