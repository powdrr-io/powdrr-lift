"""Service-owned acceptance rules for execute-PR integration-test corrections."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from typing import Any


class ExecutePrTestVerificationError(ValueError):
    """A generated test suite does not satisfy the verification contract."""


def _test_case_fingerprints(files: Mapping[str, str]) -> dict[str, str]:
    cases: dict[str, str] = {}
    for path, source in files.items():
        tree = ast.parse(source, filename=path)
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test_"):
                cases[f"{path}::{node.name}"] = ast.dump(node, include_attributes=False)
    return cases


def changed_execute_pr_test_cases(
    original_sources: Mapping[str, str], corrected_sources: Mapping[str, str]
) -> tuple[str, ...]:
    """Require a correction to preserve existing cases and change behavior tests."""
    original_cases = _test_case_fingerprints(original_sources)
    corrected_cases = _test_case_fingerprints(corrected_sources)
    removed_cases = set(original_cases) - set(corrected_cases)
    if removed_cases:
        raise ExecutePrTestVerificationError(
            "test correction may not remove generated test cases: "
            f"{sorted(removed_cases)}"
        )
    changed_cases = tuple(
        sorted(
            test_id
            for test_id, fingerprint in corrected_cases.items()
            if original_cases.get(test_id) != fingerprint
        )
    )
    if not changed_cases:
        raise ExecutePrTestVerificationError(
            "test correction must change or add at least one behavior test case"
        )
    return changed_cases


def require_behavioral_red_result(result: Mapping[str, Any]) -> None:
    """Reject baseline failures caused by setup, imports, or collection."""
    output = "\n".join(
        str(result.get(key, "")) for key in ("stdout", "stderr", "error")
    )
    if result.get("returncode") in (None, 0):
        raise ExecutePrTestVerificationError(
            "generated integration tests did not fail against the untouched baseline"
        )
    if "AssertionError" not in output and "DID NOT RAISE" not in output:
        raise ExecutePrTestVerificationError(
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
        raise ExecutePrTestVerificationError(
            "generated integration tests have a collection, import, or syntax "
            "failure instead of a behavioral assertion failure"
        )
    if not re.search(r"(?m)^FAILED\s+\S+::test_\S+", output):
        raise ExecutePrTestVerificationError(
            "generated integration tests did not report a failing test assertion"
        )
    if re.search(r"(?m)^ERROR(?:S)?\b", output) or re.search(
        r"\b\d+\s+errors?\b", output
    ):
        raise ExecutePrTestVerificationError(
            "generated integration tests also have collection or setup errors"
        )


def require_execute_pr_test_correction_baseline(
    result: Mapping[str, Any], *, corrected_test_ids: tuple[str, ...]
) -> None:
    """Require each changed case to fail behaviorally against the clean baseline."""
    require_behavioral_red_result(result)
    output = "\n".join(str(result.get(key, "")) for key in ("stdout", "stderr"))
    failed_cases = set(re.findall(r"(?m)^FAILED\s+(\S+::test_\S+)", output))
    missing_failures = set(corrected_test_ids) - failed_cases
    if missing_failures:
        raise ExecutePrTestVerificationError(
            "each corrected test must still fail through its behavior assertion "
            "on the untouched baseline; "
            f"missing failures={sorted(missing_failures)}"
        )


__all__ = [
    "ExecutePrTestVerificationError",
    "changed_execute_pr_test_cases",
    "require_behavioral_red_result",
    "require_execute_pr_test_correction_baseline",
]
