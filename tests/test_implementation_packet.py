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
