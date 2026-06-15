from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import powdrr_lift.core.index as core_index
from powdrr_lift import build_changelog_index


def test_build_changelog_index_tracks_lineage_across_later_insertions(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "print('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 1
        change_id: 1
        title: Add application scaffold

        intent:
          problem: The repository had no application entry point.
          goal: Introduce the initial application file.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 2
            summary: Add the initial application body.
            affects: []
            rationale: Bootstrap the app file.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-1-changelog.yaml")
    _git(repo_root, "commit", "-m", "Add application scaffold (#1)")

    (repo_root / "src" / "app.py").write_text(
        "print('intro')\nprint('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "changelogs" / "PR-2-changelog.yaml").write_text(
        """
        version: 1
        change_id: 2
        title: Add app intro

        intent:
          problem: The application output should start with a short intro.
          goal: Prepend an intro line to the app.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Insert an intro line.
            affects: []
            rationale: Make the output start with an intro.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-2-changelog.yaml")
    _git(repo_root, "commit", "-m", "Add app intro (#2)")

    index = build_changelog_index(repo_root)

    assert index.documents[0].commit_sha is not None
    assert index.documents[1].commit_sha is not None
    assert index.provenance_for("src/app.py", 1).pr_number == 2
    assert index.provenance_for("src/app.py", 1).kind == "declared"
    assert index.provenance_for("src/app.py", 2).pr_number == 1
    assert (
        index.provenance_for("src/app.py", 2).summary
        == "Add the initial application body."
    )
    assert index.provenance_for("src/app.py", 3).pr_number == 1
    assert index.provenance_for("src/app.py", 3).rationale == "Bootstrap the app file."
    assert (
        index.provenance_for("docs/changelogs/PR-1-changelog.yaml", 1).kind
        == "artifact"
    )
    assert index.provenance_for("docs/changelogs/PR-1-changelog.yaml", 1).pr_number == 1


def test_build_changelog_index_uses_pr_description_when_changelog_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "print('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Add application scaffold (#11)")

    def _fake_fetch_pr_metadata(
        repo_root_arg: Path,
        *gh_args: str,
    ) -> dict[str, object]:
        assert repo_root_arg == repo_root
        assert gh_args == ("view", "11")
        return {
            "number": 11,
            "title": "Add application scaffold",
            "body": "Introduce the app file without a changelog artifact.",
        }

    monkeypatch.setattr(core_index, "_fetch_pr_metadata", _fake_fetch_pr_metadata)

    index = build_changelog_index(repo_root)

    assert index.documents[0].changelog_path.name == "PR-11-description.md"
    assert index.provenance_for("src/app.py", 1).pr_number == 11
    assert index.provenance_for("src/app.py", 1).title == "Add application scaffold"
    assert (
        index.provenance_for("src/app.py", 1).intent_goal
        == "Introduce the app file without a changelog artifact."
    )


def test_build_changelog_index_uses_commit_body_when_pr_is_missing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo_root, "add", "src/app.py")
    _git(
        repo_root,
        "commit",
        "-m",
        "Add helper",
        "-m",
        "Track intent in the commit comment.",
    )

    index = build_changelog_index(repo_root)

    provenance = index.provenance_for("src/app.py", 1)
    assert provenance is not None
    assert provenance.kind == "commented"
    assert provenance.title == "Add helper"
    assert provenance.intent_problem == "Add helper"
    assert provenance.intent_goal == "Track intent in the commit comment."
    assert provenance.rationale == "Track intent in the commit comment."


def test_build_changelog_index_normalizes_entities_and_relationships(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 1
        change_id: 1
        title: Introduce Foo

        intent:
          problem: Foo does not exist.
          goal: Add Foo to the graph.

        entities:
          - id: foo
            type: Service
            action: added

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Introduce Foo.
            affects: [foo]
            rationale: Foo is now part of the system.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-1-changelog.yaml")
    _git(repo_root, "commit", "-m", "Introduce Foo (#1)")

    (repo_root / "src" / "app.py").write_text(
        "print('hello again')\n", encoding="utf-8"
    )
    (repo_root / "docs" / "changelogs" / "PR-2-changelog.yaml").write_text(
        """
        version: 1
        change_id: 2
        title: Connect Foo to Bar

        intent:
          problem: Foo needs to talk to Bar.
          goal: Model the dependency between the shared entities.

        entities:
          - id: foo
            type: Service
          - id: bar
            type: Cache
            action: added

        relationship_changes:
          - action: add
            source: foo
            target: bar
            relationship: reads_from
            rationale: Foo depends on Bar.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Wire Foo to Bar.
            affects: [foo, bar]
            rationale: Foo now uses Bar.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-2-changelog.yaml")
    _git(repo_root, "commit", "-m", "Connect Foo to Bar (#2)")

    index = build_changelog_index(repo_root)

    assert set(index.entity_graph.entities) == {"bar", "foo"}
    assert len(index.entity_graph.entities["foo"]) == 2
    assert index.entity_graph.entities["foo"][0].action == "added"
    assert index.entity_graph.entities["foo"][1].action is None
    assert index.entity_graph.relationships[0].source == "foo"
    assert index.entity_graph.relationships[0].target == "bar"


def test_build_changelog_index_normalizes_version_two_changes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 2
        change_id: 1
        title: Introduce ReviewSkill

        intent:
          problem: ReviewSkill does not exist.
          goal: Add the skill to the graph.

        changes:
          - files:
              - path: src/app.py
                type: modified
            entities:
              - id: ReviewSkill
                type: Skill
                action: added
            invariants: []
            guidance: []
            span:
              start_line: 1
              end_line: 1
            summary: Introduce ReviewSkill.
            rationale: ReviewSkill is now part of the system.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-1-changelog.yaml")
    _git(repo_root, "commit", "-m", "Introduce ReviewSkill (#1)")

    index = build_changelog_index(repo_root)

    assert index.documents[0].changelog.version == 2
    assert index.entity_graph.entities["ReviewSkill"][0].action == "added"
    assert index.provenance_for("src/app.py", 1).affects == ("ReviewSkill",)


def test_build_changelog_index_skips_relationships_for_undeclared_entities(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "Initial commit")

    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 1
        change_id: 1
        title: Introduce Foo

        intent:
          problem: Foo does not exist.
          goal: Add Foo to the graph.

        entities:
          - id: foo
            type: Service
            action: added

        relationship_changes:
          - action: add
            source: foo
            target: bar
            relationship: reads_from
            rationale: This edge is invalid because bar is not declared.

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Introduce Foo.
            affects: [foo]
            rationale: Foo is now part of the system.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-1-changelog.yaml")
    _git(repo_root, "commit", "-m", "Introduce Foo (#1)")

    index = build_changelog_index(repo_root)

    assert set(index.entity_graph.entities) == {"foo"}
    assert index.entity_graph.relationships == ()


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
