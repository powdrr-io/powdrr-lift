from __future__ import annotations

import subprocess
from pathlib import Path

from powdrr_lift import Decision, create_change_log_template, parse_change_log


def test_create_change_log_template_uses_branch_diff(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    _git(repo_root, "checkout", "-b", "feature/empty")
    _git(repo_root, "checkout", "main")
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

    output_path = create_change_log_template(
        branch_name="feature/change-log",
        output_path=tmp_path / "change-log.template.yaml",
        repo_root=repo_root,
    )

    template_text = output_path.read_text(encoding="utf-8")
    assert "branch `feature/change-log`" in template_text
    assert "Compared against default branch `main`." in template_text
    assert "version: 2" in template_text
    assert "change_id: null" in template_text
    assert "decisions:" in template_text
    assert "files:" in template_text
    assert "entities:" in template_text
    assert "entity_relationships:" in template_text
    assert "invariants:" in template_text
    assert "guidance:" in template_text
    assert "    related:" in template_text
    assert "Remove this block entirely if it does not point" in template_text
    assert "to anything." in template_text
    assert "A src/app.py" in template_text
    assert "A tests/test_app.py" in template_text

    change_log = parse_change_log(template_text)
    assert change_log.version == 2
    assert [change.path for change in change_log.file_changes] == [
        "src/app.py",
        "tests/test_app.py",
    ]
    assert [change.span.start_line for change in change_log.file_changes] == [1, 1]
    assert [change.span.end_line for change in change_log.file_changes] == [1, 2]
    assert [change.type for change in change_log.file_changes] == [
        "added",
        "added",
    ]
    assert [change.entities for change in change_log.file_changes] == [[], []]
    assert [change.related.entities for change in change_log.file_changes] == [
        [],
        [],
    ]
    assert change_log.entity_changes == []
    assert change_log.decisions == [Decision()]


def test_create_change_log_template_populates_full_related_sections(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "print('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    (repo_root / "tests").mkdir()
    (repo_root / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert True\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 1
        change_id: 1
        title: Add application scaffold

        intent:
          problem: The repository had no application entry point.
          goal: Introduce the initial application and test files.

        entities:
          - id: AppService
            type: Component
            action: added

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 2
            summary: Add the application scaffold.
            affects:
              - AppService
            rationale: Bootstrap the app file.
          - file: tests/test_app.py
            span:
              start_line: 1
              end_line: 2
            summary: Add the initial test scaffold.
            affects:
              - AppService
            rationale: Cover the app file.
        """,
        encoding="utf-8",
    )
    _git(
        repo_root,
        "add",
        "src/app.py",
        "tests/test_app.py",
        "docs/changelogs/PR-1-changelog.yaml",
    )
    _git(repo_root, "commit", "-m", "Add application scaffold")

    _git(repo_root, "checkout", "-b", "feature/change-log")
    (repo_root / "src" / "app.py").write_text(
        "print('hello')\nprint('world v2')\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert True is True\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "tests/test_app.py")
    _git(repo_root, "commit", "-m", "Update application scaffold")

    output_path = create_change_log_template(
        branch_name="feature/change-log",
        output_path=tmp_path / "change-log.template.yaml",
        repo_root=repo_root,
    )

    template_text = output_path.read_text(encoding="utf-8")
    assert "Review each prefilled `related` section" in template_text

    change_log = parse_change_log(template_text)
    assert [change.path for change in change_log.file_changes] == [
        "src/app.py",
        "tests/test_app.py",
    ]
    assert change_log.file_changes[0].related.files == []
    assert change_log.file_changes[1].related.files == []
    assert change_log.file_changes[0].related.entities == ["AppService"]
    assert change_log.file_changes[1].related.entities == ["AppService"]
    assert change_log.file_changes[0].related.invariants == []
    assert change_log.file_changes[0].related.guidance == []


def test_create_change_log_template_handles_empty_diff(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")
    _git(repo_root, "checkout", "-b", "feature/empty")

    output_path = create_change_log_template(
        branch_name="feature/empty",
        output_path=tmp_path / "empty.template.yaml",
        repo_root=repo_root,
        default_branch="main",
    )

    change_log = parse_change_log(output_path.read_text(encoding="utf-8"))
    assert change_log.file_changes == []
    assert change_log.entity_changes == []
    assert change_log.decisions == [Decision()]
    assert change_log.version == 2


def test_create_change_log_template_ignores_rename_only_diff(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo_root, "add", "README.md", "src/app.py")
    _git(repo_root, "commit", "-m", "Initial commit")

    _git(repo_root, "checkout", "-b", "feature/change-log")
    (repo_root / "src" / "renamed.py").parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "mv", "src/app.py", "src/renamed.py")
    _git(repo_root, "commit", "-m", "Rename app file")

    output_path = create_change_log_template(
        branch_name="feature/change-log",
        output_path=tmp_path / "rename-only.template.yaml",
        repo_root=repo_root,
        default_branch="main",
    )

    change_log = parse_change_log(output_path.read_text(encoding="utf-8"))
    assert change_log.file_changes == []


def test_create_change_log_template_tracks_sparse_spans(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    sparse_source = "\n".join(f"line {index}" for index in range(1, 9)) + "\n"
    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(sparse_source, encoding="utf-8")
    _git(repo_root, "add", "README.md", "src/app.py")
    _git(repo_root, "commit", "-m", "Initial commit")

    _git(repo_root, "checkout", "-b", "feature/change-log")
    (repo_root / "src" / "app.py").write_text(
        "\n".join(
            [
                "line 1",
                "line 2 updated",
                "line 3",
                "line 4 updated",
                "line 5",
                "line 6 updated",
                "line 7",
                "line 8 updated",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Sparse app edit")

    output_path = create_change_log_template(
        branch_name="feature/change-log",
        output_path=tmp_path / "sparse.template.yaml",
        repo_root=repo_root,
        default_branch="main",
    )

    change_log = parse_change_log(output_path.read_text(encoding="utf-8"))
    assert [change.path for change in change_log.file_changes] == [
        "src/app.py",
        "src/app.py",
        "src/app.py",
        "src/app.py",
    ]
    assert [file_entry.path for file_entry in change_log.file_changes] == [
        "src/app.py",
        "src/app.py",
        "src/app.py",
        "src/app.py",
    ]
    assert [file_entry.span.start_line for file_entry in change_log.file_changes] == [
        2,
        4,
        6,
        8,
    ]
    assert [file_entry.span.end_line for file_entry in change_log.file_changes] == [
        2,
        4,
        6,
        8,
    ]


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
