"""Bounded per-PR test-design questions for reviewed implementation plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_service.planning.contracts import PlanningContractError, Question
from powdrr_service.planning.questions import (
    ContextRecord,
    QuestionRequest,
    SourceExcerpt,
)

TEST_TYPES = ("unit", "integration", "end_to_end", "contract")
_MAX_TEXT_LENGTH = 1_000


def build_test_coverage_request(
    item: Mapping[str, Any],
    *,
    existing_test_paths: Sequence[str] = (),
    source_excerpts: Sequence[SourceExcerpt] = (),
) -> QuestionRequest[dict[str, Any]]:
    """Ask for one concrete test design per accepted PR criterion."""
    item_id = item.get("id")
    criteria = item.get("acceptance_criteria")
    if not isinstance(item_id, str) or not item_id.strip():
        raise PlanningContractError("/id", "must be a non-empty plan item ID")
    if (
        not isinstance(criteria, Sequence)
        or isinstance(criteria, (str, bytes))
        or not criteria
        or not all(isinstance(value, str) and value.strip() for value in criteria)
    ):
        raise PlanningContractError(
            "/acceptance_criteria", "must contain non-empty criteria"
        )
    normalized_criteria = tuple(value.strip() for value in criteria)
    test_paths = tuple(sorted(set(existing_test_paths))[:40])
    facts = (
        ContextRecord(
            f"planned-pr:{item_id}",
            {
                "id": item_id,
                "title": item.get("title"),
                "objective": item.get("objective"),
                "scope": list(item.get("scope", [])),
                "implementation_steps": list(item.get("implementation_steps", [])),
                "acceptance_criteria": list(normalized_criteria),
                "allowed_paths": list(item.get("allowed_paths", [])),
            },
        ),
        ContextRecord("existing-test-paths", {"paths": list(test_paths)}),
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["cases"],
        "properties": {
            "cases": {
                "type": "array",
                "minItems": len(normalized_criteria),
                "maxItems": len(normalized_criteria),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["test_type", "scenario", "expected_outcome"],
                    "properties": {
                        "test_type": {"type": "string", "enum": list(TEST_TYPES)},
                        "scenario": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_TEXT_LENGTH,
                        },
                        "expected_outcome": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_TEXT_LENGTH,
                        },
                    },
                },
            }
        },
    }
    example = {
        "cases": [
            {
                "test_type": "integration",
                "scenario": "Exercise the observable behavior for this criterion.",
                "expected_outcome": (
                    "The externally visible result satisfies the criterion."
                ),
            }
            for _criterion in normalized_criteria
        ]
    }

    def parse(value: Mapping[str, Any]) -> dict[str, Any]:
        if set(value) != {"cases"}:
            raise PlanningContractError("/", "must contain only cases")
        raw_cases = value.get("cases")
        if (
            not isinstance(raw_cases, Sequence)
            or isinstance(raw_cases, (str, bytes))
            or len(raw_cases) != len(normalized_criteria)
        ):
            raise PlanningContractError(
                "/cases", "must have exactly one test design per acceptance criterion"
            )
        cases: list[dict[str, str]] = []
        for index, (raw_case, description) in enumerate(
            zip(raw_cases, normalized_criteria, strict=True), start=1
        ):
            path = f"/cases/{index - 1}"
            if not isinstance(raw_case, Mapping) or set(raw_case) != {
                "test_type",
                "scenario",
                "expected_outcome",
            }:
                raise PlanningContractError(
                    path, "must contain test_type, scenario, and expected_outcome"
                )
            test_type = raw_case.get("test_type")
            if test_type not in TEST_TYPES:
                raise PlanningContractError(
                    f"{path}/test_type", "must select a supported test type"
                )
            scenario = _required_text(raw_case.get("scenario"), f"{path}/scenario")
            expected = _required_text(
                raw_case.get("expected_outcome"), f"{path}/expected_outcome"
            )
            cases.append(
                {
                    "id": f"AC-{index:02d}",
                    "description": description,
                    "status": "planned",
                    "test_type": str(test_type),
                    "scenario": scenario,
                    "expected_outcome": expected,
                }
            )
        return {"required_test_cases": cases, "validation_runs": []}

    question_id = f"plan-tests-{item_id}"
    digest_data = {
        "item": facts[0].value,
        "existing_test_paths": list(test_paths),
        "source_excerpts": [
            {
                "ref_id": excerpt.ref_id,
                "text": excerpt.text,
                "start_offset": excerpt.start_offset,
            }
            for excerpt in source_excerpts
        ],
        "contract_version": 1,
    }
    digest = hashlib.sha256(
        json.dumps(digest_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return QuestionRequest(
        question=Question(
            question_id=question_id,
            kind="plan_test_coverage",
            contract_version=1,
            subject_ids=(item_id,),
            input_digest=f"sha256:{digest}",
            context_ref_ids=(
                *(excerpt.ref_id for excerpt in source_excerpts),
                *(fact.ref_id for fact in facts),
            ),
            expected_answer_schema="plan-test-coverage-v1",
            attempt=1,
        ),
        exact_question=(
            "Design the tests for this one reviewed PR. For every acceptance "
            "criterion, choose the narrowest useful test type (unit, integration, "
            "end-to-end, or contract), then describe one concrete scenario and "
            "its observable expected outcome. Use the existing test paths as "
            "repository conventions, not as proof that coverage already exists. "
            "Prefer an existing public boundary and realistic behavior assertions. "
            "Do not repeat or rewrite the acceptance criterion, propose test IDs, "
            "invent behavior, or choose a broader test type without a reason. "
            "Return cases in exactly the supplied acceptance-criterion order."
        ),
        answer_field_definitions={
            "cases": "One test type, scenario, and expected outcome per criterion.",
        },
        source_excerpts=tuple(source_excerpts),
        repository_evidence=(facts[1],),
        accepted_facts=(facts[0],),
        valid_example=example,
        empty_example=example,
        answer_schema=schema,
        parse_answer=parse,
    )


def _required_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningContractError(path, "must be non-empty text")
    normalized = " ".join(value.split())
    if len(normalized) > _MAX_TEXT_LENGTH:
        raise PlanningContractError(path, "exceeds the 1,000-character limit")
    if normalized.startswith(("TODO", "TBD", "<")):
        raise PlanningContractError(path, "must contain a concrete test design")
    return normalized


__all__ = ["TEST_TYPES", "build_test_coverage_request"]
