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


def test_atomicity_rejects_model_authored_structural_fields() -> None:
    ledger = compile_instruction_ledger("feature", "Data is fresh.")

    with pytest.raises(InstructionLedgerError, match="atomicity response"):
        apply_atomicity_decisions(
            ledger,
            {"instruction-001": {"multiple": False, "clause_id": "invented"}},
        )
