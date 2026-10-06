from __future__ import annotations

from powdrr_lift.core.classifier_input import format_classifier_input
from powdrr_lift.core.instruction_context import (
    context_retry_reason,
    local_instruction_context,
)
from powdrr_lift.core.instruction_ledger import (
    apply_atomicity_decisions,
    compile_instruction_ledger,
)
from powdrr_lift.workrr.command_catalog import _bounded_instruction_context


def test_context_retry_uses_existing_unresolved_reasons() -> None:
    assert (
        context_retry_reason(
            {"status": "unresolved", "reason_code": "source_underspecified"}
        )
        == "source_underspecified"
    )
    assert (
        context_retry_reason(
            {"status": "unresolved", "reason_code": "unsupported_concept"}
        )
        is None
    )
    assert context_retry_reason({"status": "resolved", "reason_code": None}) is None


def test_local_context_returns_source_sentence_and_neighbors() -> None:
    instruction = (
        "A worker exposes a public process method. "
        "The instruction handles dataclasses. "
        "The function returns a PartialResult."
    )
    ledger = compile_instruction_ledger("context-test", instruction)
    source_clauses = ledger.to_data()["clauses"]

    context = local_instruction_context(
        instruction,
        source_clauses,
        source_clauses[1],
    )

    assert context == {
        "previous_sentence": "A worker exposes a public process method.",
        "source_sentence": "The instruction handles dataclasses.",
        "next_sentence": "The function returns a PartialResult.",
    }


def test_local_context_restores_parent_sentence_for_split_clause() -> None:
    instruction = "Data is fresh on entry and removed on exit."
    ledger = compile_instruction_ledger("context-test", instruction)
    source_clauses = ledger.to_data()["clauses"]
    split_ledger = apply_atomicity_decisions(
        ledger,
        {
            "instruction-001": {
                "multiple": True,
                "statements": ["Data is fresh on entry.", "Data is removed on exit."],
            }
        },
    )

    context = local_instruction_context(
        instruction,
        source_clauses,
        split_ledger.to_data()["clauses"][0],
    )

    assert context == {"source_sentence": instruction}


def test_atomicity_context_has_paragraph_and_one_neighbor_with_ids() -> None:
    ledger = compile_instruction_ledger(
        "context-test",
        "Before behavior. Split A and B. After behavior.\n\nAnother paragraph.",
    )

    context = _bounded_instruction_context(ledger, ledger.clauses[1])

    assert context["parent_sentence"] == "Split A and B."
    assert context["parent_clause_id"] == "instruction-002"
    assert context["preceding_sentence"] == {
        "clause_id": "instruction-001",
        "text": "Before behavior.",
    }
    assert context["following_sentence"] == {
        "clause_id": "instruction-003",
        "text": "After behavior.",
    }
    assert "Split A and B." in context["containing_paragraph"]
    assert context["paragraph_truncated"] is False


def test_classifier_input_adds_context_but_keeps_target_proposition_explicit() -> None:
    text = format_classifier_input(
        "The instruction handles dataclasses.",
        {"previous_sentence": "A worker exposes a public process method."},
    )

    assert "Previous sentence: A worker exposes a public process method." in text
    assert "Proposition to classify:\nThe instruction handles dataclasses." in text
