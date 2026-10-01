from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from powdrr_lift.core.repository_inventory import (
    InventoryError,
    InventoryRecord,
    LookupQuery,
    RepositoryInventory,
    StructrrLookupContext,
    aggregate_candidate_relations,
    build_inventory,
    build_python_lookup_context,
    enumerate_population,
    inventory_from_source_subjects,
    normalize_terms,
    retrieve_candidates,
)
from powdrr_lift.workrr.command_catalog import (
    FeatureCommandRuntime,
    feature_command_catalog,
)
from powdrr_lift.workrr.repository_subject_binding import (
    add_candidate_source_excerpts,
    bind_candidate_relation_decisions,
    extract_explicit_repository_names,
    finalize_subject_binding,
    prepare_candidate_relation_decisions,
    retrieve_subject_candidates,
)


def _inventory(*records: InventoryRecord) -> RepositoryInventory:
    return RepositoryInventory("commit:current", "structrr:v1", ("test:v1",), records)


def _record(name: str, *, aliases: tuple[str, ...] = ()) -> InventoryRecord:
    return InventoryRecord(
        inventory_id=f"python:src/{name.lower()}.py::{name}",
        kind="class",
        canonical_name=name,
        qualified_name=f"models.{name}",
        normalized_terms=normalize_terms(name),
        aliases=aliases,
        path=f"src/{name.lower()}.py",
        span=(1, 10),
        language="python",
    )


def test_normalization_preserves_domain_terms_and_splits_names() -> None:
    assert normalize_terms("All UserRecord_data") == ("user", "record", "data")


def test_lookup_orders_exact_matches_and_deduplicates_candidates() -> None:
    inventory = _inventory(
        _record("UserRecord", aliases=("data",)), _record("UserData")
    )
    query = LookupQuery(
        "instruction-001",
        "All data should pickle",
        "data",
        "invariant",
        "serialize",
        inventory.fingerprint,
    )
    candidates = retrieve_candidates(
        query, inventory, StructrrLookupContext(aliases={"data": ("data",)})
    )
    assert candidates.candidates[0].record.canonical_name == "UserRecord"
    assert candidates.candidates[0].evidence.score == 95


def test_relation_aggregation_never_chooses_between_multiple_matches() -> None:
    inventory = _inventory(_record("DataA"), _record("DataB"))
    query = LookupQuery(
        "instruction-001",
        "data",
        "data",
        "invariant",
        "serialize",
        inventory.fingerprint,
    )
    candidates = retrieve_candidates(query, inventory)
    decisions = {
        candidate.record.inventory_id: "matches" for candidate in candidates.candidates
    }
    result = aggregate_candidate_relations(candidates, decisions, quantifier="every")
    assert result == {"status": "unresolved", "reason_code": "multiple_candidates"}


def test_one_match_with_an_uncertain_candidate_does_not_bind() -> None:
    candidates = _inventory(_record("DataA"), _record("DataB"))
    query = LookupQuery(
        "instruction-001",
        "data",
        "data",
        "invariant",
        "serialize",
        candidates.fingerprint,
    )
    candidate_set = retrieve_candidates(query, candidates)
    decisions = {
        candidate.record.inventory_id: (
            "matches"
            if candidate.record.canonical_name == "DataA"
            else "insufficient_evidence"
        )
        for candidate in candidate_set.candidates
    }
    result = aggregate_candidate_relations(candidate_set, decisions, quantifier="one")
    assert result == {
        "status": "unresolved",
        "reason_code": "repository_evidence_missing",
    }


def test_population_receipt_validates_current_members() -> None:
    inventory = _inventory(_record("Data"))
    receipt = enumerate_population(
        inventory,
        population_ref="population:data",
        membership_rule_ref="relationship:registered-data",
        member_ids=(next(iter(inventory.records)).inventory_id,),
        complete=True,
    )
    assert receipt.to_data()["complete"] is True
    with pytest.raises(InventoryError, match="unknown member"):
        enumerate_population(
            inventory,
            population_ref="population:data",
            membership_rule_ref="relationship:registered-data",
            member_ids=("missing",),
            complete=True,
        )


def test_python_adapter_collects_symbols_without_model_input(tmp_path: Path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "class DataRecord:\n    pass\n\ndef pickle_data(value):\n    return value\n"
    )
    inventory = build_inventory(
        tmp_path, commit_ref="commit:test", structrr_revision="structrr:test"
    )
    assert {record.canonical_name for record in inventory.records} >= {
        "DataRecord",
        "pickle_data",
    }
    assert inventory.fingerprint.startswith("sha256:")


def test_structrr_source_subjects_preserve_identity_and_locations() -> None:
    inventory = inventory_from_source_subjects(
        [
            {
                "id": "python:src/client.py::client.fetch",
                "qualified_name": "client.fetch",
                "kind": "function",
                "language": "python",
                "path": "src/client.py",
                "span": {"start_line": 4, "end_line": 9},
                "file_entity_id": "file:src/client.py",
            }
        ],
        commit_ref="working-tree",
        structrr_revision="bootstrap:current",
    )
    record = inventory.records[0]
    assert record.inventory_id == "python:src/client.py::client.fetch"
    assert record.canonical_name == "fetch"
    assert record.span == (4, 9)
    assert record.component_refs == ("file:src/client.py",)


def test_python_relationship_context_resolves_import_aliases_and_test_imports(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    (repository / "src/pkg").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src/pkg/__init__.py").write_text(
        "from .api import Client as PublicClient\n"
    )
    (repository / "src/pkg/api.py").write_text(
        "class Client:\n    def fetch(self):\n        return 1\n"
    )
    (repository / "tests/test_api.py").write_text("from pkg.api import Client\n")
    inventory = build_inventory(
        repository,
        commit_ref="working-tree",
        structrr_revision="test",
    )

    context = build_python_lookup_context(repository, inventory)
    query = LookupQuery(
        "instruction-001",
        "Use the public client.",
        "PublicClient",
        "feature",
        "create",
        inventory.fingerprint,
        explicit_names=("PublicClient",),
    )
    candidates = retrieve_subject_candidates(query, inventory, context)
    requests = prepare_candidate_relation_decisions(query, candidates, context)

    assert [candidate.record.qualified_name for candidate in candidates.candidates] == [
        "src.pkg.api.Client"
    ]
    assert "resolved_import_alias" in candidates.candidates[0].evidence.reasons
    assert requests[0]["repository_relationships"] == [
        "referenced_by_test|tests.test_api",
        "imported_by|src.pkg",
        "contained_by|src.pkg.api",
    ]
    assert context.fingerprint.startswith("sha256:")

    package_query = LookupQuery(
        "instruction-002",
        "Extend the pkg API.",
        "pkg",
        "feature",
        "create",
        inventory.fingerprint,
        explicit_names=("pkg",),
    )
    package_candidates = retrieve_subject_candidates(package_query, inventory, context)
    implementation = next(
        candidate
        for candidate in package_candidates.candidates
        if candidate.record.qualified_name == "src.pkg.api.Client"
    )
    assert "repository_import_relationship" in implementation.evidence.reasons


def test_explicit_repository_names_extract_code_identifiers() -> None:
    names = extract_explicit_repository_names(
        "Use `sqlite_utils.Database` and enable_safe_import(); "
        "B620 calls requests.get and urllib.request.urlopen; do not call()."
    )
    assert "sqlite_utils.Database" in names
    assert "Database" in names
    assert "enable_safe_import" in names
    assert "B620" in names
    assert "urlopen" in names
    assert "get" not in names
    assert "call" not in names


def test_candidate_relation_requests_include_bounded_repository_source(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    source = repository / "src" / "client.py"
    source.parent.mkdir(parents=True)
    source.write_text("class Client:\n    def fetch(self):\n        return 42\n")
    requests = [
        {
            "candidate": {
                "record": {
                    "path": "src/client.py",
                    "span": {"start_line": 1, "end_line": 3},
                }
            }
        }
    ]

    enriched = add_candidate_source_excerpts(requests, repository)

    assert enriched[0]["candidate"]["source_excerpt"] == {
        "path": "src/client.py",
        "start_line": 1,
        "text": "class Client:\n    def fetch(self):\n        return 42",
    }


def test_candidate_source_excerpt_rejects_paths_outside_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("private source")
    request = {
        "candidate": {
            "record": {
                "path": "../secret.py",
                "span": {"start_line": 1, "end_line": 1},
            }
        }
    }

    enriched = add_candidate_source_excerpts([request], repository)

    assert "source_excerpt" not in enriched[0]["candidate"]


def test_workrr_prepares_and_aggregates_one_c09_decision_per_candidate() -> None:
    record = _record("Data")
    inventory = _inventory(record)
    contract_query = LookupQuery(
        "instruction-001",
        "All data should pickle",
        "data",
        "invariant",
        "serialize",
        inventory.fingerprint,
    )
    candidates = retrieve_candidates(contract_query, inventory)
    requests = prepare_candidate_relation_decisions(contract_query, candidates)
    assert len(requests) == 1
    decisions = bind_candidate_relation_decisions(
        requests, [{"status": "resolved", "value": "matches", "reason_code": None}]
    )
    result = finalize_subject_binding(candidates, decisions, quantifier="every")
    assert result["status"] == "bound"
    assert result["binding_ref"] == record.inventory_id


def test_inventory_is_available_through_the_procedrr_command_boundary(
    tmp_path: Path,
) -> None:
    (tmp_path / "models.py").write_text("class DataRecord:\n    pass\n")
    runtime = FeatureCommandRuntime(
        config=None,
        runner=None,
        worktree=tmp_path,
        output_root=tmp_path,
        branch="feature/test",
        slug="inventory",
        state={},
        catalog=feature_command_catalog(),
    )
    result = runtime.dispatch(
        "build_semantic_repository_inventory",
        ["build_semantic_repository_inventory"],
        {},
    )
    assert result["schema_version"] == "semantic-repository-inventory-v1"
    assert any(item["canonical_name"] == "DataRecord" for item in result["records"])


def test_command_inventory_prefers_bootstrap_source_subjects(tmp_path: Path) -> None:
    (tmp_path / "models.py").write_text("class LocalOnly:\n    pass\n")
    (tmp_path / "validation-bootstrap.yaml").write_text(
        yaml.safe_dump(
            {
                "source_subjects": [
                    {
                        "id": "python:pkg/api.py::pkg.api.RemoteRecord",
                        "qualified_name": "pkg.api.RemoteRecord",
                        "kind": "class",
                        "language": "python",
                        "path": "pkg/api.py",
                        "span": {"start_line": 3, "end_line": 8},
                        "file_entity_id": "file:pkg/api.py",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime = FeatureCommandRuntime(
        config=None,
        runner=None,
        worktree=tmp_path,
        output_root=tmp_path,
        branch="feature/test",
        slug="inventory",
        state={},
        catalog=feature_command_catalog(),
    )
    result = runtime.dispatch(
        "build_semantic_repository_inventory",
        ["build_semantic_repository_inventory"],
        {},
    )
    records = result["records"]
    assert [item["inventory_id"] for item in records] == [
        "python:pkg/api.py::pkg.api.RemoteRecord"
    ]
    assert records[0]["component_refs"] == ["file:pkg/api.py"]
