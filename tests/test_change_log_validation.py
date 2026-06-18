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
        action: added

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


def test_validate_change_log_yaml_reports_success_for_version_two_changes(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = """
    version: 2
    change_id: 7
    title: Add review workflow metadata

    intent:
      problem: The changelog format needs richer per-change structure.
      goal: Capture files, entities, invariants, and guidance per hunk.

    decisions:
      - id: ADR-200
        summary: Introduce version 2 of the changelog schema.

    files:
      - path: src/app.py
        type: modified
        entities:
          - AppService
          - Cache
        span:
          start_line: 1
          end_line: 1
        summary: Add the review skill wiring.
        rationale: Keep the first hunk focused on the skill metadata.
      - path: tests/test_app.py
        type: modified
        entities:
          - TestSuite
        span:
          start_line: 1
          end_line: 2
        summary: Add the review workflow test.
        rationale: Keep the second hunk focused on the test harness.

    entities:
      - id: AppService
        type: Service
        action: added
      - id: TestSuite
        type: Test suite
        action: added

    entity_relationships: []

    invariants:
      - id: INV-001
        description: The app service remains available.
        action: added
        related:
          files:
            - src/app.py
          entities:
            - AppService
      - id: INV-002
        description: The app test remains present.
        action: added
        related:
          files:
            - tests/test_app.py
          entities:
            - TestSuite

    guidance:
      - id: GUID-001
        description: Keep the example command visible.
        action: added
        related:
          files:
            - src/app.py
          entities:
            - AppService
      - id: GUID-002
        description: Keep the test command visible.
        action: added
        related:
          files:
            - tests/test_app.py
          entities:
            - TestSuite
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


def test_validate_change_log_yaml_accepts_version_two_top_level_entities(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = """
    version: 2
    change_id: 7
    title: Add review workflow metadata

    intent:
      problem: The changelog format needs richer per-change structure.
      goal: Capture files, entities, invariants, and guidance per hunk.

    files:
      - path: src/app.py
        type: modified
        entities:
          - AppService
        span:
          start_line: 1
          end_line: 1
        summary: Add app code.
        rationale: Needed for the feature.
      - path: tests/test_app.py
        type: modified
        entities:
          - TestSuite
        span:
          start_line: 1
          end_line: 2
        summary: Add app test.
        rationale: Needed for the feature.

    entities:
      - id: LegacyTopLevelEntity
        type: Service
        action: added
      - id: AppService
        type: Service
        action: added
      - id: TestSuite
        type: Test suite
        action: added

    entity_relationships: []

    invariants: []
    guidance: []
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


def test_validate_change_log_yaml_rejects_version_two_changes(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = """
    version: 2
    change_id: 7
    title: Add review workflow metadata

    intent:
      problem: The changelog format needs richer per-change structure.
      goal: Capture files, entities, invariants, and guidance per hunk.

    changes:
      - files:
          - path: src/app.py
            type: modified
            entities:
              - AppService
            span:
              start_line: 1
              end_line: 1
            summary: Add app code.
            rationale: Needed for the feature.
        entities:
          - id: AppService
            type: Service
            action: added
        invariants: []
        guidance: []
    """

    report = parse_validation_report(
        validate_change_log_yaml(
            proposed_yaml,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert [issue.code for issue in report.issues] == ["top_level_changes_not_allowed"]


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

    entities:
      - id: AppService
        type: Service
        action: added

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


def test_validate_change_log_yaml_allows_hunks_in_any_order(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_multi_hunk_feature_branch(tmp_path)
    proposed_yaml = """
    version: 1
    change_id: 7
    title: Sparse application changes

    intent:
      problem: The application should skip a bad line and append a new one.
      goal: Preserve the change in a narrow file span.

    decisions:
      - id: ADR-101
        summary: Keep the file entry scoped to the changed lines.

    entities:
      - id: AppService
        type: Service
        action: added

    changes:
      - file: src/app.py
        span:
          start_line: 8
          end_line: 8
        summary: Update the fourth line.
        affects:
          - AppService
        rationale: Needed for the feature.
      - file: src/app.py
        span:
          start_line: 2
          end_line: 2
        summary: Update the first line.
        affects:
          - AppService
        rationale: Needed for the feature.
      - file: src/app.py
        span:
          start_line: 6
          end_line: 6
        summary: Update the third line.
        affects:
          - AppService
        rationale: Needed for the feature.
      - file: src/app.py
        span:
          start_line: 4
          end_line: 4
        summary: Update the second line.
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


def test_validate_change_log_yaml_reports_missing_hunk_entries(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_multi_hunk_feature_branch(tmp_path)
    proposed_yaml = """
    version: 1
    change_id: 7
    title: Sparse application changes

    intent:
      problem: The application should update four separate lines.
      goal: Preserve each diff hunk as a distinct changelog entry.

    decisions:
      - id: ADR-101
        summary: Keep the file entry scoped to the changed lines.

    entities:
      - id: AppService
        type: Service
        action: added

    changes:
      - file: src/app.py
        span:
          start_line: 2
          end_line: 2
        summary: Update the first line.
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
    assert sum(issue.code == "missing_change" for issue in report.issues) == 3
    assert all(issue.path == "src/app.py" for issue in report.issues)


def test_validate_change_log_yaml_ignores_changelog_artifact(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-99-changelog.yaml").write_text(
        "version: 1\nchange_id: 99\ntitle: Artifact only\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "docs/changelogs/PR-99-changelog.yaml")
    _git(repo_root, "commit", "-m", "Add artifact changelog")

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
        action: added

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
    assert report.expected_change_files == ["src/app.py", "tests/test_app.py"]
    assert report.issues == []


def test_validate_change_log_yaml_rejects_unknown_relationship_entities(
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

    entities:
      - id: AppService
        type: Service
        action: added

    changes:
      - file: src/app.py
        span:
          start_line: 1
          end_line: 1
        summary: Add app code
        affects:
          - AppService
        rationale: Needed for the feature.

    relationship_changes:
      - action: add
        source: AppService
        target: RedisCache
        relationship: stores_sessions
        rationale: This relationship is not declared in the file entities section.
    """

    report = parse_validation_report(
        validate_change_log_yaml(
            proposed_yaml,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert any(issue.code == "relationship_unknown_entity" for issue in report.issues)


def test_validate_change_log_yaml_requires_new_entities_to_be_marked_added(
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
    """

    report = parse_validation_report(
        validate_change_log_yaml(
            proposed_yaml,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert any(issue.code == "entity_missing_from_parent" for issue in report.issues)


def test_validate_change_log_yaml_rejects_added_entities_that_already_exist(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_entity_history(tmp_path)
    proposed_yaml = """
    version: 1
    change_id: 8
    title: Reuse application service

    intent:
      problem: The shared service already exists.
      goal: Do not mark an existing entity as new.

    entities:
      - id: AppService
        type: Service
        action: added

    changes:
      - file: src/app.py
        span:
          start_line: 2
          end_line: 2
        summary: Update the service usage.
        affects:
          - AppService
        rationale: The existing service is still relevant.
    """

    report = parse_validation_report(
        validate_change_log_yaml(
            proposed_yaml,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert any(issue.code == "entity_already_exists" for issue in report.issues)


def test_validate_change_log_yaml_allows_existing_entities_without_action(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_entity_history(tmp_path)
    proposed_yaml = """
    version: 1
    change_id: 8
    title: Reuse application service

    intent:
      problem: The shared service already exists.
      goal: Reference the existing service without redefining it.

    entities:
      - id: AppService
        type: Service

    changes:
      - file: src/app.py
        span:
          start_line: 2
          end_line: 2
        summary: Update the service usage.
        affects:
          - AppService
        rationale: The existing service is still relevant.
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


def test_validate_change_log_yaml_requires_non_added_entities_to_exist_in_parent(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = """
    version: 1
    change_id: 8
    title: Reuse application service

    intent:
      problem: The shared service does not exist in the parent graph.
      goal: Ensure non-added entities must already exist.

    entities:
      - id: BrandNewService
        type: Service

    changes:
      - file: src/app.py
        span:
          start_line: 2
          end_line: 2
        summary: Update the service usage.
        affects:
          - BrandNewService
        rationale: The entity is not new according to the changelog.
    """

    report = parse_validation_report(
        validate_change_log_yaml(
            proposed_yaml,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert any(issue.code == "entity_missing_from_parent" for issue in report.issues)


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


def test_validate_change_log_yaml_rejects_instruction_comments(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = """
    # Keep this file valid YAML.
    version: 1
    change_id: 7
    title: Add application files

    intent:
      problem: Missing files
      goal: Ship the new files

    entities:
      - id: AppService
        type: Service
        action: added

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
    assert report.issues[0].code == "instructions_not_removed"


def test_validate_change_log_yaml_rejects_null_entity_actions(
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

    entities:
      - id: AppService
        type: Service
        action: null

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
    assert report.issues[0].code == "entity_action_null_not_allowed"


def test_validate_change_log_yaml_rejects_unknown_added_entity_types(
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

    entities:
      - id: AppService
        type: NotARealType
        action: added

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
    assert any(issue.code == "entity_type_not_allowed" for issue in report.issues)


def test_validate_change_log_yaml_rejects_unknown_added_entity_types_in_v2(
    tmp_path: Path,
) -> None:
    repo_root = _create_repo_with_feature_branch(tmp_path)
    proposed_yaml = """
    version: 2
    change_id: 7
    title: Add review workflow metadata

    intent:
      problem: The changelog format needs richer per-change structure.
      goal: Capture files, entities, invariants, and guidance per hunk.

    files:
      - path: src/app.py
        type: modified
        entities:
          - AppService
        span:
          start_line: 1
          end_line: 1
        summary: Add app code.
        rationale: Needed for the feature.

    entities:
      - id: AppService
        type: NotARealType
        action: added

    entity_relationships: []

    invariants: []
    guidance: []
    """

    report = parse_validation_report(
        validate_change_log_yaml(
            proposed_yaml,
            branch_name="feature/change-log",
            repo_root=repo_root,
        )
    )

    assert report.validation_successful is False
    assert any(issue.code == "entity_type_not_allowed" for issue in report.issues)


def _create_repo_with_feature_branch(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "\n".join(
            [
                "line 1",
                "line 2",
                "line 3",
                "line 4",
                "line 5",
                "line 6",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "README.md")
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Initial commit")
    _write_taxonomy_file(repo_root)

    _git(repo_root, "checkout", "-b", "feature/change-log")
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo_root / "tests").mkdir()
    (repo_root / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py", "tests/test_app.py")
    _git(repo_root, "commit", "-m", "Add application files")

    return repo_root


def _create_repo_with_entity_history(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _write_taxonomy_file(repo_root)
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "changelogs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "changelogs" / "PR-1-changelog.yaml").write_text(
        """
        version: 1
        change_id: 1
        title: Introduce AppService

        intent:
          problem: AppService does not exist.
          goal: Add the shared service to the graph.

        entities:
          - id: AppService
            type: Service
            action: added

        changes:
          - file: src/app.py
            span:
              start_line: 1
              end_line: 1
            summary: Introduce the shared service.
            affects:
              - AppService
            rationale: AppService is part of the baseline graph.
        """,
        encoding="utf-8",
    )
    _git(repo_root, "add", "README.md")
    _git(repo_root, "add", "src/app.py", "docs/changelogs/PR-1-changelog.yaml")
    _git(repo_root, "commit", "-m", "Introduce AppService (#1)")

    _git(repo_root, "checkout", "-b", "feature/change-log")
    (repo_root / "src" / "app.py").write_text(
        "print('hello')\nprint('world')\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Reuse AppService (#8)")

    return repo_root


def _write_taxonomy_file(repo_root: Path) -> None:
    taxonomy_source = Path(__file__).resolve().parents[1] / (
        "software_development_entity_taxonomy.md"
    )
    (repo_root / "software_development_entity_taxonomy.md").write_text(
        taxonomy_source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _create_repo_with_sparse_feature_branch(tmp_path: Path) -> Path:
    repo_root = _create_repo_with_multi_hunk_feature_branch(tmp_path)
    return repo_root


def _create_repo_with_multi_hunk_feature_branch(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")

    (repo_root / "README.md").write_text("initial\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text(
        "\n".join(
            [
                "line 1",
                "line 2",
                "line 3",
                "line 4",
                "line 5",
                "line 6",
                "line 7",
                "line 8",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", "README.md")
    _git(repo_root, "add", "src/app.py")
    _git(repo_root, "commit", "-m", "Initial commit")
    _write_taxonomy_file(repo_root)

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
    _git(repo_root, "commit", "-m", "Sparse application changes")

    return repo_root


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
