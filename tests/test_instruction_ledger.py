from __future__ import annotations

import pytest

from powdrr_lift.core.instruction_ledger import (
    InstructionLedger,
    InstructionLedgerError,
    apply_atomicity_decisions,
    compile_instruction_ledger,
)


def test_instruction_ledger_normalizes_wrapped_prose_without_splitting_it() -> None:
    ledger = compile_instruction_ledger(
        "state-data",
        "States lack built-in data ownership, forcing manual variable management "
        "without\n"
        "scoping or lifecycle. On exit, data is removed.",
    )

    assert [clause.clause_id for clause in ledger.clauses] == [
        "instruction-001",
        "instruction-002",
    ]
    assert ledger.clauses[0].text == (
        "States lack built-in data ownership, forcing manual variable management "
        "without scoping or lifecycle."
    )
    assert ledger.source.text.startswith("States lack built-in")
    assert ledger.clauses[0].source_span[1] <= ledger.clauses[1].source_span[0]


def test_instruction_ledger_ignores_markdown_markers_and_preserves_offsets() -> None:
    source = (
        "Requirements\n\n"
        "1. First behavior. Second behavior.\n"
        "2. Third behavior.\n\n"
        "Out of Scope\n\n"
        "CREATE TABLE ... LIKE ... must pass through unchanged."
    )

    ledger = compile_instruction_ledger("feature", source)

    assert [clause.text for clause in ledger.clauses] == [
        "First behavior.",
        "Second behavior.",
        "Third behavior.",
        "CREATE TABLE ... LIKE ... must pass through unchanged.",
    ]
    for clause in ledger.clauses:
        start, end = clause.source_span
        assert source[start:end] == clause.text


def test_instruction_ledger_ids_and_fingerprint_do_not_depend_on_model_output() -> None:
    first = compile_instruction_ledger("feature", "First behavior. Second behavior.")
    second = compile_instruction_ledger("feature", "First behavior. Second behavior.")

    assert first.fingerprint == second.fingerprint
    assert [item.clause_id for item in first.clauses] == [
        "instruction-001",
        "instruction-002",
    ]
    assert "intent_refs" not in first.to_data()


def test_instruction_ledger_round_trips_and_rejects_stale_fingerprints() -> None:
    ledger = compile_instruction_ledger("feature", "One behavior.")

    assert (
        InstructionLedger.from_data(ledger.to_data()).fingerprint == ledger.fingerprint
    )
    stale = ledger.to_data()
    stale["clauses"][0]["clause_id"] = "feature_data_ownership"
    with pytest.raises(InstructionLedgerError, match="fingerprint"):
        InstructionLedger.from_data(stale)


def test_atomicity_split_gets_compiler_owned_ids() -> None:
    ledger = compile_instruction_ledger("feature", "Data is fresh and removed.")

    split = apply_atomicity_decisions(
        ledger,
        {
            "instruction-001": {
                "multiple": True,
                "statements": [
                    "Data is fresh on entry.",
                    "Data is removed on exit.",
                ],
            }
        },
    )

    assert [item.clause_id for item in split.clauses] == [
        "instruction-001",
        "instruction-002",
    ]
    assert all(
        item.parent_clause_id == "candidate:instruction-001" for item in split.clauses
    )
    assert [item.text for item in split.clauses] == [
        "Data is fresh on entry.",
        "Data is removed on exit.",
    ]


def test_atomicity_split_preserves_joint_validation_relationships() -> None:
    ledger = compile_instruction_ledger(
        "feature", "For each valid request, return 200 and include its account ID."
    )
    split = apply_atomicity_decisions(
        ledger,
        {
            "instruction-001": {
                "multiple": True,
                "statements": [
                    "For each valid request, return status 200.",
                    "For each valid request, include its account ID.",
                ],
                "validation_groups": [{"members": [1, 2], "relation": "all_together"}],
            }
        },
    )

    assert [item.validation_group_id for item in split.clauses] == [
        "validation:instruction-001:1",
        "validation:instruction-001:1",
    ]
    assert {item.validation_relation for item in split.clauses} == {"all_together"}
    restored = InstructionLedger.from_data(split.to_data())
    assert restored.fingerprint == split.fingerprint


def test_atomicity_merges_overlapping_all_together_validation_groups() -> None:
    ledger = compile_instruction_ledger("feature", "Add, validate, and commit safely.")

    split = apply_atomicity_decisions(
        ledger,
        {
            "instruction-001": {
                "multiple": True,
                "statements": [
                    "The import creates a checkpoint.",
                    "The import validates invariants.",
                    "The import commits only on success.",
                ],
                "validation_groups": [
                    "./members=1,2;relation=all_together",
                    "members=1,3;relation=all_together",
                ],
            }
        },
    )

    assert {item.validation_group_id for item in split.clauses} == {
        "validation:instruction-001:1"
    }
    assert {item.validation_relation for item in split.clauses} == {"all_together"}


def test_atomicity_discards_single_member_validation_group() -> None:
    ledger = compile_instruction_ledger("feature", "Import returns a result.")

    split = apply_atomicity_decisions(
        ledger,
        {
            "instruction-001": {
                "multiple": True,
                "statements": ["The import succeeds.", "The import returns a result."],
                "validation_groups": ["members=1;relation=all_together"],
            }
        },
    )

    assert all(item.validation_group_id is None for item in split.clauses)


def test_atomicity_rejects_overlapping_groups_with_different_relations() -> None:
    ledger = compile_instruction_ledger("feature", "Add, validate, and commit safely.")

    with pytest.raises(InstructionLedgerError, match="cannot be safely combined"):
        apply_atomicity_decisions(
            ledger,
            {
                "instruction-001": {
                    "multiple": True,
                    "statements": [
                        "The import creates a checkpoint.",
                        "The import validates invariants.",
                        "The import commits only on success.",
                    ],
                    "validation_groups": [
                        "members=1,2;relation=all_together",
                        "members=1,3;relation=ordered",
                    ],
                }
            },
        )


def test_atomicity_rejects_model_authored_structural_fields() -> None:
    ledger = compile_instruction_ledger("feature", "Data is fresh.")

    with pytest.raises(InstructionLedgerError, match="atomicity response"):
        apply_atomicity_decisions(
            ledger,
            {"instruction-001": {"multiple": False, "clause_id": "invented"}},
        )


def test_atomicity_split_preserves_each_state_data_api_requirement() -> None:
    ledger = compile_instruction_ledger(
        "python-statemachine-state-data-scoping",
        "set_state_data(state, key, value) validates active state, declared key, "
        "and DataVar type constraints, raising InvalidDefinition on violation.",
    )

    split = apply_atomicity_decisions(
        ledger,
        {
            "instruction-001": {
                "multiple": True,
                "statements": [
                    "set_state_data rejects an inactive state.",
                    "set_state_data rejects an undeclared key.",
                    "set_state_data enforces the declared DataVar type constraint.",
                    "An invalid set_state_data call raises InvalidDefinition.",
                ],
            }
        },
    )

    assert [item.text for item in split.clauses] == [
        "set_state_data rejects an inactive state.",
        "set_state_data rejects an undeclared key.",
        "set_state_data enforces the declared DataVar type constraint.",
        "An invalid set_state_data call raises InvalidDefinition.",
    ]
    assert all(
        item.parent_clause_id == "candidate:instruction-001" for item in split.clauses
    )
