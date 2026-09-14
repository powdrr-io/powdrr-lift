from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from powdrr_lift.structrr.bootstrap import (
    bootstrap_structrr,
    validate_bootstrap_document,
)


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
    (repo / "src/app.py").write_text("def run() -> None:\n    pass\n", encoding="utf-8")
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
    assert result.output_path == repo / "docs/structrr/bootstrap-changelog.yaml"
    assert result.output_path.is_file()
    assert {entity["id"] for entity in result.document["entities"]} >= {
        "product",
        "app",
        "file:src/app.py",
    }
    relationship = result.document["entity_relationships"][0]
    assert relationship["source"] == "product"
    assert relationship["target"] == "app"
    assert result.document["files"][0]["span"]["start_line"] == 1


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
