from __future__ import annotations

import pytest
import yaml
from powdrr_service.planning.contracts import PlanningContractError
from powdrr_service.planning.questions import SourceExcerpt
from powdrr_service.planning.test_coverage import (
    build_test_coverage_request,
    exact_source_test_coverage_answer,
)


def _dimensions() -> dict[str, str]:
    return {
        name: "not_applicable"
        for name in (
            "normal_result",
            "error_behavior",
            "continuation",
            "unsupported_behavior",
            "cancellation_cleanup",
            "compatibility",
            "negative_boundaries",
        )
    }


def _behavior(evidence_ref: str = "anchor-ci") -> dict[str, object]:
    return {
        "subject": "The acceptance criterion behavior.",
        "given": "The source-defined preconditions hold.",
        "evidence": [evidence_ref],
        "dimensions": _dimensions(),
        "capability_matrix": [],
        "assumptions": [],
        "validator": "Run the focused test for this behavior.",
    }


def test_planner_assigns_stable_ids_to_llm_test_designs() -> None:
    request = build_test_coverage_request(
        {
            "id": "item-0001",
            "title": "Preserve CI triggers",
            "objective": "Run verification on pull requests.",
            "scope": ["Configure GitHub Actions."],
            "implementation_steps": ["Update the CI workflow."],
            "acceptance_criteria": [
                "The workflow runs on pull requests.",
                "The workflow runs the required test command.",
            ],
            "allowed_paths": [".github/workflows", "tests"],
        },
        existing_test_paths=("tests/test_ci.py",),
        source_excerpts=(SourceExcerpt("anchor-ci", "CI runs on pull requests."),),
        requirements=(
            {
                "id": "req-ci-trigger",
                "statement": "The workflow runs on pull requests.",
                "source_validation_groups": [
                    {"id": "validation:ci:1", "relation": "alternatives"}
                ],
            },
            {
                "id": "req-test-command",
                "statement": "The workflow runs the required test command.",
            },
        ),
    )
    answer = {
        "cases": [
            {
                "test_type": "integration",
                "scenario": "Parse the workflow and inspect its pull_request triggers.",
                "expected_outcome": "Pull requests trigger the verification workflow.",
                "requirement_ids": ["req-ci-trigger"],
                "behavior_scenario": _behavior(),
            },
            {
                "test_type": "contract",
                "scenario": "Inspect the verification step in the CI job.",
                "expected_outcome": (
                    "The configured step invokes the repository test command."
                ),
                "requirement_ids": ["req-test-command"],
                "behavior_scenario": _behavior(),
            },
        ]
    }

    request.validate()
    planned = request.parse_answer(answer)

    assert request.question.kind == "plan_test_coverage"
    assert request.question.contract_version == 6
    assert request.question.expected_answer_schema == "plan-test-coverage-v6"
    assert request.max_answer_chars == 8_000
    item_context = next(
        fact for fact in request.accepted_facts if fact.ref_id == "planned-pr:item-0001"
    )
    assert item_context.value["requirements"][0]["source_validation_groups"] == [
        {"id": "validation:ci:1", "relation": "alternatives"}
    ]
    assert [case["id"] for case in planned["required_test_cases"]] == [
        "AC-01",
        "AC-02",
    ]
    assert planned["required_test_cases"][0]["test_type"] == "integration"
    assert planned["required_test_cases"][1]["scenario"] == (
        "Inspect the verification step in the CI job."
    )
    assert [case["requirement_ids"] for case in planned["required_test_cases"]] == [
        ["req-ci-trigger"],
        ["req-test-command"],
    ]
    assert [case["description"] for case in planned["required_test_cases"]] == [
        "The workflow runs on pull requests.",
        "The workflow runs the required test command.",
    ]
    behavior = planned["required_test_cases"][0]["behavior_scenario"]
    assert behavior["scenario_id"] == "item-0001-01"
    assert behavior["evidence"] == ["anchor-ci"]
    assert behavior["faithfulness_ref"]["fingerprint"].startswith("sha256:")
    assert behavior["when"] == planned["required_test_cases"][0]["scenario"]
    assert behavior["then"] == planned["required_test_cases"][0]["expected_outcome"]


def test_unknown_test_dimension_is_carried_as_unresolved() -> None:
    request = build_test_coverage_request(
        {
            "id": "item-unknown-dimension",
            "title": "Handle unknown failure behavior",
            "objective": "Design a source-grounded test.",
            "scope": ["Exercise the behavior."],
            "implementation_steps": ["Inspect existing behavior."],
            "acceptance_criteria": ["The behavior remains source-faithful."],
            "allowed_paths": ["src", "tests"],
        },
        source_excerpts=(
            SourceExcerpt(
                "anchor-unknown-dimension",
                "The behavior is required, but error handling is not specified.",
            ),
        ),
    )
    dimensions = _dimensions()
    dimensions["error_behavior"] = "unresolved"
    behavior = _behavior("anchor-unknown-dimension")
    behavior["dimensions"] = dimensions
    answer = {
        "cases": [
            {
                "test_type": "unit",
                "scenario": "Exercise the required behavior.",
                "expected_outcome": "The required behavior is preserved.",
                "requirement_ids": [],
                "behavior_scenario": behavior,
            }
        ]
    }

    planned = request.parse_answer(answer)

    assert planned["required_test_cases"][0]["behavior_scenario"][
        "unresolved_dimensions"
    ] == ["error_behavior"]

    contradictory_behavior = _behavior("anchor-unknown-dimension")
    contradictory_behavior["dimensions"] = dimensions
    contradictory_behavior["assumptions"] = [
        {
            "dimension": "error_behavior",
            "resolution": "Use ValueError.",
            "rationale": "Common library convention.",
            "basis": "repository_convention",
            "basis_reference": "the parser tests",
            "confidence": "medium",
        }
    ]
    contradictory = {
        "cases": [
            {
                "test_type": "unit",
                "scenario": "Exercise the required behavior.",
                "expected_outcome": "The required behavior is preserved.",
                "requirement_ids": [],
                "behavior_scenario": contradictory_behavior,
            }
        ]
    }
    with pytest.raises(PlanningContractError, match="also marked unresolved"):
        request.parse_answer(contradictory)


def test_complete_typed_source_test_case_is_carried_without_restatement() -> None:
    criterion = "The account is locked after the configured failures."
    source_behavior = _behavior("stale-untrusted-reference")
    source_behavior["evidence"] = []
    source_dimensions = _dimensions()
    source_dimensions["error_behavior"] = "unresolved"
    source_behavior["dimensions"] = source_dimensions
    source_case = {
        "id": "source-case-7",
        "description": criterion,
        "status": "planned",
        "test_type": "integration",
        "scenario": "Submit the configured number of invalid credentials.",
        "expected_outcome": "The account enters the locked state.",
        "behavior_scenario": source_behavior,
    }
    source_excerpt = SourceExcerpt(
        "anchor-locked-account",
        "```yaml\n"
        + yaml.safe_dump(
            {"required_test_cases": [source_case]}, sort_keys=False
        ).rstrip()
        + "\n```",
    )
    item = {
        "id": "item-0001",
        "title": "Lock accounts after repeated failures",
        "objective": "Apply the configured account lockout policy.",
        "scope": ["Authentication behavior."],
        "implementation_steps": ["Enforce failed-attempt limits."],
        "acceptance_criteria": [criterion],
        "allowed_paths": ["src/auth", "tests"],
    }
    request = build_test_coverage_request(
        item,
        source_excerpts=(source_excerpt,),
    )

    answer = exact_source_test_coverage_answer([criterion], (source_excerpt,))

    assert answer is not None
    planned = request.parse_answer(answer)
    case = planned["required_test_cases"][0]
    assert case["id"] == "AC-01"
    assert case["description"] == criterion
    assert case["scenario"] == source_case["scenario"]
    assert case["behavior_scenario"]["evidence"] == [source_excerpt.ref_id]
    assert case["behavior_scenario"]["unresolved_dimensions"] == ["error_behavior"]
    assert case["behavior_scenario"]["when"] == source_case["scenario"]


def test_typed_source_case_requires_exact_unambiguous_criterion_match() -> None:
    criterion = "The account is locked after the configured failures."
    incomplete = SourceExcerpt(
        "anchor-incomplete",
        "required_test_cases:\n"
        "  - description: The account is locked after the configured failures.\n"
        "    test_type: integration\n",
    )

    assert exact_source_test_coverage_answer([criterion], (incomplete,)) is None


def test_planner_rejects_missing_or_unsupported_test_designs() -> None:
    with pytest.raises(ValueError, match="pinned source evidence"):
        build_test_coverage_request(
            {
                "id": "item-0001",
                "acceptance_criteria": ["The behavior works."],
            }
        )
    request = build_test_coverage_request(
        {
            "id": "item-0001",
            "acceptance_criteria": ["The behavior works."],
        },
        source_excerpts=(SourceExcerpt("anchor-behavior", "The behavior works."),),
    )
    assert request.max_answer_chars == 4_000

    with pytest.raises(ValueError, match="exactly one test design"):
        request.parse_answer({"cases": []})
    with pytest.raises(ValueError, match="supported test type"):
        request.parse_answer(
            {
                "cases": [
                    {
                        "test_type": "manual",
                        "scenario": "Check the behavior.",
                        "expected_outcome": "The behavior works.",
                        "requirement_ids": [],
                        "behavior_scenario": _behavior("anchor-behavior"),
                    }
                ]
            }
        )


def test_planner_does_not_store_source_backed_behavior_as_an_assumption() -> None:
    request = build_test_coverage_request(
        {
            "id": "item-0001",
            "acceptance_criteria": ["Denial paths are covered."],
        },
        source_excerpts=(SourceExcerpt("anchor-ci", "Denial paths are covered."),),
    )
    behavior = _behavior()
    behavior["assumptions"] = [
        {
            "dimension": "negative_boundaries",
            "resolution": "Denial paths are tested as negative scenarios.",
            "rationale": "The pinned source requires denial path coverage.",
            "basis": "source_defined",
            "basis_reference": "anchor-ci",
            "confidence": "high",
        },
        {
            "dimension": "cancellation_cleanup",
            "resolution": "not_applicable",
            "rationale": "No cancellation behavior is described.",
            "basis": "repository_convention",
            "basis_reference": "existing-test-paths",
            "confidence": "medium",
        },
    ]

    planned = request.parse_answer(
        {
            "cases": [
                {
                    "test_type": "unit",
                    "scenario": "Exercise a denial path.",
                    "expected_outcome": "The denial behavior is covered.",
                    "requirement_ids": [],
                    "behavior_scenario": behavior,
                }
            ]
        }
    )

    compiled = planned["required_test_cases"][0]["behavior_scenario"]
    assert compiled.get("assumptions", []) == []
    assert compiled["evidence"] == ["anchor-ci"]
