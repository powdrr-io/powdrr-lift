from __future__ import annotations

import pytest
from powdrr_service.planning.test_coverage import build_test_coverage_request


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
    )
    answer = {
        "cases": [
            {
                "test_type": "integration",
                "scenario": "Parse the workflow and inspect its pull_request triggers.",
                "expected_outcome": "Pull requests trigger the verification workflow.",
            },
            {
                "test_type": "contract",
                "scenario": "Inspect the verification step in the CI job.",
                "expected_outcome": (
                    "The configured step invokes the repository test command."
                ),
            },
        ]
    }

    request.validate()
    planned = request.parse_answer(answer)

    assert request.question.kind == "plan_test_coverage"
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


def test_planner_rejects_missing_or_unsupported_test_designs() -> None:
    request = build_test_coverage_request(
        {
            "id": "item-0001",
            "acceptance_criteria": ["The behavior works."],
        }
    )

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
                    }
                ]
            }
        )
