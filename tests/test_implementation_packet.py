from __future__ import annotations

import pytest

from powdrr_lift.core.implementation_packet import compile_implementation_packet


def test_packet_renders_bounded_obligations_and_exact_selectors() -> None:
    packet = compile_implementation_packet(
        objective="Add state data support.",
        obligations=("Data initializes on entry.",),
        required_tests=(
            {
                "description": "Verify entry initialization.",
                "provider": "pytest",
                "profile": "pytest",
                "selector": "tests/test_state.py::test_entry",
            },
        ),
        allowed_paths=("src/state.py", "tests/test_state.py"),
        validation_profiles=("pytest",),
        existing_tests=(
            {
                "provider": "pytest",
                "profile": "pytest",
                "selector": "tests/test_state.py::test_existing",
                "fingerprint": "sha256:test",
            },
        ),
    )

    rendered = packet.render()
    assert "Required behavioral tests:" in rendered
    assert "obligation 001" not in rendered
    assert "Add state data support." not in rendered
    assert "IMPORTANT:" not in rendered
    assert "tests/test_state.py::test_entry" in rendered
    assert "Verify entry initialization." in rendered
    assert "create the exact selectors" not in rendered
    assert "candidate 1: tests/test_state.py::test_existing" not in rendered
    assert "model-authored" not in rendered
    assert "repair the test and implementation as needed" in rendered
    assert "repository validation" not in rendered
    assert packet.from_data(packet.to_data()).to_data() == packet.to_data()


def test_packet_removes_source_workflow_instructions_from_objective() -> None:
    packet = compile_implementation_packet(
        objective=(
            "Add behavior.\n\n"
            "IMPORTANT: create a branch from main and commit everything."
        ),
        obligations=("The behavior exists.",),
        required_tests=(
            {
                "description": "Verify the behavior.",
                "provider": "pytest",
                "profile": "pytest",
                "selector": "tests/test_feature.py::test_behavior",
            },
        ),
        allowed_paths=("src",),
        validation_profiles=("pytest",),
    )

    assert packet.objective == "Add behavior."


def test_packet_requires_a_contract_for_each_obligation() -> None:
    with pytest.raises(ValueError, match="requires test contracts"):
        compile_implementation_packet(
            objective="Add behavior.",
            obligations=("The behavior exists.",),
            required_tests=(),
            allowed_paths=("src/feature.py",),
            validation_profiles=("pytest",),
        )


def test_packet_can_focus_one_obligation_and_its_matching_test() -> None:
    packet = compile_implementation_packet(
        objective="Add state data support.",
        obligations=("Initialize data.", "Reset data."),
        required_tests=(
            {
                "description": "Verify initialization.",
                "provider": "pytest",
                "profile": "pytest",
            },
            {
                "description": "Verify reset.",
                "provider": "pytest",
                "profile": "pytest",
            },
        ),
        allowed_paths=("src/state.py",),
        validation_profiles=("pytest",),
    )

    focused = packet.for_obligation(2)

    assert focused.obligations == ("Reset data.",)
    assert focused.required_tests[0]["description"] == "Verify reset."
    assert "Initialize data." not in focused.render()
    assert "Verify initialization." not in focused.render()
