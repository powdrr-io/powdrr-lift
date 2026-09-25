from __future__ import annotations

from pathlib import Path

import pytest

from powdrr_lift.core.repository_inventory import (
    InventoryError,
    InventoryRecord,
    LookupQuery,
    RepositoryInventory,
    StructrrLookupContext,
    aggregate_candidate_relations,
    build_inventory,
    enumerate_population,
    normalize_terms,
    retrieve_candidates,
)
from powdrr_lift.workrr.command_catalog import (
    FeatureCommandRuntime,
    feature_command_catalog,
)
from powdrr_lift.workrr.repository_subject_binding import (
    bind_candidate_relation_decisions,
    finalize_subject_binding,
    prepare_candidate_relation_decisions,
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
