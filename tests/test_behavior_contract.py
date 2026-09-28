from __future__ import annotations

from typing import Any

import pytest

from powdrr_lift.core.behavior_contract import (
    BEHAVIOR_DIMENSIONS,
    BehaviorContractError,
    compile_behavior_scenarios,
    validate_capability_matrix,
)
from powdrr_lift.core.implementation_packet import compile_implementation_packet
from powdrr_lift.workrr.coding_agent_validation import (
    ValidationReport,
    ValidationReportStatus,
    ValidationResult,
    ValidationResultStatus,
    normalize_failure,
)


def _scenario() -> dict[str, Any]:
    return {
        "scenario_id": "nested-error-continues",
        "subject": "record processor",
        "given": {"result": "nested error with source location"},
        "when": "process this record followed by a valid record",
        "then": {"errors": "preserved with location", "later_record": "processed"},
        "dimensions": {name: "not_applicable" for name in BEHAVIOR_DIMENSIONS},
        "evidence": ["tests/test_processor.py::test_nested_error_and_continuation"],
        "validator": (
            "pytest -q tests/test_processor.py::test_nested_error_and_continuation"
        ),
        "capability_matrix": [],
    }


def test_behavior_scenario_is_rendered_once_as_a_behavior_matrix() -> None:
    scenario = _scenario()
    scenario["dimensions"] = {
        **scenario["dimensions"],
        "error_behavior": {"nested_errors": "preserve locations"},
        "continuation": {"later_records": "continue"},
    }
    packet = compile_implementation_packet(
        objective="process nested results",
        obligations=("preserve nested error details",),
        required_tests=({"description": "errors and continuation"},),
        allowed_paths=("src/", "tests/"),
        validation_profiles=("pytest",),
        behavior_scenarios=(scenario,),
    )
    rendered = packet.render()
    assert rendered.count("nested-error-continues") == 1
    assert '"nested_errors": "preserve locations"' in rendered
    assert "Required behavioral tests:" not in rendered
    restored = type(packet).from_data(packet.to_data())
    assert restored.render() == rendered


def test_behavior_scenario_rejects_an_omitted_dimension() -> None:
    scenario = _scenario()
    scenario["dimensions"] = {"normal_result": "not_applicable"}
    with pytest.raises(BehaviorContractError, match="omits dimensions"):
        compile_behavior_scenarios((scenario,))


def test_behavior_scenario_rejects_implicit_not_applicable() -> None:
    scenario = _scenario()
    scenario["dimensions"] = {**scenario["dimensions"], "compatibility": ""}
    with pytest.raises(BehaviorContractError, match="must be explicit"):
        compile_behavior_scenarios((scenario,))


def test_capability_matrix_requires_evidence_and_rejection_error() -> None:
    with pytest.raises(BehaviorContractError, match="needs a defined error"):
        validate_capability_matrix(
            (
                {
                    "capability": "unsupported context",
                    "behavior": "reject",
                    "evidence": ["test"],
                },
            )
        )
    with pytest.raises(BehaviorContractError, match="must not be empty"):
        validate_capability_matrix(
            (
                {
                    "capability": "supported context",
                    "behavior": "support",
                    "evidence": [],
                },
            )
        )


def test_behavior_scenario_serializes_capability_evidence_as_json_array() -> None:
    scenario = _scenario()
    scenario["capability_matrix"] = [
        {
            "capability": "unsupported declaration",
            "behavior": "reject",
            "evidence": ["instruction-1: invalid declarations raise an error"],
            "error": "InvalidDefinition",
        }
    ]

    compiled = compile_behavior_scenarios((scenario,))[0]

    assert compiled.to_data()["capability_matrix"][0]["evidence"] == [
        "instruction-1: invalid declarations raise an error"
    ]


def test_behavior_scenario_preserves_normative_assumption_provenance() -> None:
    scenario = _scenario()
    scenario["assumptions"] = [
        {
            "dimension": "error_behavior",
            "resolution": "Propagate the parser's native literal error.",
            "rationale": "Preserves the underlying parser failure without masking it.",
            "basis": "language_or_framework_default",
            "basis_reference": "Python ast.literal_eval behavior",
            "confidence": "medium",
        }
    ]

    compiled = compile_behavior_scenarios((scenario,))[0]

    assert compiled.to_data()["assumptions"] == scenario["assumptions"]
    packet = compile_implementation_packet(
        objective="parse literal expressions",
        obligations=("parse supported literal values",),
        required_tests=({"description": "invalid literal handling"},),
        allowed_paths=("src/", "tests/"),
        validation_profiles=("pytest",),
        behavior_scenarios=(scenario,),
    )
    assert '"basis_reference": "Python ast.literal_eval behavior"' in packet.render()


def test_non_rejecting_capabilities_do_not_emit_irrelevant_error_values() -> None:
    matrix = validate_capability_matrix(
        [
            {
                "capability": "ordinary mapping",
                "behavior": "support",
                "evidence": ["source requires a data mapping"],
                "error": "./././",
            }
        ]
    )

    assert "error" not in matrix[0]


def test_validation_report_exposes_structured_repair_failures() -> None:
    report = ValidationReport(
        attempt_id="attempt-1",
        request_id="request-1",
        status=ValidationReportStatus.FAILED,
        results=(
            ValidationResult(
                profile="pytest",
                command=("pytest", "-q", "tests/test_processor.py"),
                status=ValidationResultStatus.FAILED,
                returncode=1,
                error="validation command failed",
                test_id="tests/test_processor.py::test_nested_error",
                expected="nested errors preserved",
                actual="nested error missing",
            ),
        ),
    )
    failure = report.to_data()["failures"][0]
    assert failure["stage"] == "local_validation"
    assert failure["test_id"] == "tests/test_processor.py::test_nested_error"
    assert failure["expected"] == "nested errors preserved"
    assert failure["actual"] == "nested error missing"
    verifier_failure = normalize_failure(
        failure_id="benchmark:case-1",
        stage="verifier",
        test_id="case-1",
        expected="behavior succeeds",
        actual="behavior regressed",
    )
    assert verifier_failure.keys() == failure.keys()
    assert verifier_failure["stage"] == "verifier"
