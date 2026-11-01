"""Service-owned deterministic validation of execute-PR authored test suites."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any


class ExecutePrTestAuthoringValidationError(ValueError):
    """An authored execute-PR test suite violates its contract."""


def discover_python_test_root(
    tracked_paths: Sequence[str], *, allowed_paths: Sequence[str]
) -> str:
    """Choose the nearest existing Python test root to the PR's file scope."""
    test_counts: dict[str, int] = {}
    for raw_path in tracked_paths:
        path = PurePosixPath(raw_path)
        if path.suffix != ".py" or not (
            path.name == "conftest.py"
            or path.name.startswith("test_")
            or path.name.endswith("_test.py")
        ):
            continue
        for index, part in enumerate(path.parts):
            if part in {"test", "tests"}:
                root = PurePosixPath(*path.parts[: index + 1]).as_posix()
                test_counts[root] = test_counts.get(root, 0) + 1
                break
    if not test_counts:
        return "tests"

    scope_directories: list[tuple[str, ...]] = []
    for raw_path in allowed_paths:
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            continue
        parts = path.parts[:-1] if path.suffix else path.parts
        if parts and parts != (".",):
            scope_directories.append(parts)

    def common_prefix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
        length = 0
        for left_part, right_part in zip(left, right, strict=False):
            if left_part != right_part:
                break
            length += 1
        return length

    def rank(root: str) -> tuple[int, int, int, int, str]:
        root_parts = PurePosixPath(root).parts
        if not scope_directories:
            common, distance = 0, len(root_parts)
        else:
            matches = [
                (common_prefix_length(root_parts, scope), scope)
                for scope in scope_directories
            ]
            common = max(length for length, _scope in matches)
            distance = min(
                len(root_parts) + len(scope) - 2 * length
                for length, scope in matches
                if length == common
            )
        return common, -distance, test_counts[root], -len(root_parts), root

    if not scope_directories and "tests" in test_counts:
        return "tests"
    if scope_directories:
        best_common = max(
            max(
                common_prefix_length(PurePosixPath(root).parts, scope)
                for scope in scope_directories
            )
            for root in test_counts
        )
        if best_common == 0 and "tests" in test_counts:
            return "tests"
    return max(test_counts, key=rank)


def validate_coverage_references(
    files: Mapping[str, str], obligations: Sequence[Mapping[str, Any]]
) -> dict[str, list[str]]:
    """Require every deterministic obligation to link to an actual authored test."""
    tests = {
        node.name
        for source in files.values()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }
    expected_ids = [str(item.get("obligation_id", "")) for item in obligations]
    if not expected_ids or any(not item for item in expected_ids):
        raise ExecutePrTestAuthoringValidationError(
            "coverage obligations must have non-empty IDs"
        )
    if len(set(expected_ids)) != len(expected_ids):
        raise ExecutePrTestAuthoringValidationError(
            "coverage obligations contain duplicate IDs"
        )
    references: dict[str, set[str]] = {item: set() for item in expected_ids}
    markers = re.compile(
        r"(?m)^[ \t]*#[ \t]*Covers:[ \t]*(AC-[0-9]{2,})"
        r"[ \t]+(test_[A-Za-z0-9_]+)[ \t]*$"
    )
    for source in files.values():
        for obligation_id, test_name in markers.findall(source):
            if obligation_id not in references:
                raise ExecutePrTestAuthoringValidationError(
                    f"test coverage marker refers to unknown obligation {obligation_id}"
                )
            if test_name not in tests:
                raise ExecutePrTestAuthoringValidationError(
                    f"coverage marker {obligation_id} refers to missing test "
                    f"{test_name}"
                )
            references[obligation_id].add(test_name)
    covered_ids = {key for key, test_names in references.items() if test_names}
    missing = sorted(set(expected_ids) - covered_ids)
    if missing:
        raise ExecutePrTestAuthoringValidationError(
            "tests must cover every acceptance criterion; "
            f"missing={missing}. Add '# Covers: <AC-ID> <test_function>' above "
            "an authored test."
        )
    if not tests:
        raise ExecutePrTestAuthoringValidationError(
            "coverage references contain no actual tests"
        )
    referenced_tests = set().union(*references.values())
    unlinked_tests = sorted(tests - referenced_tests)
    if unlinked_tests:
        raise ExecutePrTestAuthoringValidationError(
            "authored tests must link to an acceptance criterion; "
            f"unlinked={unlinked_tests}"
        )
    return {key: sorted(value) for key, value in sorted(references.items())}


def validate_generated_test_files(
    files: Mapping[str, str], *, test_root: str = "tests"
) -> dict[str, str]:
    """Require safe new pytest files with executable, assertive synchronous tests."""
    if not files:
        raise ExecutePrTestAuthoringValidationError(
            "test author produced no integration test files"
        )
    root = _validated_test_root(test_root)
    validated: dict[str, str] = {}
    for raw_path, source in sorted(files.items()):
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[: len(root.parts)] != root.parts
            or path.suffix != ".py"
            or not (path.name.startswith("test_") or path.name.endswith("_test.py"))
        ):
            raise ExecutePrTestAuthoringValidationError(
                f"test author wrote outside {test_root}: {raw_path}"
            )
        try:
            tree = ast.parse(source, filename=raw_path)
            compile(tree, raw_path, "exec")
        except (SyntaxError, ValueError) as error:
            raise ExecutePrTestAuthoringValidationError(
                f"generated test file {raw_path} is not valid Python: {error}"
            ) from error
        tests = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        if not tests:
            raise ExecutePrTestAuthoringValidationError(
                f"{raw_path} defines no pytest test functions"
            )
        for test in tests:
            if isinstance(test, ast.AsyncFunctionDef):
                raise ExecutePrTestAuthoringValidationError(
                    f"{raw_path}:{test.name} must be a synchronous test"
                )
            if not any(isinstance(node, ast.Assert) for node in ast.walk(test)):
                raise ExecutePrTestAuthoringValidationError(
                    f"{raw_path}:{test.name} has no behavior assertion"
                )
            if _uses_skip_or_xfail(test):
                raise ExecutePrTestAuthoringValidationError(
                    f"{raw_path}:{test.name} skips or xfails the required behavior"
                )
        validated[path.as_posix()] = source.rstrip() + "\n"
    return validated


def validate_test_correction(
    files: Mapping[str, str],
    *,
    expected_paths: Sequence[str],
    obligations: Sequence[Mapping[str, Any]],
    test_root: str = "tests",
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Validate a bounded correction to the generated acceptance-test suite."""
    if set(files) != set(expected_paths):
        raise ExecutePrTestAuthoringValidationError(
            "test correction must edit only the originally generated test files; "
            f"expected={sorted(expected_paths)}, actual={sorted(files)}"
        )
    validated = validate_generated_test_files(files, test_root=test_root)
    return validated, validate_coverage_references(validated, obligations)


def generated_test_source_paths_under_root(
    changed_paths: Sequence[str], *, test_root: str
) -> tuple[str, ...]:
    """Select authored Python tests, ignoring caches under one test root."""
    root = _validated_test_root(test_root)
    sources: set[str] = set()
    for raw_path in changed_paths:
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[: len(root.parts)] != root.parts
        ):
            raise ExecutePrTestAuthoringValidationError(
                f"test author changed a path outside {test_root}: {raw_path}"
            )
        if (
            "__pycache__" in path.parts
            or ".pytest_cache" in path.parts
            or path.suffix == ".pyc"
        ):
            continue
        if path.suffix != ".py":
            raise ExecutePrTestAuthoringValidationError(
                f"test author changed a non-Python test source path: {raw_path}"
            )
        sources.add(path.as_posix())
    if not sources:
        raise ExecutePrTestAuthoringValidationError(
            "test author produced no Python test source files"
        )
    return tuple(sorted(sources))


def generated_test_source_paths(changed_paths: Sequence[str]) -> tuple[str, ...]:
    """Select authored Python test files under the conventional tests root."""
    return generated_test_source_paths_under_root(changed_paths, test_root="tests")


def pytest_execution_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    """Extract actual collected and executed test counts from pytest output."""
    output = "\n".join(str(result.get(key, "")) for key in ("stdout", "stderr"))
    summaries = re.findall(
        r"(?m)^\s*=*\s*((?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed)"
        r"(?:,\s*)?)+)\s+in\s+[\d.]+s\s*=*\s*$",
        output,
    )
    if not summaries:
        raise ExecutePrTestAuthoringValidationError(
            "pytest output has no terminal summary for test-count evidence"
        )
    summary = summaries[-1]
    counts = {
        status: int(count)
        for count, status in re.findall(
            r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)",
            summary,
        )
    }
    executed = sum(
        counts.get(status, 0) for status in ("passed", "failed", "error", "errors")
    )
    if not executed:
        raise ExecutePrTestAuthoringValidationError("pytest reported no executed tests")
    return {"summary": summary, "counts": counts, "executed": executed}


def _validated_test_root(test_root: str) -> PurePosixPath:
    root = PurePosixPath(test_root)
    if (
        root.is_absolute()
        or not root.parts
        or ".." in root.parts
        or root.parts[-1] not in {"tests", "test"}
    ):
        raise ExecutePrTestAuthoringValidationError(
            f"unsupported Python test root: {test_root}"
        )
    return root


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
    "ExecutePrTestAuthoringValidationError",
    "discover_python_test_root",
    "generated_test_source_paths",
    "generated_test_source_paths_under_root",
    "pytest_execution_evidence",
    "validate_coverage_references",
    "validate_generated_test_files",
    "validate_test_correction",
]
