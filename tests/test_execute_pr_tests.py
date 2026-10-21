from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from powdrr_lift.workrr.coding_agent_validation import ValidationProfile
from powdrr_lift.workrr.execute_pr_tests import (
    ExecutePrTestError,
    candidate_test_changes,
    fingerprint_test_files,
    install_frozen_test_files,
    pytest_command_for_files,
    require_behavioral_red_result,
    validate_generated_test_files,
)


def test_generated_pytest_files_require_assertive_tests_under_test_root() -> None:
    result = validate_generated_test_files(
        {
            "tests/integration/test_public_api.py": (
                "def test_public_behavior():\n"
                "    result = public_api('input')\n"
                "    assert result == 'expected'\n"
            )
        }
    )

    assert tuple(result) == ("tests/integration/test_public_api.py",)


@pytest.mark.parametrize(
    "path, source",
    [
        (
            "src/test_product.py",
            "def test_product():\n    assert public_api()\n",
        ),
        (
            "tests/test_no_assertion.py",
            "def test_no_assertion():\n    public_api()\n",
        ),
        (
            "tests/test_skipped.py",
            "import pytest\n"
            "def test_skipped():\n"
            "    pytest.skip('not implemented')\n"
            "    assert public_api()\n",
        ),
        (
            "tests/test_invalid.py",
            "def test_invalid(:\n    assert False\n",
        ),
    ],
)
def test_generated_test_validation_rejects_non_behavioral_tests(
    path: str, source: str
) -> None:
    with pytest.raises(ExecutePrTestError):
        validate_generated_test_files({path: source})


def test_generated_tests_must_fail_baseline_through_assertions() -> None:
    require_behavioral_red_result(
        {
            "returncode": 1,
            "stdout": "FAILED tests/test_public_api.py::test_public_behavior",
            "stderr": "AssertionError: expected behavior is missing",
        }
    )

    for result in (
        {"returncode": 0, "stdout": "1 passed"},
        {"returncode": 1, "stderr": "ImportError: cannot import name 'public_api'"},
        {"returncode": 1, "stderr": "ERROR collecting tests/test_public_api.py"},
        {
            "returncode": 1,
            "stdout": (
                "FAILED tests/test_public_api.py::test_behavior\n1 failed, 1 error"
            ),
            "stderr": "AssertionError: expected behavior is missing",
        },
    ):
        with pytest.raises(ExecutePrTestError):
            require_behavioral_red_result(result)


def test_generated_tests_use_the_plan_pytest_command_and_are_frozen(
    tmp_path: Path,
) -> None:
    profile = ValidationProfile("verify", ("uv", "run", "pytest", "-q"))
    assert pytest_command_for_files(profile, ("tests/test_api.py",)) == (
        "uv",
        "run",
        "pytest",
        "-q",
        "--tb=short",
        "tests/test_api.py",
    )

    files = {"tests/test_api.py": "def test_api():\n    assert api()\n"}
    fingerprints = fingerprint_test_files(files)
    assert candidate_test_changes(tmp_path, files) == ("tests/test_api.py",)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_api.py").write_text(files["tests/test_api.py"])
    assert candidate_test_changes(tmp_path, files) == ()
    assert fingerprints["tests/test_api.py"]


def test_frozen_test_installation_stages_files_and_rejects_unsafe_paths(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    files = {"tests/integration/test_api.py": "def test_api():\n    assert api()\n"}
    install_frozen_test_files(tmp_path, files)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.splitlines() == ["tests/integration/test_api.py"]

    with pytest.raises(ExecutePrTestError, match="invalid frozen"):
        install_frozen_test_files(tmp_path, {"../outside.py": "assert True\n"})


def test_generated_tests_require_a_declared_pytest_runner() -> None:
    with pytest.raises(ExecutePrTestError, match="require.*pytest"):
        pytest_command_for_files(
            ValidationProfile("verify", ("make", "test")),
            ("tests/test_api.py",),
        )
