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
    assert "obligation 001: Data initializes on entry." in rendered
    assert "test_verify_entry_initialization" in rendered
    assert "create the exact selectors" not in rendered
    assert "candidate 1: tests/test_state.py::test_existing" not in rendered
    assert "model-authored" not in rendered
    assert "repair the test and the implementation as needed" in rendered
    assert "rerunning it at most twice after a repair" in rendered
    assert (
        "Do not run the full repository suite, coverage, lint, type checks" in rendered
    )
    assert "Workrr owns repository-wide validation" in rendered
    assert "repository validation" not in rendered
    assert packet.from_data(packet.to_data()).to_data() == packet.to_data()


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


def test_packet_can_focus_one_obligation_with_a_later_task_test() -> None:
    packet = compile_implementation_packet(
        objective="Add state data support.",
        obligations=("Initialize data.",),
        required_tests=(
            {
                "description": "Verify the first failing case.",
                "provider": "pytest",
                "profile": "pytest",
            },
            {
                "description": "Verify the second failing case.",
                "provider": "pytest",
                "profile": "pytest",
            },
        ),
        allowed_paths=("src/state.py",),
        validation_profiles=("pytest",),
    )

    focused = packet.for_obligation(1, required_test_ordinal=2)

    assert focused.obligations == ("Initialize data.",)
    assert focused.required_tests[0]["description"] == "Verify the second failing case."
