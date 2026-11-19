from __future__ import annotations

from typing import Any

import pytest

from powdrr_lift.workrr.coding_agent_validation import (
    ValidationResult,
    ValidationResultStatus,
)
from powdrr_lift.workrr.registered_corrections import (
    select_registered_correction,
)


def validation_result(
    *,
    path: str = "retry.py",
    command: tuple[str, ...] | None = None,
    status: ValidationResultStatus = ValidationResultStatus.FAILED,
    returncode: int | None = 1,
    stdout: str | None = None,
    stderr: str = "",
) -> dict[str, Any]:
    return ValidationResult(
        profile="format",
        command=command or ("ruff", "format", "--check", path),
        status=status,
        returncode=returncode,
        stdout=(
            stdout
            if stdout is not None
            else f"Would reformat: {path}\n1 file would be reformatted\n"
        ),
        stderr=stderr,
    ).to_data()


def test_selects_only_one_exact_failed_formatter_for_changed_allowed_path() -> None:
    correction = select_registered_correction(
        {"results": [validation_result()]},
        changed_paths=("retry.py",),
        allowed_paths=("retry.py",),
    )

    assert correction is not None
    assert correction.correction_id == "ruff-format-v1"
    assert correction.path == "retry.py"


@pytest.mark.parametrize(
    ("result", "changed_paths", "allowed_paths"),
    [
        (validation_result(stdout=""), ("retry.py",), ("retry.py",)),
        (
            validation_result(stdout="Would reformat: other.py\n"),
            ("retry.py",),
            ("retry.py",),
        ),
        (validation_result(path="../retry.py"), ("../retry.py",), ("..",)),
        (validation_result(path="retry.py"), ("retry.py",), ("src",)),
        (
            validation_result(returncode=2),
            ("retry.py",),
            ("retry.py",),
        ),
        (
            validation_result(stderr="formatter warning"),
            ("retry.py",),
            ("retry.py",),
        ),
        (
            validation_result(command=("ruff", "format", "--check", ".")),
            ("retry.py",),
            (".",),
        ),
    ],
)
def test_rejects_stale_ambiguous_or_out_of_scope_formatter_evidence(
    result: dict[str, Any],
    changed_paths: tuple[str, ...],
    allowed_paths: tuple[str, ...],
) -> None:
    assert (
        select_registered_correction(
            {"results": [result]},
            changed_paths=changed_paths,
            allowed_paths=allowed_paths,
        )
        is None
    )


def test_rejects_any_second_nonpassing_validation_result() -> None:
    assert (
        select_registered_correction(
            {
                "results": [
                    validation_result(),
                    ValidationResult(
                        profile="types",
                        command=("mypy",),
                        status=ValidationResultStatus.BLOCKED,
                        returncode=None,
                    ).to_data(),
                ]
            },
            changed_paths=("retry.py",),
            allowed_paths=("retry.py",),
        )
        is None
    )
