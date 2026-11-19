"""Bounded per-PR test-design questions for reviewed implementation plans."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

import yaml

from powdrr_service.behavior_contract import (
    ASSUMPTION_BASES,
    BEHAVIOR_DIMENSIONS,
    SUPPORTED_ASSUMPTION_DIMENSIONS,
    compile_behavior_scenarios,
)
from powdrr_service.planning.contracts import PlanningContractError, Question
from powdrr_service.planning.questions import (
    MAX_QUESTION_ANSWER_CHARS,
    ContextRecord,
    QuestionRequest,
    SourceExcerpt,
)

TEST_TYPES = ("unit", "integration", "end_to_end", "contract")
_MAX_TEXT_LENGTH = 1_000
_TEST_CASE_SOURCE_FIELDS = frozenset(
    {
        "id",
        "description",
        "status",
        "test_type",
        "scenario",
        "expected_outcome",
        "behavior_scenario",
    }
)
_RAW_BEHAVIOR_SCENARIO_FIELDS = frozenset(
    {
        "subject",
        "given",
        "dimensions",
        "evidence",
        "validator",
        "capability_matrix",
        "assumptions",
    }
)


def exact_source_test_coverage_answer(
    acceptance_criteria: Sequence[str],
    source_excerpts: Sequence[SourceExcerpt],
    *,
    requirement_ids_by_statement: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any] | None:
    """Carry complete typed test cases from a single exact source anchor."""
    criteria = tuple(value.strip() for value in acceptance_criteria)
    if not criteria or len(set(criteria)) != len(criteria) or not source_excerpts:
        return None

    matches: dict[str, list[tuple[SourceExcerpt, Mapping[str, Any]]]] = {
        criterion: [] for criterion in criteria
    }
    for excerpt in source_excerpts:
        document = excerpt.text.strip()
        fenced = re.fullmatch(r"```(?:yaml|yml)\s*\n(.*?)\n```", document, re.S)
        if fenced is not None:
            document = fenced.group(1)
        try:
            value = yaml.safe_load(document)
        except yaml.YAMLError:
            continue
        if not isinstance(value, Mapping) or set(value) != {"required_test_cases"}:
            continue
        raw_cases = value.get("required_test_cases")
        if not isinstance(raw_cases, list):
            continue
        for raw_case in raw_cases:
            if not isinstance(raw_case, Mapping):
                continue
            criterion = raw_case.get("description")
            if (
                isinstance(criterion, str)
                and criterion in matches
                and set(raw_case) == _TEST_CASE_SOURCE_FIELDS
                and isinstance(raw_case.get("id"), str)
                and bool(raw_case["id"].strip())
                and raw_case.get("status") == "planned"
            ):
                matches[criterion].append((excerpt, raw_case))

    if any(len(cases) != 1 for cases in matches.values()):
        return None
    raw_cases: list[dict[str, Any]] = []
    for criterion in criteria:
        excerpt, case = matches[criterion][0]
        raw_behavior = case.get("behavior_scenario")
        if (
            not isinstance(raw_behavior, Mapping)
            or set(raw_behavior) != _RAW_BEHAVIOR_SCENARIO_FIELDS
        ):
            return None
        raw_cases.append(
            {
                "test_type": case.get("test_type"),
                "scenario": case.get("scenario"),
                "expected_outcome": case.get("expected_outcome"),
                "requirement_ids": list(
                    (requirement_ids_by_statement or {}).get(criterion, ())
                ),
                "behavior_scenario": {
                    **dict(raw_behavior),
                    # The containing pinned anchor is the provenance for this
                    # exact typed record; source-authored IDs are not runtime IDs.
                    "evidence": [excerpt.ref_id],
                },
            }
        )
    return {"cases": raw_cases}


def build_test_coverage_request(
    item: Mapping[str, Any],
    *,
    existing_test_paths: Sequence[str] = (),
    source_excerpts: Sequence[SourceExcerpt] = (),
    requirements: Sequence[Mapping[str, Any]] = (),
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
    normalized_criteria = tuple(
        value.strip() for value in cast(Sequence[str], criteria)
    )
    normalized_requirements: list[dict[str, str]] = []
    for index, requirement in enumerate(requirements):
        requirement_id = requirement.get("id")
        statement = requirement.get("statement")
        if (
            not isinstance(requirement_id, str)
            or not requirement_id.strip()
            or not isinstance(statement, str)
            or not statement.strip()
        ):
            raise PlanningContractError(
                f"/requirements/{index}", "must include a non-empty id and statement"
            )
        normalized_requirements.append(
            {"id": requirement_id.strip(), "statement": statement.strip()}
        )
    if len({entry["id"] for entry in normalized_requirements}) != len(
        normalized_requirements
    ):
        raise PlanningContractError("/requirements", "must have unique IDs")
    normalized_requirements.sort(key=lambda entry: entry["id"])
    if not source_excerpts:
        raise PlanningContractError(
            "/source_excerpts",
            "typed behavior scenarios require pinned source evidence",
        )
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
                "requirements": normalized_requirements,
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
                    "required": [
                        "test_type",
                        "scenario",
                        "expected_outcome",
                        "requirement_ids",
                        "behavior_scenario",
                    ],
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
                        "requirement_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "enum": [
                                    requirement["id"]
                                    for requirement in normalized_requirements
                                ],
                            },
                        },
                        "behavior_scenario": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "subject",
                                "given",
                                "dimensions",
                                "evidence",
                                "validator",
                                "capability_matrix",
                                "assumptions",
                            ],
                            "properties": {
                                "subject": {"type": "string", "minLength": 1},
                                "given": {"type": "string", "minLength": 1},
                                "dimensions": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": list(BEHAVIOR_DIMENSIONS),
                                    "properties": {
                                        name: {"type": "string", "minLength": 1}
                                        for name in BEHAVIOR_DIMENSIONS
                                    },
                                },
                                "evidence": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {
                                        "type": "string",
                                        "enum": [
                                            excerpt.ref_id
                                            for excerpt in source_excerpts
                                        ],
                                    },
                                },
                                "validator": {"type": "string", "minLength": 1},
                                "capability_matrix": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "capability",
                                            "behavior",
                                            "evidence",
                                            "error",
                                        ],
                                        "properties": {
                                            "capability": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                            "behavior": {
                                                "type": "string",
                                                "enum": [
                                                    "support",
                                                    "reject",
                                                    "fallback",
                                                ],
                                            },
                                            "evidence": {
                                                "type": "array",
                                                "items": {
                                                    "type": "string",
                                                    "enum": [
                                                        excerpt.ref_id
                                                        for excerpt in source_excerpts
                                                    ],
                                                },
                                            },
                                            "error": {"type": ["string", "null"]},
                                        },
                                    },
                                },
                                "assumptions": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "dimension",
                                            "resolution",
                                            "rationale",
                                            "basis",
                                            "basis_reference",
                                            "confidence",
                                        ],
                                        "properties": {
                                            "dimension": {
                                                "type": "string",
                                                "enum": sorted(
                                                    SUPPORTED_ASSUMPTION_DIMENSIONS
                                                ),
                                            },
                                            "resolution": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                            "rationale": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                            "basis": {
                                                "type": "string",
                                                "enum": sorted(ASSUMPTION_BASES),
                                            },
                                            "basis_reference": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                            "confidence": {
                                                "type": "string",
                                                "enum": ["high", "medium", "low"],
                                            },
                                        },
                                    },
                                },
                            },
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
                "requirement_ids": [],
                "behavior_scenario": {
                    "subject": "The behavior covered by this criterion.",
                    "given": "The source-defined preconditions hold.",
                    "dimensions": {
                        name: "not_applicable" for name in BEHAVIOR_DIMENSIONS
                    },
                    "evidence": [excerpt.ref_id for excerpt in source_excerpts],
                    "validator": "Run the focused test for this criterion.",
                    "capability_matrix": [],
                    "assumptions": [],
                },
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
        cases: list[dict[str, Any]] = []
        for index, (raw_case, description) in enumerate(
            zip(raw_cases, normalized_criteria, strict=True), start=1
        ):
            path = f"/cases/{index - 1}"
            if not isinstance(raw_case, Mapping) or set(raw_case) != {
                "test_type",
                "scenario",
                "expected_outcome",
                "requirement_ids",
                "behavior_scenario",
            }:
                raise PlanningContractError(
                    path,
                    "must contain test_type, scenario, expected_outcome, "
                    "and behavior_scenario",
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
            raw_requirement_ids = raw_case.get("requirement_ids")
            allowed_requirement_ids = {
                requirement["id"] for requirement in normalized_requirements
            }
            if (
                not isinstance(raw_requirement_ids, Sequence)
                or isinstance(raw_requirement_ids, (str, bytes))
                or not all(
                    isinstance(requirement_id, str)
                    and requirement_id in allowed_requirement_ids
                    for requirement_id in raw_requirement_ids
                )
                or len(set(raw_requirement_ids)) != len(raw_requirement_ids)
            ):
                raise PlanningContractError(
                    f"{path}/requirement_ids",
                    "must contain unique IDs allocated to this plan item",
                )
            raw_behavior = raw_case.get("behavior_scenario")
            if not isinstance(raw_behavior, Mapping):
                raise PlanningContractError(
                    f"{path}/behavior_scenario", "must be an object"
                )
            expected_behavior_fields = {
                "subject",
                "given",
                "dimensions",
                "evidence",
                "validator",
                "capability_matrix",
                "assumptions",
            }
            if set(raw_behavior) != expected_behavior_fields:
                raise PlanningContractError(
                    f"{path}/behavior_scenario",
                    "must contain exactly the typed behavior scenario fields",
                )
            evidence = raw_behavior.get("evidence")
            if (
                not isinstance(evidence, Sequence)
                or isinstance(evidence, (str, bytes))
                or not evidence
                or not all(
                    isinstance(ref, str)
                    and ref in {excerpt.ref_id for excerpt in source_excerpts}
                    for ref in evidence
                )
            ):
                raise PlanningContractError(
                    f"{path}/behavior_scenario/evidence",
                    "must cite at least one pinned source excerpt",
                )
            behavior: dict[str, Any] = {
                **dict(raw_behavior),
                "schema_version": "behavior-scenario-v1",
                "scenario_id": f"{item_id}-{index:02d}",
                # These fields duplicate the test design. Bind them from the
                # accepted scenario/outcome so independently phrased model
                # text cannot make an otherwise valid plan unpublishable.
                "when": scenario,
                "then": expected,
                "evidence": list(dict.fromkeys(evidence)),
                "faithfulness_ref": {"fingerprint": f"sha256:{digest}"},
                "routing": "include",
                "capability_matrix": raw_behavior.get("capability_matrix"),
            }
            assumptions = behavior.get("assumptions")
            if isinstance(assumptions, Sequence) and not isinstance(
                assumptions, (str, bytes)
            ):
                normalized_assumptions: list[Any] = []
                for assumption_index, assumption in enumerate(assumptions):
                    if isinstance(assumption, Mapping):
                        assumption_dimension = assumption.get("dimension")
                        dimensions = raw_behavior.get("dimensions")
                        dimension_value = (
                            dimensions.get(assumption_dimension)
                            if isinstance(dimensions, Mapping)
                            and isinstance(assumption_dimension, str)
                            else None
                        )
                        if _is_not_applicable_value(
                            dimension_value
                        ) and _is_not_applicable_value(assumption.get("resolution")):
                            # An irrelevant dimension has no default to record.
                            continue
                    if (
                        isinstance(assumption, Mapping)
                        and assumption.get("basis") not in ASSUMPTION_BASES
                    ):
                        if assumption.get("basis_reference") not in evidence:
                            raise PlanningContractError(
                                f"{path}/behavior_scenario/assumptions/{assumption_index}",
                                "unsupported assumption bases must be grounded in a "
                                "pinned excerpt cited by scenario evidence",
                            )
                        # A source-backed behavior is evidence, not a default
                        # assumption. The dimension and scenario retain it.
                        continue
                    normalized_assumptions.append(assumption)
                behavior["assumptions"] = normalized_assumptions
            try:
                compiled_behavior = compile_behavior_scenarios([behavior])[0]
            except (TypeError, ValueError) as error:
                raise PlanningContractError(
                    f"{path}/behavior_scenario", str(error)
                ) from error
            cases.append(
                {
                    "id": f"AC-{index:02d}",
                    "description": description,
                    "status": "planned",
                    "test_type": str(test_type),
                    "scenario": scenario,
                    "expected_outcome": expected,
                    "requirement_ids": sorted(raw_requirement_ids),
                    "behavior_scenario": compiled_behavior.to_data(),
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
        "contract_version": 5,
    }
    digest = hashlib.sha256(
        json.dumps(digest_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return QuestionRequest(
        question=Question(
            question_id=question_id,
            kind="plan_test_coverage",
            contract_version=5,
            subject_ids=(item_id,),
            input_digest=f"sha256:{digest}",
            context_ref_ids=(
                *(excerpt.ref_id for excerpt in source_excerpts),
                *(fact.ref_id for fact in facts),
            ),
            expected_answer_schema="plan-test-coverage-v5",
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
            "For each case, also provide a typed behavior_scenario with all seven "
            "behavior dimensions explicitly resolved and requirement_ids: the "
            "IDs of supplied requirements whose observable behavior this case "
            "directly verifies. Use an empty list when no supplied requirement "
            "is directly verified; do not include an ID merely because it belongs "
            "to this PR. Never invent requirement IDs. "
            "Use not_applicable only "
            "when the pinned source evidence makes a dimension irrelevant; use "
            "an explicit assumption when repository conventions must resolve an "
            "unspecified dimension. Do not add assumptions for dimensions marked "
            "not_applicable. Ground the scenario in the supplied source "
            "excerpts and cite only relevant excerpt IDs in evidence. Do not list "
            "source-defined behavior as an assumption; include an assumption only "
            "when a repository convention resolves an unspecified dimension. The "
            "planner will bind the behavior scenario's when/then to the test "
            "scenario and expected outcome. "
            "Return cases in exactly the supplied acceptance-criterion order."
        ),
        answer_field_definitions={
            "cases": (
                "One test type, test scenario, expected outcome, and typed "
                "source-grounded behavior scenario per criterion, plus direct "
                "requirement IDs verified by that case."
            ),
        },
        source_excerpts=tuple(source_excerpts),
        repository_evidence=(facts[1],),
        accepted_facts=(facts[0],),
        valid_example=example,
        empty_example=example,
        answer_schema=schema,
        parse_answer=parse,
        # Each required criterion contributes one structured case with seven
        # dimensions and optional evidence detail. Give every case the normal
        # bounded-answer budget instead of applying one budget to the whole set.
        max_answer_chars=MAX_QUESTION_ANSWER_CHARS * len(normalized_criteria),
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


def _is_not_applicable_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    return normalized == "not_applicable" or normalized.startswith(
        ("not_applicable ", "not_applicable-", "not_applicable—", "not_applicable:")
    )


__all__ = ["TEST_TYPES", "build_test_coverage_request"]
