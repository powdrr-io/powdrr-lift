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
        "related_requirements": [],
        "dimensions": {name: "not_applicable" for name in BEHAVIOR_DIMENSIONS},
        "evidence": ["tests/test_processor.py::test_nested_error_and_continuation"],
        "validator": (
            "pytest -q tests/test_processor.py::test_nested_error_and_continuation"
        ),
        "capability_matrix": [],
    }


def test_behavior_scenario_is_rendered_as_a_concrete_check() -> None:
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
    assert "Given result: nested error with source location" in rendered
    assert "when process this record followed by a valid record" in rendered
    assert "expect errors: preserved with location; later_record: processed" in rendered
    assert "Related requirement:" not in rendered
    assert "nested_errors: preserve locations" not in rendered
    assert "later_records: continue" not in rendered
    assert "not_applicable" not in rendered
    assert '"scenario"' not in rendered
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
    assert packet.behavior_scenarios[0].assumptions[0]["basis_reference"] == (
        "Python ast.literal_eval behavior"
    )
    assert "error_behavior: Propagate the parser's native literal error." in (
        packet.render()
    )


def test_worker_check_includes_defaults_capabilities_and_execution_paths() -> None:
    scenario = _scenario()
    scenario["dimensions"] = {
        **scenario["dimensions"],
        "normal_result": "The later record is processed.",
    }
    scenario["assumptions"] = [
        {
            "dimension": "negative_boundaries",
            "resolution": "An invalid record does not abort the batch.",
            "rationale": "The operation is isolated per record.",
            "basis": "conservative_default",
            "basis_reference": "No batch failure behavior was specified.",
            "confidence": "medium",
        }
    ]
    scenario["capability_matrix"] = [
        {
            "capability": "invalid records",
            "behavior": "reject",
            "error": "InvalidRecord",
            "evidence": ["test invalid input"],
        }
    ]
    packet = compile_implementation_packet(
        objective="process records",
        obligations=("process records",),
        required_tests=({"description": "process records"},),
        allowed_paths=("src/",),
        validation_profiles=("pytest",),
        behavior_scenarios=(scenario,),
    )

    rendered = packet.render()
    assert "synchronous and asynchronous implementations" in rendered
    assert "Defaults for behavior the source leaves unspecified:" in rendered
    assert (
        "negative_boundaries: An invalid record does not abort the batch." in rendered
    )
    assert "Capabilities: reject invalid records with InvalidRecord" in rendered
    assert "Do not treat a passing test on one execution path" in rendered


def test_worker_check_preserves_explicit_cross_requirement_relationships() -> None:
    scenario = _scenario()
    scenario["related_requirements"] = [
        "The public operation exposes this result through the existing adapter."
    ]
    packet = compile_implementation_packet(
        objective="implement the operation",
        obligations=("implement the operation",),
        required_tests=({"description": "exercise the adapter"},),
        allowed_paths=("src/", "tests/"),
        validation_profiles=("pytest",),
        behavior_scenarios=(scenario,),
    )

    rendered = packet.render()

    assert (
        "Related requirement: The public operation exposes this result through "
        "the existing adapter."
    ) in rendered
    restored = type(packet).from_data(packet.to_data())
    assert restored.render() == rendered


def test_normative_assumption_cannot_claim_not_applicable_as_a_default() -> None:
    scenario = _scenario()
    scenario["assumptions"] = [
        {
            "dimension": "cancellation_cleanup",
            "resolution": "not_applicable",
            "rationale": "The operation is synchronous.",
            "basis": "conservative_default",
            "basis_reference": "No cancellation source applies.",
            "confidence": "high",
        }
    ]

    with pytest.raises(BehaviorContractError, match="cannot resolve to not_applicable"):
        compile_behavior_scenarios((scenario,))


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
