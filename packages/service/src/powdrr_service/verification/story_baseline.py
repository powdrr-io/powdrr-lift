"""Baseline policy for story tests, separate from the legacy AC red policy."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_service.structrr.story_coverage import StoryCoverageError


def story_baseline_policy(proposed: Mapping[str, Any]) -> str:
    """Only the compiler-owned validation item can start from existing behavior."""
    return (
        "existing_behavior"
        if proposed.get("item_id") == "story-integration-validation"
        and proposed.get("story_validation") is True
        else "new_behavior"
    )


def require_story_baseline(
    result: Mapping[str, Any],
    *,
    policy: str,
    missing_interfaces: Sequence[Mapping[str, Any]] = (),
) -> str:
    observed = result.get("story_execution", {})
    reports = observed.get("reports", [])
    if observed.get("collection_errors") != 0 or not observed.get("selectors"):
        raise StoryCoverageError("story baseline collection is unavailable")
    if result.get("returncode") is None:
        raise StoryCoverageError("story baseline execution is unavailable")
    if any(
        report.get("wasxfail")
        or report.get("outcome") == "skipped"
        or (report.get("phase") != "call" and report.get("outcome") != "passed")
        for report in reports
    ):
        raise StoryCoverageError(
            "story baseline contains skipped tests or unavailable setup/teardown"
        )
    calls = [report for report in reports if report.get("phase") == "call"]
    if len(calls) != len(observed["selectors"]):
        raise StoryCoverageError("story baseline did not execute every collected case")
    if result.get("returncode") == 0 and all(
        report.get("outcome") == "passed" for report in calls
    ):
        if policy != "existing_behavior":
            raise StoryCoverageError(
                "new behavior requires a behavioral failure or a classified "
                "missing interface on baseline"
            )
        return "existing_behavior_pass"
    failures = [report for report in calls if report.get("outcome") == "failed"]
    if not failures:
        raise StoryCoverageError("story baseline has no behavioral failure")
    absent = {
        record["module"]
        for record in missing_interfaces
        if record.get("exists") is False
    }
    behavioral = False
    for failure in failures:
        diagnostic = str(failure.get("diagnostic", ""))
        if failure.get("exception_type") == "AssertionError" or (
            failure.get("exception_type") == "Failed" and "DID NOT RAISE" in diagnostic
        ):
            behavioral = True
            continue
        missing = re.search(
            r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]", diagnostic
        )
        if (
            failure.get("exception_type") != "ModuleNotFoundError"
            or missing is None
            or not any(
                module == missing[1] or module.startswith(missing[1] + ".")
                for module in absent
            )
        ):
            raise StoryCoverageError(
                "story baseline failed for an environment/import/syntax reason, "
                "not a declared missing public interface"
            )
    return "behavioral_failure" if behavioral else "missing_interface"


def missing_interface_catalog(
    operations: Sequence[Mapping[str, Any]], observations: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Match observed file absence to a normal approved added-source operation."""
    added_paths = {
        str(operation.get("content", {}).get("path", ""))
        for operation in operations
        if operation.get("section") == "files"
        and operation.get("action") in {"add", "added"}
    }
    result = []
    for observation in observations:
        path = observation.get("path")
        if (
            path not in added_paths
            or not str(path).endswith(".py")
            or observation.get("exists") is not False
        ):
            continue
        module = (
            str(path)
            .removeprefix("src/")
            .removesuffix(".py")
            .replace("/", ".")
            .removesuffix(".__init__")
        )
        result.append({"module": module, "path": path, "exists": False})
    return tuple(result)
