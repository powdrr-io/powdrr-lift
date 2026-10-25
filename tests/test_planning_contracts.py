from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.planning.contracts import (
    COVERAGE_ANSWER_JSON_SCHEMA,
    ENUMERATION_ANSWER_JSON_SCHEMA,
    HUMAN_SETUP_DESCRIPTION_JSON_SCHEMA,
    MAX_ENUMERATION_RECORDS,
    QUESTION_JSON_SCHEMA,
    VALIDATION_ERROR_JSON_SCHEMA,
    CompleteCoverage,
    EnumerationAnswer,
    HumanSetupDescriptionAnswer,
    MissingCoverage,
    PartialCoverage,
    PlanningContractError,
    Question,
    parse_coverage_answer,
)

FIXTURES = Path(__file__).parent / "fixtures" / "planning"
FAILURE_EXAMPLES = (
    Path(__file__).parents[1]
    / "docs"
    / "plans"
    / "bounded-planning-failure-examples.json"
)


def _fixture(case: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{case}.json").read_text())


def test_failure_fixtures_preserve_captured_responses_and_provenance() -> None:
    source = json.loads(FAILURE_EXAMPLES.read_text())
    examples = {example["case"]: example for example in source["examples"]}
    fixture_paths = {path.stem for path in FIXTURES.glob("*.json")}

    assert fixture_paths == set(examples)
    for case, example in examples.items():
        fixture = _fixture(case)
        assert fixture["response"] == example["captured_response"]
        assert fixture["provenance"] == {
            key: example[key]
            for key in (
                "captured_at",
                "source_exchange",
                "source_exchange_sha256",
                "selection",
            )
        }


def test_question_contract_round_trips_exact_envelope() -> None:
    question = {
        "question_id": "q-000123",
        "kind": "assess_requirement_coverage",
        "contract_version": 1,
        "subject_ids": ["req-0017"],
        "input_digest": "sha256:" + "a" * 64,
        "context_ref_ids": ["evidence-0042", "evidence-0043"],
        "expected_answer_schema": "coverage-answer-v1",
        "attempt": 1,
    }

    parsed = Question.from_mapping(question)

    assert parsed.to_data() == question
    assert set(QUESTION_JSON_SCHEMA["properties"]) == set(question)
    assert QUESTION_JSON_SCHEMA["additionalProperties"] is False


def test_validation_error_contract_round_trips_pointer_and_message() -> None:
    error = PlanningContractError("/records/0/title", "must not be empty")

    assert PlanningContractError.from_mapping(error.to_data()).to_data() == {
        "json_pointer": "/records/0/title",
        "message": "must not be empty",
    }
    assert set(VALIDATION_ERROR_JSON_SCHEMA["properties"]) == {
        "json_pointer",
        "message",
    }


@pytest.mark.parametrize(
    ("field", "value", "pointer"),
    [
        ("kind", "whole_plan", "/kind"),
        ("attempt", 4, "/attempt"),
        ("extra", "unbounded", "/extra"),
        ("input_digest", "not-a-digest", "/input_digest"),
    ],
)
def test_question_rejects_unknown_or_invalid_fields(
    field: str, value: Any, pointer: str
) -> None:
    question: dict[str, Any] = {
        "question_id": "q-000123",
        "kind": "assess_requirement_coverage",
        "contract_version": 1,
        "subject_ids": ["req-0017"],
        "input_digest": "sha256:" + "a" * 64,
        "context_ref_ids": [],
        "expected_answer_schema": "coverage-answer-v1",
        "attempt": 1,
    }
    question[field] = value

    with pytest.raises(PlanningContractError) as error:
        Question.from_mapping(question)

    assert error.value.json_pointer == pointer


def test_enumeration_answer_requires_bounded_records_and_an_advancing_cursor() -> None:
    answer = EnumerationAnswer.from_mapping(
        {"records": [{"statement": "A"}], "has_more": True, "next_anchor": "block-02"}
    )
    assert answer.to_data() == {
        "records": [{"statement": "A"}],
        "has_more": True,
        "next_anchor": "block-02",
    }
    assert set(ENUMERATION_ANSWER_JSON_SCHEMA["properties"]) == {
        "records",
        "has_more",
        "next_anchor",
    }

    with pytest.raises(PlanningContractError, match="next_anchor"):
        EnumerationAnswer.from_mapping(
            {"records": [], "has_more": True, "next_anchor": None}
        )
    with pytest.raises(PlanningContractError, match="final page"):
        EnumerationAnswer.from_mapping(
            {"records": [], "has_more": False, "next_anchor": "block-02"}
        )
    with pytest.raises(PlanningContractError, match="at most"):
        EnumerationAnswer.from_mapping(
            {
                "records": [{}] * (MAX_ENUMERATION_RECORDS + 1),
                "has_more": False,
                "next_anchor": None,
            }
        )


@pytest.mark.parametrize(
    ("answer", "expected_type"),
    [
        (
            {"status": "missing", "evidence_ids": [], "reason": "No code exists."},
            MissingCoverage,
        ),
        (
            {
                "status": "complete",
                "evidence_ids": ["evidence-0001"],
                "reason": "Implementation and tests cover it.",
            },
            CompleteCoverage,
        ),
        (
            {
                "status": "partial",
                "evidence_ids": ["evidence-0001"],
                "reason": "Parsing exists.",
                "remaining_statement": "Reject oversized input.",
            },
            PartialCoverage,
        ),
    ],
)
def test_coverage_answers_use_explicit_tagged_variants(
    answer: dict[str, Any], expected_type: type[Any]
) -> None:
    parsed = parse_coverage_answer(answer)
    assert isinstance(parsed, expected_type)
    assert set(COVERAGE_ANSWER_JSON_SCHEMA["oneOf"][0]["properties"]) == {
        "status",
        "evidence_ids",
        "reason",
    }


def test_missing_coverage_has_no_partial_only_remaining_field() -> None:
    parsed = parse_coverage_answer(
        {"status": "missing", "evidence_ids": [], "reason": "No implementation found."}
    )

    assert isinstance(parsed, MissingCoverage)
    with pytest.raises(PlanningContractError):
        parse_coverage_answer(
            {
                "status": "missing",
                "evidence_ids": [],
                "reason": "No implementation found.",
                "remaining_statement": "Implement it.",
            }
        )
    with pytest.raises(PlanningContractError):
        parse_coverage_answer(
            {
                "status": "partial",
                "evidence_ids": ["evidence-1"],
                "reason": "Partial.",
                "remaining_item": {"title": "Implement it"},
            }
        )


def test_human_setup_description_rejects_noop_placeholders_and_pr_id_fields() -> None:
    valid = {
        "title": "Install the GitHub App for the repository",
        "instructions": [
            "Ask a repository administrator to install the app.",
            "Grant access to the target repository.",
        ],
        "verification": "Run the app installation check and confirm the repository ID.",
        "source_refs": ["anchor-0001"],
    }
    parsed = HumanSetupDescriptionAnswer.from_mapping(valid)
    assert parsed.to_data() == valid
    assert set(HUMAN_SETUP_DESCRIPTION_JSON_SCHEMA["properties"]) == set(valid)

    with pytest.raises(PlanningContractError, match="concrete setup"):
        HumanSetupDescriptionAnswer.from_mapping(
            {
                **valid,
                "title": "No manual prerequisite",
            }
        )
    with pytest.raises(PlanningContractError, match="unknown field"):
        HumanSetupDescriptionAnswer.from_mapping({**valid, "unblocks": "pr-02"})


@pytest.mark.parametrize(
    "case",
    [
        "runtime-in-test-subject",
        "bare-work-item",
        "bare-audit-assessment",
        "missing-audit-remaining-item",
        "placeholder-human-setup",
        "bare-structrr-operation",
    ],
)
def test_captured_legacy_responses_do_not_parse_as_new_contracts(case: str) -> None:
    fixture = _fixture(case)
    response = fixture["response"]
    assert "/" not in fixture["provenance"]["source_exchange"]
    assert len(fixture["provenance"]["source_exchange_sha256"]) == 64
    assert "/private/tmp/" not in json.dumps(fixture)

    with pytest.raises(PlanningContractError):
        if case == "placeholder-human-setup":
            HumanSetupDescriptionAnswer.from_mapping(response)
        elif case in {"bare-audit-assessment", "missing-audit-remaining-item"}:
            parse_coverage_answer(response)
        else:
            EnumerationAnswer.from_mapping(response)
