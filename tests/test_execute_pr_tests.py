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
    pytest_execution_evidence,
    require_behavioral_red_result,
    validate_coverage_plan,
    validate_coverage_references,
    validate_generated_test_files,
    validate_quality_review,
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


def test_coverage_plan_must_cover_each_criterion_and_link_real_tests() -> None:
    criteria = ["Returns computed values", "Rejects malformed expressions"]
    plan = {
        "criteria": [
            {
                "criterion_id": "AC-01",
                "criterion": criteria[0],
                "partitions": [
                    {
                        "partition_id": "P01",
                        "behavior_class": "operator precedence",
                        "case_strategy": "parameterized expressions across operators",
                        "minimum_case_count": 12,
                        "observable_oracle": "numeric evaluation result",
                        "test_ids": ["test_precedence"],
                    }
                ],
            },
            {
                "criterion_id": "AC-02",
                "criterion": criteria[1],
                "partitions": [
                    {
                        "partition_id": "P01",
                        "behavior_class": "invalid syntax",
                        "case_strategy": "multiple malformed tokens",
                        "minimum_case_count": 5,
                        "observable_oracle": "documented parse error",
                        "test_ids": ["test_rejects_invalid_syntax"],
                    }
                ],
            },
        ]
    }
    validated = validate_coverage_plan(plan, criteria)
    files = {
        "tests/test_parser.py": (
            "# Covers: AC-01.P01 test_precedence\n"
            "def test_precedence():\n    assert evaluate('4 + 3') == 7\n\n"
            "# Covers: AC-02.P01 test_rejects_invalid_syntax\n"
            "def test_rejects_invalid_syntax():\n"
            "    with pytest.raises(ParseError):\n        evaluate('4 +')\n"
        )
    }
    assert validate_coverage_references(files, validated) == {
        "AC-01.P01": 1,
        "AC-02.P01": 1,
    }
    with pytest.raises(ExecutePrTestError, match="every acceptance criterion"):
        validate_coverage_plan({"criteria": plan["criteria"][:1]}, criteria)
    with pytest.raises(ExecutePrTestError, match="not linked"):
        validate_coverage_references(
            {"tests/test_parser.py": "def test_precedence():\n    assert True\n"},
            validated,
        )


def test_quality_review_and_pytest_execution_evidence_fail_closed() -> None:
    assert validate_quality_review({"outcome": "pass", "findings": []}) == {
        "outcome": "pass",
        "findings": [],
    }
    with pytest.raises(ExecutePrTestError, match="without explaining"):
        validate_quality_review({"outcome": "fail", "findings": []})
    assert pytest_execution_evidence(
        {"stdout": "14 passed, 2 skipped in 0.50s", "stderr": ""}
    ) == {
        "summary": "14 passed, 2 skipped",
        "counts": {"passed": 14, "skipped": 2},
        "executed": 14,
    }
    with pytest.raises(ExecutePrTestError, match="no terminal summary"):
        pytest_execution_evidence({"stdout": "", "stderr": ""})


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
