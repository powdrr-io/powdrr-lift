"""Service-owned prompt policy for execute-PR integration test authoring."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

WORKSPACE_ROOT_PLACEHOLDER = "{{powdrr_workspace_root}}"


def build_execute_pr_test_author_prompt(
    *,
    test_path: str,
    plan_context: Mapping[str, Any],
    obligations: Sequence[Mapping[str, Any]],
) -> str:
    """Build the private authoring prompt without receiving a repository path."""
    return (
        "Write independent executable pytest integration or end-to-end tests "
        "for this reviewed PR before implementation starts. The exact, only "
        "workspace for this attempt is:\n"
        f"{WORKSPACE_ROOT_PLACEHOLDER}\n"
        "Before inspecting or editing anything, run `pwd` and "
        "`git rev-parse --show-toplevel`. Both must identify this workspace. "
        "If either command reports a different directory, stop without making "
        "or staging any changes and report the observed paths. Do not use the "
        "parent checkout or any path outside this workspace.\n\n"
        "Inspect the untouched repository and its public entry points. Add only "
        f"the new test file `{test_path}`; do not create other files or edit "
        "implementation, existing tests, configuration, or documentation. Use "
        "realistic setup and real application wiring. "
        "Exercise public behavior at the narrowest end-to-end or integration "
        "boundary that proves the acceptance criterion. Do not mock the entry "
        "point or behavior under test. Do not use private implementation "
        "details as the oracle. Every test must make a specific observable "
        "assertion grounded in the supplied acceptance criteria. Avoid "
        "tautologies, constant-only assertions, unconditional passes, skips, "
        "and expected failures. Stage only the specified file individually "
        f"with `git add -f {test_path}` so ignored source paths remain visible. Do "
        "not stage `__pycache__`, `.pytest_cache`, or `.pyc` files; stage only "
        "authored Python test files. Do not commit. Stop after creating "
        "the tests.\n\n"
        "The implementation is intentionally absent. Tests must execute "
        "against the baseline and fail through a behavioral assertion, not "
        "through syntax, import, collection, or environment errors.\n\n"
        "Reviewed PR contract:\n"
        f"{json.dumps(plan_context, indent=2, sort_keys=True)}\n"
        "Deterministic test obligations. IDs are assigned in acceptance-criterion "
        "order. Every authored test must identify the criterion or criteria it "
        "covers with one or more comments of the form `# Covers: AC-01 "
        "test_function_name` immediately above the test. Cover every ID. You "
        "may use multiple tests for a criterion and one test for multiple "
        "criteria. Choose useful normal, boundary, invalid, interaction, or "
        "state cases from the contract; do not invent behavior beyond it.\n\n"
        "Make relationship assertions when criteria describe connected pieces "
        "of configuration. For a CI workflow, parse the YAML and verify that "
        "the pull_request trigger is enabled, that a job which runs the exact "
        "verification command also uses the required runner, and that no job "
        "condition excludes pull requests. Do not satisfy these checks with "
        "the command in a comment, unrelated YAML field, or a different job. "
        "For a default-branch requirement, use the `repository_default_branch` "
        "evidence in the reviewed PR contract; never guess a branch name. If "
        "that value is null, do not claim a specific branch is the default. "
        "Treat the supplied suite "
        "as a contract: assert every acceptance criterion directly and keep "
        "checks linked to the same workflow/job/step that provides the behavior.\n\n"
        f"{json.dumps(list(obligations), indent=2, sort_keys=True)}\n"
    )


__all__ = ["WORKSPACE_ROOT_PLACEHOLDER", "build_execute_pr_test_author_prompt"]
