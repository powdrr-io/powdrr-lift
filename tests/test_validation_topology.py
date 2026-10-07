from __future__ import annotations

import subprocess
from pathlib import Path

from powdrr_lift.structrr.bootstrap import bootstrap_structrr
from powdrr_lift.structrr.python_topology import discover_python_topology


def test_distinguishes_project_metadata_from_environment_managers(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / ".python-version").write_text("3.12\n", encoding="utf-8")
    topology = discover_python_topology(
        tmp_path, ("pyproject.toml", "uv.lock", ".python-version")
    )

    assert topology.components[0]["package_name"] == "sample"
    assert topology.environments[0]["manager"] == "uv"
    assert topology.environments[0]["python"] == {
        "requires_python": ">=3.11",
        "version_file": "3.12",
    }


def test_discovers_nested_components_and_ignores_test_projects(tmp_path: Path) -> None:
    paths = (
        "pyproject.toml",
        "packages/worker/pyproject.toml",
        "tests/sample/pyproject.toml",
        "src/app.py",
        "packages/worker/main.py",
    )
    topology = discover_python_topology(tmp_path, paths)

    assert [item["path"] for item in topology.components] == [".", "packages/worker"]


def test_requirements_only_project_is_pip_and_malformed_config_is_reported(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    topology = discover_python_topology(
        tmp_path, ("requirements.txt", "pyproject.toml")
    )

    assert topology.environments[0]["manager"] == "pip"
    assert topology.diagnostics[0]["code"] == "python_project_config_invalid"


def test_bootstrap_keeps_github_workflows_as_validation_only_evidence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    taxonomy = Path(__file__).parents[1] / "software_development_entity_taxonomy.md"
    (repo / taxonomy.name).write_text(taxonomy.read_text(encoding="utf-8"))
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "sample"\n', encoding="utf-8"
    )
    workflow = repo / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: CI\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    result = bootstrap_structrr(repo, output_path=tmp_path / "result.yaml")

    assert result.validation.successful
    assert ".github/workflows/ci.yml" not in result.evidence_files
    assert ".github/workflows/ci.yml" in result.validation_evidence_files
    assert any(
        item["path"] == ".github/workflows/ci.yml"
        for item in result.document["validation_context"]["input_fingerprints"]
    )


def test_bootstrap_normalizes_tox_evidence_references(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    taxonomy = Path(__file__).parents[1] / "software_development_entity_taxonomy.md"
    (repo / taxonomy.name).write_text(taxonomy.read_text(encoding="utf-8"))
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "tox.ini").write_text(
        "[tox]\nenvlist = lint\n\n[testenv:lint]\ncommands = python -m pytest\n",
        encoding="utf-8",
    )
    workflow = repo / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: CI\njobs:\n  test:\n    steps:\n      - run: tox -e lint\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    result = bootstrap_structrr(repo, output_path=tmp_path / "result.yaml")

    assert result.validation.successful
    context_ids = {
        item["id"] for item in result.document["validation_context"]["evidence"]
    }
    tox_checks = [
        item
        for item in result.document["validation_inventory"]
        if item["provider"] == "aggregate"
    ]
    assert tox_checks
    assert all(
        evidence in context_ids
        for check in tox_checks
        for evidence in check["provenance"]["evidence"]
    )
