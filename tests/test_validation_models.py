from __future__ import annotations

from powdrr_lift.structrr.validation_models import (
    LEGACY_VALIDATION_INVENTORY_SCHEMA_VERSION,
    VALIDATION_CONTEXT_SCHEMA_VERSION,
    VALIDATION_INVENTORY_SCHEMA_VERSION,
    ValidationCheck,
    ValidationContext,
    validate_validation_records,
)


def _check_data(
    check_id: str = "validation:unit",
    *,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": check_id,
        "schema_version": VALIDATION_INVENTORY_SCHEMA_VERSION,
        "provider": "pytest",
        "profile": "unit",
        "command": ["python", "-m", "pytest", "tests"],
        "source": "pyproject.toml",
        "execution": {"kind": "argv", "cwd": ".", "shell": None, "script": None},
        "selectors": [],
        "requiredness": {"status": "unknown", "evidence": []},
        "applicability": {"evaluation": "unknown"},
        "provenance": {"declaration": "declared", "evidence": []},
        "confirmation": {"level": "static", "observations": []},
        "baseline": {"status": "not_run", "observation": None},
        "depends_on": depends_on or [],
    }


def _context_data() -> dict[str, object]:
    return ValidationContext().to_data()


def test_v2_check_round_trips_and_inventory_validates() -> None:
    data = _check_data()

    check = ValidationCheck.from_data(data)

    assert check.to_data()["id"] == "validation:unit"
    assert validate_validation_records([data], _context_data()) == ()


def test_v1_inventory_loads_without_claiming_execution_context_or_confirmation() -> (
    None
):
    old = {
        "schema_version": LEGACY_VALIDATION_INVENTORY_SCHEMA_VERSION,
        "provider": "pytest",
        "profile": "unit",
        "command": ["pytest", "tests"],
        "selectors": [],
    }

    check = ValidationCheck.from_data(old)

    assert check.schema_version == VALIDATION_INVENTORY_SCHEMA_VERSION
    assert check.execution["cwd"] == "."
    assert check.confirmation["level"] == "static"
    assert check.provenance["declaration"] == "unknown"
    assert check.unresolved
    assert check.to_data()["schema_version"] == VALIDATION_INVENTORY_SCHEMA_VERSION


def test_v2_rejects_malformed_execution_and_duplicate_ids() -> None:
    malformed = _check_data()
    malformed["execution"] = {
        "kind": "argv",
        "cwd": "../escape",
        "shell": "/bin/sh",
        "script": "echo unsafe shape",
    }

    issues = validate_validation_records([malformed, _check_data()], _context_data())

    assert any(issue.code == "execution_cwd_invalid" for issue in issues)
    assert any(issue.code == "execution_argv_shape_invalid" for issue in issues)
    assert any(issue.code == "validation_inventory_id_duplicate" for issue in issues)


def test_dependency_cycle_is_reported() -> None:
    first = _check_data("validation:first", depends_on=["validation:second"])
    second = _check_data("validation:second", depends_on=["validation:first"])

    issues = validate_validation_records([first, second], _context_data())

    assert any(issue.code == "validation_dependency_cycle" for issue in issues)


def test_context_schema_version_is_required() -> None:
    context = _context_data()
    context["schema_version"] = "unknown"

    issues = validate_validation_records([], context)

    assert VALIDATION_CONTEXT_SCHEMA_VERSION == "validation-context-v1"
    assert any(
        issue.code == "validation_context_schema_unsupported" for issue in issues
    )


def test_component_environment_and_evidence_references_must_resolve() -> None:
    check = _check_data()
    check["component"] = "component:missing"
    check["environment"] = "environment:missing"
    check["provenance"] = {
        "declaration": "declared",
        "evidence": ["evidence:missing"],
    }

    issues = validate_validation_records([check], _context_data())

    codes = {issue.code for issue in issues}
    assert "validation_component_reference_invalid" in codes
    assert "validation_environment_reference_invalid" in codes
    assert "validation_evidence_reference_invalid" in codes
