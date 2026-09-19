from __future__ import annotations

import json
from pathlib import Path

import pytest

from powdrr_lift.structrr.proposal import (
    compile_proposal_revision,
    load_proposal_revision,
    validate_proposal_revision,
)


def test_proposal_revision_fingerprint_is_stable_for_mapping_order() -> None:
    baseline = {"entities": [{"id": "adapter", "type": "Feature"}]}
    first = {
        "features": [{"id": "adapter", "action": "added", "description": "Add it."}]
    }
    second = {
        "features": [{"description": "Add it.", "action": "added", "id": "adapter"}]
    }

    first_revision = compile_proposal_revision(
        "adapter",
        baseline,
        first,
        acceptance_criteria=("it works",),
        must_preserve=("bounded",),
        non_goals=("no redesign",),
        allowed_paths=("src/adapter.py",),
        source_refs=("structrr:baseline.yaml",),
    )
    second_revision = compile_proposal_revision(
        "adapter",
        baseline,
        second,
        acceptance_criteria=("it works",),
        must_preserve=("bounded",),
        non_goals=("no redesign",),
        allowed_paths=("src/adapter.py",),
        source_refs=("structrr:baseline.yaml",),
    )

    assert first_revision.fingerprint == second_revision.fingerprint
    assert first_revision.operations[0].operation_id == "add:features:adapter"


def test_proposal_revision_records_removals_and_deduplicates_context() -> None:
    revision = compile_proposal_revision(
        "adapter",
        {"entities": []},
        {"features": [{"id": "old", "action": "removed", "description": "Retire it."}]},
        acceptance_criteria=("it works", "it works"),
        must_preserve=("bounded", "bounded"),
        non_goals=(),
        allowed_paths=("src", "src"),
        source_refs=("plan", "plan"),
    )

    assert revision.operations[0].action == "remove"
    assert revision.acceptance_criteria == ("it works",)
    assert revision.must_preserve == ("bounded",)
    assert revision.allowed_paths == ("src",)
    assert revision.source_refs == ("plan",)


def test_persisted_proposal_revision_round_trips_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    revision = compile_proposal_revision(
        "adapter",
        {"entities": [{"id": "adapter"}]},
        {"features": [{"id": "adapter", "action": "added"}]},
        acceptance_criteria=("it works",),
        must_preserve=("bounded",),
        non_goals=("no redesign",),
        allowed_paths=("src/adapter.py",),
        source_refs=("structrr:baseline.yaml",),
    )
    path = tmp_path / "proposal-revision.json"
    path.write_text(json.dumps(revision.to_data()), encoding="utf-8")

    loaded = load_proposal_revision(path)
    validate_proposal_revision(path, revision)
    assert loaded.fingerprint == revision.fingerprint

    tampered = revision.to_data()
    tampered["acceptance_criteria"] = ["different outcome"]
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        load_proposal_revision(path)
