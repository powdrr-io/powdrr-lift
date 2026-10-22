"""Pre-implementation integration tests for execute-PR tasks."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from powdrr_lift.workrr.coding_agent_validation import ValidationProfile
from powdrr_lift.workrr.test_obligations import missing_obligation_ids


class ExecutePrTestError(ValueError):
    """Generated execute-PR tests are invalid or did not prove behavior."""


def validate_coverage_references(
    files: Mapping[str, str], obligations: Sequence[Mapping[str, Any]]
) -> dict[str, list[str]]:
    """Require each deterministic obligation to link to an actual authored test."""
    tests = {
        node.name
        for source in files.values()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }
    expected_ids = [str(item.get("obligation_id", "")) for item in obligations]
    if not expected_ids or any(not item for item in expected_ids):
        raise ExecutePrTestError("coverage obligations must have non-empty IDs")
    if len(set(expected_ids)) != len(expected_ids):
        raise ExecutePrTestError("coverage obligations contain duplicate IDs")
    references: dict[str, set[str]] = {item: set() for item in expected_ids}
    markers = re.compile(
        r"(?m)^[ \t]*#[ \t]*Covers:[ \t]*(AC-[0-9]{2,})"
        r"[ \t]+(test_[A-Za-z0-9_]+)[ \t]*$"
    )
    for source in files.values():
        for obligation_id, test_name in markers.findall(source):
            if obligation_id not in references:
                raise ExecutePrTestError(
                    f"test coverage marker refers to unknown obligation {obligation_id}"
                )
            if test_name not in tests:
                raise ExecutePrTestError(
                    f"coverage marker {obligation_id} refers to missing test "
                    f"{test_name}"
                )
            references[obligation_id].add(test_name)
    covered_ids = {key for key, test_names in references.items() if test_names}
    missing = missing_obligation_ids(expected_ids, covered_ids)
    if missing:
        raise ExecutePrTestError(
            "tests must cover every acceptance criterion; "
            f"missing={missing}. Add '# Covers: <AC-ID> <test_function>' above "
            "an authored test."
        )
    if not tests:
        raise ExecutePrTestError("coverage references contain no actual tests")
    referenced_tests = set().union(*references.values())
    unlinked_tests = sorted(tests - referenced_tests)
    if unlinked_tests:
        raise ExecutePrTestError(
            "authored tests must link to an acceptance criterion; "
            f"unlinked={unlinked_tests}"
        )
    return {key: sorted(value) for key, value in sorted(references.items())}


def pytest_execution_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    """Extract actual collected and executed test counts from pytest output."""
    output = "\n".join(str(result.get(key, "")) for key in ("stdout", "stderr"))
    summaries = re.findall(
        r"(?m)^\s*=*\s*((?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed)"
        r"(?:,\s*)?)+)\s+in\s+[\d.]+s\s*=*\s*$",
        output,
    )
    if not summaries:
        raise ExecutePrTestError(
            "pytest output has no terminal summary for test-count evidence"
        )
    summary = summaries[-1]
    counts = {
        status: int(count)
        for count, status in re.findall(
            r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)", summary
        )
    }
    executed = sum(
        counts.get(status, 0) for status in ("passed", "failed", "error", "errors")
    )
    if executed == 0:
        raise ExecutePrTestError("pytest reported no executed tests")
    return {"summary": summary, "counts": counts, "executed": executed}


def validate_quality_review(review: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only an explicit clean verdict from the independent test reviewer."""
    outcome = review.get("outcome")
    findings = review.get("findings")
    if outcome not in {"pass", "fail"} or not isinstance(findings, list):
        raise ExecutePrTestError("test-quality review has an invalid verdict")
    normalized: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, Mapping) or any(
            not isinstance(finding.get(key), str) or not finding[key].strip()
            for key in ("test_id", "issue", "evidence")
        ):
            raise ExecutePrTestError("test-quality review contains an invalid finding")
        normalized.append(
            {key: finding[key].strip() for key in ("test_id", "issue", "evidence")}
        )
    if outcome == "pass" and normalized:
        raise ExecutePrTestError(
            "test-quality reviewer passed while reporting findings"
        )
    if outcome == "fail" and not normalized:
        raise ExecutePrTestError("test-quality reviewer failed without explaining why")
    return {"outcome": outcome, "findings": normalized}


def validate_generated_test_files(
    files: Mapping[str, str],
) -> dict[str, str]:
    """Require new pytest tests with observable assertions and safe paths."""
    if not files:
        raise ExecutePrTestError("test author produced no integration test files")
    validated: dict[str, str] = {}
    for raw_path, source in sorted(files.items()):
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] not in {"tests", "test"}
            or path.suffix != ".py"
            or not (path.name.startswith("test_") or path.name.endswith("_test.py"))
        ):
            raise ExecutePrTestError(
                f"test author wrote outside the supported Python test paths: {raw_path}"
            )
        try:
            tree = ast.parse(source, filename=raw_path)
            compile(tree, raw_path, "exec")
        except (SyntaxError, ValueError) as error:
            raise ExecutePrTestError(
                f"generated test file {raw_path} is not valid Python: {error}"
            ) from error
        tests = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        if not tests:
            raise ExecutePrTestError(f"{raw_path} defines no pytest test functions")
        for test in tests:
            if isinstance(test, ast.AsyncFunctionDef):
                raise ExecutePrTestError(
                    f"{raw_path}:{test.name} must be a synchronous test"
                )
            if not any(isinstance(node, ast.Assert) for node in ast.walk(test)):
                raise ExecutePrTestError(
                    f"{raw_path}:{test.name} has no behavior assertion"
                )
            if _uses_skip_or_xfail(test):
                raise ExecutePrTestError(
                    f"{raw_path}:{test.name} skips or xfails the required behavior"
                )
        validated[path.as_posix()] = source.rstrip() + "\n"
    return validated


def pytest_command_for_files(
    profile: ValidationProfile,
    paths: Sequence[str],
) -> tuple[str, ...]:
    """Select a pytest runner for frozen tests and append their paths.

    Prefer the repository's declared verification command when it is pytest.
    Otherwise, use Powdrr's Python environment to run the isolated generated
    contract tests; the repository verification command is run separately.
    """
    command = profile.command
    pytest_present = any(
        Path(token).name == "pytest"
        or (
            token == "-m"
            and index + 1 < len(command)
            and command[index + 1] == "pytest"
        )
        for index, token in enumerate(command)
    )
    if not pytest_present:
        command = (sys.executable, "-m", "pytest")
    return (*command, "--tb=short", *paths)


def run_generated_pytest(
    profile: ValidationProfile,
    *,
    root: Path,
    paths: Sequence[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run only the frozen generated tests, independent of project validation."""
    command = pytest_command_for_files(profile, paths)
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath = [str(root), str(root / "src")]
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "passed": False,
            "command": list(command),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": str(error),
        }
    return {
        "passed": completed.returncode == 0,
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "error": None,
    }


def require_behavioral_red_result(result: Mapping[str, Any]) -> None:
    """Reject baseline failures caused by test setup, imports, or collection."""
    output = "\n".join(
        str(result.get(key, "")) for key in ("stdout", "stderr", "error")
    )
    if result.get("returncode") in (None, 0):
        raise ExecutePrTestError(
            "generated integration tests did not fail against the untouched baseline"
        )
    if "AssertionError" not in output and "DID NOT RAISE" not in output:
        raise ExecutePrTestError(
            "generated integration tests failed on baseline for a non-assertion "
            "reason; they must execute behavior and fail their expected assertion"
        )
    invalid_markers = (
        "ERROR collecting",
        "ImportError",
        "ModuleNotFoundError",
        "SyntaxError",
        "collected 0 items",
    )
    if any(marker in output for marker in invalid_markers):
        raise ExecutePrTestError(
            "generated integration tests have a collection, import, or syntax "
            "failure instead of a behavioral assertion failure"
        )
    if not re.search(r"(?m)^FAILED\s+\S+::test_\S+", output):
        raise ExecutePrTestError(
            "generated integration tests did not report a failing test assertion"
        )
    if re.search(r"(?m)^ERROR(?:S)?\b", output) or re.search(
        r"\b\d+\s+errors?\b", output
    ):
        raise ExecutePrTestError(
            "generated integration tests also have collection or setup errors"
        )


def fingerprint_test_files(files: Mapping[str, str]) -> dict[str, str]:
    """Fingerprint frozen generated tests before coding starts."""
    return {
        path: hashlib.sha256(source.encode("utf-8")).hexdigest()
        for path, source in sorted(files.items())
    }


def candidate_test_changes(root: Path, expected: Mapping[str, str]) -> tuple[str, ...]:
    """Report missing or edited frozen test files in the implementation tree."""
    changed: list[str] = []
    for path, source in expected.items():
        candidate = root / path
        if not candidate.is_file() or candidate.read_text(encoding="utf-8") != source:
            changed.append(path)
    return tuple(sorted(changed))


def install_frozen_test_files(root: Path, files: Mapping[str, str]) -> None:
    """Install and stage generated tests so the coding agent can run them."""
    for path, source in files.items():
        candidate = PurePosixPath(path)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or not candidate.parts
            or candidate.parts[0] not in {"tests", "test"}
        ):
            raise ExecutePrTestError(f"invalid frozen integration-test path: {path}")
        destination = root.joinpath(*candidate.parts)
        if destination.is_symlink():
            destination.unlink()
        try:
            destination.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise ExecutePrTestError(
                f"generated integration test escaped the worktree: {path}"
            ) from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
    if not files:
        raise ExecutePrTestError("cannot install an empty integration-test suite")
    staged = subprocess.run(
        ["git", "add", "-f", "--", *sorted(files)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode:
        raise ExecutePrTestError(
            "could not stage the frozen integration tests: " + staged.stderr.strip()
        )


def _uses_skip_or_xfail(test: ast.FunctionDef) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Call):
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else ""
            )
            if name in {"skip", "skipif", "xfail", "SkipTest"}:
                return True
        if isinstance(node, ast.Attribute) and node.attr in {"skip", "xfail"}:
            return True
    return False


__all__ = [
    "ExecutePrTestError",
    "candidate_test_changes",
    "pytest_command_for_files",
    "require_behavioral_red_result",
    "run_generated_pytest",
    "fingerprint_test_files",
    "install_frozen_test_files",
    "validate_generated_test_files",
]
