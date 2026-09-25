from __future__ import annotations

import pytest

from powdrr_lift.core.implementation_packet import compile_implementation_packet


def test_packet_renders_behavioral_test_descriptions_without_selectors() -> None:
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
    assert "add a focused test proving Verify entry initialization." in rendered
    assert "tests/test_state.py::test_entry" not in rendered
    assert "pytest/pytest" not in rendered
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


def test_packet_can_focus_a_compiled_code_task() -> None:
    packet = compile_implementation_packet(
        objective="Implement the entire feature.",
        obligations=("The entire feature exists.",),
        required_tests=({"description": "Verify the entire feature."},),
        allowed_paths=("src/state.py",),
        validation_profiles=("pytest",),
    )

    focused = packet.for_task(
        objective="Implement the reset behavior.",
        acceptance_criteria=("The reset behavior works.",),
    )

    assert focused.objective == "Implement the reset behavior."
    assert focused.obligations == ("Implement the reset behavior.",)
    assert focused.required_tests == ({"description": "The reset behavior works."},)
    assert "Implement the entire feature." not in focused.render()
    assert "Verify the entire feature." not in focused.render()
    assert "The reset behavior works." in focused.render()


def test_packet_prompt_contains_every_required_test_without_compiler_metadata() -> None:
    packet = compile_implementation_packet(
        objective="Add state data support.",
        obligations=("Initialize data on entry.", "Reset data on re-entry."),
        required_tests=(
            {
                "description": "Verify initialization.",
                "provider": "pytest",
                "profile": "pytest",
                "name_hint": "test_state_data_initialization",
                "selector": "tests/test_state.py::test_initialization",
            },
            {
                "description": "Verify reset.",
                "provider": "pytest",
                "profile": "pytest",
                "name_hint": "test_state_data_reset",
                "selector": "tests/test_state.py::test_reset",
            },
        ),
        allowed_paths=("src/state.py", "tests/test_state.py"),
        validation_profiles=("pytest",),
    )

    rendered = packet.render()

    for expected in (
        "add a focused test proving Verify initialization.",
        "add a focused test proving Verify reset.",
    ):
        assert expected in rendered
    assert "tests/test_state.py::test_initialization" not in rendered
    assert "tests/test_state.py::test_reset" not in rendered
    assert "pytest/pytest" not in rendered

    assert "Initialize data on entry." not in rendered
    assert "Reset data on re-entry." not in rendered
