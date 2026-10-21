"""Pre-implementation integration tests for execute-PR tasks."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from powdrr_lift.workrr.coding_agent_validation import ValidationProfile


class ExecutePrTestError(ValueError):
    """Generated execute-PR tests are invalid or did not prove behavior."""


def validate_coverage_plan(
    plan: Mapping[str, Any], acceptance_criteria: Sequence[Any]
) -> dict[str, Any]:
    """Require an explicit, test-linked partition plan for every criterion."""
    criteria = plan.get("criteria")
    if not isinstance(criteria, list):
        raise ExecutePrTestError("test coverage plan must contain criteria")
    expected_ids = {f"AC-{index:02d}" for index, _ in enumerate(acceptance_criteria, 1)}
    by_id: dict[str, dict[str, Any]] = {}
    assigned_test_ids: set[str] = set()
    for raw in criteria:
        if not isinstance(raw, Mapping):
            raise ExecutePrTestError(
                "test coverage plan contains a malformed criterion"
            )
        criterion_id = raw.get("criterion_id")
        partitions = raw.get("partitions")
        if (
            not isinstance(criterion_id, str)
            or criterion_id not in expected_ids
            or criterion_id in by_id
            or not isinstance(raw.get("criterion"), str)
            or not raw["criterion"].strip()
            or not isinstance(partitions, list)
            or not partitions
        ):
            raise ExecutePrTestError(
                "test coverage plan has missing or invalid criterion IDs"
            )
        checked_partitions: list[dict[str, Any]] = []
        partition_ids: set[str] = set()
        for partition in partitions:
            if not isinstance(partition, Mapping):
                raise ExecutePrTestError(f"{criterion_id} has a malformed partition")
            partition_id = partition.get("partition_id")
            test_ids = partition.get("test_ids")
            if (
                not isinstance(partition_id, str)
                or not partition_id
                or partition_id in partition_ids
                or not isinstance(partition.get("behavior_class"), str)
                or not partition["behavior_class"].strip()
                or not isinstance(partition.get("observable_oracle"), str)
                or not partition["observable_oracle"].strip()
                or not isinstance(partition.get("case_strategy"), str)
                or not partition["case_strategy"].strip()
                or not isinstance(partition.get("minimum_case_count"), int)
                or isinstance(partition.get("minimum_case_count"), bool)
                or partition["minimum_case_count"] < 1
                or not isinstance(test_ids, list)
                or not test_ids
                or any(not isinstance(value, str) or not value for value in test_ids)
                or len(set(test_ids)) != len(test_ids)
                or assigned_test_ids.intersection(test_ids)
            ):
                raise ExecutePrTestError(f"{criterion_id} has an incomplete partition")
            partition_ids.add(partition_id)
            assigned_test_ids.update(test_ids)
            checked_partitions.append(dict(partition))
        by_id[criterion_id] = {**dict(raw), "partitions": checked_partitions}
    if set(by_id) != expected_ids:
        missing = sorted(expected_ids - set(by_id))
        extra = sorted(set(by_id) - expected_ids)
        raise ExecutePrTestError(
            "coverage plan must represent every acceptance criterion; "
            f"missing={missing}, extra={extra}"
        )
    return {"criteria": [by_id[key] for key in sorted(by_id)]}


def validate_coverage_references(
    files: Mapping[str, str], plan: Mapping[str, Any]
) -> dict[str, int]:
    """Check every planned partition is explicitly linked to authored tests."""
    tests = {
        node.name
        for source in files.values()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }
    references: dict[str, int] = {}
    for criterion in plan["criteria"]:
        criterion_id = criterion["criterion_id"]
        for partition in criterion["partitions"]:
            reference = f"{criterion_id}.{partition['partition_id']}"
            referenced = 0
            for source in files.values():
                for test_id in partition["test_ids"]:
                    if test_id in tests and re.search(
                        rf"(?m)^\s*#\s*Covers:\s*{re.escape(reference)}\s+{re.escape(test_id)}\s*$",
                        source,
                    ):
                        referenced += 1
            if referenced == 0:
                raise ExecutePrTestError(
                    f"coverage partition {reference} is not linked to an actual test; "
                    "add '# Covers: <partition-id> <test_function>' above its test"
                )
            references[reference] = referenced
    return references


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
    """Extend the task's declared pytest command with generated test files."""
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
        raise ExecutePrTestError(
            "execute-PR generated integration tests currently require the plan's "
            "verification command to use pytest"
        )
    return (*command, "--tb=short", *paths)


def run_generated_pytest(
    profile: ValidationProfile,
    *,
    root: Path,
    paths: Sequence[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run only the frozen generated tests with the plan's approved runner."""
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
