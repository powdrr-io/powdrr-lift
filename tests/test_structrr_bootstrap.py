from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from powdrr_lift.structrr.bootstrap import (
    bootstrap_structrr,
    validate_bootstrap_document,
)
from powdrr_lift.structrr.rebase import rebase_structrr_snapshot, snapshot_digest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "example-product"
    repo.mkdir()
    taxonomy = Path(__file__).parents[1] / "software_development_entity_taxonomy.md"
    (repo / taxonomy.name).write_text(
        taxonomy.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text(
        "class App:\n"
        "    def run(self) -> None:\n"
        "        pass\n\n"
        "def run() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (repo / "docs/current").mkdir(parents=True)
    (repo / "docs/current/product").mkdir()
    (repo / "docs/current/product/architecture-specification.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "https://powdrr.io/schemas/specification-v1",
                "id": "product-architecture",
                "entities": [
                    {"id": "product", "type": "Product"},
                    {"id": "app", "type": "Application"},
                ],
                "entity_relationships": [
                    {
                        "id": "product-contains-app",
                        "source": "product",
                        "target": "app",
                        "relationship": "contains",
                    }
                ],
                "requirements": [
                    {
                        "id": "req-run-app",
                        "description": (
                            "The application must expose a runnable entry point."
                        ),
                        "state": "added",
                    }
                ],
                "approach": [
                    {
                        "id": "app-python-module",
                        "description": "Implement the entry point as a Python module.",
                        "state": "added",
                    }
                ],
                "tools": [
                    {
                        "id": "run-tests",
                        "related_modules": ["app"],
                        "when_to_use": "When validating application changes.",
                        "validation_action": "pytest",
                    }
                ],
                "invariants": [
                    {
                        "id": "app-entry-point",
                        "description": "The application entry point remains callable.",
                        "rationale": "Preserve the application contract.",
                        "related": {"entities": ["app"]},
                    },
                    {
                        "id": "app-reference-valid",
                        "description": "Every app reference must resolve.",
                    },
                ],
                "guidance": [
                    {
                        "id": "run-tests-before-edit",
                        "description": (
                            "Run the validation tool before editing application "
                            "behavior."
                        ),
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def test_bootstrap_writes_validated_source_anchored_snapshot(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)

    result = bootstrap_structrr(repo)

    assert result.validation.successful
    short_hash = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert result.output_path == repo / (
        f"docs/structrr/current/baseline-{short_hash}.yaml"
    )
    assert result.output_path.is_file()
    assert {entity["id"] for entity in result.document["entities"]} >= {
        "product",
        "app",
        "file:src/app.py",
    }
    relationships = result.document["entity_relationships"]
    relationship = next(
        relationship
        for relationship in relationships
        if relationship["id"] == "product-contains-app"
    )
    assert relationship["source"] == "product"
    assert relationship["target"] == "app"
    source_link = next(
        relationship
        for relationship in relationships
        if relationship["relationship"] == "implemented_by"
    )
    assert source_link["source"] == "app"
    assert source_link["target"] == "file:src/app.py"
    assert source_link["relationship"] == "implemented_by"
    subjects = result.document["source_subjects"]
    module = next(subject for subject in subjects if subject["kind"] == "module")
    assert module["stable_key"] == "python::src.app"
    assert any(subject["qualified_name"] == "src.app.App.run" for subject in subjects)
    binding = next(
        binding
        for binding in result.document["source_bindings"]
        if binding["entity_id"] == "app"
    )
    assert binding["subject_id"].startswith("python:src/app.py::")
    assert binding["span"]["start_line"] >= 1
    assert result.document["files"][0]["span"]["start_line"] == 1
    statements = [
        statement
        for statement in result.document["statements"]
        if statement["id"] in {"app-python-module", "req-run-app"}
    ]
    assert statements == [
        {
            "id": "app-python-module",
            "declared_kind": "approach",
            "kind": "approach",
            "action": "added",
            "description": "Implement the entry point as a Python module.",
            "source": "docs/current/product/architecture-specification.yaml",
            "classification": "declared",
            "classification_reason": "Preserved from the specification section.",
            "state": "added",
        },
        {
            "id": "req-run-app",
            "declared_kind": "requirement",
            "kind": "requirement",
            "action": "added",
            "description": "The application must expose a runnable entry point.",
            "source": "docs/current/product/architecture-specification.yaml",
            "classification": "declared",
            "classification_reason": "Preserved from the specification section.",
            "state": "added",
        },
    ]
    assert result.document["tools"][0]["validation_action"] == "pytest"
    assert result.document["invariants"][0]["id"] == "app-entry-point"
    assert result.document["invariants"][0]["kind"] == "invariant"
    assert result.document["invariants"][0]["classification"] == "declared"
    validation_invariant = next(
        item
        for item in result.document["invariants"]
        if item["id"] == "app-reference-valid"
    )
    assert validation_invariant["kind"] == "validation_rule"
    assert validation_invariant["classification"] == "inferred"
    assert any(
        item["id"] == "app-entry-point" and item["kind"] == "invariant"
        for item in result.document["statements"]
    )
    assert any(
        item["id"] == "run-tests-before-edit" for item in result.document["guidance"]
    )


def test_bootstrap_ignores_tracked_github_metadata(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    github_workflow = repo / ".github" / "workflows" / "ci.yml"
    github_workflow.parent.mkdir(parents=True)
    github_workflow.write_text("name: CI\n", encoding="utf-8")
    _git(repo, "add", ".github/workflows/ci.yml")
    _git(repo, "commit", "-qm", "add GitHub automation")

    result = bootstrap_structrr(repo, output_path=tmp_path / "bootstrap.yaml")

    assert result.validation.successful
    assert ".github/workflows/ci.yml" not in result.evidence_files
    assert all(
        file_entry["path"] != ".github/workflows/ci.yml"
        for file_entry in result.document["files"]
    )


def test_bootstrap_is_deterministic_and_does_not_stage_output(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)

    first = bootstrap_structrr(repo).document
    second = bootstrap_structrr(repo).document

    assert first == second
    status = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_bootstrap_validation_rejects_dangling_relationship(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    result = bootstrap_structrr(repo)
    document = dict(result.document)
    document["entity_relationships"] = [
        {"source": "product", "target": "missing", "relationship": "contains"}
    ]

    report = validate_bootstrap_document(document, root=repo)

    assert not report.successful
    assert any(issue.code == "relationship_dangling" for issue in report.issues)


def test_bootstrap_validation_rejects_unknown_statement_kind(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    result = bootstrap_structrr(repo)
    document = dict(result.document)
    document["statements"] = [
        {
            "id": "bad-statement",
            "kind": "unsupported",
            "action": "added",
            "description": "Invalid statement kind.",
        }
    ]

    report = validate_bootstrap_document(document, root=repo)

    assert not report.successful
    assert any(issue.code == "statement_kind_invalid" for issue in report.issues)


def test_bootstrap_validation_rejects_unknown_source_binding(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    result = bootstrap_structrr(repo)
    document = dict(result.document)
    document["source_bindings"] = [
        {
            "id": "binding:missing",
            "subject_id": "python:missing.py::run",
            "entity_id": "missing",
            "relationship": "implemented_by",
            "span": {"start_line": 1, "end_line": 1},
        }
    ]

    report = validate_bootstrap_document(document, root=repo)

    assert not report.successful
    assert any(
        issue.code == "source_binding_subject_unknown" for issue in report.issues
    )


def test_bootstrap_validation_rejects_duplicate_stable_key(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    result = bootstrap_structrr(repo)
    document = dict(result.document)
    subjects = list(document["source_subjects"])
    subjects[1] = dict(subjects[1])
    subjects[1]["stable_key"] = subjects[0]["stable_key"]
    document["source_subjects"] = subjects

    report = validate_bootstrap_document(document, root=repo)

    assert not report.successful
    assert any(
        issue.code == "source_subject_stable_key_duplicate" for issue in report.issues
    )


def _snapshot(
    *, description: str = "Keep the API stable.", path: str = "src/api.py"
) -> dict:
    return {
        "schema": "https://powdrr.io/schema/changelog-v2",
        "structrr_schema": "https://powdrr.io/schema/structrr-bootstrap-v1",
        "entities": [{"id": "api", "type": "Application", "description": description}],
        "entity_relationships": [],
        "source_subjects": [
            {
                "id": f"python:{path}::api.Client",
                "stable_key": "python::api.Client",
                "kind": "class",
                "qualified_name": "api.Client",
                "path": path,
                "span": {"start_line": 1, "end_line": 4},
            }
        ],
        "source_bindings": [
            {
                "id": "binding:api",
                "subject_id": f"python:{path}::api.Client",
                "entity_id": "api",
                "relationship": "implemented_by",
                "path": path,
                "span": {"start_line": 1, "end_line": 4},
            }
        ],
    }


def test_snapshot_digest_ignores_semantic_collection_order() -> None:
    first = _snapshot()
    second = _snapshot()
    second["entities"] = list(reversed(second["entities"]))

    assert snapshot_digest(first) == snapshot_digest(second)


def test_rebase_ignores_unrelated_current_changes() -> None:
    baseline = _snapshot()
    current = _snapshot()
    current["entities"].append(
        {"id": "billing", "type": "Service", "description": "Unrelated."}
    )

    report = rebase_structrr_snapshot(baseline, current, referenced_entity_ids=("api",))

    assert report.classification == "clean"
    assert len(report.changes) == 1
    assert report.affected_changes == ()


def test_rebase_reports_entity_contract_change_as_conflict() -> None:
    baseline = _snapshot()
    current = _snapshot(description="The API must require authentication.")
    current["entities"][0]["type"] = "Service"

    report = rebase_structrr_snapshot(baseline, current, referenced_entity_ids=("api",))

    assert report.classification == "conflicted"
    assert report.conflicts[0].identity == "api"
    assert report.requires_targeted_review


def test_rebase_mechanically_remaps_source_move() -> None:
    baseline = _snapshot()
    current = _snapshot(path="src/client_api.py")
    current["source_subjects"][0]["stable_key"] = "python::client_api.Client"

    report = rebase_structrr_snapshot(
        baseline,
        current,
        referenced_source_subject_keys=("python::api.Client",),
    )

    assert report.classification == "mechanically_rebased"
    assert report.remappings[0].before == "python::api.Client"
    assert report.remappings[0].after == "python::client_api.Client"
    assert report.conflicts == ()


def test_rebase_requires_targeted_update_for_binding_contract_change() -> None:
    baseline = _snapshot()
    current = _snapshot()
    current["source_bindings"][0] = {
        **current["source_bindings"][0],
        "relationship": "verified_by",
    }

    report = rebase_structrr_snapshot(baseline, current, referenced_entity_ids=("api",))

    assert report.classification == "invalidated"
    assert any("source_binding" in item for item in report.amendment_items)
