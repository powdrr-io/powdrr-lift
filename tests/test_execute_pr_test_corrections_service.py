from __future__ import annotations

import pytest
from powdrr_service.verification.execute_pr_test_corrections import (
    ExecutePrTestVerificationError,
    changed_execute_pr_test_cases,
    require_behavioral_red_result,
    require_execute_pr_test_correction_baseline,
)


def test_correction_must_preserve_cases_and_change_a_behavior_case() -> None:
    original = {
        "tests/test_api.py": (
            "def test_existing():\n    assert public_api() == 'old'\n"
            "def test_other():\n    assert other_api()\n"
        )
    }
    corrected = {
        "tests/test_api.py": (
            "def test_existing():\n    assert public_api() == 'new'\n"
            "def test_other():\n    assert other_api()\n"
            "def test_boundary():\n    assert public_api('edge')\n"
        )
    }

    assert changed_execute_pr_test_cases(original, corrected) == (
        "tests/test_api.py::test_boundary",
        "tests/test_api.py::test_existing",
    )


@pytest.mark.parametrize(
    ("corrected", "message"),
    [
        ({}, "may not remove"),
        (
            {"tests/test_api.py": "def test_existing():\n    assert public_api()\n"},
            "must change or add",
        ),
    ],
)
def test_correction_rejects_removed_or_unchanged_cases(
    corrected: dict[str, str], message: str
) -> None:
    original = {"tests/test_api.py": "def test_existing():\n    assert public_api()\n"}

    with pytest.raises(ExecutePrTestVerificationError, match=message):
        changed_execute_pr_test_cases(original, corrected)


def test_correction_requires_each_changed_case_to_fail_on_clean_baseline() -> None:
    result = {
        "returncode": 1,
        "stdout": ("FAILED tests/test_api.py::test_changed\n1 failed in 0.02s\n"),
        "stderr": "AssertionError: expected behavior is missing",
    }

    require_execute_pr_test_correction_baseline(
        result, corrected_test_ids=("tests/test_api.py::test_changed",)
    )
    with pytest.raises(ExecutePrTestVerificationError, match="missing failures"):
        require_execute_pr_test_correction_baseline(
            result,
            corrected_test_ids=(
                "tests/test_api.py::test_changed",
                "tests/test_api.py::test_not_reported",
            ),
        )


def test_baseline_must_fail_through_behavior_assertion_without_collection_errors() -> (
    None
):
    with pytest.raises(ExecutePrTestVerificationError, match="non-assertion"):
        require_behavioral_red_result(
            {
                "returncode": 1,
                "stdout": "FAILED tests/test_api.py::test_api",
                "stderr": "ModuleNotFoundError: no module named 'api'",
            }
        )
