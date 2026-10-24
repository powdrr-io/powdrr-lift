from __future__ import annotations

from powdrr_lift.workrr.candidate_safety import (
    existing_test_contract_changes,
    unapproved_public_default_changes,
)


def test_existing_test_body_changes_are_reported() -> None:
    baseline = """\
import pytest

class TestCompatibility:
    def test_event_without_transition_raises(self):
        with pytest.raises(RuntimeError):
            machine.send('unknown')
"""
    candidate = """\
import pytest

class TestCompatibility:
    def test_event_without_transition_raises(self):
        assert machine.send('unknown') is None
"""

    findings = existing_test_contract_changes(
        "tests/test_compat.py",
        baseline,
        candidate,
        accepted_contract="Add state data support.",
    )

    assert findings == (
        "existing test contract changed in tests/test_compat.py: "
        "function:TestCompatibility.test_event_without_transition_raises",
    )


def test_adding_a_test_does_not_change_existing_test_contract() -> None:
    baseline = """\
def test_existing_behavior():
    assert public_api() is True
"""
    candidate = (
        baseline + "\n\ndef test_new_feature():\n    assert public_api() is False\n"
    )

    assert (
        existing_test_contract_changes(
            "tests/test_feature.py",
            baseline,
            candidate,
            accepted_contract="Add a new feature.",
        )
        == ()
    )


def test_renaming_an_existing_test_is_reported() -> None:
    findings = existing_test_contract_changes(
        "tests/test_compat.py",
        "def test_old_behavior():\n    assert api()\n",
        "def test_new_behavior():\n    assert api()\n",
        accepted_contract="Add state data support.",
    )

    assert findings == (
        "existing test contract changed in tests/test_compat.py: "
        "function:test_old_behavior",
    )


def test_unrequested_public_default_change_is_reported() -> None:
    baseline = """\
class StateMachine:
    allow_event_without_transition: bool = False
"""
    candidate = """\
class StateMachine:
    allow_event_without_transition: bool = True
"""

    assert unapproved_public_default_changes(
        "statemachine/core.py",
        baseline,
        candidate,
        accepted_contract="Add state data support.",
    ) == (
        "unapproved public default change in statemachine/core.py: "
        "StateMachine.allow_event_without_transition",
    )


def test_explicitly_requested_public_default_change_is_allowed() -> None:
    baseline = "class StateMachine:\n    allow_x: bool = False\n"
    candidate = "class StateMachine:\n    allow_x: bool = True\n"

    assert (
        unapproved_public_default_changes(
            "statemachine/core.py",
            baseline,
            candidate,
            accepted_contract="Change allow_x to true.",
        )
        == ()
    )


def test_unrequested_public_default_addition_is_reported() -> None:
    assert unapproved_public_default_changes(
        "statemachine/core.py",
        "class StateMachine:\n    existing: bool = False\n",
        (
            "class StateMachine:\n    existing: bool = False\n"
            "    new_default: bool = True\n"
        ),
        accepted_contract="Add state data support.",
    ) == (
        "unapproved public default addition in statemachine/core.py: "
        "StateMachine.new_default",
    )
