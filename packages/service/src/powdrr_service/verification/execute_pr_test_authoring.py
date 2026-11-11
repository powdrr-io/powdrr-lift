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
    existing = plan_context.get("baseline_policy") == "existing_behavior"
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
        + (
            "This is integrated or existing behavior validation. Baseline tests "
            "may pass. Preserve the agreed oracle and record the actual result; "
            "never manufacture a baseline failure. Semantic review and appropriate "
            "negative controls establish assertion sensitivity.\n\n"
            if existing
            else "The implementation is intentionally absent. Tests must execute "
            "against the baseline and fail through a behavioral assertion. A "
            "declared new public module may be classified as a missing interface "
            "only during an actual test call. Syntax, collection and dependency "
            "environment errors are not behavioral failures.\n\n"
        )
        + "Reviewed PR contract:\n"
        f"{json.dumps(plan_context, indent=2, sort_keys=True)}\n"
        "Deterministic test obligations. IDs are assigned in acceptance-criterion "
        "order. Every authored test must identify the criterion or criteria it "
        "covers with one or more comments of the form `# Covers: AC-01 "
        "test_function_name` immediately above the test. Cover every AC ID. You "
        "may use multiple tests for a criterion and one test for multiple "
        "criteria. Choose useful normal, boundary, invalid, interaction, or "
        "state cases from the contract; do not invent behavior beyond it. "
        "The reviewed PR contract includes planned_test_cases. Follow each "
        "case's test_type, scenario, and expected_outcome when authoring its "
        "behavioral assertion. If repository evidence makes a planned approach "
        "unworkable, preserve the acceptance criterion and report the evidence "
        "instead of silently omitting the case.\n\n"
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
        "Review and documentation boundaries: do not treat a pull-request review, "
        "merge, or repository review record as runtime behavior unless the reviewed "
        "acceptance criteria explicitly require a public interface for it. Do not "
        "assert or request edits to `docs/structrr/current/baseline-bootstrap.yaml`; "
        "it is the prior accepted snapshot, not a review log for the current change. "
        "Documentation review is a pull-request process requirement. For a "
        "documentation criterion, test the required statements and tracked file "
        "path without expecting generated bootstrap metadata to change.\n\n"
        f"{json.dumps(list(obligations), indent=2, sort_keys=True)}\n"
        + (
            "\nPinned story cases are additional obligations. Follow their complete "
            "transition walks, fixtures, initial conditions and observations. "
            "Do not replace expected observations or invent missing customer choices. "
            "Report design_amendment_needed if a behavioral decision is missing. "
            "For SC obligations, add one module-level Python literal declaration "
            "__powdrr_story_links__. This is a typed request to the existing artifact "
            "writer; it compiles the manifest after actual pytest collection. Shape:\n"
            "__powdrr_story_links__ = {\n"
            "  'test_actual_function': [{\n"
            "    'obligation_id': '<supplied SC-ID>',\n"
            "    'observations': {'<observation ID>': [<assertion start line>]},\n"
            "    'description': '<what those assertions establish>',\n"
            "    'boundary': '<real public/API/CLI integration boundary>',\n"
            "    'substitutions': ['<external dependency boundary, if any>'],\n"
            "  }],\n"
            "}\n"
            "Use [] for substitutions when there are none. Reference executable "
            "assert statements or pytest.raises start lines inside that test, not "
            "comments, helper functions, setup or a mocked target. Each required "
            "observation must have a link. Several tests may establish one case, "
            "and one test may link several cases with separate requests. No authored "
            "selector or # Covers comment can replace collected assertion links. "
            "Story semantic review is required; unavailable review blocks authoring.\n"
            if any(obligation.get("story_refs") for obligation in obligations)
            else ""
        )
    )


__all__ = ["WORKSPACE_ROOT_PLACEHOLDER", "build_execute_pr_test_author_prompt"]
