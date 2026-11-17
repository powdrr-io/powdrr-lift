from __future__ import annotations

import pytest
from powdrr_service.planning.questions import SourceExcerpt
from powdrr_service.planning.test_coverage import build_test_coverage_request


def _behavior(evidence_ref: str = "anchor-ci") -> dict[str, object]:
    return {
        "subject": "The acceptance criterion behavior.",
        "given": "The source-defined preconditions hold.",
        "evidence": [evidence_ref],
        "dimensions": {
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
        },
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
    )
    answer = {
        "cases": [
            {
                "test_type": "integration",
                "scenario": "Parse the workflow and inspect its pull_request triggers.",
                "expected_outcome": "Pull requests trigger the verification workflow.",
                "behavior_scenario": _behavior(),
            },
            {
                "test_type": "contract",
                "scenario": "Inspect the verification step in the CI job.",
                "expected_outcome": (
                    "The configured step invokes the repository test command."
                ),
                "behavior_scenario": _behavior(),
            },
        ]
    }

    request.validate()
    planned = request.parse_answer(answer)

    assert request.question.kind == "plan_test_coverage"
    assert request.question.contract_version == 4
    assert request.question.expected_answer_schema == "plan-test-coverage-v4"
    assert request.max_answer_chars == 8_000
    assert [case["id"] for case in planned["required_test_cases"]] == [
        "AC-01",
        "AC-02",
    ]
    assert planned["required_test_cases"][0]["test_type"] == "integration"
    assert planned["required_test_cases"][1]["scenario"] == (
        "Inspect the verification step in the CI job."
    )
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
                    "behavior_scenario": behavior,
                }
            ]
        }
    )

    compiled = planned["required_test_cases"][0]["behavior_scenario"]
    assert compiled.get("assumptions", []) == []
    assert compiled["evidence"] == ["anchor-ci"]
