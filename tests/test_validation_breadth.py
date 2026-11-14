from __future__ import annotations

from pathlib import Path

import yaml
from powdrr_service.structrr.python_topology import discover_python_topology
from powdrr_service.structrr.validation import (
    assign_python_topology,
    discover_validation_profiles,
    validation_context,
    validation_inventory,
)
from powdrr_service.structrr.validation_corpus import (
    evaluate_repository,
    render_corpus_markdown,
    validate_corpus_definition,
)
from powdrr_service.structrr.validation_models import validate_validation_records

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "validation_discovery"
CORPUS_PATH = Path(__file__).parent / "fixtures" / "validation_corpus" / "corpus.yaml"


def test_reviewed_discovery_fixtures_match_exact_commands_and_context() -> None:
    expectations = yaml.safe_load(
        (FIXTURE_ROOT / "expectations.yaml").read_text(encoding="utf-8")
    )
    assert expectations["schema_version"] == "validation-discovery-fixtures-v1"

    for case in expectations["cases"]:
        root = FIXTURE_ROOT / case["id"]
        profiles = discover_validation_profiles(root)
        tracked_paths = tuple(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )
        topology = discover_python_topology(root, tracked_paths)
        profiles = assign_python_topology(profiles, topology)
        inventory = validation_inventory(profiles)
        observed = {
            profile.name: {
                "command": list(profile.command),
                "cwd": profile.cwd,
                "provider": profile.provider,
                "evidence": list(profile.evidence),
                "applicability": profile.applicability
                or {"local": True, "evaluation": "unknown"},
                "depends_on": list(profile.depends_on),
                "unresolved": list(profile.unresolved),
            }
            for profile in profiles
        }
        expected = {
            profile["name"]: {
                "command": profile["command"],
                "cwd": profile["cwd"],
                "provider": profile["provider"],
                "evidence": profile["evidence"],
                "applicability": profile["applicability"],
                "depends_on": profile["depends_on"],
                "unresolved": profile["unresolved"],
            }
            for profile in case["profiles"]
        }

        assert observed == expected, case["id"]
        unresolved_count = sum(bool(value["unresolved"]) for value in observed.values())
        assert unresolved_count == len(case["unresolved"]), case["id"]

        context = validation_context(profiles, topology)
        assert validate_validation_records(inventory, context) == (), case["id"]


def test_nested_discovery_skips_test_fixture_projects(tmp_path: Path) -> None:
    fixture = tmp_path / "tests" / "fixtures" / "tox-project"
    (fixture / ".github" / "workflows").mkdir(parents=True)
    (fixture / "tox.ini").write_text(
        "[testenv:lint]\ncommands = ruff check .\n", encoding="utf-8"
    )
    (fixture / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  validation:\n    steps:\n      - run: tox -e lint\n",
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(tmp_path)

    assert not any("tox-project" in profile.name for profile in profiles)


def test_local_corpus_expectations_do_not_assert_requiredness_without_rules() -> None:
    expectations = yaml.safe_load(
        (FIXTURE_ROOT / "expectations.yaml").read_text(encoding="utf-8")
    )

    for case in expectations["cases"]:
        assert "requiredness" not in case
        assert case["profiles"]


def test_corpus_evaluation_separates_discovery_from_requiredness(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    entry = {
        "id": "fixture",
        "repository": "https://github.com/example/project",
        "revision": "a" * 40,
        "license": "MIT",
        "license_evidence": "LICENSE",
        "coverage": ["pytest"],
        "evidence": [],
        "obligations": [
            {
                "id": "unit-tests",
                "profile": "pytest",
                "command": ["pytest", "-q"],
                "cwd": ".",
                "state": "executable",
                "requiredness": "unknown",
                "evidence": [],
            }
        ],
    }
    corpus = {"schema_version": "validation-corpus-v1", "repositories": [entry]}

    assert validate_corpus_definition(corpus) == ()
    result = evaluate_repository(entry, tmp_path)
    report = {
        "corpus_revision": "fixture",
        "repositories": [result],
        "totals": {
            "expected_obligations": result["expected_obligations"],
            "exact_executable_discoveries": result["exact_executable_discoveries"],
            "missed_obligations": len(result["missed_obligations"]),
            "invented_required_checks": len(result["invented_required_checks"]),
            "command_context_mismatches": len(result["command_context_mismatches"]),
            "provider_mismatches": len(result["provider_mismatches"]),
            "evidence_gaps": len(result["evidence_gaps"]),
            "expected_unresolved_obligations": result[
                "expected_unresolved_obligations"
            ],
            "observed_unresolved_profiles": len(result["observed_unresolved_profiles"]),
        },
    }
    markdown = render_corpus_markdown(report)

    assert result["exact_executable_discoveries"] == 1
    assert result["invented_required_checks"] == []
    assert "Expected unresolved obligations: 0" in markdown
    assert "Observed unresolved profiles: 0" in markdown


def test_corpus_definition_rejects_unpinned_or_unlicensed_sources() -> None:
    corpus = {
        "schema_version": "validation-corpus-v1",
        "repositories": [
            {
                "id": "mutable",
                "repository": "https://github.com/example/project",
                "revision": "main",
                "coverage": [],
                "evidence": [],
                "obligations": [],
            }
        ],
    }

    assert (
        "repositories[0].revision must be a full 40-character commit SHA"
        in validate_corpus_definition(corpus)
    )
    assert (
        "repositories[0].license must record the reviewed SPDX identifier"
        in validate_corpus_definition(corpus)
    )


def test_reviewed_real_repository_corpus_is_pinned_and_keeps_requiredness_unknown() -> (
    None
):
    corpus = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))

    assert validate_corpus_definition(corpus) == ()
    coverage = {
        item for repository in corpus["repositories"] for item in repository["coverage"]
    }
    assert {
        "pytest",
        "Django",
        "Poetry",
        "uv",
        "PDM",
        "Hatch",
        "Conda",
        "Nox",
        "pre-commit",
        "reusable-workflows",
        "monorepo",
        "Rust-native-extension",
        "custom-shell-scripts",
    } <= coverage
    assert all(
        obligation["requiredness"] == "unknown"
        for repository in corpus["repositories"]
        for obligation in repository["obligations"]
    )
